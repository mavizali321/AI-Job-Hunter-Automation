# Maviz AI Job Hunter

Hybrid, approval-gated job-hunting automation system. Discovers jobs from ATS APIs, scores them deterministically, routes approvals via WhatsApp, and fills application forms with Playwright.

## Pipeline

discover → verify → deduplicate → score → shortlist → WhatsApp approval → prepare truthful application → local browser fill → final approval → submit → record confirmation

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

## Discovery Adapters

Configure in `.env` (comma-separated lists):

| Variable | Source |
|----------|--------|
| `GREENHOUSE_BOARD_TOKENS` | Greenhouse public boards API |
| `LEVER_COMPANY_SLUGS` | Lever public postings API |
| `ASHBY_COMPANY_SLUGS` | Ashby GraphQL API |
| `CAREER_PAGE_URLS` | Generic career page scraper |

The orchestrator runs all configured adapters concurrently, applies freshness/eligibility/hard-reject filters, deduplicates, scores, and creates applications for shortlisted jobs.

LinkedIn and Indeed are treated as discovery-only sources — jobs from them require official ATS/company URLs.

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/run` | Run full discovery orchestrator |
| POST | `/api/csv-import` | Import from tracker CSV |
| POST | `/api/approve/{token}` | Approve application |
| POST | `/api/reject/{token}` | Reject application |
| GET | `/api/export` | Export jobs CSV |
| GET | `/api/worker/jobs` | Worker: get pending jobs |
| POST | `/api/worker/submit/{id}` | Worker: report submission |

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

108 tests covering scoring, dedup, state machine, approvals, webhooks, discovery orchestrator, API endpoints, and E2E flow.

## Architecture

- **Python 3.11+** / FastAPI / SQLAlchemy 2 / Celery
- **15-state finite state machine** with enforced valid transitions
- **Deterministic 100-point scoring** (no LLM required)
- **Dual-approval guard** before any submission
- **Payload hash binding** — approval invalidated if job/resume/answers change
- **Scheduled discovery** at 09:00 and 18:00 Asia/Karachi via Celery Beat
- **Async adapter orchestrator** with per-source error isolation
- **Resume integrity** verified via SHA256 checksum

## Security

- Secrets in `.env` only (never committed)
- Resume PDF is source of truth — never fabricated
- All external content (job pages, webhooks) treated as untrusted data
- No submission without explicit dual approval
- Worker authentication via HMAC-compared token
