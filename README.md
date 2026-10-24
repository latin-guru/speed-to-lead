# speed-to-lead

Capture a lead, verify where it came from, and get an AI-scored next step in well under a second, with strict guardrails so untrusted lead text can never steer the system.

The demo business is **Copperline Roofing & Solar (fictional)**. All sample data uses `555-01xx` phone numbers and `example.com` emails.

> **Status: sessions 1-2 of 6.** Capture, webhook intake and AI scoring are done. Queueing, outreach and dashboards are next.

## Quickstart

Requires Python 3.11+.

```bash
make install                 # venv + pinned dev dependencies
cp .env.example .env         # then edit the placeholders
make run                     # http://127.0.0.1:8000  (form at /, API docs at /docs)
```

`make test`, `make lint` and `make typecheck` run pytest with coverage, ruff, and mypy (strict on `app/`).

To try the webhook against a running server:

```bash
META_APP_SECRET=<same value as .env> .venv/bin/python scripts/send_test_webhook.py
```

Run it again with `--leadgen-id <id from the first run>` to see a duplicate delivery handled as a no-op. Pass `--bad-signature` to see the 401.

Docker: `docker compose up --build` starts the app with Postgres. Set `POSTGRES_PASSWORD` in `.env` first.

## What's built

### Session 1: skeleton, capture, webhook

- **App factory** (`app/main.py`) and settings from env vars (`app/config.py`, pydantic-settings). `/healthz` checks the database. `/readyz` checks startup and the database, and reports config warnings.
- **Structured JSON logs** with a PII-masking filter on the handler, so records from every logger get it. Phones become `***-***-1234` and emails become `j***@example.com`. This covers messages, `extra=` fields and tracebacks.
- **Models**: `Lead` (UUID, contact fields, stage, scoring fields, `first_response_at`), `Activity` (the timeline) and `WebhookEvent` (an idempotency ledger with a unique `(provider, event_id)`).
- **`POST /api/leads`** validates every field:
  - Names are cleaned: Unicode spaces are normalized, invisible characters dropped, and URLs, emails, digits, emoji, keyboard mash and overlong input are rejected.
  - Phones are normalized to E.164 under NANP rules.
  - Emails are validated.
  - Messages are length-capped.
  - The response never echoes the lead's phone or email.
- **HTML form at `/`** (Jinja, server-rendered). It uses a signed, expiring double-submit CSRF token and a hidden honeypot field. A bot that fills the honeypot is shown "success" and nothing is saved.
- **Rate limits**:
  - A token bucket keyed by client IP, with a pluggable store (in-memory by default).
  - A per-phone daily cap, checked against the database.
  - Both return `429` with `Retry-After`.
  - `X-Forwarded-For` is only honored when `TRUST_FORWARDED_FOR=true`, and then only the right-most hop.
- **Meta Lead Ads-style webhook**:
  - `GET /webhooks/meta` handles the `hub.*` verification handshake.
  - `POST /webhooks/meta` verifies `X-Hub-Signature-256` (HMAC-SHA256 over the raw body, constant-time compare). A missing or bad signature gets `401`. If no secret is configured, the endpoint fails closed with `503`.
  - It parses `entry[].changes[].value.field_data` into a lead.
  - It is idempotent on `leadgen_id`.

### Session 2: AI scoring

- **`AIScorer` protocol** with three implementations, chosen by `AI_PROVIDER`:
  - `MockScorer`: deterministic rules, no network. This is the default.
  - `ClaudeScorer`: Anthropic Messages API over httpx. The model is set by `ANTHROPIC_MODEL`.
  - `GrokScorer`: xAI chat completions over httpx.
  - If a real provider is selected without its API key, the app refuses to start.
- **Strict output schema** (`ScoreResult`). Fields: `score` (int 0-100), `summary` (at most 2 sentences and 300 characters), `next_step` (`call_now | send_info | nurture | disqualify | human_review`), `confidence` (0-1) and `flags`. Extra keys and type coercion are rejected. Malformed output gets exactly one repair pass (strip code fences, extract the `{...}`, drop trailing commas). If that still fails, the lead goes to the fallback scorer.
- **Resilience**:
  - Every call has a timeout.
  - Timeouts, transport errors, 5xx and 429 are retried up to 3 attempts in total, with exponential backoff and full jitter. A `Retry-After` header is honored (capped at 5s).
  - A total time budget stops retries early.
  - A small circuit breaker stops calling a provider that keeps failing.
  - Any failure falls back to the rule-based scorer, recorded as `scored_by="fallback"`.
  - Latency and the provider used are stored on the lead.
- **Prompt-injection defenses**:
  - Lead fields are JSON-encoded inside `<lead_data id="<random nonce>">` delimiters, and angle brackets are escaped, so the data cannot forge the closing tag.
  - The system prompt says the content is untrusted data.
  - No tools are offered and tool-call responses are rejected, so the model can only return the schema.
  - Raw phone numbers and emails are never sent to the model. It sees `has_phone`, `has_email` and the email domain.
  - A pre-check flags suspicious input: "ignore previous instructions", `system:` prefixes, role tags, persona switches, attempts to set the score, and URLs. It logs a masked warning.
  - Suspicious input, a model-reported `injection_attempt`, or low confidence forces `next_step=human_review`.
  - The scorer only writes scoring fields. **It never changes `stage`.**
- **Scoring runs inline** right after capture, behind a `ScoringDispatcher` seam (`app/services/leads.py`), and writes an `ai_scored` activity. Session 3 swaps in a queue-backed dispatcher. A scorer crash never loses a captured lead: the lead gets an `ai_scoring_failed` activity instead.

## Layout

```
app/
  main.py config.py db.py models.py schemas.py logging.py
  api/        health, leads, webhooks, shared deps (rate limits, client IP)
  services/   lead creation + timeline, input normalization, Meta payload parsing
  ai/         scorer protocol, providers, rules, guard (injection), parsing, resilience, service
  security/   webhook signatures, CSRF, token bucket
  web/        form routes + Jinja templates
scripts/send_test_webhook.py
tests/unit  tests/integration  tests/fixtures
```

## Open decisions

- **Migrations**: tables are created with `create_all` at startup. Alembic will be added once the schema settles (session 3 or 4).
- **Real Meta payloads**: production leadgen webhooks carry only IDs, and the answers have to be fetched from the Graph API. Inline `field_data` is supported now. Changes without it are recorded as `needs_fetch` for a future fetcher.
- **Rate-limit store**: the in-memory buckets are per process. Running several workers needs a shared store (Redis or the database) behind the same `BucketStore` protocol.
- **Inline scoring latency**: scoring is bounded by `AI_TOTAL_BUDGET_SECONDS` (default 8s), but it still delays the webhook response when a real provider is slow. Session 3 moves it to a queue.
- **Defaults to confirm**: phone cap per UTC day (3) and the confidence threshold for human review (0.5).

## License

MIT. See [LICENSE](LICENSE).
