"""网盘直链解析插件。

移植自 LinkSwift（https://github.com/hmjz100/LinkSwift）的取链思路，
在 AstrBot 中实现：

1. 用正则识别聊天中的网盘分享链接；
2. 通过 `_conf_schema.json` 中配置的 Cookie / Token 调用各网盘接口获取文件直链；
3. 支持在分享内逐层进入 / 返回文件夹浏览；
4. 分享或目录内存在多个文件时，把列表发回会话，由用户回复序号选择；
5. 通过 OneBot（aiocqhttp）把文件发送出去。

注意：各网盘的非公开接口随时可能变化，解析失败时会返回具体错误信息。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import File, Plain
from astrbot.api.star import Context, Star

# ---------------------------------------------------------------------------
# 正则：分享链接识别
# ---------------------------------------------------------------------------

PAN_PATTERNS: dict[str, str] = {
    "baidu": r"https?://(?:pan|yun)\.baidu\.com/s/(1[A-Za-z0-9_\-]{6,})",
    "aliyun": r"https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/([A-Za-z0-9]+)",
    "mcloud": r"https?://(?:yun|caiyun)\.139\.com/(?:shareweb/#/w/i/|web/share/)([A-Za-z0-9_\-]+)",
    "tcloud": r"https?://cloud\.189\.cn/(?:web/share\?code=|t/)([A-Za-z0-9]+)",
    "xunlei": r"https?://pan\.xunlei\.com/s/([A-Za-z0-9_\-]+)",
    "quark": r"https?://pan\.quark\.cn/s/([A-Za-z0-9]+)",
    "uc": r"https?://drive\.uc\.cn/s/([A-Za-z0-9]+)",
    "123pan": r"https?://(?:www\.)?(?:123pan\.com|123684\.com|123865\.com|123952\.com|123912\.com)/s/([A-Za-z0-9_\-]+)",
}

PAN_NAMES: dict[str, str] = {
    "baidu": "百度网盘",
    "aliyun": "阿里云盘",
    "mcloud": "中国移动云盘",
    "tcloud": "天翼云盘",
    "xunlei": "迅雷云盘",
    "quark": "夸克网盘",
    "uc": "UC网盘",
    "123pan": "123云盘",
}

PWD_PATTERNS: dict[str, str] = {
    "baidu": r"[?&#]pwd=([A-Za-z0-9]{4,8})",
    "aliyun": r"[?&#]pwd=([A-Za-z0-9]{4,8})",
    "mcloud": r"[?&#](?:pwd|password)=([A-Za-z0-9]{4,8})",
    "tcloud": r"[?&#]code=([A-Za-z0-9]{4,8})",
    "xunlei": r"[?&#]pwd=([A-Za-z0-9]{4,8})",
    "quark": r"[?&#]pwd=([A-Za-z0-9]{4,8})",
    "uc": r"[?&#]pwd=([A-Za-z0-9]{4,8})",
    "123pan": r"[?&#](?:pwd|password)=([A-Za-z0-9]{4,8})",
}

# 当提取码未写在链接里时，从整段文本中兜底识别（如 “提取码: abcd”）
PWD_TEXT_PATTERN = (
    r"(?:提取码|访问码|密码|口令|pwd|passcode)\s*[:：=]?\s*([A-Za-z0-9]{4,8})"
)

# 各类客户端 UA（对应 LinkSwift 中的 config.$xxx.api.ua）
UA_PAN_DOWNLOAD = "pan.baidu.com"
UA_QUARK = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "quark-cloud-drive/3.20.0 Chrome/112.0.5615.165 Electron/24.1.3.8 Safari/537.36 "
    "Channel/pckk_other_ch"
)
UA_UC = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "uc-cloud-drive/2.5.20 Chrome/100.0.4896.160 Electron/18.3.5.4-b478491100 "
    "Safari/537.36 Channel/pckk_other_ch"
)


# 结束浏览的关键词
CLOSE_WORDS = {"关闭", "取消", "q"}

# 返回上级的关键词
UP_WORDS = {"..", "返回"}


class PanError(Exception):
    """网盘接口调用失败。"""


@dataclass
class PanFile:
    """统一的文件 / 文件夹条目。"""

    name: str
    size: int = 0
    is_dir: bool = False
    file_id: str = ""
    # 进入该目录时使用的引用值（各网盘不同：路径 / file_id / fid 等）
    dir_ref: str = ""
    # 网盘上下文（shareid、stoken、cookie 等），供 _link_* 使用
    extra: dict[str, Any] = field(default_factory=dict)
    link: str = ""

    @property
    def size_text(self) -> str:
        return human_size(self.size)


@dataclass
class PendingShare:
    """等待用户浏览 / 选择的分享会话。"""

    pan: str
    raw_url: str
    share_id: str
    pwd: str
    ctx: dict[str, Any]
    # 目录栈：[("根目录", root_ref), ("子目录", child_ref), ...]
    stack: list[tuple[str, str]]
    files: list[PanFile]
    umo: str = ""
    created_at: float = field(default_factory=time.time)

    @property
    def current_ref(self) -> str:
        return self.stack[-1][1]

    @property
    def path_text(self) -> str:
        if len(self.stack) <= 1:
            return "/"
        return "/" + "/".join(name for name, _ in self.stack[1:])

    @property
    def path_display(self) -> str:
        """带结尾斜杠的展示路径，如 `/`、`/电影/`。"""
        text = self.path_text
        return text if text.endswith("/") else text + "/"

    @property
    def root_ref(self) -> str:
        return self.stack[0][1]


def human_size(size: int | float | str) -> str:
    """把字节数格式化成可读文本。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return "未知"
    if value <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if value >= 100 or index == 0:
        return f"{value:.0f} {units[index]}"
    return f"{value:.2f} {units[index]}"


def safe_filename(name: str, fallback: str = "file") -> str:
    """清理文件名中的非法字符。"""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r'[!?&|`"\'*:<>/\r\n\t]', "_", name).strip()
    if name in {"", ".", ".."}:
        return fallback
    return name


def extract_links(text: str) -> list[tuple[str, str, str, str]]:
    """从文本中提取所有可识别的分享链接。

    Returns:
        [(pan, raw_url, share_id, pwd), ...]
    """
    fallback_pwd = ""
    fallback_match = re.search(PWD_TEXT_PATTERN, text)
    if fallback_match:
        fallback_pwd = fallback_match.group(1)

    results: list[tuple[str, str, str, str]] = []
    for pan, pattern in PAN_PATTERNS.items():
        pwd_pattern = PWD_PATTERNS.get(pan, "")
        for match in re.finditer(pattern, text):
            raw_url = match.group(0)
            share_id = match.group(1)
            pwd = ""
            if pwd_pattern:
                pwd_match = re.search(pwd_pattern, text)
                if pwd_match:
                    pwd = pwd_match.group(1)
            if not pwd:
                pwd = fallback_pwd
            results.append((pan, raw_url, share_id, pwd))

    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str, str, str]] = []
    for item in results:
        key = (item[0], item[2])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


