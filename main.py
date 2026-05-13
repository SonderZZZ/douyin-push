import asyncio
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_douyin_push"
DOUYIN_POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
DOUYIN_PROFILE_API = "https://www.douyin.com/aweme/v1/web/user/profile/other/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SEC_UID_PATTERN = re.compile(r"MS4wLj[\w\-.~%]+")


@register(PLUGIN_NAME, "douyin-push", "监控抖音用户作品更新并主动推送/下载", "1.2.0")
class DouyinPushPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path("data") / "plugins" / PLUGIN_NAME
        self.download_dir = Path(str(self.config.get("download_dir") or self.data_dir / "downloads"))
        self.state_path = self.data_dir / "state.json"
        self.cookie_path = Path(str(self.config.get("cookie_file") or self.data_dir / "douyin_cookie.txt"))
        self._state: Dict[str, Any] = {"users": {}, "targets": []}
        self._task: Optional[asyncio.Task] = None
        self._client: Optional[httpx.AsyncClient] = None
        self._running = False

    async def initialize(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self._load_state()
        self._merge_config_users()
        if self._enabled:
            self._ensure_monitor_task()

    async def terminate(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._client:
            await self._client.aclose()

    @property
    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    @property
    def _interval(self) -> int:
        return max(60, int(self.config.get("check_interval_seconds", 300)))

    @property
    def _summary_enabled(self) -> bool:
        return bool(self.config.get("daily_summary_enabled", True))

    @property
    def _summary_time(self) -> str:
        return str(self.config.get("daily_summary_time") or "23:55")

    @property
    def _summary_utc_offset(self) -> str:
        return str(self.config.get("daily_summary_utc_offset") or "+08:00")

    @property
    def _history_limit(self) -> int:
        return max(50, int(self.config.get("seen_aweme_history_limit", 200)))

    @property
    def _manual_check_push_enabled(self) -> bool:
        return bool(self.config.get("manual_check_push_enabled", True))

    @property
    def _summary_records_retention_days(self) -> int:
        return max(31, int(self.config.get("summary_records_retention_days", 370)))

    @filter.command("dy_bind", alias={"抖音绑定"})
    async def bind_target(self, event: AstrMessageEvent):
        """绑定当前会话为抖音更新推送目标。"""
        origin = event.unified_msg_origin
        targets: List[str] = self._state.setdefault("targets", [])
        if origin not in targets:
            targets.append(origin)
            self._save_state()
            yield event.plain_result("已绑定当前会话为抖音更新推送目标。")
        else:
            yield event.plain_result("当前会话已经是抖音更新推送目标。")

    @filter.command("dy_unbind", alias={"抖音解绑"})
    async def unbind_target(self, event: AstrMessageEvent):
        """取消当前会话的抖音更新推送。"""
        origin = event.unified_msg_origin
        targets: List[str] = self._state.setdefault("targets", [])
        if origin in targets:
            targets.remove(origin)
            self._save_state()
            yield event.plain_result("已取消当前会话的抖音更新推送。")
        else:
            yield event.plain_result("当前会话尚未绑定抖音更新推送。")

    @filter.command("dy_add", alias={"抖音添加"})
    async def add_user(self, event: AstrMessageEvent):
        """添加监控用户：/dy_add <sec_user_id 或主页链接> [备注名]。"""
        args = self._split_command_args(event.message_str)
        if not args:
            yield event.plain_result("用法：/dy_add <sec_user_id 或抖音主页链接> [备注名]")
            return

        sec_user_id = self._extract_sec_user_id(args[0])
        if not sec_user_id:
            yield event.plain_result("未识别到 sec_user_id。请使用抖音用户主页 URL 或 MS4wLj... 格式的 sec_user_id。")
            return

        nickname = args[1] if len(args) > 1 else sec_user_id[-8:]
        self._ensure_user(sec_user_id, nickname)
        self._save_state()
        yield event.plain_result(f"已添加监控：{nickname}\nsec_user_id: {sec_user_id}")

    @filter.command("dy_remove", alias={"抖音删除"})
    async def remove_user(self, event: AstrMessageEvent):
        """移除监控用户：/dy_remove <sec_user_id 或备注名>。"""
        args = self._split_command_args(event.message_str)
        if not args:
            yield event.plain_result("用法：/dy_remove <sec_user_id 或备注名>")
            return

        key = args[0]
        users: Dict[str, Any] = self._state.setdefault("users", {})
        target = key if key in users else None
        if target is None:
            for sec_user_id, info in users.items():
                if info.get("nickname") == key:
                    target = sec_user_id
                    break

        if not target:
            yield event.plain_result("未找到该监控用户。")
            return

        nickname = users[target].get("nickname", target[-8:])
        users.pop(target, None)
        self._save_state()
        yield event.plain_result(f"已移除监控：{nickname}")

    @filter.command("dy_cookie_status", alias={"抖音Cookie状态"})
    async def cookie_status(self, event: AstrMessageEvent):
        """查看抖音 Cookie 配置/文件状态。"""
        source = "配置项 cookie" if str(self.config.get("cookie") or "").strip() else f"Cookie 文件 {self.cookie_path}"
        cookie = self._cookie_value()
        status = "已读取" if cookie else "未配置"
        yield event.plain_result(
            f"抖音 Cookie 状态：{status}\n"
            f"来源：{source}\n"
            "如果接口返回 HTML/空内容，请更新 Cookie；可在插件目录运行 scripts/douyin_cookie_login.py 扫码生成 Cookie 文件。"
        )

    @filter.command("dy_reload_cookie", alias={"抖音重载Cookie"})
    async def reload_cookie(self, event: AstrMessageEvent):
        """重载 Cookie 文件/配置，并重建 HTTP 客户端。"""
        if self._client:
            await self._client.aclose()
            self._client = None
        yield event.plain_result("已重载抖音 Cookie，后续请求会使用最新 Cookie。")

    @filter.command("dy_push_test", alias={"抖音推送测试"})
    async def push_test(self, event: AstrMessageEvent):
        """向当前会话发起一次主动推送测试。"""
        origin = event.unified_msg_origin
        targets: List[str] = self._state.setdefault("targets", [])
        if origin not in targets:
            targets.append(origin)
            self._save_state()
        sent_count = await self._push_text("抖音监控主动推送测试：如果你看到这条消息，说明当前会话可以接收主动推送。", origins=[origin])
        self._save_state()
        if sent_count > 0:
            yield event.plain_result("主动推送测试成功，当前会话已绑定。")
        else:
            last_error = (self._state.get("last_push_errors") or {}).get(origin, "未知错误")
            yield event.plain_result(f"主动推送测试失败：{last_error}")

    @filter.command("dy_status", alias={"抖音状态"})
    async def status(self, event: AstrMessageEvent):
        """查看抖音监控状态。"""
        users = self._state.get("users", {})
        targets = self._state.get("targets", [])
        lines = [
            f"抖音监控：{'启用' if self._enabled else '停用'}",
            f"检查间隔：{self._interval} 秒",
            f"作品去重历史上限：{self._history_limit} 条",
            f"每日总结：{'启用' if self._summary_enabled else '停用'}，时间 {self._summary_time}({self._summary_utc_offset})，"
            f"上次发送 {self._state.get('last_daily_summary_date') or '未发送'}",
            f"后台任务：{self._monitor_task_status()}",
            f"上次后台检查：{self._state.get('last_monitor_check_at') or '未执行'}；"
            f"结果 {self._state.get('last_monitor_result') or '无'}",
            f"手动检查发现新作品时推送：{'启用' if self._manual_check_push_enabled else '停用'}",
            f"推送会话数：{len(targets)}",
            f"最近主动推送错误数：{len(self._state.get('last_push_errors') or {})}",
            f"监控用户数：{len(users)}",
        ]
        for sec_user_id, info in users.items():
            stats = info.get("latest_stats") or {}
            stat_text = self._format_stats_inline(stats) if stats else "暂无主页数据"
            lines.append(
                f"- {info.get('nickname', sec_user_id[-8:])}: 最新发布 {self._format_timestamp(info.get('latest_publish_time'))}，"
                f"作品 {info.get('latest_aweme_id') or '未初始化'}；{stat_text}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("dy_summary", alias={"抖音总结"})
    async def summary_now(self, event: AstrMessageEvent):
        """立即生成一次主页数据总结分析。"""
        report = self._build_daily_summary(force=True)
        if report:
            self._save_state()
        yield event.plain_result(report or "暂无可用于总结的主页数据。")

    @filter.command("dy_weekly_summary", alias={"抖音周总结"})
    async def weekly_summary(self, event: AstrMessageEvent):
        """基于每日总结记录生成近 7 天总结。"""
        yield event.plain_result(self._build_period_summary(days=7, title="抖音主页数据周总结"))

    @filter.command("dy_monthly_summary", alias={"抖音月总结"})
    async def monthly_summary(self, event: AstrMessageEvent):
        """基于每日总结记录生成近 30 天总结。"""
        yield event.plain_result(self._build_period_summary(days=30, title="抖音主页数据月总结"))

    @filter.command("dy_check", alias={"抖音检查"})
    async def check_now(self, event: AstrMessageEvent):
        """立即检查一次抖音更新。"""
        self._ensure_monitor_task()
        yield event.plain_result("开始检查抖音更新，请稍候……")
        reports = await self._check_all_users(push=self._manual_check_push_enabled, verbose=True, source="manual")
        summary_result = await self._maybe_push_daily_summary(trigger="manual_check")
        lines = [self._format_check_overview()]
        if reports:
            lines.extend(reports)
        else:
            lines.append("没有监控用户，或本次没有可展示的检查结果。")
        if summary_result:
            lines.append(summary_result)
        yield event.plain_result("\n".join(lines))

    async def _monitor_loop(self):
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._check_all_users(push=True, source="monitor")
                await self._maybe_push_daily_summary(trigger="schedule")
            except Exception as exc:  # noqa: BLE001 - background task must not crash the plugin
                logger.error(f"Douyin monitor loop failed: {exc}")
            await asyncio.sleep(self._interval)

    async def _check_all_users(self, push: bool, verbose: bool = False, source: str = "manual") -> List[str]:
        users: Dict[str, Any] = self._state.setdefault("users", {})
        reports: List[str] = []
        errors = 0
        for sec_user_id, info in list(users.items()):
            try:
                report = await self._check_user(sec_user_id, info, push=push, verbose=verbose)
                if report:
                    reports.append(report)
            except Exception as exc:  # noqa: BLE001 - keep checking other users
                errors += 1
                nickname = info.get("nickname", sec_user_id[-8:])
                message = f"检查 {nickname} 失败：{exc}"
                logger.error(message)
                reports.append(message)
        self._state[f"last_{source}_check_at"] = datetime.now().isoformat(timespec="seconds")
        self._state[f"last_{source}_result"] = f"{len(reports)} 条结果，{errors} 个失败"
        self._save_state()
        return reports

    async def _check_user(self, sec_user_id: str, info: Dict[str, Any], push: bool, verbose: bool = False) -> Optional[str]:
        profile = await self._safe_fetch_user_profile(sec_user_id)
        if profile:
            self._record_profile_stats(sec_user_id, info, profile)

        aweme_list = await self._fetch_latest_awemes(sec_user_id)
        if not aweme_list:
            return self._format_no_change_report(info.get("nickname", sec_user_id[-8:]), info, "未获取到作品列表。") if verbose else None

        if not profile:
            self._record_profile_stats(sec_user_id, info, aweme_list[0].get("author") or {})

        nickname = info.get("nickname") or self._author_name(aweme_list[0]) or sec_user_id[-8:]
        info["nickname"] = nickname
        known_ids: Set[str] = set(str(i) for i in info.get("seen_aweme_ids", []))
        current_ids = [str(item.get("aweme_id")) for item in aweme_list if item.get("aweme_id")]
        if not current_ids:
            return self._format_no_change_report(nickname, info, "作品列表中没有有效作品 ID。") if verbose else None

        sorted_items = self._sort_awemes_by_publish_time(aweme_list)
        latest_item = sorted_items[0]
        latest_aweme_id = str(latest_item.get("aweme_id") or current_ids[0])
        latest_publish_time = self._aweme_publish_time(latest_item)
        last_publish_time = int(info.get("latest_publish_time") or 0)
        if not last_publish_time and known_ids:
            last_publish_time = self._infer_latest_known_publish_time(sorted_items, known_ids)

        if not last_publish_time and not known_ids:
            info["latest_aweme_id"] = latest_aweme_id
            info["latest_publish_time"] = latest_publish_time
            info["seen_aweme_ids"] = current_ids[: self._history_limit]
            if not bool(self.config.get("notify_existing_on_first_run", False)):
                if verbose:
                    return self._format_no_change_report(nickname, info, "首次初始化完成，本次只记录水位线，不推送历史作品。")
                return None

        new_items = [
            item
            for item in sorted_items
            if item.get("aweme_id")
            and str(item.get("aweme_id")) not in known_ids
            and self._aweme_publish_time(item) >= last_publish_time
        ]

        if not new_items:
            info["latest_aweme_id"] = latest_aweme_id
            info["latest_publish_time"] = max(last_publish_time, latest_publish_time)
            info["seen_aweme_ids"] = self._merge_seen_aweme_ids(current_ids, known_ids)
            return self._format_no_change_report(nickname, info, "暂无新作品。") if verbose else None

        new_items.reverse()
        messages = []
        push_success_total = 0
        push_attempt_total = 0
        for item in new_items:
            downloaded = await self._download_aweme(item) if (push or bool(self.config.get("download_enabled", True))) else []
            text = self._format_aweme_message(nickname, item, downloaded)
            messages.append(text)
            if push:
                target_count = len(self._state.get("targets", []))
                push_attempt_total += target_count
                push_success_total += await self._push_aweme_message(text, downloaded)

        prefix = f"{nickname}：发现 {len(new_items)} 个新作品。"
        if push:
            if push_attempt_total <= 0:
                prefix += " 但没有绑定推送会话，未更新水位线；请先在目标会话发送 /dy_bind 后再次检查。"
                return prefix + "\n" + "\n\n".join(messages)
            if push_success_total <= 0:
                prefix += f" 推送 0/{push_attempt_total} 条会话消息，未更新水位线；请检查绑定会话是否可用。"
                return prefix + "\n" + "\n\n".join(messages)
            prefix += f" 已推送 {push_success_total}/{push_attempt_total} 条会话消息。"
        else:
            prefix += " 本次仅检查不推送，若要手动检查时也推送，请开启 manual_check_push_enabled。"

        info["latest_aweme_id"] = latest_aweme_id
        info["latest_publish_time"] = max(last_publish_time, latest_publish_time)
        info["seen_aweme_ids"] = self._merge_seen_aweme_ids(current_ids, known_ids)
        return prefix + "\n" + "\n\n".join(messages)

    def _format_check_overview(self) -> str:
        users = self._state.get("users", {})
        return (
            f"检查完成：监控用户 {len(users)} 个，推送会话 {len(self._state.get('targets', []))} 个，"
            f"后台任务 {self._monitor_task_status()}，"
            f"手动发现新作品推送 {'启用' if self._manual_check_push_enabled else '停用'}，"
            f"每日总结 {'启用' if self._summary_enabled else '停用'}（{self._summary_time} {self._summary_utc_offset}）。"
        )

    def _ensure_monitor_task(self):
        if not self._enabled:
            return
        if self._task and not self._task.done():
            self._running = True
            return
        if self._task and self._task.done():
            try:
                self._task.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - restart after unexpected background failure
                logger.error(f"Douyin monitor task stopped unexpectedly and will restart: {exc}")
        self._running = True
        self._task = asyncio.create_task(self._monitor_loop())
        logger.info("DouyinPushPlugin monitor task started")

    def _monitor_task_status(self) -> str:
        if not self._enabled:
            return "停用"
        if not self._task:
            return "未启动"
        if self._task.cancelled():
            return "已取消"
        if self._task.done():
            return "已停止"
        return "运行中"

    def _format_no_change_report(self, nickname: str, info: Dict[str, Any], reason: str) -> str:
        return (
            f"{nickname}：{reason} 最新发布 {self._format_timestamp(info.get('latest_publish_time'))}，"
            f"作品 {info.get('latest_aweme_id') or '未初始化'}；{self._format_stats_inline(info.get('latest_stats') or {})}"
        )

    def _sort_awemes_by_publish_time(self, aweme_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return sorted(
            aweme_list,
            key=lambda item: (self._aweme_publish_time(item), str(item.get("aweme_id") or "")),
            reverse=True,
        )

    def _aweme_publish_time(self, item: Dict[str, Any]) -> int:
        return self._to_int(item.get("create_time")) or 0

    def _infer_latest_known_publish_time(self, sorted_items: List[Dict[str, Any]], known_ids: Set[str]) -> int:
        known_times = [self._aweme_publish_time(item) for item in sorted_items if str(item.get("aweme_id")) in known_ids]
        return max(known_times, default=0)

    def _merge_seen_aweme_ids(self, current_ids: List[str], known_ids: Set[str]) -> List[str]:
        return list(dict.fromkeys(current_ids + list(known_ids)))[: self._history_limit]

    async def _fetch_latest_awemes(self, sec_user_id: str) -> List[Dict[str, Any]]:
        client = self._get_client()
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": sec_user_id,
            "max_cursor": "0",
            "count": str(int(self.config.get("fetch_count", 10))),
            "publish_video_strategy_type": "2",
            "version_code": "290100",
            "version_name": "29.1.0",
            "pc_client_type": "1",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "124.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "platform": "PC",
        }
        response = await client.get(DOUYIN_POST_API, params=params)
        response.raise_for_status()
        data = await self._read_json_response(response, "作品列表")
        if data.get("status_code") not in (0, None):
            raise RuntimeError(data.get("status_msg") or data.get("message") or "Douyin API returned non-zero status")
        return data.get("aweme_list") or []


    async def _read_json_response(self, response: httpx.Response, scene: str) -> Dict[str, Any]:
        content_type = response.headers.get("content-type", "")
        text = response.text
        try:
            data = response.json()
        except json.JSONDecodeError as exc:
            preview = self._response_preview(text)
            message = (
                f"抖音{scene}接口未返回 JSON（HTTP {response.status_code}, Content-Type: {content_type or '未知'}）。"
                "这通常是 Cookie 失效/缺失、触发登录页或风控导致的；请更新 Cookie 后执行 /dy_reload_cookie。"
            )
            if preview:
                message += f" 响应片段：{preview}"
            raise RuntimeError(message) from exc
        if not isinstance(data, dict):
            raise RuntimeError(f"抖音{scene}接口返回了非对象 JSON，请检查 Cookie 或接口是否变更。")
        return data

    def _response_preview(self, text: str) -> str:
        cleaned = " ".join(text.strip().split())
        if not cleaned:
            return "空响应"
        return cleaned[:160]

    async def _safe_fetch_user_profile(self, sec_user_id: str) -> Dict[str, Any]:
        try:
            return await self._fetch_user_profile(sec_user_id)
        except Exception as exc:  # noqa: BLE001 - stats are best-effort and should not block work checks
            logger.error(f"fetch douyin profile stats failed for {sec_user_id}: {exc}")
            return {}

    async def _fetch_user_profile(self, sec_user_id: str) -> Dict[str, Any]:
        client = self._get_client()
        params = {
            "device_platform": "webapp",
            "aid": "6383",
            "channel": "channel_pc_web",
            "sec_user_id": sec_user_id,
            "publish_video_strategy_type": "2",
            "source": "channel_pc_web",
            "pc_client_type": "1",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "124.0.0.0",
            "os_name": "Windows",
            "os_version": "10",
            "platform": "PC",
        }
        response = await client.get(DOUYIN_PROFILE_API, params=params)
        response.raise_for_status()
        data = await self._read_json_response(response, "主页数据")
        if data.get("status_code") not in (0, None):
            raise RuntimeError(data.get("status_msg") or data.get("message") or "Douyin profile API returned non-zero status")
        return data.get("user") or data.get("user_info") or data.get("user_info_v2") or data

    async def _maybe_push_daily_summary(self, trigger: str) -> str:
        if not self._summary_enabled:
            return "每日总结未发送：配置已停用。" if trigger == "manual_check" else ""
        if not self._is_summary_time_reached():
            return f"每日总结未发送：尚未到达计划时间 {self._summary_time}({self._summary_utc_offset})。" if trigger == "manual_check" else ""
        today = self._summary_now().date().isoformat()
        if self._state.get("last_daily_summary_date") == today:
            return f"每日总结今日已发送：{today}。" if trigger == "manual_check" else ""

        targets = self._state.get("targets", [])
        if not targets:
            message = "每日总结未发送：没有绑定推送会话，请先在目标会话发送 /dy_bind。"
            logger.warning(message)
            return message if trigger == "manual_check" else ""

        report = self._build_daily_summary(force=True)
        if not report:
            message = "每日总结未发送：暂无可总结的主页数据。"
            logger.warning(message)
            return message if trigger == "manual_check" else ""

        sent_count = await self._push_text(report)
        if sent_count <= 0:
            message = "每日总结发送失败：所有绑定会话推送都失败，今天不会标记为已发送。"
            logger.error(message)
            self._save_state()
            return message if trigger == "manual_check" else ""

        self._state["last_daily_summary_date"] = today
        self._state["last_daily_summary_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state()
        message = f"每日总结已发送：成功推送到 {sent_count}/{len(targets)} 个会话。"
        logger.info(message)
        return message if trigger == "manual_check" else ""

    def _is_summary_time_reached(self) -> bool:
        try:
            hour, minute = [int(part) for part in self._summary_time.split(":", 1)]
            if not 0 <= hour <= 23 or not 0 <= minute <= 59:
                raise ValueError
        except ValueError:
            hour, minute = 23, 55
        now = self._summary_now()
        scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return now >= scheduled

    def _summary_now(self) -> datetime:
        return datetime.now(self._summary_timezone())

    def _summary_timezone(self) -> timezone:
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", self._summary_utc_offset.strip())
        if not match:
            return timezone(timedelta(hours=8))
        sign, hour_text, minute_text = match.groups()
        hours = int(hour_text)
        minutes = int(minute_text)
        if hours > 23 or minutes > 59:
            return timezone(timedelta(hours=8))
        delta = timedelta(hours=hours, minutes=minutes)
        if sign == "-":
            delta = -delta
        return timezone(delta)

    def _record_profile_stats(self, sec_user_id: str, info: Dict[str, Any], profile: Dict[str, Any]):
        stats = self._extract_profile_stats(profile)
        if not any(value is not None for key, value in stats.items() if key != "nickname"):
            return

        nickname = stats.pop("nickname", "")
        if nickname:
            info["nickname"] = nickname

        now = self._summary_now()
        entry = {
            "ts": int(now.timestamp()),
            "date": now.date().isoformat(),
            **stats,
        }
        info["latest_stats"] = entry
        history = info.setdefault("stat_history", [])
        history.append(entry)
        info["stat_history"] = self._trim_stat_history(history)
        logger.info(f"recorded douyin profile stats for {sec_user_id}: {stats}")

    def _extract_profile_stats(self, profile: Dict[str, Any]) -> Dict[str, Any]:
        stats = profile.get("stats") or profile.get("statistics") or {}
        merged = {**stats, **profile}
        return {
            "nickname": merged.get("nickname") or merged.get("unique_id") or "",
            "following_count": self._pick_int(merged, "following_count", "follow_count"),
            "follower_count": self._pick_int(merged, "follower_count", "fans_count"),
            "total_favorited": self._pick_int(merged, "total_favorited", "favoriting_count"),
            "aweme_count": self._pick_int(merged, "aweme_count", "video_count"),
        }

    def _trim_stat_history(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        retention_days = max(1, int(self.config.get("profile_stats_retention_days", 30)))
        cutoff = datetime.now() - timedelta(days=retention_days)
        cutoff_ts = int(cutoff.timestamp())
        return [entry for entry in history if int(entry.get("ts") or 0) >= cutoff_ts]

    def _build_daily_summary(self, force: bool) -> str:
        today = self._summary_now().date()
        yesterday = today - timedelta(days=1)
        lines = [f"抖音主页数据每日总结（{today.isoformat()}，对比昨日 {yesterday.isoformat()}）"]
        records: List[Dict[str, Any]] = []
        has_data = False
        for sec_user_id, info in self._state.get("users", {}).items():
            today_entry = self._latest_stat_for_date(info, today.isoformat()) or info.get("latest_stats") or {}
            yesterday_entry = self._latest_stat_for_date(info, yesterday.isoformat())
            if not today_entry:
                continue
            nickname = info.get("nickname", sec_user_id[-8:])
            lines.append(self._format_user_summary(nickname, yesterday_entry or {}, today_entry))
            records.append(
                {
                    "sec_user_id": sec_user_id,
                    "nickname": nickname,
                    "date": today.isoformat(),
                    "baseline_date": yesterday.isoformat(),
                    "current": self._compact_stats(today_entry),
                    "baseline": self._compact_stats(yesterday_entry or {}),
                    "delta": self._stats_delta(yesterday_entry or {}, today_entry),
                }
            )
            has_data = True

        if not has_data:
            return "" if not force else "暂无可用于总结的主页数据；请等待至少一次主页数据采样后再生成总结。"

        self._record_daily_summary(today.isoformat(), yesterday.isoformat(), "\n".join(lines), records)
        return "\n".join(lines)

    def _latest_stat_for_date(self, info: Dict[str, Any], day: str) -> Optional[Dict[str, Any]]:
        entries = [entry for entry in info.get("stat_history", []) if entry.get("date") == day]
        if not entries:
            return None
        return max(entries, key=lambda item: int(item.get("ts") or 0))

    def _record_daily_summary(self, day: str, baseline_day: str, text: str, users: List[Dict[str, Any]]):
        records: Dict[str, Any] = self._state.setdefault("daily_summary_records", {})
        records[day] = {
            "date": day,
            "baseline_date": baseline_day,
            "generated_at": self._summary_now().isoformat(timespec="seconds"),
            "text": text,
            "users": users,
        }
        self._trim_daily_summary_records()

    def _trim_daily_summary_records(self):
        records: Dict[str, Any] = self._state.setdefault("daily_summary_records", {})
        cutoff = self._summary_now().date() - timedelta(days=self._summary_records_retention_days)
        for day in list(records.keys()):
            try:
                day_date = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                records.pop(day, None)
                continue
            if day_date < cutoff:
                records.pop(day, None)

    def _build_period_summary(self, days: int, title: str) -> str:
        records: Dict[str, Any] = self._state.get("daily_summary_records", {})
        if not records:
            return "暂无每日总结记录；请先等待每日总结生成。"
        end_day = self._summary_now().date()
        start_day = end_day - timedelta(days=days - 1)
        selected = []
        for day, record in records.items():
            try:
                day_date = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start_day <= day_date <= end_day:
                selected.append(record)
        if not selected:
            return f"暂无近 {days} 天每日总结记录。"
        selected.sort(key=lambda record: record.get("date", ""))

        aggregate: Dict[str, Dict[str, Any]] = {}
        for record in selected:
            for user in record.get("users", []):
                key = user.get("sec_user_id") or user.get("nickname") or "unknown"
                item = aggregate.setdefault(
                    key,
                    {"nickname": user.get("nickname", key), "delta": {}, "latest": {}},
                )
                item["latest"] = user.get("current", {}) or item["latest"]
                for stat_key, value in (user.get("delta") or {}).items():
                    if value is not None:
                        item["delta"][stat_key] = item["delta"].get(stat_key, 0) + value

        lines = [f"{title}（{start_day.isoformat()} ~ {end_day.isoformat()}，基于 {len(selected)} 条每日记录）"]
        for item in aggregate.values():
            lines.append(self._format_period_user_summary(item["nickname"], item.get("latest", {}), item.get("delta", {})))
        return "\n".join(lines)

    def _format_period_user_summary(self, nickname: str, latest: Dict[str, Any], delta: Dict[str, Any]) -> str:
        parts = []
        for key, label in (
            ("following_count", "关注"),
            ("follower_count", "粉丝"),
            ("total_favorited", "获赞"),
            ("aweme_count", "作品"),
        ):
            current = latest.get(key)
            change = delta.get(key)
            if current is None and change is None:
                continue
            parts.append(f"{label} {self._format_number(current)}（累计 {self._format_delta(change)}）")
        return f"- {nickname}: " + "，".join(parts)

    def _compact_stats(self, stats: Dict[str, Any]) -> Dict[str, Optional[int]]:
        return {
            "following_count": self._to_int(stats.get("following_count")),
            "follower_count": self._to_int(stats.get("follower_count")),
            "total_favorited": self._to_int(stats.get("total_favorited")),
            "aweme_count": self._to_int(stats.get("aweme_count")),
        }

    def _stats_delta(self, baseline: Dict[str, Any], current: Dict[str, Any]) -> Dict[str, Optional[int]]:
        return {
            "following_count": self._delta(baseline.get("following_count"), current.get("following_count")),
            "follower_count": self._delta(baseline.get("follower_count"), current.get("follower_count")),
            "total_favorited": self._delta(baseline.get("total_favorited"), current.get("total_favorited")),
            "aweme_count": self._delta(baseline.get("aweme_count"), current.get("aweme_count")),
        }

    def _format_user_summary(self, nickname: str, first: Dict[str, Any], last: Dict[str, Any]) -> str:
        parts = []
        for key, label in (
            ("following_count", "关注"),
            ("follower_count", "粉丝"),
            ("total_favorited", "获赞"),
            ("aweme_count", "作品"),
        ):
            current = last.get(key)
            if current is None:
                continue
            parts.append(f"{label} {self._format_number(current)}（{self._format_delta(self._delta(first.get(key), current))}）")
        return f"- {nickname}: " + "，".join(parts)

    def _format_stats_inline(self, stats: Dict[str, Any]) -> str:
        parts = []
        for key, label in (
            ("following_count", "关注"),
            ("follower_count", "粉丝"),
            ("total_favorited", "获赞"),
            ("aweme_count", "作品"),
        ):
            value = stats.get(key)
            if value is not None:
                parts.append(f"{label} {self._format_number(value)}")
        return "，".join(parts) if parts else "暂无主页数据"

    def _delta(self, start: Any, end: Any) -> Optional[int]:
        start_value = self._to_int(start)
        end_value = self._to_int(end)
        if start_value is None or end_value is None:
            return None
        return end_value - start_value

    def _format_delta(self, value: Optional[int]) -> str:
        if value is None:
            return "无对比"
        if value > 0:
            return f"+{self._format_number(value)}"
        return self._format_number(value)

    def _format_number(self, value: Any) -> str:
        number = self._to_int(value)
        if number is None:
            return "未知"
        return f"{number:,}"

    def _pick_int(self, data: Dict[str, Any], *keys: str) -> Optional[int]:
        for key in keys:
            value = self._to_int(data.get(key))
            if value is not None:
                return value
        return None

    def _to_int(self, value: Any) -> Optional[int]:
        if value is None or value == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    async def _download_aweme(self, item: Dict[str, Any]) -> List[str]:
        aweme_id = str(item.get("aweme_id") or int(time.time()))
        urls = self._media_urls(item)
        saved: List[str] = []
        for index, url in enumerate(urls, start=1):
            suffix = ".mp4" if "video" in url or item.get("video") else ".jpg"
            path = self.download_dir / f"{aweme_id}_{index}{suffix}"
            if path.exists():
                saved.append(str(path))
                continue
            try:
                await self._download_url(url, path)
                saved.append(str(path))
            except Exception as exc:  # noqa: BLE001 - one media failure should not hide the update
                logger.error(f"download aweme {aweme_id} media failed: {exc}")
        return saved

    async def _download_url(self, url: str, path: Path):
        client = self._get_client()
        async with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            with path.open("wb") as fp:
                async for chunk in response.aiter_bytes():
                    fp.write(chunk)

    def _media_urls(self, item: Dict[str, Any]) -> List[str]:
        urls: List[str] = []
        video_urls = self._best_video_urls(item)
        if video_urls:
            urls.extend(video_urls[:1])
        else:
            for image in item.get("images") or []:
                urls.extend(image.get("url_list") or [])
        return list(dict.fromkeys(urls))[: int(self.config.get("max_download_files_per_aweme", 10))]

    def _best_video_urls(self, item: Dict[str, Any]) -> List[str]:
        video = item.get("video") or {}
        bit_rates = video.get("bit_rate") or []
        candidates = []
        for rate in bit_rates:
            play_addr = rate.get("play_addr") or {}
            url_list = play_addr.get("url_list") or []
            if not url_list:
                continue
            quality_score = self._to_int(rate.get("bit_rate")) or self._to_int(rate.get("quality_type")) or 0
            size_score = self._to_int(play_addr.get("data_size")) or self._to_int(rate.get("data_size")) or 0
            candidates.append((quality_score, size_score, url_list))
        if candidates:
            candidates.sort(key=lambda item_: (item_[0], item_[1]), reverse=True)
            return candidates[0][2]

        urls: List[str] = []
        for key in ("download_addr", "play_addr"):
            urls.extend((video.get(key) or {}).get("url_list") or [])
        return list(dict.fromkeys(urls))

    def _format_timestamp(self, value: Any) -> str:
        timestamp = self._to_int(value)
        if not timestamp:
            return "未初始化"
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))

    def _format_aweme_message(self, nickname: str, item: Dict[str, Any], downloaded: List[str]) -> str:
        aweme_id = str(item.get("aweme_id") or "")
        desc = item.get("desc") or "无标题"
        create_time = item.get("create_time")
        publish_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_time)) if create_time else "未知"
        lines = [
            f"抖音用户 {nickname} 发布了新作品",
            f"标题：{desc}",
            f"发布时间：{publish_time}",
            f"作品 ID：{aweme_id}",
        ]
        if downloaded:
            lines.append("已上传最高画质媒体。")
        return "\n".join(lines)

    async def _push_text(self, text: str, origins: Optional[List[str]] = None) -> int:
        return await self._push_components([Comp.Plain(text)], origins=origins)

    async def _push_aweme_message(self, text: str, media_paths: List[str]) -> int:
        components: List[Any] = [Comp.Plain(text)]
        for path in media_paths:
            if path.lower().endswith(".mp4"):
                components.append(Comp.Video.fromFileSystem(path=path))
            else:
                components.append(Comp.Image.fromFileSystem(path))
        return await self._push_components(components)

    async def _push_components(self, components: List[Any], origins: Optional[List[str]] = None) -> int:
        sent_count = 0
        push_errors: Dict[str, str] = self._state.setdefault("last_push_errors", {})
        for origin in list(origins if origins is not None else self._state.get("targets", [])):
            try:
                await self.context.send_message(origin, components)
                sent_count += 1
                push_errors.pop(origin, None)
            except Exception as exc:  # noqa: BLE001 - keep other targets available
                error_text = f"{type(exc).__name__}: {exc}"
                push_errors[origin] = error_text
                logger.error(f"push douyin update to {origin} failed: {error_text}")
        return sent_count

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "User-Agent": str(self.config.get("user_agent") or USER_AGENT),
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            }
            cookie = self._cookie_value()
            if cookie:
                headers["Cookie"] = cookie
            self._client = httpx.AsyncClient(headers=headers, timeout=float(self.config.get("request_timeout", 20)))
        return self._client

    def _cookie_value(self) -> str:
        configured_cookie = str(self.config.get("cookie") or "").strip()
        if configured_cookie:
            return configured_cookie
        try:
            return self.cookie_path.read_text("utf-8").strip()
        except OSError:
            return ""

    def _load_state(self):
        if not self.state_path.exists():
            return
        try:
            self._state = json.loads(self.state_path.read_text("utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error(f"load douyin state failed: {exc}")
            self._state = {"users": {}, "targets": []}

    def _save_state(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), "utf-8")

    def _merge_config_users(self):
        raw = str(self.config.get("monitored_users") or "")
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=1)
            sec_user_id = self._extract_sec_user_id(parts[0])
            if sec_user_id:
                self._ensure_user(sec_user_id, parts[1] if len(parts) > 1 else sec_user_id[-8:])
        self._save_state()

    def _ensure_user(self, sec_user_id: str, nickname: str):
        users: Dict[str, Any] = self._state.setdefault("users", {})
        info = users.setdefault(sec_user_id, {})
        info.setdefault("seen_aweme_ids", [])
        info["nickname"] = nickname

    def _extract_sec_user_id(self, value: str) -> Optional[str]:
        value = value.strip()
        match = SEC_UID_PATTERN.search(value)
        return match.group(0) if match else None

    def _split_command_args(self, message: str) -> List[str]:
        parts = message.strip().split(maxsplit=2)
        return parts[1:] if len(parts) > 1 else []

    def _author_name(self, item: Dict[str, Any]) -> str:
        author = item.get("author") or {}
        return author.get("nickname") or author.get("unique_id") or ""

    def _share_url(self, item: Dict[str, Any], aweme_id: str) -> str:
        share_info = item.get("share_info") or {}
        return share_info.get("share_url") or f"https://www.douyin.com/video/{aweme_id}"
