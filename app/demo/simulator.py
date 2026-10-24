"""Demo traffic: a new fake lead every few seconds, through the app's REAL endpoints.

Nothing is inserted behind the app's back. The simulator is an HTTP client:

- ``meta_ads`` leads go to ``POST /webhooks/meta`` as a signed Meta leadgen payload;
- ``web_form``, ``google_ads`` and ``referral`` leads go to ``POST /api/leads``;
- now and then a lead "replies" (or texts STOP) through the signed inbound SMS webhook;
- now and then an operator moves a lead along the pipeline with the admin API.

The worker then scores, acknowledges and syncs those leads exactly as it would real ones.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import httpx
from faker import Faker

from app.demo.seed import AREA_CODES, FIRST_NAMES, MESSAGES, REPLIES, email_for
from app.security.signatures import sign_body

log = logging.getLogger(__name__)

LEAD_SOURCES = (("meta_ads", 40), ("web_form", 35), ("google_ads", 15), ("referral", 10))
NEXT_STAGE = {"new": "qualified", "qualified": "contacted", "contacted": "won"}


@dataclass
class Simulator:
    client: httpx.Client  # base_url = the app itself
    meta_secret: str
    inbound_secret: str
    admin_token: str
    seed: int = 7
    rng: random.Random = field(init=False)
    fake: Faker = field(init=False)
    counter: int = 0
    recent: deque[tuple[str, str | None]] = field(default_factory=lambda: deque(maxlen=20))

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.fake = Faker("en_US")
        self.fake.seed_instance(self.seed)

    # ------------------------------------------------------------------ fake data

    def _person(self) -> dict[str, Any]:
        first, last = str(self.fake.random_element(FIRST_NAMES)), self.fake.last_name()
        self.counter += 1
        return {
            "name": f"{first} {last}",
            "email": email_for(first, last, f"live{self.counter}"),
            "phone": f"({self.rng.choice(AREA_CODES)}) 555-01{self.rng.randint(0, 99):02d}",
            "message": self.rng.choice(MESSAGES).format(
                n=self.rng.randint(12, 30), sqft=self.rng.choice((1400, 1800, 2200))
            ),
        }

    # ------------------------------------------------------------------ actions

    def post_meta_lead(self, person: dict[str, Any]) -> httpx.Response:
        leadgen_id = str(self.rng.randrange(10**14, 10**15))
        payload = {
            "object": "page",
            "entry": [
                {
                    "id": "100000000000001",
                    "changes": [
                        {
                            "field": "leadgen",
                            "value": {
                                "leadgen_id": leadgen_id,
                                "form_id": "700000000000001",
                                "page_id": "100000000000001",
                                "field_data": [
                                    {"name": "full_name", "values": [person["name"]]},
                                    {"name": "email", "values": [person["email"]]},
                                    {"name": "phone_number", "values": [person["phone"]]},
                                    {
                                        "name": "what_do_you_need_help_with?",
                                        "values": [person["message"]],
                                    },
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        body = json.dumps(payload).encode()
        return self.client.post(
            "/webhooks/meta",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": sign_body(self.meta_secret.encode(), body),
            },
        )

    def post_api_lead(self, person: dict[str, Any], source: str) -> httpx.Response:
        return self.client.post("/api/leads", json={**person, "source": source})

    def new_lead(self) -> str:
        person = self._person()
        source = self.rng.choices(
            [s for s, _ in LEAD_SOURCES], weights=[w for _, w in LEAD_SOURCES]
        )[0]
        if source == "meta_ads":
            response = self.post_meta_lead(person)
            lead_id = None
        else:
            response = self.post_api_lead(person, source)
            lead_id = response.json().get("id") if response.status_code == 201 else None
        self.recent.append((person["phone"], lead_id))
        return f"lead:{source}:{response.status_code}"

    def reply(self) -> str:
        if not self.recent:
            return "reply:none"
        phone, _ = self.rng.choice(list(self.recent))
        body = "STOP" if self.rng.random() < 0.25 else self.rng.choice(REPLIES)
        raw = json.dumps(
            {"message_id": f"demo-{self.rng.getrandbits(48):x}", "from": phone, "body": body}
        ).encode()
        response = self.client.post(
            "/webhooks/inbound/sms",
            content=raw,
            headers={
                "content-type": "application/json",
                "x-signature-256": sign_body(self.inbound_secret.encode(), raw),
            },
        )
        return f"reply:{response.status_code}"

    def advance_stage(self) -> str:
        auth = {"Authorization": f"Bearer {self.admin_token}"}
        activity = self.client.get("/api/metrics/activity", params={"limit": 40}, headers=auth)
        if activity.status_code != 200 or not activity.json():
            return "stage:none"
        lead_id = self.rng.choice(activity.json())["lead_id"]
        lead = self.client.get(f"/api/leads/{lead_id}", headers=auth)
        target = NEXT_STAGE.get(lead.json().get("stage", "")) if lead.status_code == 200 else None
        if target is None:
            return "stage:final"
        if target == "won" and self.rng.random() < 0.4:
            body = {"stage": "lost", "actor": "dana", "reason": "chose another contractor"}
        else:
            body = {"stage": target, "actor": self.rng.choice(("dana", "lee", "sam"))}
        response = self.client.patch(f"/api/leads/{lead_id}/stage", json=body, headers=auth)
        return f"stage:{body['stage']}:{response.status_code}"

    def tick(self) -> str:
        roll = self.rng.random()
        try:
            if roll < 0.65:
                return self.new_lead()
            if roll < 0.85:
                return self.advance_stage()
            return self.reply()
        except httpx.HTTPError as exc:
            log.warning("demo_simulator_http_error", extra={"error": type(exc).__name__})
            return "error"

    def run(self, stop: threading.Event, *, min_seconds: float, max_seconds: float) -> None:
        log.info("demo_simulator_started")
        while not stop.wait(self.rng.uniform(min_seconds, max_seconds)):
            outcome = self.tick()
            log.info("demo_simulator_tick", extra={"outcome": outcome})
        log.info("demo_simulator_stopped")
