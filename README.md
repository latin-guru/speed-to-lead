# speed-to-lead

[![CI](../../actions/workflows/ci.yml/badge.svg)](../../actions/workflows/ci.yml)
[![Security](../../actions/workflows/security.yml/badge.svg)](../../actions/workflows/security.yml)
![Coverage floor](https://img.shields.io/badge/coverage-%E2%89%A595%25%20enforced%20in%20CI-brightgreen)

This is a public showcase version of a lead pipeline I run in production. Business rules, prompts, and client data are kept private.

Capture a lead, verify where it came from, and reply in seconds: the lead is AI-scored, gets an instant acknowledgment, follows a guarded follow-up sequence, and is handed to a human the moment they reply. Strict guardrails mean untrusted lead text can never steer the system, and the dashboard is protected by real accounts, roles and two-factor login.

The demo business is **Copperline Roofing & Solar (fictional)**. All sample data uses `555-01xx` phone numbers and `example.com` emails.

> **Nothing is ever really sent.** The SMS and email senders are MOCKS that write to an `outbox_messages` table, and the CRM adapters used by default are in-memory MOCKS. There is no code path that delivers a message.

![Dashboard overview](docs/screenshots/dashboard.png)

## Run the demo

```bash
make install     # venv + pinned dependencies (Python 3.11+)
make demo        # or: .venv/bin/python -m app.demo
```

Open http://127.0.0.1:8000/login and pick a demo account (see [Demo login](#demo-login)). A SQLite database (`./demo.db`) is reset and seeded with 150 fake leads from the last 30 days, and a simulator sends a new lead through the real endpoints every few seconds (signed Meta webhook, `POST /api/leads`, signed inbound SMS replies), which the in-process worker scores, acknowledges and syncs to the MOCK CRM. No API keys are needed.

### Demo login

| Account | Role | What it shows |
| --- | --- | --- |
| `admin@example.com` | admin | everything, including users, dead-letter retries, the kill switch and the audit log. Two-factor is on: the code step shows the current code, because the demo secret is public. |
| `rep@example.com` | rep | about a third of the leads are assigned to this rep: contact details can be revealed on those, the others need "Assign to me" first |
| `viewer@example.com` | viewer | read-only, contact details always masked |

All three use the password `copperline-demo`, shown on the login page only in demo mode. The accounts are reseeded on every start and cannot be changed. For a public deployment, set `DEMO_ROLES=viewer`.

## Run the tests

```bash
make check       # ruff, ruff format --check, mypy (strict), pytest with coverage (fails under 95%)
```

For your own data instead of the demo: copy `.env.example` to `.env`, set the keys it lists, run `make migrate`, create the first admin with `make create-admin EMAIL=you@example.com`, then `make run` and `make worker`. `docker compose up --build` runs Postgres, the app and a worker.

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
  - `MockScorer`: an example rule set, deterministic, no network. This is the default.
  - `ClaudeScorer`: Anthropic Messages API over httpx. The model is set by `ANTHROPIC_MODEL`.
  - `GrokScorer`: xAI chat completions over httpx.
  - If a real provider is selected without its API key, the app refuses to start.
- **Strict output schema** (`ScoreResult`). Fields: `score` (int 0-100), `summary` (at most 2 sentences and 300 characters), `next_step` (`call_now | send_info | nurture | disqualify | human_review`), `confidence` (0-1) and `flags`. Extra keys and type coercion are rejected. Malformed output gets exactly one repair pass (strip code fences, extract the `{...}`, drop trailing commas). If that still fails, the lead goes to the fallback scorer.
- **Resilience**:
  - Every call has a timeout.
  - Timeouts, transport errors, 5xx and 429 are retried up to 3 attempts in total, with exponential backoff and full jitter. A `Retry-After` header is honored (capped at 5s).
  - A total time budget stops retries early.
  - A small circuit breaker stops calling a provider that keeps failing.
  - Any failure falls back to the example rule set, recorded as `scored_by="fallback"`.
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
  - **Sending window** in the lead's timezone (optional `timezone` on capture, else `BUSINESS_TIMEZONE`, default `America/New_York`). The default `SEND_WINDOWS` is every day 08:00-20:00. Outside it the job is deferred to the next opening. DST-safe.
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
  - A `HubSpotAdapter` skeleton with the real v3/v4 request shapes. It raises `NotConfigured` without a token and is only built when `CRM_PROVIDER=hubspot` and `CRM_MOCK=false`.
  
  Lead and stage map to contact `lifecyclestage`/`hs_lead_status` and the deal's `dealstage`. Sync is a queued job on create and on every stage change, keyed per stage (and per reopen).

  **No duplicate deals on retry**: the real adapter upserts deals with `POST /crm/v3/objects/deals/batch/upsert` and `idProperty=external_lead_id`, a custom deal property created once with `hasUniqueValue: true` (`property_setup_request()` shows the request). Contacts upsert by `email`, or by `external_lead_id` for phone-only leads. A job that created the deal and then failed before committing simply updates the same deal when it retries. The MOCK enforces the same unique property and rejects a second deal with the same `external_lead_id`.
- **Machine API** behind `Authorization: Bearer $ADMIN_TOKEN`, for scripts and integrations. It fails closed with `503` if the token is unset.
  - `GET /admin/jobs?status=dead` is the dead-letter queue, and `POST /admin/jobs/{id}/retry` gives a dead job a fresh set of attempts.
  - The kill switch, stage moves, contact logging, tasks and lead detail are behind the same token.

### Session 4: dashboard and demo mode

- **Dashboard** (`/dashboard`, `app/web/dashboard.py`): server-rendered Jinja, about 300 lines of vanilla JS, and Chart.js 4.5.1 vendored under `app/web/static/vendor/` and loaded with an SRI hash.
  - KPI tiles: leads in the last 7 days (with the week before), median first-response time, win rate (New -> Won, 30 days), average AI score, open handoff tasks, and dead-letter jobs (flagged with an icon and label, not only color).
  - Charts: pipeline funnel with stage-to-stage conversion, leads by source, AI score histogram, and median first-response time by day. Each one has a "Show data" table.
  - Recent activity feed. It shows a short summary per event and never the reply text or contact details.
  - The page polls `/api/metrics/summary` every 5 seconds (no websockets), pauses in a background tab, and shows when it last updated.
  - **Leads** list with stage, source and score-range filters and paging. **Lead detail** shows the fields, the AI summary and flags, the timeline, the MOCK outbox, tasks, a stage-change form and the CRM sync status (ids, last job, last error).
  - **Masking**: phones and emails (and any inside messages) are masked by default. "Show contact details" reveals them and logs a `contact_revealed` activity (a role-checked POST since session 5).
  - Light and dark follow `prefers-color-scheme`. It works at phone width, has labels on every input, visible keyboard focus and a skip link, and charts use one data color with text labels.
  - Strict `Content-Security-Policy` (scripts and styles from this origin only, no inline script), `nosniff`, `no-store`. All dynamic text is written with `textContent`.
- **Login**: a shared-token login at first; session 5 replaced it with user accounts (below).
- **Metrics JSON** (`/api/metrics/summary`, `kpis`, `funnel`, `sources`, `scores`, `response-times`, `activity`): typed response models in the OpenAPI docs at `/docs`. They accept either the bearer token or the dashboard cookie.
- **Metrics service** (`app/services/metrics.py`), with portable SQL tested on fixed data:
  - Response time is `julianday` math on SQLite and `EXTRACT(EPOCH ...)` on Postgres.
  - The median uses `ORDER BY ... LIMIT/OFFSET` around the middle rows.
  - The funnel ranks each lead by the furthest stage it ever reached (current stage plus every `stage_changed`). A lead that went new -> contacted -> lost still counts toward "qualified" and "contacted".
- **Demo mode** (`app/demo/`): see [Run the demo in 60 seconds](#run-the-demo-in-60-seconds).
  - The seed builds history from the real pieces (the sequence templates, the sending window, the after-hours wording and the example scoring rules).
  - It is deterministic: the same seed gives the same stage counts at any time of day, and a test pins them.
  - First names come from a fixed list. All phones are `555-01xx` and all emails are `@example.com`.
  - Demo accounts are described in [Demo login](#demo-login). Any secret you did not set gets a random per-process value, and the demo password is never accepted by the machine API.

### Session 5: security, CI, more CRMs

- **Accounts and roles** (`app/services/users.py`, `app/security/authz.py`): email + password users with argon2id hashes, roles admin / rep / viewer, `python -m app.cli create-user`. Every dashboard route checks its permission on the server, and routes on one lead also check ownership: reps reveal contact details only on their own leads. `ADMIN_TOKEN` stays, for the machine API only.
- **Two-factor** (`app/services/mfa.py`, `app/security/totp.py`): TOTP with any authenticator app, required for admins, optional for reps. QR code drawn on the server, secret encrypted at rest, 10 hashed single-use recovery codes, wrong codes count toward the lockout.
- **Sessions** (`app/security/sessions.py`): server-side, hashed ids, 12h absolute and 30 min idle limits, rotation on login, revoked on logout and on any change to the user.
- **Admin pages**: dead-letter retries, the kill switch and read-only settings, users (create, role, deactivate, unlock, password, two-factor reset) and the audit log with filters.
- **Contact details encrypted at rest** with a blind index for lookups, a migration that backfills existing rows, key rotation, and a retention job (`python -m app.cli retention`).
- **HTTP hardening**: strict CSP and other security headers on every response, session-bound CSRF tokens plus an Origin check, CORS allowlist (off by default), request size limits, outbound host allowlist.
- **CRM adapters**: GoHighLevel and Salesforce next to HubSpot, each as a MOCK and a real-API skeleton, picked with `CRM_PROVIDER` (`hubspot`, `gohighlevel`, `salesforce`, `none`) and `CRM_MOCK` (default `true`). GoHighLevel upserts the contact and one opportunity per contact per pipeline; Salesforce upserts Lead and Opportunity by the unique `External_Lead_Id__c`.
- **CI** (`.github/workflows/`): lint, strict typing and tests on Python 3.11 and 3.12 with a 95% coverage floor (the summary goes to the job summary); gitleaks, pip-audit and CodeQL; Dependabot for pip and actions. Actions are pinned to commit SHAs.

## Security highlights

Details, with the file behind each protection, are in [SECURITY.md](SECURITY.md).

- Accounts with argon2id passwords, lockout with backoff, generic login errors and timing-safe paths; TOTP two-factor required for admins.
- Roles enforced on the server for every route, with tests for every role against every sensitive action, including other people's lead ids.
- Server-side sessions (HttpOnly, SameSite=Strict, `__Host-` + Secure off localhost), CSRF tokens bound to the session.
- CSP without inline script, HSTS, frame denial and other headers on every response; strict CORS; body size limits.
- Phone and email encrypted at rest (Fernet, rotatable keys) with an HMAC blind index; masked on screen and in logs; reveals are audited.
- An audit log of logins, two-factor events, reveals, stage changes, retries, the kill switch and user changes.
- Outbound calls only to allowlisted HTTPS hosts; signed, idempotent webhooks; prompt-injection guards on AI scoring.


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
  main.py config.py db.py models.py schemas.py logging.py runtime.py worker.py cli.py
  api/        health, leads (+ stage, contacts), tasks, admin, metrics, Meta webhook, inbound SMS, deps
  services/   lead creation + intake, pipeline, handoff, scoring, metrics, timeline, normalization,
              Meta parsing, users, two-factor, audit log, retention
  jobs/       queue primitives, job kinds + keys, handlers
  followups/  sequence YAML + loader, MOCK senders, guards, engine
  crm/        adapter protocol, HubSpot / GoHighLevel / Salesforce (MOCK + skeleton each), sync
  ai/         scorer protocol, providers, rules, guard (injection), parsing, resilience, service
  security/   field encryption, passwords, sessions, TOTP, roles, CSRF, headers, outbound allowlist,
              webhook signatures, token bucket
  web/        form, login + two-factor, dashboard, account, admin pages, Jinja templates, static files
  demo/       seed (fake history), simulator (real HTTP paths), in-process runner, CLI
migrations/   Alembic env + versions
scripts/      send_test_webhook.py, take_screenshots.py (Playwright, for the README images)
docs/screenshots/
tests/unit  tests/integration  tests/fixtures
```

## Open decisions

- **Opt-out vs. a new form**: STOP applies to the number, so a person who texted STOP and later fills in the form again gets no messages until they text START. Submitting a form could instead count as fresh consent for that new request.
- **Defaults to confirm**: phone cap per UTC day (3), confidence threshold for human review (0.5), callback promise "within 15 minutes", session limits (12h absolute, 30 min idle), lockout (5 failures, 60s doubling to 1h), retention (365 days, anonymize).
- **Hosting the demo**: `DEMO_MODE=1` works on one free web service with an ephemeral disk. Should the public demo be deployed, and with which demo roles?

## Roadmap

Not built yet:

- Fetching full Meta leads from the Graph API (production webhooks carry only ids; those are recorded as `needs_fetch`).
- A shared rate-limit store (Redis or the database) for running several web processes.
- Self-service password reset by email, and WebAuthn / passkeys.
- Shipping audit events to tamper-evident external storage.
- OAuth token refresh for the Salesforce skeleton, and running the CRM skeletons against live sandboxes.
- Real SMS and email senders (the repository only has mocks).

## License

All rights reserved. This is a public showcase: you may read the code and run the demo locally to evaluate it. See [LICENSE](LICENSE). Vendored Chart.js keeps its own MIT license (`app/web/static/vendor/`).
