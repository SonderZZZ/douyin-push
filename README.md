# astrbot_plugin_douyin_push

用于 AstrBot 的抖音作品监控插件：定时监控指定抖音用户，如果发现新作品，就向已绑定会话主动推送通知，并按配置下载视频或图集媒体。

## 功能

- 通过 `sec_user_id` 或抖音用户主页链接添加监控用户。
- 后台定时检查用户主页最新作品。
- 支持把当前聊天会话绑定为主动推送目标。
- 支持下载新作品的视频/图片到 `data/plugins/astrbot_plugin_douyin_push/downloads`（可配置）。
- 同步记录用户主页关注数、粉丝数、获赞数和作品数，并按配置每天定时推送总结分析。
- 状态保存到 `data/plugins/astrbot_plugin_douyin_push/state.json`，避免插件更新时覆盖数据。

> 注意：抖音 Web 接口可能调整或触发风控。建议在 WebUI 配置中填写自己的 `douyin.com` Cookie，并设置合理的检查间隔。请仅监控和下载你有权访问、保存的内容。

## 安装

1. 将本仓库放入 AstrBot 的 `data/plugins/` 目录。
2. 在 AstrBot WebUI 的插件页安装依赖或手动执行：

```bash
pip install -r requirements.txt
```

3. 在插件配置页填写 `monitored_users`、`cookie` 等配置。
4. 重载插件。

## 配置示例

`monitored_users` 每行一个用户：

```text
MS4wLjABAAAAxxxxxxxxxxxxxxxxxxxx 用户备注
https://www.douyin.com/user/MS4wLjABAAAAyyyyyyyyyyyyyyyyyyyy 另一个用户
```

首次运行默认只记录最新作品，不推送历史内容；如果希望首次也推送拉取到的作品，可开启 `notify_existing_on_first_run`。

每日总结默认在服务器本地时间 `23:55` 推送到已绑定会话，可通过 `daily_summary_time` 调整；总结会对比 `summary_window_days` 窗口内首次和最新采样，展示关注、粉丝、获赞、作品数的当前值和变化量。

## 指令

| 指令 | 说明 |
| --- | --- |
| `/dy_bind` 或 `/抖音绑定` | 绑定当前会话为推送目标。 |
| `/dy_unbind` 或 `/抖音解绑` | 取消当前会话推送。 |
| `/dy_add <sec_user_id/主页链接> [备注]` 或 `/抖音添加 ...` | 添加监控用户。 |
| `/dy_remove <sec_user_id/备注>` 或 `/抖音删除 ...` | 移除监控用户。 |
| `/dy_status` 或 `/抖音状态` | 查看监控状态和最近一次主页数据。 |
| `/dy_summary` 或 `/抖音总结` | 立即生成一次主页数据总结分析。 |
| `/dy_check` 或 `/抖音检查` | 立即检查一次，不主动推送到其它会话，只把结果回复当前会话。 |

## 数据目录

- 状态文件：`data/plugins/astrbot_plugin_douyin_push/state.json`（包含作品去重状态、推送会话、主页数据采样历史和每日总结日期）
- 默认下载目录：`data/plugins/astrbot_plugin_douyin_push/downloads/`

## 开发参考

插件结构遵循 [AstrBot 插件开发指南](https://docs.astrbot.app/dev/star/plugin-new.html)。
