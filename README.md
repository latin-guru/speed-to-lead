# speed-to-lead

Capture a lead, verify where it came from, and reply in seconds: the lead is AI-scored, gets an instant acknowledgment, follows a guarded follow-up sequence, and is handed to a human the moment they reply. Strict guardrails mean untrusted lead text can never steer the system.

The demo business is **Copperline Roofing & Solar (fictional)**. All sample data uses `555-01xx` phone numbers and `example.com` emails.

> **Status: sessions 1-3 of 6.** Capture, webhook intake, AI scoring, the job queue, the follow-up engine, human handoff and CRM sync (mock) are done. Dashboards and reporting are next.
>
> **Nothing is ever really sent.** The SMS and email senders are MOCKS that write to an `outbox_messages` table, and the HubSpot adapter used by default is an in-memory MOCK. There is no code path that delivers a message.

## Quickstart

Requires Python 3.11+.

```bash
make install                 # venv + pinned dev dependencies
cp .env.example .env         # then edit the placeholders
make run                     # migrates, then serves http://127.0.0.1:8000 (form at /, docs at /docs)
make worker                  # in a second terminal: runs queued jobs (scoring, follow-ups, CRM sync)
```

`make migrate` applies Alembic migrations on its own. `.venv/bin/python -m app.worker --drain` runs everything that is due and exits.

`make test`, `make lint` and `make typecheck` run pytest with coverage, ruff, and mypy (strict on `app/`). Set `TEST_POSTGRES_URL=postgresql+psycopg://...` to also run the Postgres queue tests (`SKIP LOCKED`); they are skipped otherwise.

To try the webhook against a running server:

```bash
META_APP_SECRET=<same value as .env> .venv/bin/python scripts/send_test_webhook.py
```

Run it again with `--leadgen-id <id from the first run>` to see a duplicate delivery handled as a no-op. Pass `--bad-signature` to see the 401.

Docker: `docker compose up --build` starts Postgres, the app (which runs `alembic upgrade head` first) and a worker. Set `POSTGRES_PASSWORD` in `.env` first.

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
- Scoring writes an `ai_scored` activity. Since session 3 it runs as a queued job (see below), so a slow provider never delays capture.

### Session 3: pipeline, queue, follow-ups

- **Alembic migrations** (`migrations/`). `0001` is the session 1-2 schema and `0002` adds session 3. A test applies them to a fresh database and diffs the result against the models, so they cannot drift. Outside the test suite, the app no longer calls `create_all`: `/readyz` reports `schema: false` (503) and the worker refuses to start until `alembic upgrade head` has run.
- **Pipeline**: stages `new -> qualified -> contacted -> won | lost`, with an explicit allowed-transition map (`app/services/pipeline.py`). `won` and `lost` are final, and `lost` needs a reason.
  - `PATCH /api/leads/{id}/stage` with `{"stage", "actor", "reason"}`. Bad moves get `409`, a missing reason gets `422`.
  - Every move writes a `stage_changed` activity with the actor (`human:<name>`) and `from`/`to`.
  - Only a `human:` actor can move a stage. The scorer's `next_step` is a suggestion. There is no code path from a score to a stage change.
