#!/usr/bin/env python3
"""Use a real Chromium browser to log in to douyin.com and export Cookie text.

Install optional dependencies first:
    python -m pip install playwright
    python -m playwright install chromium

Then run from the AstrBot root or this plugin directory:
    python scripts/douyin_cookie_login.py
"""

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright


DEFAULT_OUTPUT = Path("data/plugins/astrbot_plugin_douyin_push/douyin_cookie.txt")
COOKIE_DOMAINS = ("douyin.com", "amemv.com", "snssdk.com", "bytedance.com")
IMPORTANT_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "uid_tt", "uid_tt_ss"}


def cookie_applies_to_douyin(cookie: dict) -> bool:
    domain = str(cookie.get("domain") or "").lstrip(".")
    return any(domain == item or domain.endswith(f".{item}") for item in COOKIE_DOMAINS)


def format_cookie_header(cookies: list[dict]) -> str:
    selected = [cookie for cookie in cookies if cookie_applies_to_douyin(cookie)]
    return "; ".join(f"{cookie['name']}={cookie['value']}" for cookie in selected if cookie.get("name") and cookie.get("value"))


def has_login_cookie(cookies: list[dict]) -> bool:
    return any(cookie.get("name") in IMPORTANT_COOKIE_NAMES and cookie.get("value") for cookie in cookies)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open Douyin in Chromium, scan/login manually, and export cookies.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="Cookie text output path used by the plugin.")
    parser.add_argument("--user-data-dir", default=".douyin-browser", help="Chromium profile directory for keeping login state.")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless. Headed mode is recommended for QR login.")
    return parser.parse_args()


def main():
    args = parse_args()
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            args.user_data_dir,
            headless=args.headless,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        print("已打开抖音网页版。请在浏览器中点击登录并使用抖音 App 扫码/确认登录。")
        print("登录完成、页面显示你的账号后，回到此终端按 Enter 导出 Cookie。")
        input("按 Enter 继续导出 Cookie...")

        cookies = context.cookies()
        cookie_header = format_cookie_header(cookies)
        if not cookie_header:
            context.close()
            raise SystemExit("未读取到 douyin.com 相关 Cookie，请确认浏览器已完成登录。")

        output.write_text(cookie_header, "utf-8")
        context.storage_state(path=str(output.with_suffix(".storage_state.json")))
        context.close()

    login_hint = "检测到登录 Cookie" if has_login_cookie(cookies) else "未检测到常见登录 Cookie，可能仍需确认是否登录成功"
    print(f"{login_hint}。Cookie 已保存到：{output}")
    print("如果 AstrBot 插件正在运行，请在会话中发送 /dy_reload_cookie 后再 /dy_check。")


if __name__ == "__main__":
    main()
