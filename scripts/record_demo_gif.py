#!/usr/bin/env python3
"""Record docs/demo.gif from a running demo: the dashboard updating as simulated leads
arrive, then opening the newest lead.

Usage:
    python scripts/record_demo_gif.py [--url http://127.0.0.1:8000] [--out docs/demo.gif]

Needs the ``playwright`` package with a Chromium, and ``ffmpeg`` on PATH. Start the demo with
a short simulator interval so a few leads arrive while recording, e.g.
``DEMO_INTERVAL_MIN_SECONDS=1.5 DEMO_INTERVAL_MAX_SECONDS=3 make demo``. Logs in as the demo
viewer, so contact details stay masked.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
SIZE = {"width": 1280, "height": 800}


def record(url: str, video_dir: Path, seconds: float) -> Path:
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Log in outside the recording, then hand the session to a recorded context.
        login_ctx = browser.new_context(viewport=SIZE)
        page = login_ctx.new_page()
        page.goto(f"{url}/login")
        page.fill("#email", "viewer@example.com")
        page.fill("#password", page.inner_text("#demo-password").strip())
        page.click("button[type=submit]")
        page.wait_for_url("**/dashboard**")
        state = login_ctx.storage_state()
        login_ctx.close()

        ctx = browser.new_context(
            viewport=SIZE,
            storage_state=state,
            record_video_dir=str(video_dir),
            record_video_size=SIZE,
            color_scheme="light",
        )
        page = ctx.new_page()
        page.goto(f"{url}/dashboard")
        page.locator("#updated", has_text="Updated").wait_for()  # the CSP forbids eval
        # Scroll so the KPI tiles and the live activity feed are both in view while it updates.
        page.wait_for_timeout(int(seconds * 1000 * 0.35))
        page.mouse.wheel(0, 700)
        page.wait_for_timeout(int(seconds * 1000 * 0.35))
        page.mouse.wheel(0, -700)
        page.wait_for_timeout(1000)
        page.goto(f"{url}/dashboard/leads")
        page.wait_for_timeout(1200)
        page.click("table.leads tbody a >> nth=0")
        page.wait_for_selector("text=AI assessment")
        page.wait_for_timeout(int(seconds * 1000 * 0.2))
        video = page.video
        ctx.close()
        browser.close()
        assert video is not None
        return Path(video.path())


def to_gif(video: Path, out: Path, width: int, fps: int) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise SystemExit("ffmpeg not found on PATH")
    filters = f"fps={fps},scale={width}:-1:flags=lanczos"
    use_palette = "paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    with tempfile.TemporaryDirectory() as tmp:
        palette = Path(tmp) / "palette.png"
        subprocess.run(  # noqa: S603  (fixed arguments, local files)
            [ffmpeg, "-y", "-v", "error", "-i", str(video),
             "-vf", f"{filters},palettegen=stats_mode=diff", str(palette)],
            check=True,
        )  # fmt: skip
        subprocess.run(  # noqa: S603
            [ffmpeg, "-y", "-v", "error", "-i", str(video), "-i", str(palette),
             "-lavfi", f"{filters}[x];[x][1:v]{use_palette}", str(out)],
            check=True,
        )  # fmt: skip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--out", type=Path, default=ROOT / "docs" / "demo.gif")
    parser.add_argument("--seconds", type=float, default=14.0)
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--fps", type=int, default=6)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        video = record(args.url, Path(tmp), args.seconds)
        to_gif(video, args.out, args.width, args.fps)
    print(f"saved {args.out} ({args.out.stat().st_size / 1_000_000:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
