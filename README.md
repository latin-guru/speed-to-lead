# speed-to-lead

Capture a lead, verify where it came from, and reply in seconds: the lead is AI-scored, gets an instant acknowledgment, follows a guarded follow-up sequence, and is handed to a human the moment they reply. Strict guardrails mean untrusted lead text can never steer the system.

The demo business is **Copperline Roofing & Solar (fictional)**. All sample data uses `555-01xx` phone numbers and `example.com` emails.

> **Status: sessions 1-4 of 6.** Capture, webhook intake, AI scoring, the job queue, the follow-up engine, human handoff, CRM sync (mock), the operator dashboard and a live demo mode are done. Real user accounts and roles are next.
>
> **Nothing is ever really sent.** The SMS and email senders are MOCKS that write to an `outbox_messages` table, and the HubSpot adapter used by default is an in-memory MOCK. There is no code path that delivers a message.

![Dashboard overview](docs/screenshots/dashboard.png)

## Run the demo in 60 seconds

```bash
make install     # venv + pinned dependencies
make demo        # or: .venv/bin/python -m app.demo
```

Open http://127.0.0.1:8000/login. The demo token is printed in the terminal and shown on the login page (`copperline-demo`).

What happens:

1. A SQLite database (`./demo.db`) is reset with the real migrations and seeded with 150 fake leads from the last 30 days (Faker, fixed seed, so every run looks the same).
2. The web process also runs the job worker and a simulator. Every 5-10 seconds the simulator sends a new lead through the **real** paths: a signed Meta webhook for `meta_ads`, `POST /api/leads` for `web_form`, `google_ads` and `referral`. Now and then a lead replies or texts STOP (signed inbound SMS webhook), and an operator moves a lead along the pipeline (admin API).
3. The worker scores each lead with the mock AI, sends the MOCK acknowledgment, and syncs to the MOCK CRM. The dashboard refreshes itself every 5 seconds.

No API keys are needed and nothing is sent anywhere. For one-service hosting (for example a free web service with an ephemeral disk), set `DEMO_MODE=1` and a SQLite `DATABASE_URL` and start the app normally (`uvicorn app.main:create_app --factory --host 0.0.0.0 --port $PORT`). It reseeds on every start. Demo mode refuses to run on Postgres, because it wipes the database.

| Login | Lead detail |
| --- | --- |
| ![Login page with the demo token](docs/screenshots/login.png) | ![Lead detail with masked contact details](docs/screenshots/lead-detail.png) |

## Quickstart

Requires Python 3.11+.

```bash
make install                 # venv + pinned dev dependencies
cp .env.example .env         # then edit the placeholders
make run                     # migrates, then serves http://127.0.0.1:8000 (form at /, docs at /docs)
make worker                  # in a second terminal: runs queued jobs (scoring, follow-ups, CRM sync)
```

The dashboard is at http://127.0.0.1:8000/dashboard. Log in with your `ADMIN_TOKEN`.

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
- **Pipeline**: stages `new -> qualified -> contacted -> won | lost`, with an explicit allowed-transition map (`app/services/pipeline.py`). `won` is final, and `lost` needs a reason. A `lost` lead can be reopened to `qualified` with a required reason. That writes a `lead_reopened` activity, and automation stays stopped.
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
  - **Sending window** in the lead's timezone (optional `timezone` on capture, else `BUSINESS_TIMEZONE`, default `America/New_York`). The default `SEND_WINDOWS` is Mon-Sat 09:00-20:00 and Sun 15:00-18:00. Outside it the job is deferred to the next opening. DST-safe.
  - **After-hours acknowledgment**: a lead that arrives outside the window gets its first message when the window opens, in an after-hours wording ("Thanks for reaching out last night. Copperline ... will call you this morning") instead of the "within 15 minutes" promise.
  - **One voice**: at most `MAX_TOUCHES_PER_WEEK` (default 3) per *person* in a rolling 7 days, counted across every lead with the same phone or email, and `MIN_TOUCH_GAP_HOURS` (default 24) between touches. The instant SMS + email pair is one touch and skips the gap.
