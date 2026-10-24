#!/usr/bin/env python3
"""Sign and POST a fake Meta leadgen payload to a running speed-to-lead instance.

Usage:
    META_APP_SECRET=... python scripts/send_test_webhook.py [--url URL] [--payload FILE]

A fresh random leadgen_id is used unless --leadgen-id is given (repeat an id to see the
idempotent duplicate handling). All data in the default fixture is fictional.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.security.signatures import sign_body  # noqa: E402

DEFAULT_PAYLOAD = ROOT / "tests" / "fixtures" / "meta_leadgen.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://127.0.0.1:8000/webhooks/meta")
    parser.add_argument("--payload", type=Path, default=DEFAULT_PAYLOAD)
    parser.add_argument("--leadgen-id", default=None)
    parser.add_argument("--bad-signature", action="store_true", help="send an invalid signature")
    args = parser.parse_args()

    secret = os.environ.get("META_APP_SECRET")
    if not secret:
        print("META_APP_SECRET is not set", file=sys.stderr)
        return 2

    payload = json.loads(args.payload.read_text())
    leadgen_id = args.leadgen_id or str(secrets.randbelow(10**15) + 10**14)
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            change.setdefault("value", {})["leadgen_id"] = leadgen_id

    body = json.dumps(payload).encode()
    signature = sign_body(secret.encode(), body)
    if args.bad_signature:
        signature = "sha256=" + "0" * 64
    response = httpx.post(
        args.url,
        content=body,
        headers={"content-type": "application/json", "x-hub-signature-256": signature},
        timeout=15,
    )
    print(f"leadgen_id={leadgen_id} status={response.status_code} body={response.text}")
    return 0 if response.is_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