class PanLinkPlugin(Star):
    """网盘直链解析插件主类。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.config = config
        self._session: aiohttp.ClientSession | None = None
        self._session_lock = asyncio.Lock()
        # unified_msg_origin -> PendingShare
        self._pending: dict[str, PendingShare] = {}
        # unified_msg_origin -> asyncio.Lock
        self._locks: dict[str, asyncio.Lock] = {}
        # unified_msg_origin -> 待发送文件队列（后台下载，不阻塞浏览）
        self._queues: dict[str, asyncio.Queue] = {}
        # unified_msg_origin -> 后台发送 worker
        self._workers: dict[str, asyncio.Task] = {}
        self._temp_dir = Path(tempfile.gettempdir()) / "astrbot_pan_link"
        self._temp_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        await self.cleanup_temp_files()

    async def terminate(self) -> None:
        for task in self._workers.values():
            if not task.done():
                task.cancel()
        self._workers.clear()
        self._queues.clear()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None
        self._pending.clear()

    # ------------------------------------------------------------------
    # HTTP 工具
    # ------------------------------------------------------------------

    def _cfg(self, key: str, default: Any = "") -> Any:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return default if value is None else value

    @property
    def timeout(self) -> aiohttp.ClientTimeout:
        seconds = int(self._cfg("request_timeout", 30) or 30)
        return aiohttp.ClientTimeout(total=max(5, seconds))

    async def _get_session(self) -> aiohttp.ClientSession:
        async with self._session_lock:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession(
                    timeout=self.timeout,
                    trust_env=True,
                )
            return self._session

    def _base_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self._cfg(
                "download_ua",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "DNT": "1",
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_data: Any = None,
        form: dict[str, Any] | None = None,
        as_text: bool = False,
    ) -> Any:
        """发送请求，默认按 JSON 解析响应。"""
        session = await self._get_session()
        merged_headers = self._base_headers(headers)
        if json_data is not None:
            merged_headers.setdefault("Content-Type", "application/json;charset=UTF-8")
        if form is not None:
            merged_headers.setdefault(
                "Content-Type", "application/x-www-form-urlencoded;charset=UTF-8"
            )

        try:
            async with session.request(
                method.upper(),
                url,
                headers=merged_headers,
                params=params,
                json=json_data,
                data=form,
                allow_redirects=True,
            ) as resp:
                text = await resp.text(errors="ignore")
                if as_text:
                    return text
                if not text:
                    return {}
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    return text
        except asyncio.TimeoutError as exc:
            raise PanError("请求超时，请稍后重试") from exc
        except aiohttp.ClientError as exc:
            raise PanError(f"网络请求失败：{exc}") from exc

    @staticmethod
    def _cookie_has(cookie: str, *keys: str) -> bool:
        """判断 Cookie 串中是否包含指定键。"""
        return all(re.search(rf"(?:^|;\s*){re.escape(key)}=", cookie) for key in keys)

    # ------------------------------------------------------------------
    # 入口 1：正则识别分享链接
    # ------------------------------------------------------------------

    @filter.regex(
        r"https?://(?:pan|yun)\.baidu\.com/s/1[A-Za-z0-9_\-]{6,}"
        r"|https?://(?:www\.)?(?:alipan|aliyundrive)\.com/s/[A-Za-z0-9]+"
        r"|https?://(?:yun|caiyun)\.139\.com/(?:shareweb/#/w/i/|web/share/)[A-Za-z0-9_\-]+"
        r"|https?://cloud\.189\.cn/(?:web/share\?code=|t/)[A-Za-z0-9]+"
        r"|https?://pan\.xunlei\.com/s/[A-Za-z0-9_\-]+"
        r"|https?://pan\.quark\.cn/s/[A-Za-z0-9]+"
        r"|https?://drive\.uc\.cn/s/[A-Za-z0-9]+"
        r"|https?://(?:www\.)?(?:123pan\.com|123684\.com|123865\.com|123952\.com|123912\.com)/s/[A-Za-z0-9_\-]+"
    )
    async def on_share_link(self, event: AstrMessageEvent):
        """识别到网盘分享链接后，解析文件列表。"""
        text = event.get_message_str()
        links = extract_links(text)
        if not links:
            return

        enabled = self._cfg("enabled_pans", list(PAN_PATTERNS.keys())) or []
        if isinstance(enabled, str):
            enabled = [enabled]
        links = [item for item in links if item[0] in enabled]
        if not links:
            yield event.plain_result("该网盘未在配置中启用。")
            return

        umo = event.unified_msg_origin
        lock = self._locks.setdefault(umo, asyncio.Lock())
        if lock.locked():
            yield event.plain_result("上一次解析还在进行中，请稍候~")
            return

        async with lock:
            event.should_call_llm(False)
            pan, raw_url, share_id, pwd = links[0]
            pan_name = PAN_NAMES.get(pan, pan)
            yield event.plain_result(
                f"正在解析{pan_name}分享（{share_id}），请稍候..."
            )

            try:
                ctx, files, root_ref = await self._open_share(
                    pan, raw_url, share_id, pwd
                )
            except PanError as exc:
                yield event.plain_result(f"解析失败：{exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("解析分享失败")
                yield event.plain_result(f"解析失败：{exc}")
                return

            visible = [f for f in files if f.name and f.name != "."]
            if not visible:
                yield event.plain_result("分享为空或没有可访问的内容。")
                return

            regular = [f for f in visible if not f.is_dir]
            has_dir = any(f.is_dir for f in visible)
            if len(regular) == 1 and not has_dir and bool(
                self._cfg("auto_send_single", True)
            ):
                await self._queue_files(event, pan, regular)
                yield event.plain_result("已获取到文件，正在后台发送...")
                return

            # 新的分享开始，旧的浏览会话与待发送队列一并清空
            self._clear_queue(umo)
            self._clear_queue(umo)
            pending = PendingShare(
                pan=pan,
                raw_url=raw_url,
                share_id=share_id,
                pwd=pwd,
                ctx=ctx,
                stack=[("根目录", root_ref)],
                files=visible,
                umo=umo,
            )
            self._pending[umo] = pending
            yield event.plain_result(self._render_listing(pending))

    # ------------------------------------------------------------------
    # 入口 2：选择文件 / 文件夹导航
    # ------------------------------------------------------------------

    @filter.regex(
        r"^\s*(?:"
        r"(?i:cd\s*\d+|进入\s*\d+|\d+/)"
        r"|\.\.|(?i:cd\s*\.\.|返回|上级|上一级|back)"
        r"|(?i:p|ls|pwd|路径)"
        r"|(?i:关闭|close|exit|quit|结束|取消|q)"
        r"|[0-9]+(?:[\s,，、~\-]+[0-9]+)*"
        r"|(?i:all|全部|全选|\*)"
        r")\s*$"
    )
    async def on_select_files(self, event: AstrMessageEvent):
        """序号进入文件夹或下载文件；.. 返回上级；关闭 结束浏览。"""
        umo = event.unified_msg_origin
        pending = self._pending.get(umo)
        if not pending:
            return

        timeout = int(self._cfg("selection_timeout", 180) or 180)
        if time.time() - pending.created_at > timeout:
            self._pending.pop(umo, None)
            self._clear_queue(umo)
            yield event.plain_result("浏览已超时，会话已结束。请重新发送分享链接。")
            return

        event.should_call_llm(False)
        text = event.get_message_str().strip()
        low = text.lower()

        # 关闭浏览
        if low in CLOSE_WORDS:
            self._pending.pop(umo, None)
            pending_count = self._clear_queue(umo)
            if pending_count:
                yield event.plain_result(
                    f"已关闭浏览，并取消 {pending_count} 个待发送文件。"
                )
            else:
                yield event.plain_result("已关闭浏览。")
            return

        # 查看路径
        if low in {"p", "ls", "pwd", "路径"}:
            pending.created_at = time.time()
            yield event.plain_result(self._render_listing(pending))
            return

        # 返回上级
        if low in UP_WORDS:
            if len(pending.stack) <= 1:
                pending.created_at = time.time()
                yield event.plain_result(
                    "已经在根目录了。\n\n" + self._render_listing(pending)
                )
                return
            popped = pending.stack.pop()
            try:
                files = await self._list_dir(
                    pending.pan, pending.ctx, pending.current_ref
                )
            except PanError as exc:
                pending.stack.append(popped)
                yield event.plain_result(f"返回上级失败：{exc}")
                return
            pending.files = [f for f in files if f.name and f.name != "."]
            pending.created_at = time.time()
            yield event.plain_result(self._render_listing(pending))
            return

        # 解析序号：目录则进入，文件则下载
        indexes = self._parse_selection(text, len(pending.files))
        if not indexes:
            yield event.plain_result(
                "输入无效。回复序号进入文件夹或下载文件，.. 返回上级，关闭 结束浏览。"
            )
            return

        # 单个序号且命中文件夹 -> 进入
        if len(indexes) == 1:
            target = pending.files[indexes[0]]
            if target.is_dir:
                try:
                    files = await self._list_dir(
                        pending.pan, pending.ctx, target.dir_ref
                    )
                except PanError as exc:
                    yield event.plain_result(f"进入文件夹失败：{exc}")
                    return
                visible = [f for f in files if f.name and f.name != "."]
                if not visible:
                    pending.created_at = time.time()
                    yield event.plain_result(
                        f"「{target.name}」是空文件夹。\n\n"
                        + self._render_listing(pending)
                    )
                    return
                pending.stack.append((target.name, target.dir_ref))
                pending.files = visible
                pending.created_at = time.time()
                yield event.plain_result(self._render_listing(pending))
                return

        # 下载选中的文件（文件夹自动跳过），发送后保留浏览状态
        selected = [pending.files[i] for i in indexes if not pending.files[i].is_dir]
        skipped = [pending.files[i].name for i in indexes if pending.files[i].is_dir]

        if not selected:
            yield event.plain_result(
                "选中的都是文件夹，请输入单个序号进入，或 .. 返回上级。"
            )
            return

        await self._queue_files(event, pending.pan, selected)

        msg = f"已加入发送队列：{len(selected)} 个文件（后台下载，不阻塞浏览）"
        if skipped:
            msg += f"（跳过文件夹：{'、'.join(skipped)}）"
        msg += (
            f"\n当前仍在 {pending.path_display}，可继续进入其它文件夹，"
            "或输入 .. 返回上级，关闭 结束浏览。"
        )
        pending.created_at = time.time()
        yield event.plain_result(msg)

    @staticmethod
    def _parse_selection(text: str, total: int) -> list[int]:
        """解析用户输入为 0 基索引列表。"""
        if text.lower() in {"all", "*", "全部", "全选"}:
            return list(range(total))

        indexes: list[int] = []
        for part in re.split(r"[\s,，、]+", text):
            if not part:
                continue
            range_match = re.fullmatch(r"(\d+)\s*[~\-]\s*(\d+)", part)
            if range_match:
                start, end = int(range_match.group(1)), int(range_match.group(2))
                if start > end:
                    start, end = end, start
                indexes.extend(range(start - 1, end))
                continue
            if part.isdigit():
                indexes.append(int(part) - 1)

        valid: list[int] = []
        for index in indexes:
            if 0 <= index < total and index not in valid:
                valid.append(index)
        return valid

    def _render_listing(self, pending: PendingShare) -> str:
        """渲染目录列表：标题 + 序号条目 + 返回/关闭项 + 操作提示。

        形如::

            目录:/
            1. 📂 x1
            2. 📂 x2
            3. 📄 x3（1.24 KB）
            ❌关闭

            ▸ 输入序号进入文件夹或下载文件；输入 .. 返回上级；输入 关闭 结束浏览
        """
        lines = [f"目录:{pending.path_display}"]

        # 返回上级项（非根目录才显示）
        if len(pending.stack) > 1:
            lines.append("↩  ..")

        for index, item in enumerate(pending.files, start=1):
            if item.is_dir:
                lines.append(f"{index}. 📂 {item.name}")
            else:
                lines.append(f"{index}. 📄 {item.name}（{item.size_text}）")

        lines.append("❌关闭")
        lines.append("")

        tips = ["输入序号进入文件夹或下载文件"]
        if len(pending.stack) > 1:
            tips.append("输入 .. 返回上级")
        tips.append("输入 关闭 结束浏览")

        queue = self._queues.get(pending.umo)
        pending_count = queue.qsize() if queue is not None else 0
        if pending_count:
            tips.append(f"后台队列剩余 {pending_count} 个")
        lines.append("▸ " + "；".join(tips))
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 发送文件
    # ------------------------------------------------------------------

    def _ensure_worker(self, umo: str) -> asyncio.Queue:
        """取得（必要时创建）该会话的发送队列与后台 worker。"""
        queue = self._queues.get(umo)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[umo] = queue

        task = self._workers.get(umo)
        if task is None or task.done():
            self._workers[umo] = asyncio.create_task(self._send_worker(umo, queue))
        return queue

    async def _queue_files(
        self, event: AstrMessageEvent, pan: str, files: list[PanFile]
    ):
        """把文件加入后台发送队列，立即返回；不会阻塞当前浏览。"""
        umo = event.unified_msg_origin
        queue = self._ensure_worker(umo)
        for item in files:
            queue.put_nowait((umo, pan, item))
        return len(files)

    def _clear_queue(self, umo: str) -> int:
        """取消该会话的后台 worker 与排队任务，返回被取消的任务数。"""
        count = 0
        queue = self._queues.pop(umo, None)
        if queue is not None:
            while not queue.empty():
                try:
                    queue.get_nowait()
                    queue.task_done()
                    count += 1
                except asyncio.QueueEmpty:
                    break
        task = self._workers.pop(umo, None)
        if task is not None and not task.done():
            task.cancel()
        return count

    async def _send_worker(self, umo: str, queue: asyncio.Queue) -> None:
        """后台依次下载并发送队列中的文件。

        浏览与下载解耦：用户可以在下载过程中继续进入其它文件夹、添加新的下载任务。
        队列空闲超过 `worker_idle_timeout` 秒后自动退出，避免常驻占用。
        """
        idle = max(30, int(self._cfg("worker_idle_timeout", 300) or 300))
        while True:
            try:
                umo, pan, item = await asyncio.wait_for(queue.get(), timeout=idle)
            except asyncio.TimeoutError:
                # 长时间无任务，退出 worker（队列对象保留，下次入队会重建 worker）
                if self._workers.get(umo) is asyncio.current_task():
                    self._workers.pop(umo, None)
                return
            except asyncio.CancelledError:
                raise
            try:
                await self._deliver_one(umo, pan, item)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.exception("后台发送文件失败：%s", item.name)
            finally:
                queue.task_done()

    async def _deliver_one(self, umo: str, pan: str, item: PanFile) -> None:
        """下载单个文件并通过 OneBot 发送。"""
        send_mode = str(self._cfg("send_mode", "download") or "download").lower()
        limit_mb = int(self._cfg("max_file_size_mb", 0) or 0)
        limit_bytes = limit_mb * 1024 * 1024 if limit_mb > 0 else 0

        if limit_bytes and item.size and item.size > limit_bytes:
            await self._push_text(
                umo,
                f"⚠️ {item.name} 超过大小限制（{human_size(limit_bytes)}），"
                f"直链：{item.link or '未获取'}",
            )
            return

        if not item.link:
            try:
                item.link = await self._resolve_file_link(pan, item)
            except PanError as exc:
                await self._push_text(umo, f"❌ {item.name} 获取直链失败：{exc}")
                return

        if not item.link:
            await self._push_text(umo, f"❌ {item.name} 获取直链失败。")
            return

        filename = safe_filename(item.name)

        if send_mode == "url":
            await self._push_chain(umo, [File(name=filename, url=item.link)])
            return

        await self._push_text(umo, f"⬇️ 正在下载：{item.name}（{item.size_text}）")
        try:
            local_path = await self._download_file(pan, item.link, filename)
        except PanError as exc:
            await self._push_text(umo, f"❌ {item.name} 下载失败：{exc}")
            return

        await self._push_chain(umo, [File(name=filename, file=local_path)])

    async def _push_text(self, umo: str, text: str) -> None:
        """主动向会话发送文本。"""
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception:  # noqa: BLE001
            logger.exception("主动发送消息失败")

    async def _push_chain(self, umo: str, chain: list) -> None:
        """主动向会话发送消息链。"""
        try:
            await self.context.send_message(umo, MessageChain(chain))
        except Exception:  # noqa: BLE001
            logger.exception("主动发送文件失败")

    async def _download_file(self, pan: str, url: str, filename: str) -> str:
        """把直链下载到本地临时文件。"""
        target = self._temp_dir / f"{uuid.uuid4().hex[:8]}_{filename}"
        headers = self._base_headers(self._download_headers(pan))
        session = await self._get_session()

        try:
            async with session.get(
                url, headers=headers, allow_redirects=True
            ) as resp:
                if resp.status >= 400:
                    raise PanError(f"HTTP {resp.status}")
                with target.open("wb") as file_obj:
                    async for chunk in resp.content.iter_chunked(256 * 1024):
                        file_obj.write(chunk)
        except PanError:
            raise
        except asyncio.TimeoutError as exc:
            raise PanError("下载超时") from exc
        except (aiohttp.ClientError, OSError) as exc:
            raise PanError(str(exc)) from exc

        if not target.exists() or target.stat().st_size == 0:
            raise PanError("下载结果为空")
        return str(target)

    def _download_headers(self, pan: str) -> dict[str, str]:
        """不同网盘直链下载所需的额外头。"""
        if pan == "baidu":
            return {
                "User-Agent": UA_PAN_DOWNLOAD,
                "Cookie": str(self._cfg("baidu_cookie", "")),
                "Referer": "https://pan.baidu.com/",
            }
        if pan == "aliyun":
            return {"Referer": "https://www.alipan.com/"}
        if pan == "mcloud":
            return {
                "Cookie": str(self._cfg("mcloud_cookie", "")),
                "Referer": "https://yun.139.com/",
            }
        if pan == "tcloud":
            return {
                "Cookie": str(self._cfg("tcloud_cookie", "")),
                "Referer": "https://cloud.189.cn/",
            }
        if pan == "xunlei":
            token = str(self._cfg("xunlei_access_token", ""))
            return {
                "Authorization": f"Bearer {token}" if token else "",
                "Referer": "https://pan.xunlei.com/",
            }
        if pan == "quark":
            return {
                "User-Agent": UA_QUARK,
                "Cookie": str(self._cfg("quark_cookie", "")),
                "Referer": "https://pan.quark.cn/",
            }
        if pan == "uc":
            return {
                "User-Agent": UA_UC,
                "Cookie": str(self._cfg("uc_cookie", "")),
                "Referer": "https://drive.uc.cn/",
            }
        if pan == "123pan":
            return {
                "Cookie": str(self._cfg("pan123_cookie", "")),
                "Referer": "https://www.123pan.com/",
            }
        return {}

    # ------------------------------------------------------------------
    # 解析调度
    # ------------------------------------------------------------------

    async def _open_share(
        self, pan: str, raw_url: str, share_id: str, pwd: str
    ) -> tuple[dict[str, Any], list[PanFile], str]:
        """打开分享，返回 (ctx, 根目录文件列表, 根目录引用)。"""
        opener = getattr(self, f"_open_{pan}", None)
        if opener is None:
            raise PanError(f"暂不支持的网盘：{pan}")
        return await opener(raw_url, share_id, pwd)

    async def _list_dir(
        self, pan: str, ctx: dict[str, Any], dir_ref: str
    ) -> list[PanFile]:
        """列出指定目录。"""
        lister = getattr(self, f"_list_{pan}", None)
        if lister is None:
            raise PanError(f"暂不支持的网盘：{pan}")
        return await lister(ctx, dir_ref)

    async def _resolve_file_link(self, pan: str, item: PanFile) -> str:
        resolver = getattr(self, f"_link_{pan}", None)
        if resolver is None:
            raise PanError(f"暂不支持的网盘：{pan}")
        return await resolver(item)

    # ------------------------------------------------------------------
    # 百度网盘
    # ------------------------------------------------------------------

    async def _open_baidu(self, raw_url: str, share_id: str, pwd: str):
        cookie = str(self._cfg("baidu_cookie", ""))
        if not cookie:
            raise PanError("未配置百度网盘 Cookie（baidu_cookie）")

        surl = share_id
        if surl.startswith("1") and len(surl) > 22:
            surl = surl[1:]

        page_url = f"https://pan.baidu.com/s/1{surl}"
        if pwd:
            page_url += f"?pwd={pwd}"
        html = await self._request(
            "GET", page_url, headers={"Cookie": cookie}, as_text=True
        )
        if not isinstance(html, str):
            raise PanError("获取分享页面失败")

        shareid = self._re_first(html, r'"shareid"\s*:\s*"?(\d+)"?')
        share_uk = self._re_first(html, r'"share_uk"\s*:\s*"?(\d+)"?') or self._re_first(
            html, r'"uk"\s*:\s*"?(\d+)"?'
        )
        bdstoken = self._re_first(html, r'"bdstoken"\s*:\s*"([0-9a-zA-Z]+)"') or ""
        if not shareid or not share_uk:
            raise PanError("无法从分享页面提取 shareid/uk，链接可能已失效")

        verify_cookie = cookie
        if pwd:
            verify_url = (
                "https://pan.baidu.com/share/verify"
                f"?surl={surl}&t={int(time.time() * 1000)}"
                "&channel=chunlei&web=1&app_id=250528&clienttype=0"
                f"&bdstoken={bdstoken}"
            )
            verify = await self._request(
                "POST",
                verify_url,
                headers={"Cookie": cookie, "Referer": page_url},
                form={"pwd": pwd},
            )
            if isinstance(verify, dict):
                if verify.get("errno") not in (0, None):
                    raise PanError(
                        f"提取码校验失败（errno={verify.get('errno')}），请检查提取码"
                    )
                randsk = verify.get("randsk")
                if randsk:
                    verify_cookie = f"{cookie}; BDCLND={randsk}"

        ctx = {
            "shareid": shareid,
            "uk": share_uk,
            "surl": surl,
            "bdstoken": bdstoken,
            "cookie": verify_cookie,
        }
        files = await self._list_baidu(ctx, "/")
        return ctx, files, "/"

    async def _list_baidu(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        surl = ctx.get("surl", "")
        page_url = f"https://pan.baidu.com/s/1{surl}"
        params = {
            "uk": ctx.get("uk", ""),
            "shareid": ctx.get("shareid", ""),
            "order": "other",
            "desc": "1",
            "showempty": "0",
            "web": "1",
            "page": "1",
            "num": "100",
            "dir": dir_ref or "/",
            "t": str(int(time.time() * 1000)),
            "channel": "chunlei",
            "app_id": "250528",
            "clienttype": "0",
            "bdstoken": ctx.get("bdstoken", ""),
            "shorturl": surl,
        }
        payload = await self._request(
            "GET",
            "https://pan.baidu.com/share/list",
            headers={"Cookie": ctx.get("cookie", ""), "Referer": page_url},
            params=params,
        )
        if not isinstance(payload, dict):
            raise PanError("文件列表响应异常")
        if payload.get("errno") not in (0, None):
            raise PanError(
                f"获取文件列表失败（errno={payload.get('errno')}："
                f"{payload.get('errmsg', '未知错误')}）"
            )

        files: list[PanFile] = []
        for entry in payload.get("list") or []:
            is_dir = int(entry.get("isdir") or 0) == 1
            name = entry.get("server_filename") or entry.get("filename") or "未命名"
            child_ref = entry.get("path") or (
                (dir_ref.rstrip("/") + "/" + name) if dir_ref else "/" + name
            )
            files.append(
                PanFile(
                    name=name,
                    size=int(entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=str(entry.get("fs_id") or ""),
                    dir_ref=child_ref if is_dir else "",
                    extra=dict(ctx),
                )
            )
        return files

    async def _link_baidu(self, item: PanFile) -> str:
        extra = item.extra
        cookie = extra.get("cookie") or str(self._cfg("baidu_cookie", ""))
        surl = extra.get("surl", "")

        sign_info = await self._request(
            "GET",
            "https://pan.baidu.com/share/tplconfig",
            headers={"Cookie": cookie},
            params={
                "surl": f"1{surl}",
                "fields": "sign,timestamp",
                "channel": "chunlei",
                "web": "1",
                "app_id": "250528",
                "clienttype": "0",
                "bdstoken": extra.get("bdstoken", ""),
            },
        )
        sign = ""
        timestamp = ""
        if isinstance(sign_info, dict):
            data = sign_info.get("data") or {}
            sign = data.get("sign", "")
            timestamp = data.get("timestamp", "")

        payload = await self._request(
            "POST",
            "https://pan.baidu.com/api/sharedownload",
            headers={
                "Cookie": cookie,
                "Referer": f"https://pan.baidu.com/s/1{surl}",
                "User-Agent": "netdisk;",
            },
            params={
                "sign": sign,
                "timestamp": timestamp,
                "channel": "chunlei",
                "clienttype": "0",
                "web": "1",
                "app_id": "250528",
                "bdstoken": extra.get("bdstoken", ""),
            },
            form={
                "encrypt": "0",
                "product": "share",
                "uk": extra.get("uk", ""),
                "primaryid": extra.get("shareid", ""),
                "fid_list": json.dumps([int(item.file_id)]),
            },
        )
        if not isinstance(payload, dict):
            raise PanError("直链响应异常")
        if payload.get("errno") not in (0, None):
            raise PanError(
                f"errno={payload.get('errno')}，{payload.get('errmsg', '未知错误')}"
            )
        rows = payload.get("list") or []
        if not rows:
            raise PanError("未返回下载链接")
        dlink = rows[0].get("dlink") or ""
        access_token = str(self._cfg("baidu_access_token", ""))
        if dlink and access_token:
            separator = "&" if "?" in dlink else "?"
            dlink = f"{dlink}{separator}access_token={access_token}"
        return dlink

    # ------------------------------------------------------------------
    # 阿里云盘
    # ------------------------------------------------------------------

    async def _aliyun_access_token(self) -> str:
        refresh_token = str(self._cfg("aliyun_refresh_token", ""))
        if not refresh_token:
            return ""
        payload = await self._request(
            "POST",
            "https://api.alipan.com/oauth/access_token",
            json_data={
                "client_id": "5dde4e1bdf9c49680877669",
                "client_secret": "1f5e2a3a46b04a44b5d78e1b9f4e4d4b",
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if isinstance(payload, dict) and payload.get("access_token"):
            return str(payload["access_token"])
        return ""

    async def _aliyun_share_token(self, share_id: str, pwd: str) -> str:
        payload = await self._request(
            "POST",
            "https://api.alipan.com/adrive/v3/share_link/get_share_token",
            headers={
                "Referer": "https://www.alipan.com/",
                "x-canary": "client=web,app=share,version=v3.2.1",
            },
            json_data={"share_id": share_id, "share_pwd": pwd or ""},
        )
        if isinstance(payload, dict) and payload.get("share_token"):
            return str(payload["share_token"])
        message = payload.get("message") if isinstance(payload, dict) else ""
        raise PanError(f"获取 share_token 失败：{message or '未知错误'}")

    async def _open_aliyun(self, raw_url: str, share_id: str, pwd: str):
        share_token = await self._aliyun_share_token(share_id, pwd)
        ctx = {"share_id": share_id, "share_token": share_token}
        files = await self._list_aliyun(ctx, "root")
        return ctx, files, "root"

    async def _list_aliyun(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        payload = await self._request(
            "POST",
            "https://api.alipan.com/adrive/v2/file/list_share_file",
            headers={"Referer": "https://www.alipan.com/"},
            json_data={
                "share_id": ctx.get("share_id", ""),
                "share_token": ctx.get("share_token", ""),
                "parent_file_id": dir_ref or "root",
                "limit": 100,
                "order_by": "name",
                "order_direction": "ASC",
            },
        )
        if not isinstance(payload, dict):
            raise PanError("文件列表响应异常")
        if payload.get("code"):
            raise PanError(f"{payload.get('code')}：{payload.get('message', '未知错误')}")

        files: list[PanFile] = []
        for entry in payload.get("items") or []:
            is_dir = str(entry.get("type")) == "folder"
            file_id = str(entry.get("file_id") or "")
            files.append(
                PanFile(
                    name=entry.get("name") or "未命名",
                    size=int(entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=file_id,
                    dir_ref=file_id if is_dir else "",
                    extra=dict(ctx),
                )
            )
        return files

    async def _link_aliyun(self, item: PanFile) -> str:
        payload = await self._request(
            "POST",
            "https://api.alipan.com/v2/file/get_share_link_download_url",
            headers={"Referer": "https://www.alipan.com/"},
            json_data={
                "share_id": item.extra.get("share_id", ""),
                "file_id": item.file_id,
                "share_token": item.extra.get("share_token", ""),
                "expire_sec": 14400,
            },
        )
        if isinstance(payload, dict):
            if payload.get("url"):
                return str(payload["url"])
            if payload.get("code"):
                raise PanError(
                    f"{payload.get('code')}：{payload.get('message', '未知错误')}"
                )
        raise PanError("未返回下载链接")

    # ------------------------------------------------------------------
    # 夸克 / UC 网盘
    # ------------------------------------------------------------------

    def _quark_like_conf(self, pan: str) -> tuple[str, str, str, str]:
        if pan == "quark":
            return (
                "https://drive-pc.quark.cn/1/clouddrive",
                "ucpro",
                UA_QUARK,
                str(self._cfg("quark_cookie", "")),
            )
        return (
            "https://pc-api.uc.cn/1/clouddrive",
            "UCBrowser",
            UA_UC,
            str(self._cfg("uc_cookie", "")),
        )

    async def _open_quark_like(self, pan: str, share_id: str, pwd: str):
        base, pr, ua, cookie = self._quark_like_conf(pan)
        if not cookie:
            raise PanError(f"未配置 {PAN_NAMES[pan]} Cookie")
        required = ("__pus", "__puus") if pan == "quark" else ("__puus",)
        if not self._cookie_has(cookie, *required):
            raise PanError(
                f"{PAN_NAMES[pan]} Cookie 缺少鉴权字段 {'/'.join(required)}，"
                "请在浏览器 F12 → Network → Request Headers 中复制完整的 cookie 值"
            )

        headers = {
            "Cookie": cookie,
            "User-Agent": ua,
            "Referer": f"https://{self._host_of(pan)}/",
        }
        token_payload = await self._request(
            "POST",
            f"{base}/share/sharepage/token",
            headers=headers,
            params={"pr": pr, "fr": "pc"},
            json_data={"pwd_id": share_id, "passcode": pwd or ""},
        )
        if not isinstance(token_payload, dict) or token_payload.get("code") not in (
            0,
            None,
        ):
            message = (
                token_payload.get("message")
                if isinstance(token_payload, dict)
                else "响应异常"
            )
            raise PanError(f"获取 share token 失败：{message}")
        stoken = str((token_payload.get("data") or {}).get("stoken") or "")
        if not stoken:
            raise PanError("未获取到 share stoken")

        ctx = {
            "pwd_id": share_id,
            "stoken": stoken,
            "base": base,
            "pr": pr,
            "ua": ua,
            "cookie": cookie,
        }
        files = await self._list_quark_like(pan, ctx, "0")
        return ctx, files, "0"

    async def _list_quark_like(
        self, pan: str, ctx: dict[str, Any], dir_ref: str
    ) -> list[PanFile]:
        base = ctx.get("base") or self._quark_like_conf(pan)[0]
        pr = ctx.get("pr") or self._quark_like_conf(pan)[1]
        headers = {
            "Cookie": ctx.get("cookie", ""),
            "User-Agent": ctx.get("ua", ""),
            "Referer": f"https://{self._host_of(pan)}/",
        }
        detail = await self._request(
            "GET",
            f"{base}/share/sharepage/detail",
            headers=headers,
            params={
                "pr": pr,
                "fr": "pc",
                "pwd_id": ctx.get("pwd_id", ""),
                "stoken": ctx.get("stoken", ""),
                "pdir_fid": dir_ref or "0",
                "force": "0",
                "_page": "1",
                "_size": "100",
                "_fetch_banner": "0",
                "_fetch_share": "0",
                "_fetch_total": "1",
            },
        )
        if not isinstance(detail, dict) or detail.get("code") not in (0, None):
            message = detail.get("message") if isinstance(detail, dict) else "响应异常"
            raise PanError(f"获取文件列表失败：{message}")

        files: list[PanFile] = []
        for entry in (detail.get("data") or {}).get("list") or []:
            is_dir = entry.get("file") is False or entry.get("dir") is True
            fid = str(entry.get("fid") or "")
            files.append(
                PanFile(
                    name=entry.get("file_name") or "未命名",
                    size=int(entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=fid,
                    dir_ref=fid if is_dir else "",
                    extra={
                        **ctx,
                        "share_fid_token": entry.get("share_fid_token")
                        or entry.get("fid_token")
                        or "",
                    },
                )
            )
        return files

    async def _link_quark_like(self, pan: str, item: PanFile) -> str:
        base = item.extra.get("base") or self._quark_like_conf(pan)[0]
        pr = item.extra.get("pr") or self._quark_like_conf(pan)[1]
        headers = {
            "Cookie": item.extra.get("cookie", ""),
            "User-Agent": item.extra.get("ua", ""),
        }
        payload = await self._request(
            "POST",
            f"{base}/share/sharepage/download",
            headers=headers,
            params={"pr": pr, "fr": "pc"},
            json_data={
                "fids": [item.file_id],
                "fids_token": [item.extra.get("share_fid_token", "")],
                "pwd_id": item.extra.get("pwd_id", ""),
                "stoken": item.extra.get("stoken", ""),
            },
        )
        if not isinstance(payload, dict):
            raise PanError("直链响应异常")
        if payload.get("code") not in (0, None):
            raise PanError(
                f"{payload.get('code')}：{payload.get('message', '未知错误')}"
            )
        rows = payload.get("data") or []
        if rows and rows[0].get("download_url"):
            return str(rows[0]["download_url"])
        raise PanError("未返回下载链接")

    async def _open_quark(self, raw_url: str, share_id: str, pwd: str):
        return await self._open_quark_like("quark", share_id, pwd)

    async def _list_quark(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        return await self._list_quark_like("quark", ctx, dir_ref)

    async def _link_quark(self, item: PanFile) -> str:
        return await self._link_quark_like("quark", item)

    async def _open_uc(self, raw_url: str, share_id: str, pwd: str):
        return await self._open_quark_like("uc", share_id, pwd)

    async def _list_uc(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        return await self._list_quark_like("uc", ctx, dir_ref)

    async def _link_uc(self, item: PanFile) -> str:
        return await self._link_quark_like("uc", item)

    @staticmethod
    def _host_of(pan: str) -> str:
        return {
            "quark": "pan.quark.cn",
            "uc": "drive.uc.cn",
        }.get(pan, "pan.quark.cn")

    # ------------------------------------------------------------------
    # 迅雷云盘
    # ------------------------------------------------------------------

    async def _open_xunlei(self, raw_url: str, share_id: str, pwd: str):
        token = str(self._cfg("xunlei_access_token", ""))
        if not token:
            raise PanError("未配置迅雷 access_token（xunlei_access_token）")
        ctx = {"share_id": share_id, "pass_code": pwd or ""}
        files = await self._list_xunlei(ctx, "")
        return ctx, files, ""

    async def _list_xunlei(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        token = str(self._cfg("xunlei_access_token", ""))
        headers = {
            "Authorization": f"Bearer {token}",
            "X-Captcha-Token": str(self._cfg("xunlei_captcha_token", "")),
            "X-Device-Id": str(self._cfg("xunlei_device_id", "")),
            "Referer": "https://pan.xunlei.com/",
        }
        params = {
            "share_id": ctx.get("share_id", ""),
            "pass_code": ctx.get("pass_code", ""),
            "limit": "100",
            "with_audit": "true",
        }
        if dir_ref:
            params["parent_id"] = dir_ref
        payload = await self._request(
            "GET",
            "https://api-pan.xunlei.com/drive/v1/share",
            headers=headers,
            params=params,
        )
        if not isinstance(payload, dict):
            raise PanError("文件列表响应异常")
        if payload.get("error_code") or payload.get("error"):
            raise PanError(
                str(payload.get("error_description") or payload.get("error") or "未知错误")
            )

        files: list[PanFile] = []
        for entry in payload.get("files") or []:
            is_dir = str(entry.get("kind")) == "drive#folder"
            fid = str(entry.get("id") or "")
            files.append(
                PanFile(
                    name=entry.get("name") or "未命名",
                    size=int(entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=fid,
                    dir_ref=fid if is_dir else "",
                    extra=dict(ctx),
                )
            )
        return files

    async def _link_xunlei(self, item: PanFile) -> str:
        token = str(self._cfg("xunlei_access_token", ""))
        payload = await self._request(
            "GET",
            f"https://api-pan.xunlei.com/drive/v1/files/{item.file_id}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Captcha-Token": str(self._cfg("xunlei_captcha_token", "")),
                "X-Device-Id": str(self._cfg("xunlei_device_id", "")),
            },
            params={"share_id": item.extra.get("share_id", "")},
        )
        if isinstance(payload, dict):
            if payload.get("web_content_link"):
                return str(payload["web_content_link"])
            if payload.get("error_code"):
                raise PanError(
                    str(payload.get("error_description") or payload.get("error_code"))
                )
        raise PanError("未返回下载链接")

    # ------------------------------------------------------------------
    # 123 云盘
    # ------------------------------------------------------------------

    async def _open_123pan(self, raw_url: str, share_id: str, pwd: str):
        ctx = {"share_key": share_id, "share_pwd": pwd or ""}
        files = await self._list_123pan(ctx, "0")
        return ctx, files, "0"

    async def _list_123pan(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        token = str(self._cfg("pan123_token", ""))
        share_key = ctx.get("share_key", "")
        headers = {
            "Cookie": str(self._cfg("pan123_cookie", "")),
            "Referer": f"https://www.123pan.com/s/{share_key}",
            "Platform": "android",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = await self._request(
            "GET",
            "https://www.123pan.com/b/api/share/get",
            headers=headers,
            params={
                "limit": "100",
                "next": "1",
                "orderBy": "share_id",
                "orderDirection": "desc",
                "parentFileId": dir_ref or "0",
                "shareKey": share_key,
                "SharePwd": ctx.get("share_pwd", ""),
                "Page": "1",
            },
        )
        if not isinstance(payload, dict):
            raise PanError("文件列表响应异常")
        if payload.get("code") not in (0, 200, None):
            raise PanError(
                f"{payload.get('code')}：{payload.get('message', '未知错误')}"
            )

        info = payload.get("data") or {}
        rows = info.get("InfoList") or info.get("infoList") or []
        files: list[PanFile] = []
        for entry in rows:
            is_dir = int(entry.get("Type") or 0) != 0
            fid = str(entry.get("FileId") or entry.get("fileId") or 0)
            files.append(
                PanFile(
                    name=entry.get("FileName") or entry.get("fileName") or "未命名",
                    size=int(entry.get("Size") or entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=fid,
                    dir_ref=fid if is_dir else "",
                    extra={
                        **ctx,
                        "s3key": entry.get("S3KeyFlag") or entry.get("s3KeyFlag") or "",
                        "etag": entry.get("Etag") or entry.get("etag") or "",
                    },
                )
            )
        return files

    async def _link_123pan(self, item: PanFile) -> str:
        token = str(self._cfg("pan123_token", ""))
        headers = {
            "Cookie": str(self._cfg("pan123_cookie", "")),
            "Content-Type": "application/json",
            "Platform": "ios",
            "Referer": f"https://www.123pan.com/s/{item.extra.get('share_key', '')}",
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"

        payload = await self._request(
            "POST",
            "https://www.123pan.com/api/share/download/info",
            headers=headers,
            json_data={
                "ShareKey": item.extra.get("share_key", ""),
                "FileID": item.file_id,
                "S3keyFlag": item.extra.get("s3key", ""),
                "Size": item.size,
                "Etag": item.extra.get("etag", ""),
                "SharePwd": item.extra.get("share_pwd", ""),
            },
        )
        if not isinstance(payload, dict):
            raise PanError("直链响应异常")
        data = payload.get("data") or {}
        url = data.get("DownloadUrl") or data.get("DownloadURL") or ""
        if url:
            match = re.search(r"[?&]params=([^&]+)", url)
            if match:
                url = self._safe_urlsafe_b64decode(match.group(1)) or url
            return str(url)
        if payload.get("code") not in (0, 200, None):
            raise PanError(
                f"{payload.get('code')}：{payload.get('message', '未知错误')}"
            )
        raise PanError("未返回下载链接")

    @staticmethod
    def _safe_urlsafe_b64decode(value: str) -> str:
        import base64
        import urllib.parse

        try:
            raw = urllib.parse.unquote(value)
            padded = raw + "=" * (-len(raw) % 4)
            return base64.urlsafe_b64decode(padded).decode("utf-8", "ignore")
        except Exception:  # noqa: BLE001
            return ""

    # ------------------------------------------------------------------
    # 中国移动云盘（139）
    # ------------------------------------------------------------------

    async def _open_mcloud(self, raw_url: str, share_id: str, pwd: str):
        cookie = str(self._cfg("mcloud_cookie", ""))
        if not cookie:
            raise PanError("未配置移动云盘 Cookie（mcloud_cookie）")

        info = await self._request(
            "POST",
            "https://share-kd-njs.yun.139.com/hcy/share/getShareInfoByCodeV2",
            headers={"Cookie": cookie, "Referer": "https://yun.139.com/"},
            json_data={"shareId": share_id, "sharePwd": pwd or ""},
        )
        if not isinstance(info, dict) or str(info.get("code")) not in {
            "0",
            "200",
            "None",
        }:
            message = info.get("message") if isinstance(info, dict) else "响应异常"
            raise PanError(f"获取分享信息失败：{message}")

        data = info.get("data") or {}
        ca_id = data.get("caId") or data.get("shareId") or share_id
        ctx = {"share_id": share_id, "ca_id": ca_id}
        files = await self._list_mcloud(ctx, "")
        return ctx, files, ""

    async def _list_mcloud(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        cookie = str(self._cfg("mcloud_cookie", ""))
        listing = await self._request(
            "POST",
            "https://share-kd-njs.yun.139.com/hcy/share/getContentList",
            headers={"Cookie": cookie, "Referer": "https://yun.139.com/"},
            json_data={
                "shareId": ctx.get("share_id", ""),
                "caId": ctx.get("ca_id", ""),
                "contentId": dir_ref or "",
                "pageNum": 1,
                "pageSize": 100,
                "sortType": 1,
                "isShare": True,
            },
        )
        if not isinstance(listing, dict):
            raise PanError("文件列表响应异常")
        if str(listing.get("code")) not in {"0", "200", "None"}:
            raise PanError(f"获取文件列表失败：{listing.get('message', '未知错误')}")

        rows = ((listing.get("data") or {}).get("contentList")) or []
        files: list[PanFile] = []
        for entry in rows:
            is_dir = bool(entry.get("dirEtag") or entry.get("caName"))
            cid = str(entry.get("contentID") or entry.get("coId") or "")
            files.append(
                PanFile(
                    name=entry.get("contentName") or entry.get("coName") or "未命名",
                    size=int(entry.get("contentSize") or entry.get("coSize") or 0),
                    is_dir=is_dir,
                    file_id=cid,
                    dir_ref=cid if is_dir else "",
                    extra=dict(ctx),
                )
            )
        return files

    async def _link_mcloud(self, item: PanFile) -> str:
        cookie = str(self._cfg("mcloud_cookie", ""))
        payload = await self._request(
            "POST",
            "https://personal-kd-njs.yun.139.com/hcy/file/getDownloadUrl",
            headers={"Cookie": cookie, "Referer": "https://yun.139.com/"},
            json_data={
                "contentId": item.file_id,
                "shareId": item.extra.get("share_id", ""),
                "caId": item.extra.get("ca_id", ""),
                "isShare": True,
            },
        )
        if isinstance(payload, dict):
            data = payload.get("data") or {}
            url = data.get("downloadUrl") or data.get("cdnUrl") or ""
            if url:
                return str(url)
            if str(payload.get("code")) not in {"0", "200", "None"}:
                raise PanError(
                    f"{payload.get('code')}：{payload.get('message', '未知错误')}"
                )
        raise PanError("未返回下载链接")

    # ------------------------------------------------------------------
    # 天翼云盘（189）
    # ------------------------------------------------------------------

    async def _open_tcloud(self, raw_url: str, share_id: str, pwd: str):
        cookie = str(self._cfg("tcloud_cookie", ""))
        if not cookie:
            raise PanError("未配置天翼云盘 Cookie（tcloud_cookie）")

        headers = {"Cookie": cookie, "Referer": "https://cloud.189.cn/"}
        share_code = share_id

        info = await self._request(
            "GET",
            "https://cloud.189.cn/api/portal/getShareInfoByCode.action",
            headers=headers,
            params={"shareCode": share_code},
        )
        if isinstance(info, dict) and info.get("errorCode"):
            raise PanError(str(info.get("errorMsg") or info.get("errorCode")))

        share_info = (info.get("shareInfo") if isinstance(info, dict) else {}) or {}
        share_id_real = str(share_info.get("shareId") or "")
        if not share_id_real:
            raise PanError("无法获取分享 ID，链接可能已失效")

        if pwd:
            check = await self._request(
                "POST",
                "https://cloud.189.cn/api/portal/checkAccessCode.action",
                headers=headers,
                params={"shareCode": share_code, "accessCode": pwd},
            )
            if isinstance(check, dict) and check.get("errorCode"):
                raise PanError(
                    f"提取码校验失败：{check.get('errorMsg') or check.get('errorCode')}"
                )

        root_ref = str(share_info.get("fileId") or "")
        ctx = {"share_id": share_id_real, "root": root_ref}
        files = await self._list_tcloud(ctx, root_ref)
        return ctx, files, root_ref

    async def _list_tcloud(self, ctx: dict[str, Any], dir_ref: str) -> list[PanFile]:
        cookie = str(self._cfg("tcloud_cookie", ""))
        listing = await self._request(
            "GET",
            "https://cloud.189.cn/api/portal/listFiles.action",
            headers={"Cookie": cookie, "Referer": "https://cloud.189.cn/"},
            params={
                "shareId": ctx.get("share_id", ""),
                "fileId": dir_ref or ctx.get("root", ""),
                # 顶层如果不是文件夹，isFolder 为 false 只列同级；是文件夹则列其内容
                "isFolder": "true" if (dir_ref or ctx.get("root")) else "false",
                "pageNum": "1",
                "pageSize": "100",
                "sortType": "1",
                "sortOrder": "ASC",
            },
        )
        if not isinstance(listing, dict):
            raise PanError("文件列表响应异常")
        if listing.get("errorCode"):
            raise PanError(str(listing.get("errorMsg") or listing.get("errorCode")))

        raw = listing.get("fileList") or {}
        rows = raw.get("file") if isinstance(raw, dict) else raw
        files: list[PanFile] = []
        for entry in rows or []:
            if not isinstance(entry, dict):
                continue
            is_dir = bool(entry.get("isFolder"))
            fid = str(entry.get("id") or "")
            files.append(
                PanFile(
                    name=entry.get("name") or "未命名",
                    size=int(entry.get("size") or 0),
                    is_dir=is_dir,
                    file_id=fid,
                    dir_ref=fid if is_dir else "",
                    extra=dict(ctx),
                )
            )
        return files

    async def _link_tcloud(self, item: PanFile) -> str:
        cookie = str(self._cfg("tcloud_cookie", ""))
        payload = await self._request(
            "GET",
            "https://cloud.189.cn/api/portal/getFileDownloadUrl.action",
            headers={"Cookie": cookie, "Referer": "https://cloud.189.cn/"},
            params={
                "shareId": item.extra.get("share_id", ""),
                "fileId": item.file_id,
                "dt": "1",
            },
        )
        if isinstance(payload, dict):
            url = payload.get("fileDownloadUrl") or payload.get("downloadUrl") or ""
            if url:
                return str(url)
            if payload.get("errorCode"):
                raise PanError(
                    str(payload.get("errorMsg") or payload.get("errorCode"))
                )
        raise PanError("未返回下载链接")

    # ------------------------------------------------------------------
    # 杂项
    # ------------------------------------------------------------------

    @staticmethod
    def _re_first(text: str, pattern: str) -> str:
        match = re.search(pattern, text or "")
        return match.group(1) if match else ""

    async def cleanup_temp_files(self) -> None:
        """清理插件自己的临时目录中过期的文件。"""
        now = time.time()
        for path in self._temp_dir.glob("*"):
            try:
                if path.is_file() and now - path.stat().st_mtime > 3600:
                    os.remove(path)
            except OSError:
                logger.debug("清理临时文件失败：%s", path, exc_info=True)
