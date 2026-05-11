import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import httpx
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp
from astrbot.api.star import Context, Star, register


PLUGIN_NAME = "astrbot_plugin_douyin_push"
DOUYIN_POST_API = "https://www.douyin.com/aweme/v1/web/aweme/post/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
SEC_UID_PATTERN = re.compile(r"MS4wLj[\w\-.~%]+")


@register(PLUGIN_NAME, "douyin-push", "监控抖音用户作品更新并主动推送/下载", "1.0.0")
class DouyinPushPlugin(Star):
    def __init__(self, context: Context, config: Optional[AstrBotConfig] = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path("data") / "plugins" / PLUGIN_NAME
        self.download_dir = Path(str(self.config.get("download_dir") or self.data_dir / "downloads"))
        self.state_path = self.data_dir / "state.json"
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
            self._running = True
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info("DouyinPushPlugin monitor task started")

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

    @filter.command("dy_status", alias={"抖音状态"})
    async def status(self, event: AstrMessageEvent):
        """查看抖音监控状态。"""
        users = self._state.get("users", {})
        targets = self._state.get("targets", [])
        lines = [
            f"抖音监控：{'启用' if self._enabled else '停用'}",
            f"检查间隔：{self._interval} 秒",
            f"推送会话数：{len(targets)}",
            f"监控用户数：{len(users)}",
        ]
        for sec_user_id, info in users.items():
            lines.append(f"- {info.get('nickname', sec_user_id[-8:])}: 最新 {info.get('latest_aweme_id') or '未初始化'}")
        yield event.plain_result("\n".join(lines))

    @filter.command("dy_check", alias={"抖音检查"})
    async def check_now(self, event: AstrMessageEvent):
        """立即检查一次抖音更新。"""
        yield event.plain_result("开始检查抖音更新，请稍候……")
        reports = await self._check_all_users(push=False)
        if reports:
            yield event.plain_result("\n\n".join(reports))
        else:
            yield event.plain_result("检查完成，暂无新作品。")

    async def _monitor_loop(self):
        await asyncio.sleep(5)
        while self._running:
            try:
                await self._check_all_users(push=True)
            except Exception as exc:  # noqa: BLE001 - background task must not crash the plugin
                logger.error(f"Douyin monitor loop failed: {exc}")
            await asyncio.sleep(self._interval)

    async def _check_all_users(self, push: bool) -> List[str]:
        users: Dict[str, Any] = self._state.setdefault("users", {})
        reports: List[str] = []
        for sec_user_id, info in list(users.items()):
            try:
                report = await self._check_user(sec_user_id, info, push=push)
                if report:
                    reports.append(report)
            except Exception as exc:  # noqa: BLE001 - keep checking other users
                nickname = info.get("nickname", sec_user_id[-8:])
                message = f"检查 {nickname} 失败：{exc}"
                logger.error(message)
                reports.append(message)
        self._save_state()
        return reports

    async def _check_user(self, sec_user_id: str, info: Dict[str, Any], push: bool) -> Optional[str]:
        aweme_list = await self._fetch_latest_awemes(sec_user_id)
        if not aweme_list:
            return None

        nickname = info.get("nickname") or self._author_name(aweme_list[0]) or sec_user_id[-8:]
        info["nickname"] = nickname
        latest_known = str(info.get("latest_aweme_id") or "")
        known_ids: Set[str] = set(str(i) for i in info.get("seen_aweme_ids", []))
        current_ids = [str(item.get("aweme_id")) for item in aweme_list if item.get("aweme_id")]
        if not current_ids:
            return None

        if not latest_known and not known_ids:
            info["latest_aweme_id"] = current_ids[0]
            info["seen_aweme_ids"] = current_ids[:50]
            if not bool(self.config.get("notify_existing_on_first_run", False)):
                return None

        new_items = [item for item in aweme_list if str(item.get("aweme_id")) not in known_ids]
        if latest_known:
            trimmed: List[Dict[str, Any]] = []
            for item in new_items:
                if str(item.get("aweme_id")) == latest_known:
                    break
                trimmed.append(item)
            new_items = trimmed

        if not new_items:
            info["latest_aweme_id"] = current_ids[0]
            info["seen_aweme_ids"] = current_ids[:50]
            return None

        new_items.reverse()
        messages = []
        for item in new_items:
            downloaded = await self._download_aweme(item) if bool(self.config.get("download_enabled", True)) else []
            text = self._format_aweme_message(nickname, item, downloaded)
            messages.append(text)
            if push:
                await self._push_text(text)

        info["latest_aweme_id"] = current_ids[0]
        info["seen_aweme_ids"] = list(dict.fromkeys(current_ids + list(known_ids)))[:50]
        return "\n\n".join(messages)

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
        data = response.json()
        if data.get("status_code") not in (0, None):
            raise RuntimeError(data.get("status_msg") or data.get("message") or "Douyin API returned non-zero status")
        return data.get("aweme_list") or []

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
        video = item.get("video") or {}
        for key in ("play_addr", "download_addr"):
            urls.extend((video.get(key) or {}).get("url_list") or [])
        for image in item.get("images") or []:
            urls.extend(image.get("url_list") or [])
        return list(dict.fromkeys(urls))[: int(self.config.get("max_download_files_per_aweme", 10))]

    def _format_aweme_message(self, nickname: str, item: Dict[str, Any], downloaded: List[str]) -> str:
        aweme_id = str(item.get("aweme_id") or "")
        desc = item.get("desc") or "无标题"
        create_time = item.get("create_time")
        publish_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(create_time)) if create_time else "未知"
        share_url = self._share_url(item, aweme_id)
        lines = [
            f"抖音用户 {nickname} 发布了新作品",
            f"标题：{desc}",
            f"发布时间：{publish_time}",
            f"作品 ID：{aweme_id}",
            f"链接：{share_url}",
        ]
        if downloaded:
            lines.append("已下载：")
            lines.extend(f"- {path}" for path in downloaded)
        return "\n".join(lines)

    async def _push_text(self, text: str):
        for origin in list(self._state.get("targets", [])):
            try:
                await self.context.send_message(origin, [Comp.Plain(text=text)])
            except Exception as exc:  # noqa: BLE001 - keep other targets available
                logger.error(f"push douyin update to {origin} failed: {exc}")

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            headers = {
                "User-Agent": str(self.config.get("user_agent") or USER_AGENT),
                "Referer": "https://www.douyin.com/",
                "Accept": "application/json, text/plain, */*",
            }
            cookie = str(self.config.get("cookie") or "").strip()
            if cookie:
                headers["Cookie"] = cookie
            self._client = httpx.AsyncClient(headers=headers, timeout=float(self.config.get("request_timeout", 20)))
        return self._client

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
