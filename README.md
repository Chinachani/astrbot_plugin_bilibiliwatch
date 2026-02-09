# AstrBot 哔哩哔哩链接解析插件

自动识别群聊中的 B 站链接或 `BV/AV` 号，获取视频基础信息并返回。

## 功能
- 支持完整链接、短链、BV/AV 号、小程序卡片中的链接
- 支持番剧链接（ep/ss）
- 拉取视频标题、UP 主、简介摘要、播放/弹幕/收藏/点赞等信息
- 可选 Cookie（提高获取高质量/会员内容的可访问性）
- 可选自动检测或手动命令触发

## 安装
将插件目录放入 AstrBot 插件目录后启用即可。

## 命令
- `/bili help` 或 `/bili_menu` 查看菜单
- `/bili_debug` 查看最近一次 JSON 卡片内容（用于排查小程序）
- `/bili_callback sync` 同步回调配置到 API（需配置 token）
- `/bili_callback set <token>` 设置回调 token
- `/bili_login start` 获取登录二维码
- `/bili_login status <qrcode_key>` 查询扫码状态
- `/bili_task list [页码]` 查看下载任务（分页）
- `/bili_task list <标签>` 按标签筛选任务
- `/bili_task cancel <序号>` 取消下载任务
- `/bili_task del <序号>` 删除任务
- `/bili_task canceltag <标签>` 按标签取消任务
- `/bili_task deltag <标签>` 按标签删除任务
- `/bili_task clear` 取消全部下载任务
- `/bili_file list [页码]` 查看已下载文件（分页）
- `/bili_file list <标签>` 按标签筛选文件
- `/bili_file send <序号>` 发送已下载视频
- `/bili_file del <序号>` 删除已下载文件
- `/bili_file deltag <标签>` 按标签删除文件
- `/bili_file clear` 删除全部已下载文件
- `/bili_api show` 查看当前 API 配置
- `/bili_api set <base_url> [token]` 修改 API 地址/令牌
- `/bili_net` 连通性测试（官方接口 / 自建API / 回调监听）

## 菜单
主菜单：
```
========== BiliWatch 菜单 ==========
简化入口：/bili_menu <模块>
模块列表：parse / login / task / file / config
--------------------------------------
【解析】/bili_menu parse  - 自动解析/番剧/小程序
【登录】/bili_menu login  - 扫码登录
【任务】/bili_menu task   - 下载任务
【文件】/bili_menu file   - 已下载文件
【接口】/bili_menu api    - 解析API配置
【网络】/bili_menu net    - 连通性测试
【配置】/bili_menu config - 配置说明
--------------------------------------
示例：/bili_menu task
完整命令：/bili help
```

## 配置（_conf_schema.json）
- `bilibili_cookie`: 可选 Cookie（访问会员/高清内容）
- `video_api_base_url`: 自定义视频解析 API 基础地址（可选，要求提供 `/api/video/info`）
- `video_api_login_token`: 自定义解析 API 登录令牌（用于 `/api/login/*` 与 `/api/callback/config`）
- `custom_api_fallback_official`: 自定义 API 失败时是否回退官方接口（默认 true）
- `prefer_official_api`: 获取视频信息时优先使用官方接口（默认 false）
- `video_download_timeout_sec`: 视频文件下载等待超时（秒，0=不限制）
- `video_progress_stall_limit`: 下载进度停滞判定次数（默认 3）
- `video_poll_interval_sec`: 下载状态轮询间隔（秒，默认 1）
- `max_video_duration_sec`: 视频过长不发送文件（分钟，0=不限制）
- `max_video_file_size_mb`: 视频文件大小超过限制则拒绝发送（MB，0=不限制）
- `video_quality_preset`: 视频画质预设（-1=低，0=中，1=高，默认0）
- `auto_lower_quality_on_oversize`: 文件过大时自动降到最低画质（默认 false）
- `enable_auto_detect`: 是否自动识别群聊链接（默认 true）
- `enable_parse_miniapp`: 是否解析QQ小程序卡片中的B站链接（默认 true）
- `api_timeout_sec`: 请求超时秒数（默认 10）
- `request_retries`: 重试次数（默认 2）
- `retry_delay_sec`: 重试间隔秒数（默认 1）
- `show_cover`: 是否显示封面链接（默认 true）
- `enable_video_output`: 是否尝试发送视频文件（默认 false，需要配置 `video_api_base_url`）
- `enable_http_callback`: 是否启用下载完成回调（默认 false）
- `callback_listen_host`: 回调监听地址（默认 0.0.0.0）
- `callback_listen_port`: 回调监听端口（默认 8787，设为 0 关闭）
- `callback_fallback_sec`: 回调未触达时改用轮询的等待秒数（默认 15，0=不启用）
- `callback_token`: 回调令牌（用于 API 回调校验，需与 API 的 `CALLBACK_TOKEN` 一致）
- `user_agent`: 请求 UA（建议填写）
- `debug_log`: 是否输出调试日志（默认 false）
- `enable_parse_hint`: 是否在解析前发送提示语（默认 true）

## 说明
- 默认使用公开接口 `https://api.bilibili.com/x/web-interface/view` 拉取视频信息。
- 如配置了 `video_api_base_url`，将优先使用自定义解析 API（需提供 `/api/video/info?url=...`，返回纯文本）。
- 如需禁用回退，请将 `custom_api_fallback_official` 设为 false。
- 如启用 `enable_video_output`，会调用自建解析服务的下载接口，完成后发送视频文件（合并转发里）。 
- 如启用 `enable_http_callback`，需要在 API 服务设置 `CALLBACK_URL` 指向机器人回调地址（如 `http://机器人IP:8787/bili/callback`）。
- 回调 token 建议由插件统一管理：设置好 `callback_token` 后，执行 `/bili_callback sync` 同步到 API。
- `video_api_login_token` 用于插件远程配置 API（登录/回调配置接口），不填则无法同步。
- 视频过长会提示“建议使用链接观看”（由 `max_video_duration_sec` 控制）。
- 短链会先进行一次跳转解析（b23.tv）。

## 常见错误说明
- `HTTP 400`：参数错误（链接为空或格式不正确）。
- `HTTP 403`：权限不足/Token 错误（登录相关接口）。
- `HTTP 404`：接口或文件不存在。
- `HTTP 500`：API 内部异常（解析/下载失败）。
- `HTTP 599`：网络错误/超时/无法连接 API。
- `HTTP 409`：任务已存在，稍后再试或等待当前任务完成。
