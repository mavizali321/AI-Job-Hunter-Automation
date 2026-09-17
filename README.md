# AI Job Hunter

Hybrid, approval-gated job-hunting automation system. Discovers jobs from ATS APIs and search providers, scores them deterministically, gates on profile matching, routes approvals via WhatsApp, and fills application forms with Playwright.

## Pipeline

discover → deduplicate → verify → profile match → score → shortlist → WhatsApp approval → prepare truthful application → local browser fill → final approval → submit → record confirmation

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env with your secrets and adapter config

# 3. Run seed (creates DB + imports CSV)
python scripts/seed.py

# 4. Start the app
uvicorn app.main:app --reload
```

## Candidate Profile

The system parses `profile/Maviz-Ali-Resume-Original.pdf` and `PROFILE.md` to build a structured `CandidateProfile` stored in the database. Skills are separated into **confirmed** (found in resume text) and **profile-only** (in PROFILE.md but not evidenced in the resume). Only confirmed skills are used in applications and resume claims.

- **Combined checksum**: Profile refreshes when either the resume PDF or PROFILE.md changes (combined SHA256).
- **Evidence-based verification**: `verify_claims_against_resume` runs during profile construction. Unverified skills are stored as `profile_only_skills` and never used in applications.
- **`do_not_claim`**: Skills explicitly listed as not to be claimed are respected throughout the pipeline.

## Profile Gating

Before scoring or shortlisting, every verified job passes through the **profile gate** (`_check_profile_gate`), which checks in order:

1. **Seniority fit** — poor/stretch beyond policy → SKIPPED
2. **Location eligibility** — no Pakistan/remote eligibility → SKIPPED
3. **do_not_claim conflicts** — required qualification conflicts → SKIPPED
4. **Match score** — below `MIN_PROFILE_MATCH_SCORE` (default 70) → SKIPPED

Jobs that fail any gate are transitioned to SKIPPED with the exact rejection reason stored. No application or approval is created before profile matching passes.

## Search Providers

Four search providers discover jobs before verification:

| Provider | Auth | Notes |
|----------|------|-------|
| Serper | `SERPER_API_KEY` | Google search API; combines role + location in search text |
| Adzuna | `ADZUNA_APP_ID` + `ADZUNA_API_KEY` | Country-specific job search; role and location as separate params |
| Arbeitnow | None (free) | Public API; fetches feed ONCE per run, tokenized role matching, separate location filtering |
| Remotive | None (free) | Remote-only; queries by role only, deduplicates identical role queries |

Free providers (Arbeitnow + Remotive) always run. Paid providers are added when their API keys are configured.

**Batch provider interface**: Each provider implements `search_batch()` for efficient multi-query execution:
- **Arbeitnow** fetches its public feed once per run and filters locally for all role × location queries
- **Remotive** deduplicates identical role queries (same title with different locations = one API call)
- **Serper/Adzuna** use the default per-query execution with per-query error isolation
- All providers retry on 429, 5xx, and timeout with exponential backoff (3 retries)
- A single query failure does not discard results already collected from that provider

**Search query separation**: Role/title is passed separately from location to each provider. Each provider handles them appropriately for its API. `MAX_RESULTS_PER_RUN` is enforced globally across all providers, not per provider. Pre-verification deduplication runs before any network calls to official URLs.

**Provider diagnostics**: After each run, the dashboard shows per-provider metrics: requests made, results received, results accepted, status, and error details.

## Verification Strengthening

URL verification requires more than HTTP success:

- **Substantial content**: Page must have >200 chars and ≥2 job content markers (responsibilities, qualifications, etc.)
- **Title match**: Discovered job title tokens must appear in official page content
- **Company match**: Discovered company name must appear in official page content
- **Job-specific URL**: URL must match ATS/job-listing patterns (Greenhouse, Lever, Ashby, etc.) or have substantial content
- **Generic career homepages** without the specific vacancy are rejected as MANUAL_ACTION_REQUIRED

## Discovery Adapters

ATS adapters verify official URLs found by search providers:

| Variable | Source |
|----------|--------|
| `GREENHOUSE_BOARD_TOKENS` | Greenhouse public boards API |
| `LEVER_COMPANY_SLUGS` | Lever public postings API |
| `ASHBY_COMPANY_SLUGS` | Ashby GraphQL API |
| `CAREER_PAGE_URLS` | Generic career page scraper |

LinkedIn and Indeed are treated as discovery-only sources — jobs from them require official ATS/company URLs.

## Production Orchestration

After the pipeline shortlists a job and creates a first approval, the system automatically chains through preparation, final approval, and submission:

1. **Automatic preparation** — When a first approval is APPROVED, `_enqueue_preparation` dispatches a Celery task (or runs synchronously if Celery is unavailable). The task is idempotent: it checks the job is still APPROVED and no FINAL approval exists before proceeding.
2. **Final approval** — `prepare_application` writes job snapshot, answers, and resume to an application directory, then creates a FINAL approval with a manifest hash (job + resume SHA256 + answers). Only one pending FINAL approval exists per application; creating a new one expires any existing pending ones.
3. **Worker polling** — The local browser worker polls `GET /api/worker/jobs` for FINAL_APPROVED jobs. Each payload includes `application_id`, `application_key` (deterministic slug), and `official_url`.
4. **Submission** — The worker authorizes via `POST /api/worker/authorize/{id}` (returns a one-time nonce), fills the ATS form with Playwright, then reports the outcome via `POST /api/worker/submit/{id}`.
5. **Recovery** — A periodic task (`recover_stuck_jobs`, every 5 min via Celery Beat) re-enqueues APPROVED jobs stuck >10 minutes and marks PREPARING jobs stuck >10 minutes as FAILED.

### Local Browser Worker

```bash
# Continuous polling (Ctrl+C for graceful shutdown)
python -m local_worker

