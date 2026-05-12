# astrbot_plugin_douyin_push

用于 AstrBot 的抖音作品监控插件：定时监控指定抖音用户，如果发现新作品，就向已绑定会话主动推送通知，并按配置下载视频或图集媒体。

## 功能

- 通过 `sec_user_id` 或抖音用户主页链接添加监控用户。
- 后台定时检查用户主页最新作品，并按发布时间排序识别更新，避免置顶作品长期占据第一条导致漏推。
- 支持把当前聊天会话绑定为主动推送目标。
- 支持下载新作品的视频/图片到 `data/plugins/astrbot_plugin_douyin_push/downloads`（可配置）。
- 同步记录用户主页关注数、粉丝数、获赞数和作品数，并按配置每天定时推送总结分析。
- 状态保存到 `data/plugins/astrbot_plugin_douyin_push/state.json`，避免插件更新时覆盖数据。

> 注意：抖音 Web 接口可能调整或触发风控。建议在 WebUI 配置中填写自己的 `douyin.com` Cookie，或使用扫码脚本生成 Cookie 文件，并设置合理的检查间隔。请仅监控和下载你有权访问、保存的内容。

## 安装

1. 将本仓库放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 的插件页安装依赖或手动执行：

```bash
pip install -r requirements.txt
```

3. 在插件配置页填写 `monitored_users`、`cookie` 等配置；如果不想手动复制 Cookie，可按下方“扫码获取 Cookie”生成 Cookie 文件。
4. 重载插件。

## 扫码获取 Cookie

如果 `/dy_check` 返回“接口未返回 JSON”、响应片段像 HTML 登录页，通常是 Cookie 缺失/失效或触发风控。可以用真实 Chromium 浏览器扫码登录并导出 Cookie：

```bash
python -m pip install playwright
python -m playwright install chromium
python scripts/douyin_cookie_login.py
```

脚本会打开抖音网页版。请在浏览器中点击登录并用抖音 App 扫码确认；登录完成后回到终端按 Enter，Cookie 会默认写入 `data/plugins/astrbot_plugin_douyin_push/douyin_cookie.txt`。如果插件已经在运行，请发送 `/dy_reload_cookie` 让插件重建 HTTP 客户端，再发送 `/dy_check` 验证。

也可以发送 `/dy_cookie_status` 查看当前插件是否读取到了配置项 Cookie 或 Cookie 文件。

## 配置示例

`monitored_users` 每行一个用户：

```text
MS4wLjABAAAAxxxxxxxxxxxxxxxxxxxx 用户备注
https://www.douyin.com/user/MS4wLjABAAAAyyyyyyyyyyyyyyyyyyyy 另一个用户
```

首次运行默认只记录最新作品，不推送历史内容；如果希望首次也推送拉取到的作品，可开启 `notify_existing_on_first_run`。后续判断更新时不会只看第一条作品 ID，而是按 `create_time` 发布时间排序，并结合 `seen_aweme_ids` 去重；如果作者长期有置顶作品，建议适当调大 `fetch_count` 和 `seen_aweme_history_limit`。`/dy_check` 默认开启 `manual_check_push_enabled`：手动检查发现新作品时也会推送到已绑定会话，并且只有推送成功后才更新水位线，避免手动检查把待推送作品“吃掉”。

每日总结默认在 `+08:00` 时区的 `23:55` 推送到已绑定会话，可通过 `daily_summary_time` 和 `daily_summary_utc_offset` 调整；总结会对比 `summary_window_days` 窗口内首次和最新采样，展示关注、粉丝、获赞、作品数的当前值和变化量。请先在需要接收总结的会话发送 `/dy_bind`，否则插件不会把当天标记为已发送；如果错过时间，下一次后台轮询或手动 `/dy_check` 会补发到期总结，并在回复中说明是否已发送、未到时间、已发送或没有绑定会话。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/dy_bind` 或 `/抖音绑定` | 绑定当前会话为推送目标。 |
| `/dy_unbind` 或 `/抖音解绑` | 取消当前会话推送。 |
| `/dy_add <sec_user_id/主页链接> [备注]` 或 `/抖音添加 ...` | 添加监控用户。 |
| `/dy_remove <sec_user_id/备注>` 或 `/抖音删除 ...` | 移除监控用户。 |
| `/dy_cookie_status` 或 `/抖音Cookie状态` | 查看 Cookie 配置/文件读取状态。 |
| `/dy_reload_cookie` 或 `/抖音重载Cookie` | 扫码脚本更新 Cookie 文件后，重建 HTTP 客户端。 |
| `/dy_status` 或 `/抖音状态` | 查看监控状态、后台任务是否运行、上次后台检查结果和最近一次主页数据。 |
| `/dy_summary` 或 `/抖音总结` | 立即生成一次主页数据总结分析。 |
| `/dy_check` 或 `/抖音检查` | 立即检查一次，逐个用户回复新作品/无更新/初始化/失败原因；默认会把发现的新作品推送到已绑定会话，并触发已到期的每日总结补发。 |

## 数据目录

- 状态文件：`data/plugins/astrbot_plugin_douyin_push/state.json`（包含作品去重状态、推送会话、主页数据采样历史和每日总结日期）
- 默认下载目录：`data/plugins/astrbot_plugin_douyin_push/downloads/`

## 开发参考

插件结构遵循 [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)。
