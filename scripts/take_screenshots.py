#!/usr/bin/env python3
"""Capture README screenshots of a running demo (``make demo``) with Playwright.

Usage:
    python scripts/take_screenshots.py [--url http://127.0.0.1:8000] [--out docs/screenshots]

Needs the ``playwright`` package and a Chromium it can find (not a project dependency).
It logs in with the demo accounts and the password shown on the login page (the demo
admin's two-factor code is shown on the code step), so it only ever sees fake data. Run it
against a demo started with the default DEMO_ROLES (admin, rep and viewer).
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent


def log_in(page: Page, url: str, email: str) -> None:
    """Password step, then the two-factor step if the account has one (the demo admin)."""
    page.goto(f"{url}/login")
    page.wait_for_selector("#demo-password")
    page.fill("#email", email)
    page.fill("#password", page.inner_text("#demo-password").strip())
    page.click("button[type=submit]")
    page.wait_for_load_state()
    if "/login/2fa" in page.url:
        page.fill("#code", page.inner_text(".callout code").strip())
        page.click("button[type=submit]")
    page.wait_for_url("**/dashboard**")


def log_out(page: Page) -> None:
    page.click("form.logout button")
    page.wait_for_url("**/login**")


def wait_for_dashboard(page: Page) -> None:
    # A locator wait, not wait_for_function: the page's CSP forbids eval.
    page.locator("#updated", has_text="Updated").wait_for()
    page.wait_for_timeout(800)  # let the charts finish drawing


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "screenshots")
    parser.add_argument("--scheme", choices=("light", "dark"), default="light")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(
            viewport={"width": 1366, "height": 900},
            device_scale_factor=1,
            color_scheme=args.scheme,
        )
        page.goto(f"{args.url}/login")
        page.wait_for_selector("#demo-password")
        page.screenshot(path=str(args.out / "login.png"))

        log_in(page, args.url, "rep@example.com")
        wait_for_dashboard(page)
        page.screenshot(path=str(args.out / "dashboard.png"), full_page=True)

        # A lead with some history: qualified, already messaged. Contact details stay masked.
        page.goto(f"{args.url}/dashboard/leads?stage=qualified&page=1")
        links = page.eval_on_selector_all(
            "table.leads tbody a", "els => els.map(e => e.getAttribute('href'))"
        )
        for href in links[2:] + links[:2]:
            page.goto(args.url + href)
            if page.locator("text=Nothing sent yet").count() == 0:
                break
        page.wait_for_selector("text=AI assessment")
        page.screenshot(path=str(args.out / "lead-detail.png"), full_page=True)
        log_out(page)

        # The audit log, as the demo admin (password, then the two-factor code).
        log_in(page, args.url, "admin@example.com")
        page.goto(f"{args.url}/dashboard/admin/audit")
        page.wait_for_selector("table")
        page.screenshot(path=str(args.out / "security.png"))
        browser.close()
    print(f"saved login.png, dashboard.png, lead-detail.png, security.png to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