# Process one job and exit
python -m local_worker --once

# Headless mode
python -m local_worker --headless
```

Both `python -m local_worker` and `python -m local_worker.worker` are supported.

### Dashboard Retry

If automatic preparation fails (e.g., Celery/Redis unavailable), the dashboard provides a retry button that calls `POST /api/retry-prepare/{job_id}` — this runs preparation synchronously without requiring Celery.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/run` | Run full discovery orchestrator |
| POST | `/api/csv-import` | Import from tracker CSV |
| POST | `/api/approve/{token}` | Approve application |
| POST | `/api/reject/{token}` | Reject application |
| GET | `/api/export` | Export jobs CSV |
| GET | `/api/worker/jobs` | Worker: get FINAL_APPROVED jobs with payload |
| POST | `/api/worker/authorize/{id}` | Worker: get one-time submission nonce |
| POST | `/api/worker/submit/{id}` | Worker: report submission outcome |
| POST | `/api/retry-prepare/{id}` | Dashboard: retry failed preparation |

## Dashboard

Login at `/login`, then `/dashboard` shows:
- Job counts by status
- Run Job Hunt button (triggers discovery orchestrator)
- Last run results
- Recent activity log

## Docker

```bash
docker compose up -d
```

Services: api, worker, beat, postgres, redis.

## Tests

```bash
python -m pytest tests/ -v
```

306 tests covering scoring, dedup, state machine, approvals, webhooks, discovery orchestrator, search providers, batch provider behavior, candidate profile parsing, evidence-based skills, profile gating, job matching, verification strengthening, search behavior, retry logic, provider diagnostics, Alembic migrations, production orchestration (automated preparation, recovery, idempotency, collision-safe directories, path safety, CLI entry points, connected integration, worker report validation), API endpoints, and E2E flow.

## Architecture

- **Python 3.11+** / FastAPI / SQLAlchemy 2 / Celery (optional)
- **17-state finite state machine** with enforced valid transitions
- **Deterministic 100-point scoring** (no LLM required)
- **Resume-driven CandidateProfile** with evidence-based skill extraction
- **Confirmed vs profile-only skills** — only confirmed skills used in applications
- **Profile gating** — seniority, location, do_not_claim, and match score checked before scoring
- **Combined checksum** — profile refreshes when either resume PDF or PROFILE.md changes
- **Strengthened URL verification** — substantial content, title/company match, job-specific URL patterns
- **Search query separation** — role and location passed separately to each provider
- **Batch provider interface** — Arbeitnow single-fetch, Remotive query dedup, per-query error isolation
- **Retry with exponential backoff** — 429, 5xx, timeout retried up to 3 times
- **Provider diagnostics** — requests made, results received/accepted, errors surfaced in dashboard
- **Global MAX_RESULTS_PER_RUN** — enforced across all providers, not per provider
- **Pre-verification deduplication** — dedup before network calls to official URLs
- **Dual-approval guard** before any submission
- **Payload hash binding** — approval invalidated if job/resume/answers change
- **Automatic preparation** — Celery task after first approval; idempotent, with synchronous fallback
- **Recovery task** — re-enqueues stuck APPROVED, marks stuck PREPARING as FAILED
- **Local browser worker CLI** — `python -m local_worker` with `--once`, `--headless`, graceful shutdown
- **Collision-safe application directories** — `application_slug` includes `application.id` (e.g. `testco-ai-engineer-app-42`)
- **Path traversal protection** — application directory keys validated with `Path.relative_to()`
- **One pending FINAL per application** — creating a new FINAL expires existing pending ones
- **Worker report_result validation** — logs and returns HTTP rejection details instead of silently ignoring
- **Alembic migrations** with idempotent upgrades for SQLite and PostgreSQL
- **Scheduled discovery** at 09:00 and 18:00 Asia/Karachi via Celery Beat
- **Async adapter orchestrator** with per-source error isolation
- **Resume integrity** verified via SHA256 checksum

## Security

- Secrets in `.env` only (never committed)
- Resume PDF is source of truth — never fabricated
- All external content (job pages, webhooks) treated as untrusted data
- No submission without explicit dual approval
- Worker authentication via HMAC-compared token
- Profile-only/unverified skills never used in answers or resume claims
