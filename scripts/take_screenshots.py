#!/usr/bin/env python3
"""Capture README screenshots of a running demo (``make demo``) with Playwright.

Usage:
    python scripts/take_screenshots.py [--url http://127.0.0.1:8000] [--out docs/screenshots]

Needs the ``playwright`` package and a Chromium it can find (not a project dependency).
It logs in with the demo token shown on the login page, so it only ever sees fake data.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent


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
        page.wait_for_selector("#demo-token")
        page.screenshot(path=str(args.out / "login.png"))

        token = page.inner_text("#demo-token").strip()
        page.fill("#token", token)
        page.click("button[type=submit]")
        page.wait_for_url("**/dashboard")
        page.wait_for_function(
            "document.getElementById('updated').textContent.startsWith('Updated')"
        )
        page.wait_for_timeout(800)  # let the charts finish drawing
        page.screenshot(path=str(args.out / "dashboard.png"), full_page=True)

        # A lead with some history: qualified, already messaged, a day or more old.
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
        browser.close()
    print(f"saved login.png, dashboard.png, lead-detail.png to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
