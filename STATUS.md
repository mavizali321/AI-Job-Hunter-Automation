# Status — Maviz AI Job Hunter

## Verified Working (no credentials required)

- **Database models & migrations**: 6 tables (jobs, applications, approvals, candidate_answers, events, source_runs) with SQLAlchemy 2 + SQLite/PostgreSQL
- **State machine**: 15-state explicit FSM with enforced valid transitions, event logging on every transition
- **Scoring engine**: Deterministic 100-point rubric (role relevance, technical match, experience fit, location fit, company quality, entry-level accessibility) with hard-reject rules
- **Deduplication**: URL canonicalization + company/title/content hash; second runs produce zero duplicates
- **Discovery orchestrator**: Async orchestrator runs all configured adapters (Greenhouse, Lever, Ashby, generic career pages) concurrently with error isolation per source. Applies 7-day freshness filter, hard-reject screening, LinkedIn/Indeed discovery-only enforcement, and full pipeline processing (verify → score → shortlist → create applications). Records SourceRun with counts and errors.
- **Discovery adapters**: Greenhouse (public boards API), Lever (public postings API), Ashby (GraphQL API), generic career page scraper — all async with httpx, configured via comma-separated env vars
- **CSV import**: Migrates existing `tracker/applications.csv` (available as separate `/api/csv-import` endpoint)
- **Approval system**: Token-based with SHA256 hashing, payload hash binding (invalidates on change), expiry enforcement, dual-approval guard for submissions
- **Pipeline**: Full discover → verify → score → shortlist → create application flow
- **Dashboard/API**: FastAPI with Jinja2/HTMX templates — login, dashboard (with Run Job Hunt button and last-run display), jobs, review queue, approvals, applications, audit log, source runs, CSV export
- **POST /api/run**: Executes real discovery via adapter orchestrator (not just CSV import)
- **POST /api/csv-import**: Separate endpoint for legacy CSV import
- **Celery tasks**: Scheduled discovery (morning 09:00 / evening 18:00 Asia/Karachi) calls shared orchestrator — no duplicated pipeline logic
- **WhatsApp webhook**: Verification endpoint, message parsing, approval response parsing, idempotency via message ID, signature verification
- **Worker endpoints**: Token-authenticated job polling and result submission
- **Health/readiness endpoints**: `/health` and `/ready`
- **Docker Compose**: API + worker + beat + PostgreSQL + Redis (config validated)
- **Seed script**: Creates tables and imports CSV (`python scripts/seed.py`)
- **Test suite**: 108 tests covering scoring, dedup, state machine, approval expiry/hash/dual-approval, webhook idempotency/signature, no-submit-without-approval, CSV migration, API endpoints (including discovery endpoint), discovery orchestrator (adapter wiring, DB ingestion, dedup, freshness, hard-reject, source isolation, stale-job blocking), E2E dry run
- **Resume integrity**: SHA256 checksum verified unchanged: `E47DD0E6E27E3FE906630219A1CA44D450BE5DC2F07186FF8637D17CCE59DD4E`

## Credential-Dependent (requires `.env` configuration)

- **Discovery adapters**: Set `GREENHOUSE_BOARD_TOKENS`, `LEVER_COMPANY_SLUGS`, `ASHBY_COMPANY_SLUGS`, `CAREER_PAGE_URLS` (comma-separated) to enable real job discovery
- **WhatsApp Cloud API**: Requires `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_RECIPIENT` — sends approval messages, processes webhook responses
- **LLM extraction**: Requires `LLM_API_KEY` — falls back to deterministic extraction without it
- **PostgreSQL**: Docker Compose uses Postgres; local dev uses SQLite
- **Redis/Celery**: Requires Redis for scheduled task execution (morning/evening discovery at 09:00/18:00 Asia/Karachi)
- **Playwright browser worker**: Requires `playwright install chromium` — fills ATS forms with persistent browser profile

## Acceptance Check Results

| Check | Status |
|-------|--------|
| `docker compose config` succeeds | PASS |
| DB migration + CSV import without duplicates | PASS |
| Dry-run E2E (discover → verify → score → shortlist → approve → prepare → submit) | PASS |
| Second run creates no duplicates | PASS |
| No SUBMITTED without two valid approvals | PASS |
| Sensitive/CAPTCHA → MANUAL_ACTION_REQUIRED | PASS |
| Discovery adapters wired to POST /api/run | PASS |
| Celery run_discovery calls shared orchestrator | PASS |
| Dashboard has Run Job Hunt button | PASS |
| Stale/unverified jobs blocked from approval | PASS |
| LinkedIn/Indeed discovery-only enforced | PASS |
| Adapter failure isolation (one fails, others continue) | PASS |
| All 108 tests pass | PASS |
| Original resume checksum unchanged | PASS |