- **Opt-out**: `POST /webhooks/inbound/sms` is a mock SMS provider webhook, HMAC-signed with `INBOUND_WEBHOOK_SECRET` and idempotent on `message_id`. `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END` or `QUIT` opts the number out and cancels every pending send for it. A send that was already running re-checks and skips. STOP applies to the number: a new form from an opted-out number gets no messages either.
- **Opt back in**: `START` or `UNSTOP` clears the opt-out. Sends are allowed from then on. Steps the opt-out cancelled are not brought back, and the lead's sequence stays stopped. An `opted_in` activity is logged.
- **Human handoff** (`app/services/handoff.py`): pause the sequence (cancelling pending sends), open a task and log an activity. Triggers:
  - any other inbound reply
  - a `human_review` suggestion from the scorer
  - low confidence
  
  There is one open task per lead and reason. `GET /api/tasks` lists them and `PATCH /api/tasks/{id}` assigns or closes one.

  Closing with `{"status": "done", "resume_sequence": true, "actor": "dana"}` hands the lead back to automation. It re-queues the follow-ups the handoff cancelled (overdue ones become due now, and every guard still applies) and logs `sequence_resumed`. It is refused (409) while the lead has other open tasks, after an opt-out, or once the lead is contacted, won or lost.
- **First-response time**: `first_response_at` is set by the first follow-up that actually sends, or when a human logs contact (`POST /api/leads/{id}/contacts`, which also stops automation) or moves the lead to `contacted`. `GET /api/leads/{id}` shows it with `first_response_seconds` and the full timeline.
- **CRM sync** (`app/crm/`):
  - A `CRMAdapter` protocol.
  - `MockHubSpotAdapter`: in-memory, clearly labeled MOCK, unique by email like HubSpot, and it records the API calls it would have made.
  - A `HubSpotAdapter` skeleton with the real v3/v4 request shapes. It raises `NotConfigured` without a token and is only built when `CRM_PROVIDER=hubspot`.
  
  Lead and stage map to contact `lifecyclestage`/`hs_lead_status` and the deal's `dealstage`. Sync is a queued job on create and on every stage change, keyed per stage (and per reopen).

  **No duplicate deals on retry**: the real adapter upserts deals with `POST /crm/v3/objects/deals/batch/upsert` and `idProperty=external_lead_id`, a custom deal property created once with `hasUniqueValue: true` (`property_setup_request()` shows the request). Contacts upsert by `email`, or by `external_lead_id` for phone-only leads. A job that created the deal and then failed before committing simply updates the same deal when it retries. The MOCK enforces the same unique property and rejects a second deal with the same `external_lead_id`.
- **Operator API** behind `Authorization: Bearer $ADMIN_TOKEN`. It fails closed with `503` if the token is unset.
  - `GET /admin/jobs?status=dead` is the dead-letter queue, and `POST /admin/jobs/{id}/retry` gives a dead job a fresh set of attempts.
  - The kill switch, stage moves, contact logging, tasks and lead detail are behind the same token.

### Session 4: dashboard and demo mode

- **Dashboard** (`/dashboard`, `app/web/dashboard.py`): server-rendered Jinja, about 300 lines of vanilla JS, and Chart.js 4.5.1 vendored under `app/web/static/vendor/` and loaded with an SRI hash.
  - KPI tiles: leads in the last 7 days (with the week before), median first-response time, win rate (New -> Won, 30 days), average AI score, open handoff tasks, and dead-letter jobs (flagged with an icon and label, not only color).
  - Charts: pipeline funnel with stage-to-stage conversion, leads by source, AI score histogram, and median first-response time by day. Each one has a "Show data" table.
  - Recent activity feed. It shows a short summary per event and never the reply text or contact details.
  - The page polls `/api/metrics/summary` every 5 seconds (no websockets), pauses in a background tab, and shows when it last updated.
  - **Leads** list with stage, source and score-range filters and paging. **Lead detail** shows the fields, the AI summary and flags, the timeline, the MOCK outbox, tasks, a stage-change form and the CRM sync status (ids, last job, last error).
  - **Masking**: phones and emails (and any inside messages) are masked by default. "Show contact details" reveals them and logs a `contact_revealed` activity.
  - Light and dark follow `prefers-color-scheme`. It works at phone width, has labels on every input, visible keyboard focus and a skip link, and charts use one data color with text labels.
  - Strict `Content-Security-Policy` (scripts and styles from this origin only, no inline script), `nosniff`, `no-store`. All dynamic text is written with `textContent`.
