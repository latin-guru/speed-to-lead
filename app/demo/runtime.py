"""Run the demo inside the web process: reseed, then a worker thread and a simulator thread.

Used when ``DEMO_MODE=1`` so the whole demo is one service (e.g. a free web service with an
ephemeral disk: every restart starts from a fresh, seeded database).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field

import httpx
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import Settings
from app.demo.seed import SeedReport, reset_database, seed_demo
from app.demo.simulator import Simulator
from app.followups.guards import SendWindow
from app.jobs.handlers import JobContext
from app.models import utcnow
from app.worker import Worker

log = logging.getLogger(__name__)


def demo_base_url(settings: Settings) -> str:
    return settings.demo_base_url or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"


def reseed(settings: Settings, engine: Engine, sf: sessionmaker[Session]) -> SeedReport:
    started = time.monotonic()
    reset_database(engine)
    report = seed_demo(
        sf,
        seed=settings.demo_seed,
        count=settings.demo_leads,
        now=utcnow(),
        business_timezone=settings.business_timezone,
        window=SendWindow.parse(settings.send_windows),
        company=settings.company_name,
        callback_minutes=settings.callback_minutes,
    )
    log.info(
        "demo_seeded",
        extra={
            "leads": report.leads,
            "stages": report.stages,
            "ms": int((time.monotonic() - started) * 1000),
        },
    )
    return report


@dataclass
class DemoRunner:
    settings: Settings
    session_factory: sessionmaker[Session]
    job_context: JobContext
    simulate: bool = True
    stop: threading.Event = field(default_factory=threading.Event)
    threads: list[threading.Thread] = field(default_factory=list)

    def start(self) -> None:
        worker = Worker.from_settings(self.settings, self.session_factory, self.job_context)
        self._spawn("demo-worker", worker.run_forever, self.stop, self.settings.worker_poll_seconds)
        if self.simulate:
            self._spawn("demo-simulator", self._simulate)

    def _spawn(self, name: str, target: object, *args: object) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)  # type: ignore[arg-type]
        thread.start()
        self.threads.append(thread)

    def _simulate(self) -> None:
        s = self.settings
        # Demo mode fills these in (app.config._check_demo).
        assert s.meta_app_secret is not None
        assert s.inbound_webhook_secret is not None
        assert s.admin_token is not None
        with httpx.Client(base_url=demo_base_url(s), timeout=10.0) as client:
            # Wait until the server is accepting requests.
            while not self.stop.is_set():
                try:
                    if client.get("/healthz").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                self.stop.wait(0.5)
            Simulator(
                client,
                meta_secret=s.meta_app_secret.get_secret_value(),
                inbound_secret=s.inbound_webhook_secret.get_secret_value(),
                admin_token=s.admin_token.get_secret_value(),
                seed=s.demo_seed + 1,
            ).run(
                self.stop,
                min_seconds=s.demo_interval_min_seconds,
                max_seconds=s.demo_interval_max_seconds,
            )

    def shutdown(self, timeout: float = 5.0) -> None:
        self.stop.set()
        for thread in self.threads:
            thread.join(timeout)