- **Job queue**: a `jobs` table and a worker (`python -m app.worker`). Details in [How the queue works](#how-the-queue-works).
  - Capture now returns immediately. The `score_lead` job, the follow-up sends and a `crm_sync` job are inserted **in the same transaction as the lead** (a transactional outbox), so a lead can never exist without its work, or the other way round.
- **Follow-up sequence** (`app/followups/default_sequence.yaml`, override with `FOLLOWUP_SEQUENCE_PATH`): instant SMS + email ("a specialist will call you within N minutes"), an SMS reminder at +1 day and an email nudge at +3 days. Templates are validated at load: only known placeholders, and email steps need a subject.
- **Senders are MOCKS** (`MockSmsSender`, `MockEmailSender`): they record the message in `outbox_messages`. Each send has the key `<lead_id>:<step>`, which is UNIQUE in that table, so a step reaches a lead at most once even if its job runs twice.
- **Guards before every send** (`app/followups/guards.py`, `engine.py`). A "not now" defers the job without using up an attempt:
  - **Stop checks**: opted out, stage `contacted`/`won`/`lost`, sequence paused or stopped, or no phone/email for that channel. The step is skipped and logged.
  - **Kill switch**: `SENDS_PAUSED=true` or `POST /admin/kill-switch`. Sends wait and nothing is dropped. Resuming pulls the held jobs forward to now.
  - **Quiet hours** in the lead's timezone (optional `timezone` on capture, else `BUSINESS_TIMEZONE`). The default is no sends 21:00-08:00, and the job is deferred to the next opening. DST-safe.
  - **One voice**: at most `MAX_TOUCHES_PER_WEEK` per *person* in a rolling 7 days (counted across every lead with the same phone or email) and `MIN_TOUCH_GAP_HOURS` between touches. The instant acknowledgments are marked transactional and skip the gap.
- **Opt-out**: `POST /webhooks/inbound/sms` is a mock SMS provider webhook, HMAC-signed with `INBOUND_WEBHOOK_SECRET` and idempotent on `message_id`. `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END` or `QUIT` opts the number out and cancels every pending send for it. A send that was already running re-checks and skips.
- **Human handoff** (`app/services/handoff.py`): pause the sequence (cancelling pending sends), open a task and log an activity. Triggers:
  - any other inbound reply
  - a `human_review` suggestion from the scorer
  - low confidence
  
  There is one open task per lead and reason. `GET /api/tasks` lists them and `PATCH /api/tasks/{id}` assigns or closes one.
- **First-response time**: `first_response_at` is set by the first follow-up that actually sends, or when a human logs contact (`POST /api/leads/{id}/contacts`, which also stops automation) or moves the lead to `contacted`. `GET /api/leads/{id}` shows it with `first_response_seconds` and the full timeline.
- **CRM sync** (`app/crm/`):
  - A `CRMAdapter` protocol.
  - `MockHubSpotAdapter`: in-memory, clearly labeled MOCK, unique by email like HubSpot, and it records the API calls it would have made.
  - A `HubSpotAdapter` skeleton with the real v3/v4 request shapes. It raises `NotConfigured` without a token and is only built when `CRM_PROVIDER=hubspot`.
  
  Lead and stage map to contact `lifecyclestage`/`hs_lead_status` and the deal's `dealstage`. Sync is a queued job on create and on every stage change. It is keyed per stage and upserts by stored id, so it is idempotent.
- **Operator API** behind `Authorization: Bearer $ADMIN_TOKEN`. It fails closed with `503` if the token is unset.
  - `GET /admin/jobs?status=dead` is the dead-letter queue, and `POST /admin/jobs/{id}/retry` gives a dead job a fresh set of attempts.
  - The kill switch, stage moves, contact logging, tasks and lead detail are behind the same token.

## How the queue works

```
API request ──► INSERT lead + jobs (one transaction) ──► 201 in milliseconds
                                     │
worker ── claim ──► running (lease) ──► handler + mark done (one transaction)
            ▲               │
            │               ├─ "not now" (quiet hours, cap, kill switch) ─► queued at the new time, attempt not counted
            │               ├─ error ─► queued at now + backoff ─► ... ─► dead (after max_attempts)
            │               └─ worker dies ─► lease expires ─► recovered to queued (or dead if out of attempts)
            └──────────────── admin "retry" on a dead job
```

- **Claiming.** On Postgres a worker runs `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1`, so concurrent workers never wait on or double-claim a row. SQLite has no row locks, so the claim is an atomic `UPDATE jobs SET status='running' ... WHERE id = :id AND status = 'queued'`, and only the worker that sees `rowcount = 1` owns the job. Both paths are tested with several workers running at once.
- **Leases and fencing.** A claim sets `locked_by` and `locked_until`. Expired leases are put back in the queue. Every later state change (done, retry, defer) only applies `WHERE locked_by = <me> AND status = 'running'`. A worker that stalled past its lease therefore cannot overwrite the result of the worker that took over: its whole transaction is rolled back.
- **Retries.** Exponential backoff with equal jitter: half fixed, half random, `JOB_BACKOFF_BASE_SECONDS * 2^(attempt-1)`, capped at `JOB_BACKOFF_CAP_SECONDS`. After `JOB_MAX_ATTEMPTS` the job is `dead` (dead-letter). A bad payload or unknown job kind goes straight to `dead`.
- **Idempotency.** Every job has a UNIQUE `idempotency_key` (`score_lead:<lead>`, `followup_send:<lead>:<step>`, `crm_sync:<lead>:<stage>`), so enqueueing twice is a no-op. Side effects carry their own keys too (the outbox key, CRM ids saved on the lead), so even a job that runs twice has its effect once.
- **At-most-once sends.** The mock send is a row in the same transaction as "job done", which makes it exactly once. A real sender would also pass the outbox key to its provider as an idempotency key. That covers a crash between "provider accepted it" and "we committed".

## Layout

```
app/
  main.py config.py db.py models.py schemas.py logging.py runtime.py worker.py
  api/        health, leads (+ stage, contacts), tasks, admin, Meta webhook, inbound SMS, deps
  services/   lead creation + intake, pipeline, handoff, scoring, timeline, normalization, Meta parsing
  jobs/       queue primitives, job kinds + keys, handlers
  followups/  sequence YAML + loader, MOCK senders, guards, engine
  crm/        adapter protocol, mapping, MOCK HubSpot, HubSpot skeleton, sync
  ai/         scorer protocol, providers, rules, guard (injection), parsing, resilience, service
  security/   webhook signatures, CSRF, token bucket
  web/        form routes + Jinja templates
migrations/   Alembic env + versions
scripts/send_test_webhook.py
tests/unit  tests/integration  tests/fixtures
```

## Open decisions

- **Real Meta payloads**: production leadgen webhooks carry only IDs, and the answers have to be fetched from the Graph API. Inline `field_data` is supported now. Changes without it are recorded as `needs_fetch` for a future fetcher.
- **Rate-limit store**: the in-memory buckets are per process. Running several workers needs a shared store (Redis or the database) behind the same `BucketStore` protocol.
- **Defaults to confirm**:
  - phone cap per UTC day (3)
  - confidence threshold for human review (0.5)
  - quiet hours 21:00-08:00 in `America/Chicago`
  - 4 touches per week with a 12h gap
  - callback promise "within 15 minutes"
- **Quiet hours for the instant acknowledgment**: right now every send respects quiet hours, so a lead at 22:30 gets the "we'll call within 15 minutes" text at 08:00. Options: skip the ack outside hours, or send a different "we'll call at 8am" ack.
- **Handoff is one-way**: a handoff cancels pending sends, and closing the task does not restart the sequence. A "resume sequence" action is easy to add if wanted.
- **Opt-out scope**: SMS `STOP` opts the person out of every channel, and there is no `START` to opt back in yet. A real provider also sends its own STOP confirmation.
- **Stages are forward-only**: there is no way to reopen a `lost` lead. If reopening is needed, it should be its own audited action.
- **HubSpot deal duplicates on retry**: contacts are deduplicated by email, but if a real deal create succeeds and the job then fails, the retry creates a second deal. The fix for the real adapter is HubSpot's upsert-by-unique-property on `external_lead_id`.
- **Operator auth** is a single shared bearer token. Per-user accounts and roles come with the dashboard.

## License

MIT. See [LICENSE](LICENSE).
