# 网盘直链解析插件（astrbot_plugin_pan_link）

在 AstrBot 中实现网盘分享链接识别、直链解析、会话内多文件选择与 OneBot 文件发送。

## 功能

- **正则识别分享链接**：消息中出现网盘分享链接时自动触发解析，支持百度网盘、阿里云盘、中国移动云盘、天翼云盘、迅雷云盘、夸克网盘、UC 网盘、123 云盘。
- **提取码识别**：支持链接中的 `pwd=` / `code=` 参数，也支持「提取码: xxxx」这类文本。
- **多文件选择**：分享内有多个文件时，机器人发送带序号的文件列表，用户回复 `1`、`1 3 5`、`2-4`、`all` 即可选择。
- **OneBot 发送**：默认下载到本地临时目录后通过 `File` 消息段发送（`send_mode=download`），也可把直链直接交给协议端上传（`send_mode=url`）。
- **大小限制**：可配置最大发送体积，超限时只返回直链。

## 目录结构

| 文件 | 说明 |
| --- | --- |
| [`main.py`](main.py) | 插件主体：链接识别、各网盘取链、会话选择、文件发送 |
| [`metadata.yaml`](metadata.yaml) | 插件元数据 |
| [`_conf_schema.json`](_conf_schema.json) | 各网盘 Cookie / Token 与发送参数配置 |
| [`requirements.txt`](requirements.txt) | 依赖（`aiohttp`） |

## 配置说明

在 AstrBot 插件配置页填写各网盘的登录凭证：

| 配置项 | 说明 |
| --- | --- |
| `baidu_cookie` | 百度网盘完整 Cookie（必须包含 `BDUSS`） |
| `baidu_access_token` | 可选，用于生成带鉴权的 dlink |
| `aliyun_refresh_token` | 阿里云盘 `refresh_token` |
| `mcloud_cookie` | 中国移动云盘 Cookie |
| `tcloud_cookie` | 天翼云盘 Cookie |
| `xunlei_access_token` / `xunlei_captcha_token` / `xunlei_device_id` | 迅雷云盘凭证 |
| `quark_cookie` | 夸克网盘 Cookie |
| `uc_cookie` | UC 网盘 Cookie |
| `pan123_cookie` / `pan123_token` | 123 云盘 Cookie 与 `authorToken` |
| `enabled_pans` | 启用的网盘列表 |
| `auto_send_single` | 单文件时直接发送 |
| `selection_timeout` | 选择等待超时（秒） |
| `max_file_size_mb` | 允许发送的最大文件体积，0 为不限制 |
| `send_mode` | `download`（默认）或 `url` |

## 使用方式

1. 在群里或私聊发送网盘分享链接，例如：
   `https://pan.quark.cn/s/xxxxxxxx 提取码：abcd`
2. 机器人返回解析结果。若只有一个文件且开启 `auto_send_single`，会直接发送文件。
3. 若为多文件，回复序号选择：
   - `1`：第 1 个文件
   - `1 3 5` 或 `1,3,5`：多个文件
   - `2-4`：第 2 到第 4 个
   - `all`：全部文件

## 注意事项

- 各网盘使用的是非公开接口，接口或风控策略可能随时变化，解析失败时会返回具体错误信息，便于排查。
- 百度网盘分享直链依赖 Cookie 与分享校验，超过一定大小的文件通常需要转存后下载。
- 阿里云盘 / 迅雷 / 123 云盘的直链带有时效性，插件会在发送前即时获取。
- 下载的文件保存在系统临时目录 `astrbot_pan_link/` 下，插件启动时会清理超过 1 小时的旧文件。