- **Login (temporary)**: `/login` takes the `ADMIN_TOKEN` and sets a signed session cookie: HMAC with `SECRET_KEY`, an expiry (`SESSION_TTL_HOURS`), and a fingerprint of the token. Rotating the token logs everyone out. The cookie is `HttpOnly`, `SameSite=Strict`, and `Secure` everywhere except plain-HTTP localhost. Login, logout and the stage form also carry a CSRF token. `?next=` only redirects inside `/dashboard`. Session 5 replaces this with real users and roles.
- **Metrics JSON** (`/api/metrics/summary`, `kpis`, `funnel`, `sources`, `scores`, `response-times`, `activity`): typed response models in the OpenAPI docs at `/docs`. They accept either the bearer token or the dashboard cookie.
- **Metrics service** (`app/services/metrics.py`), with portable SQL tested on fixed data:
  - Response time is `julianday` math on SQLite and `EXTRACT(EPOCH ...)` on Postgres.
  - The median uses `ORDER BY ... LIMIT/OFFSET` around the middle rows.
  - The funnel ranks each lead by the furthest stage it ever reached (current stage plus every `stage_changed`). A lead that went new -> contacted -> lost still counts toward "qualified" and "contacted".
- **Demo mode** (`app/demo/`): see [Run the demo in 60 seconds](#run-the-demo-in-60-seconds).
  - The seed builds history from the real pieces (the sequence templates, the sending window, the after-hours wording and the mock scorer).
  - It is deterministic: the same seed gives the same stage counts at any time of day, and a test pins them.
  - Names that match a short blocklist are re-drawn. All phones are `555-01xx` and all emails are `@example.com`.
  - In demo mode a public token (`DEMO_TOKEN`, default `copperline-demo`) is shown on the login page. It opens the dashboard but is never accepted by the operator API, and any secret you did not set gets a random per-process value.

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
  api/        health, leads (+ stage, contacts), tasks, admin, metrics, Meta webhook, inbound SMS, deps
  services/   lead creation + intake, pipeline, handoff, scoring, metrics, timeline, normalization, Meta parsing
  jobs/       queue primitives, job kinds + keys, handlers
  followups/  sequence YAML + loader, MOCK senders, guards, engine
  crm/        adapter protocol, mapping, MOCK HubSpot, HubSpot skeleton, sync
  ai/         scorer protocol, providers, rules, guard (injection), parsing, resilience, service
  security/   webhook signatures, CSRF, dashboard session cookie, token bucket
  web/        form routes, dashboard routes, Jinja templates, static (CSS, JS, vendored Chart.js)
  demo/       seed (fake history), simulator (real HTTP paths), in-process runner, CLI
migrations/   Alembic env + versions
scripts/      send_test_webhook.py, take_screenshots.py (Playwright, for the README images)
docs/screenshots/
tests/unit  tests/integration  tests/fixtures
```

## Decisions made in session 4

These were open after session 3 and are now implemented:

- Business timezone `America/New_York`. Sending window Mon-Sat 09:00-20:00, Sun 15:00-18:00, lead-local and configurable. Leads that arrive after hours get the after-hours acknowledgment at the next opening.
- Closing a handoff task can resume the sequence (`resume_sequence`).
- `START` / `UNSTOP` opt back in, from now on only.
- Lost -> Qualified can be reopened with a reason. Won stays final.
- HubSpot deals upsert by the unique `external_lead_id` property, and the mock enforces it.
- 3 touches per rolling 7 days, a 24h gap, and the instant SMS + email pair counts as one touch.

## Open decisions

- **Real Meta payloads**: production leadgen webhooks carry only IDs, and the answers have to be fetched from the Graph API. Inline `field_data` is supported now. Changes without it are recorded as `needs_fetch` for a future fetcher.
- **Rate-limit store**: the in-memory buckets are per process. Running several workers needs a shared store (Redis or the database) behind the same `BucketStore` protocol.
- **Defaults to confirm**: phone cap per UTC day (3), confidence threshold for human review (0.5), callback promise "within 15 minutes", and the session length for the dashboard (12h).
- **Opt-out vs. a new form**: STOP now applies to the number, so a person who texted STOP and later fills in the form again gets no messages until they text START. Submitting a form could instead count as fresh consent for that new request. This needs a call on the compliance side.
- **Reveal audit**: revealing contact details is logged per lead. Should it also need a reason, or be limited to some roles once real roles exist (session 5)?
- **Dashboard access**: one shared token until session 5. Roles to define then: who can reveal contact details, change stages, retry dead jobs, or flip the kill switch.
- **Hosting the demo**: `DEMO_MODE=1` works on one free web service with an ephemeral disk. Should the public demo be deployed, and where?

## License

All rights reserved. This is a public showcase: you may read the code and run the demo locally to evaluate it. See [LICENSE](LICENSE). Vendored Chart.js keeps its own MIT license (`app/web/static/vendor/`).
