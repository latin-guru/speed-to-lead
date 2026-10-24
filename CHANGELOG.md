# Changelog

Highlights per version. The demo company and all sample data are fictional.

## 1.1.0

- Interview mode: an admin or rep creates a single-use, two-hour link (`/try/<token>`, stored only as a hash, revocable, audited). The visitor enters their own email, consents to one demo email, and watches their lead get created, scored, answered and synced on a live panel.
- A real email sender (SMTP with STARTTLS; a Gmail API skeleton), used only for an invite's one acknowledgment and only with `EMAIL_BACKEND` set and `INTERVIEW_EMAIL_ENABLED=true`. Capped per hour, per day, per recipient and per link; over a cap the mock sender is used. SMS stays mock.
- Demo mode: an optional private owner account (`DEMO_OWNER_*`) for creating invites on the hosted demo; real email in demo mode requires `DEMO_ROLES=viewer`.

## 1.0.0

- Hosted demo: a Render Blueprint (`render.yaml`) runs the whole demo as one free web service, offering only the read-only viewer login (`DEMO_ROLES=viewer`).
- Demo mode refuses unsafe configuration: a non-SQLite database, a real AI provider without `DEMO_ALLOW_REAL_AI=true`, or a non-mock CRM.
- README rewritten around the business problem, an architecture diagram and a feature-to-test table that a test keeps in sync.
- Refreshed screenshots, a demo GIF, and scripts to regenerate both.

## 0.5.0

- Email and password accounts (argon2id) with admin, rep and viewer roles enforced on every route, server-side sessions and an audit log.
- TOTP two-factor login, required for admins, with recovery codes.
- Contact details encrypted at rest with a blind index; data-retention job.
- Security headers, strict CSP, CORS allowlist, request size limits, outbound host allowlist.
- GoHighLevel and Salesforce CRM adapters next to HubSpot (mock and real-API skeleton each).
- CI: lint, strict typing and tests with a 95% coverage floor; gitleaks, pip-audit, CodeQL; SECURITY.md.

## 0.4.0

- Operator dashboard: KPIs, funnel, sources, AI scores, response times, lead pages with masked contacts.
- Metrics JSON API.
- Demo mode with deterministic fake history and a live lead simulator.

## 0.3.0

- Pipeline stages with explicit transitions; only people move stages.
- Database-backed job queue and worker with leases, backoff and a dead-letter list.
- Follow-up sequence with guards (opt-out, sending window, frequency caps, kill switch), human handoff on reply.
- CRM sync to a mock HubSpot without duplicates on retry; Alembic migrations.

## 0.2.0

- AI lead scoring behind a provider interface (mock, Claude, Grok) with a strict schema, retries, a circuit breaker, a rule-based fallback and prompt-injection guards.

## 0.1.0

- Lead capture: web form and JSON API with validation, rate limits, a honeypot and CSRF protection.
- Signed, idempotent lead-ads style webhook; PII-masking JSON logs; health and readiness checks.
