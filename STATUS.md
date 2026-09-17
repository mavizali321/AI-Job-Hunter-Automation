# Status — Maviz AI Job Hunter

## Verified Working (no credentials required)

- **Database models & migrations**: 9 tables (jobs, applications, approvals, submission_nonces, candidate_answers, candidate_profiles, events, source_runs, alembic_version) with SQLAlchemy 2 + SQLite/PostgreSQL. Three Alembic migrations (`c4fc0511bc73`, `a7b2e3f40d12`, `b3d5f7a90123`) with idempotent upgrade/downgrade. Jobs have discovery columns (`discovery_url`, `discovery_source`, `official_url`, `official_url_method`), match columns (`matched_skills`, `missing_skills`, `seniority_fit`, `location_fit_detail`, `match_score`, `match_details`, `rejection_reason`), and verification columns (`verification_evidence`, `verification_blockers`). CandidateProfile stores parsed resume/profile data with combined checksum (resume PDF + PROFILE.md) for refresh detection, plus `confirmed_skills` and `profile_only_skills` (JSON columns). Approvals have `approval_type` (FIRST/FINAL), `approval_ref_code`, and `whatsapp_delivery_status`. Applications have `manifest_hash`, `confirmation_reference`, `screenshot_path`, `attempt_status`. SubmissionNonce stores one-time submission authorization tokens with expiry and consumption tracking.
- **State machine**: 17-state explicit FSM with enforced valid transitions, event logging on every transition. Path: DISCOVERED → VERIFIED → SCORED → SHORTLISTED → WAITING_APPROVAL → APPROVED → PREPARING → READY_TO_SUBMIT → AWAITING_FINAL_APPROVAL → FINAL_APPROVED → SUBMITTED. READY_TO_SUBMIT → SUBMITTED is **not valid** — must go through final approval states.
- **Two-stage approval**: FIRST approval validates payload hash (job + score + resume variant + answer set version). FINAL approval validates manifest hash (job snapshot + resume SHA256 + answers hash). Different hash computation for each. ApprovalType enum distinguishes FIRST vs FINAL. `_apply_decision` checks `approval_type` and uses correct hash comparison.
- **Submission nonce**: One-time authorization token created after dual approval passes. Includes manifest hash, short expiry (5 min), consumed flag. Worker must present valid unconsumed nonce to submit.
- **Scoring engine**: Deterministic 100-point rubric (role relevance, technical match, experience fit, location fit, company quality, entry-level accessibility) with hard-reject rules
- **Deduplication**: URL canonicalization + company/title/content hash; pre-verification deduplication via `_deduplicate_discovered()` before network calls; second runs produce zero duplicates
- **Evidence-based verification**: `verify_job` requires evidence including: HTTP success, substantial job content (>200 chars + ≥2 job content markers), title match (discovered title tokens in page), company match (discovered company in page), job-specific URL pattern. Missing evidence blocks as MANUAL_ACTION_REQUIRED. Generic career homepages rejected.
- **Profile gating**: Every verified job passes `_check_profile_gate` BEFORE scoring. Checks in order: seniority fit → location eligibility → do_not_claim conflicts → match score. Jobs failing any gate are SKIPPED with exact rejection reason. No application or approval created before profile matching passes. `MIN_PROFILE_MATCH_SCORE` (default 70) is configurable.
- **Evidence-based CandidateProfile**: Combined checksum of resume PDF + PROFILE.md. `verify_claims_against_resume` runs during profile construction. Skills separated into `confirmed_skills` (evidenced in resume) and `profile_only_skills` (PROFILE.md only, not in resume text). Only confirmed skills used in job matching, applications, and resume claims.
- **Missing posting date**: `is_fresh()` returns `False` for missing `posted_date`. Jobs without posting date are excluded from discovery pipeline.
- **Search providers**: `SearchProvider` interface with `search()` and `search_batch()` methods. `search_batch()` supports batch execution with per-query error isolation. Four providers implemented:
  - **Serper** (Google search API): Paid, requires `SERPER_API_KEY`. Combines role + location in search text. Uses default per-query `search_batch()` with retry.
  - **Adzuna** (job search API): Paid, requires `ADZUNA_APP_ID` + `ADZUNA_API_KEY`. Role and location as separate API params. Uses default per-query `search_batch()` with retry.
  - **Arbeitnow** (public API): Free, no key required. Overrides `search_batch()` to fetch feed ONCE per run, then filters locally for all role × location queries. Tokenized role matching (each query word must appear in title/description). Separate location filtering. Remote jobs accepted only with Pakistan/worldwide eligibility.
  - **Remotive** (public API): Free, no key required. Overrides `search_batch()` to deduplicate identical role queries — same role with different locations triggers ONE API call. Includes salary data.
  - System runs with free providers (Arbeitnow + Remotive) when paid API keys are empty.
- **Retry with exponential backoff**: All HTTP requests to search provider APIs retry on 429, 5xx, timeout, and connection errors. Up to 3 retries with exponential delay. Respects `Retry-After` header on 429 responses. Implemented in `app/search/retry.py`.
- **Per-query error isolation**: A single query failure within a provider's `search_batch()` does not discard results already collected from successful queries. Provider status is "partial" when some queries fail but results were returned, "failed" only when all results are lost.
- **Provider diagnostics**: `ProviderDiagnostics` tracks per-provider: `requests_made`, `results_received`, `results_accepted`, `errors` (with HTTP status codes/timeout detail), `status` (ok/partial/failed). Included in orchestrator summary and rendered in dashboard after each run.
- **Search query separation**: `generate_search_queries_from_profile` returns `(title, location)` tuples, not combined strings. Each provider handles role and location separately per its API requirements.
- **Global MAX_RESULTS_PER_RUN**: Enforced across all providers at the end of `discover_all`, not per provider. Configurable via `MAX_RESULTS_PER_RUN` env var (default 100).
- **CandidateProfile**: Resume PDF + PROFILE.md parsed into structured database record. Skills, experience, education, projects, target roles, location preferences, do_not_claim list all extracted. Only evidence-supported claims are stored — never fabricated. Profile refreshes automatically when either resume or PROFILE.md changes (combined checksum).
- **Job-to-profile matching**: Each verified job is matched against CandidateProfile BEFORE scoring. Uses confirmed_skills when available. Stores matched_skills, missing_skills, seniority_fit, location_fit_detail, match_score (0–100), match_details, rejection_reason. Dashboard displays these before first and final approval.
- **Dynamic search queries**: Generated from CandidateProfile target roles (sorted by priority A→B→C) × location preferences. `JOB_TITLES` and `JOB_LOCATIONS` env vars are optional overrides, not mandatory fixed lists. Empty `JOB_TITLES` triggers resume-driven search generation.
- **Official URL resolution**: Aggregator/search result URLs (LinkedIn, Indeed, Google, Adzuna, Arbeitnow, Remotive, Glassdoor, ZipRecruiter) are resolved to official company/ATS URLs via redirect following, page link extraction, and ATS slug probing. Unresolved aggregator URLs are never submitted — they are either skipped or set to MANUAL_ACTION_REQUIRED.
- **Discovery source tracking**: Job model stores `discovery_url`, `discovery_source`, `official_url`, and `official_url_method` separately from `canonical_url`. Dashboard shows discovery source, official URL with resolution method, and verification evidence.
- **Discovery orchestrator**: Async orchestrator runs all search providers + ATS adapters concurrently, isolates individual provider failures. Pipeline order: discover → deduplicate → verify → profile match → score → shortlist → create applications.
- **ATS verification adapters**: Greenhouse, Lever, Ashby, generic career page — all async with httpx. Used for verification of official URLs after search provider discovery.
- **CSV import**: Available as separate `/api/csv-import` endpoint
- **Approval system**: Token-based with SHA256 hashing. Payload hash binding for FIRST, manifest hash binding for FINAL. Expiry enforcement. Dual-approval guard (`check_dual_approval`). Ref-code-based decisions.
- **Dashboard**: Real CSRF protection — token stored in secure httponly SameSite=strict cookie, hidden `csrf_token` form fields, validated server-side with `hmac.compare_digest` (double-submit cookie pattern). POST forms for approve/reject actions (not GET links). Shows pending approvals with ref codes and action buttons. Shows blocked/manual-action-required jobs with blockers.
- **Worker authorize endpoint**: `POST /api/worker/authorize/{job_id}` — atomically verifies: job is FINAL_APPROVED, dual approval satisfied, manifest hash matches, returns one-time submission nonce.
- **Worker submit endpoint**: `POST /api/worker/submit/{job_id}` — validates outcome BEFORE consuming nonce (invalid outcome cannot burn nonce), re-verifies `application.manifest_hash`, verifies nonce is bound to the correct application, then consumes nonce. Handles CONFIRMED/UNCERTAIN/MANUAL_ACTION_REQUIRED/FAILED outcomes. CONFIRMED → SUBMITTED. UNCERTAIN → MANUAL_ACTION_REQUIRED (never auto-retry). Records confirmation reference and screenshot path.
- **ATS submission adapters**: Greenhouse, Lever, Ashby, generic form fillers in local Playwright worker. One `async_playwright` + persistent browser context remains open across form fill, submit click, confirmation detection, then closes in `finally`. Never returns a page from an exited Playwright context. Detects ATS from URL hostname. Each adapter maps fields to ATS-specific selectors. CAPTCHA → MANUAL_ACTION_REQUIRED (no submit click). Login required → MANUAL_ACTION_REQUIRED.
- **WhatsApp webhook signature verification**: When `WHATSAPP_APP_SECRET` is configured, webhook validates `X-Hub-Signature-256` header before processing. Rejects unsigned/invalid in production mode.
- **Alembic migrations**: Three migrations (`c4fc0511bc73`: submission_nonces + approval/application/job columns; `a7b2e3f40d12`: discovery + match columns + candidate_profiles table; `b3d5f7a90123`: confirmed_skills + profile_only_skills columns). All fully idempotent with `_table_exists`/`_column_exists` guards. Tested: pre-update SQLite upgrades without data loss, idempotent double-upgrade, fresh DB migration.
- **Pipeline**: Full discover → deduplicate → verify → profile match → score → shortlist → create application → auto-approval → prepare → AWAITING_FINAL_APPROVAL flow
- **Automatic preparation after first approval**: `_enqueue_preparation` dispatches Celery task (or runs synchronously) after first APPROVED. Task is idempotent (checks APPROVED state, no existing FINAL). On Celery failure, records Event with error detail for dashboard visibility.
- **Local browser worker CLI**: Both `python -m local_worker` and `python -m local_worker.worker` work. `--once` and `--headless` flags. Signal-based graceful shutdown (SIGINT/SIGTERM). `run_once()` method for testable single-pass execution. Polls `GET /api/worker/jobs` at configurable interval (`WORKER_POLL_SECONDS`). Validates `application_key` via `_safe_app_dir()` (rejects path traversal: `..`, `/`, `\`). Calls `fill_and_submit` with resolved application directory.
- **Collision-safe application directories**: `application_slug(company, title, application_id)` produces keys like `testco-ai-engineer-app-42`. Two applications with identical company/title get different directories. The same shared function is used in preparation (`prepare_application`), API payload (`/api/worker/jobs`), and worker (`_safe_app_dir`). Backward-compatible: calling without `application_id` returns base slug.
- **Worker job payload**: Returns `application_id`, `application_key` (collision-safe slug with application ID), and `official_url` for each FINAL_APPROVED job.
- **Worker report_result validation**: `report_result()` checks HTTP response status. On rejection (non-200), logs the status code and error detail and returns the error. On success, returns parsed JSON response.
- **Idempotent FINAL approval**: `create_final_approval` cancels (EXPIRED) any existing pending FINAL approvals before creating a new one — ensures exactly one pending FINAL per application.
- **Recovery task**: `recover_stuck_jobs` runs every 5 min via Celery Beat. Re-enqueues APPROVED jobs stuck >10 min. Marks PREPARING jobs stuck >10 min as FAILED.
- **Dashboard retry-prepare**: `POST /api/retry-prepare/{job_id}` calls `prepare_application` synchronously — works without Celery/Redis. Auth + CSRF protected.
- **Configurable settings**: `API_BASE_URL`, `WORKER_POLL_SECONDS`, `APPLICATIONS_DIR` in `.env`.
- **Celery optional**: `app/tasks.py` handles `ImportError` for celery gracefully. `_task` decorator registers with Celery when available, otherwise makes functions directly callable with `.delay()`.
- **Celery tasks**: Scheduled discovery (morning 09:00 / evening 18:00 Asia/Karachi), recovery (every 5 min)
- **Docker Compose**: API + worker + beat + PostgreSQL + Redis
- **Test suite**: 306 tests, 0 failed, 0 skipped, covering:
  - State machine: all valid transitions, invalid transitions blocked, event logging
  - Pipeline: ingest, verify, score, shortlist, deduplication, missing posted_date
  - Approval: create/validate, ref-code decisions, payload hash binding, expiry, dual approval enforcement
  - Final approval: creates with manifest hash, validates against manifest, rejects changed manifest
  - Submission nonce: create/consume, double-consume blocked, wrong hash rejected, expired rejected, nonce-to-application binding
  - READY_TO_SUBMIT → SUBMITTED blocked (must go through AWAITING_FINAL_APPROVAL → FINAL_APPROVED)
  - Two-stage full E2E flow: discover → verify → score → shortlist → first approve → prepare → awaiting final → final approve → submit
  - Discovery adapters: Greenhouse, Lever, Ashby, career page (mocked HTTP)
  - WhatsApp: webhook signature verification, approval message parsing
  - CSRF protection: approve/reject without CSRF → 403, wrong CSRF → 403, valid CSRF passes validation (4 tests)
  - Worker integration (end-to-end via LocalWorker.fill_and_submit): Greenhouse/Lever/Ashby fixture forms filled and submitted with CONFIRMED outcome, CAPTCHA → MANUAL_ACTION_REQUIRED with no submit click, auth failure → no browser launched, changed manifest → no browser, browser closes after success, browser closes after CAPTCHA (8 tests)
  - ATS detection from URL hostname (5 tests)
  - Submission guard: no submit without final approval, changed manifest blocks, uncertain → MANUAL_ACTION_REQUIRED (7 tests)
  - Manifest hash deterministic, changes with answers/snapshot
  - Search providers: Serper, Adzuna, Arbeitnow, Remotive — each tested with mocked HTTP (4 tests)
  - Multi-provider discovery: results merged from adapters + search providers (2 tests)
  - Provider failure isolation: one provider fails, others continue; all fail, run still completes (3 tests)
  - Cross-provider deduplication: same URL deduped, same company/title/content deduped (2 tests)
  - Freshness filtering via search providers: stale results rejected, fresh results pass (3 tests)
  - Missing posted date from search provider rejected (2 tests)
  - Aggregator URL (LinkedIn/Indeed/etc.) never submitted: detected as aggregator, skipped in orchestrator (7 tests)
  - Official URL resolution: already-official passes, aggregators rejected, company sites accepted (3 tests)
  - Unverified official URL blocked: unresolved aggregator → no submission (1 test)
  - End-to-end: search provider → verification → scoring → approval (1 test)
  - Free providers run without paid API keys (2 tests)
  - Dashboard shows discovery source, official URL, verification evidence
  - CandidateProfile parses resume + PROFILE.md (10 tests)
  - Evidence-based skills — no fabrication (3 tests)
  - Dynamic search queries from CandidateProfile (4 tests)
  - Profile refresh on checksum change (3 tests)
  - Job-to-profile matching stores match details (8 tests + 1 orchestrator test)
  - Migration idempotent: candidate_profiles table, job match columns, job discovery columns (3 tests)
  - Alembic migration: upgrade preserves data, idempotent double-upgrade, fresh DB migration (3 tests)
  - **Profile gating**: low profile match blocks application, poor seniority blocks, ineligible location blocks, profile match runs before shortlisting (4 tests)
  - **Combined checksum refresh**: PROFILE.md-only change triggers CandidateProfile refresh (1 test)
  - **Evidence-based profile creation**: unverified profile skill stored as profile_only, not confirmed (1 test)
  - **Verification strengthening**: generic career homepage rejected, title/company mismatch blocks verification (2 tests)
  - **Search behavior**: MAX_RESULTS_PER_RUN globally enforced across providers, Arbeitnow tokenized role matching without exact location phrase (2 tests)
  - **Batch provider behavior**: Arbeitnow makes one HTTP request per run (1 test), role tokens match without location in phrase (1 test), location filtered separately (1 test), partial results survive one query failure (1 test), Remotive deduplicates identical role queries (1 test), global result limit across providers (1 test), HTTP error includes status code in diagnostics (1 test), timeout error includes detail (1 test), diagnostics included in orchestrator summary (1 test) — 9 tests total
  - **Remote eligibility safety**: generic "Remote" or empty location blocked (1 test), worldwide/global accepted (1 test), pipeline blocks bare remote in verification (1 test), pipeline accepts worldwide remote (1 test) — 4 tests
  - **Exact vacancy URL enforcement**: generic careers page blocked (1 test), ATS board homepage blocked (1 test), exact Greenhouse job URL passes (1 test), exact Lever job URL passes (1 test), exact Ashby job URL passes (1 test), pipeline requires job-specific URL evidence (1 test) — 6 tests
  - **Production orchestration** (26 tests):
    - Full automated chain: discovery → first approve → auto prepare → final approve → worker poll → authorize → submit → SUBMITTED (1 test)
    - Rejection never triggers preparation (1 test)
    - Duplicate first approval produces only one pending FINAL (1 test)
    - Celery failure records Event for dashboard visibility (1 test)
    - Dashboard retry-prepare works without Celery (1 test)
    - Worker payload has application_id, application_key with app ID, official_url (1 test)
    - Worker poll returns only FINAL_APPROVED jobs (1 test)
    - Path traversal rejected (malicious keys: .., /, \, empty) (1 test)
    - Valid application slug accepted (1 test)
    - Recovery task re-enqueues stuck APPROVED >10 min (1 test)
    - Recovery task marks stuck PREPARING >10 min as FAILED (1 test)
    - UNCERTAIN outcome → MANUAL_ACTION_REQUIRED (never SUBMITTED) (1 test)
    - Worker poll returns empty on bad token (1 test)
    - Worker poll raises on connection error (1 test)
    - Preparation task skips non-APPROVED jobs (1 test)
    - Preparation task idempotent with existing FINAL approval (1 test)
    - Application slug deterministic with app ID (1 test)
    - Application slug truncated at 60 chars (1 test)
    - Application slug backward-compatible without app ID (1 test)
    - Two identical company/title jobs produce different directories (1 test)
    - CLI `python -m local_worker --help` exits 0 (1 test)
    - CLI `python -m local_worker.worker --help` exits 0 (1 test)
    - CLI `--once` exits cleanly when no server running (1 test)
    - Worker report_result logs HTTP rejection (1 test)
    - Worker report_result returns parsed JSON on success (1 test)
    - Connected integration: first approve via ref code → actual preparation task → auto FINAL → final approve via ref code → worker poll → fill_and_submit with correct unique dir → report CONFIRMED → SUBMITTED (1 test)
  - Date parsers: Serper (relative days/hours, absolute, None), Adzuna (ISO, None), Arbeitnow (Unix, None), Remotive (ISO, None) (8 tests)
  - Search result → DiscoveredJob conversion (2 tests)
  - Discovery-only sources include search providers (4 tests)
  - Job titles and locations from config (2 tests)
- **Resume integrity**: SHA256 checksum verified unchanged: `E47DD0E6E27E3FE906630219A1CA44D450BE5DC2F07186FF8637D17CCE59DD4E`

## Credential-Dependent (requires `.env` configuration)

- **ATS verification adapters**: Set `GREENHOUSE_BOARD_TOKENS`, `LEVER_COMPANY_SLUGS`, `ASHBY_COMPANY_SLUGS`, `CAREER_PAGE_URLS`
- **Serper search**: Set `SERPER_API_KEY` for Google search API. Without it, only free providers (Arbeitnow, Remotive) run.
- **Adzuna search**: Set `ADZUNA_APP_ID` + `ADZUNA_API_KEY`. Without them, Adzuna is skipped.
- **WhatsApp Cloud API**: Requires `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_RECIPIENT`. Set `WHATSAPP_APP_SECRET` for webhook signature verification.
- **LLM extraction**: Requires `LLM_API_KEY`
- **PostgreSQL**: Docker Compose uses Postgres; local dev uses SQLite
- **Redis/Celery**: Requires Redis for scheduled tasks
- **Playwright browser worker**: Requires `playwright install chromium`
- **LinkedIn/Indeed**: Discovery-only via search engine results. Never automated login, scraping, or auto-submit on those domains. Official URL resolution attempts to find the company/ATS application URL.

## Not Yet Implemented

- **Clean source archive**: Not yet created (Req 12).

## Acceptance Check Results

| Check | Status |
|-------|--------|
| `docker compose config` succeeds | PASS |
| DB migration + CSV import without duplicates | PASS |
| Alembic migration against pre-update SQLite | PASS |
| Full E2E (discover → verify → score → shortlist → first approve → prepare → awaiting final → final approve → submit) | PASS |
| READY_TO_SUBMIT → SUBMITTED blocked | PASS |
| No SUBMITTED without two valid approvals | PASS |
| FIRST approval uses payload hash | PASS |
| FINAL approval uses manifest hash | PASS |
| Manifest change invalidates final approval | PASS |
| Submission nonce: create, consume, expire, double-consume, hash mismatch | PASS |
| Missing posted_date → is_fresh() returns False | PASS |
| Dashboard approve/reject uses POST forms (not GET links) | PASS |
| Worker authorize endpoint returns nonce | PASS |
| Worker submit endpoint validates nonce and handles outcomes | PASS |
| WhatsApp webhook signature verification when secret configured | PASS |
| All 306 tests pass, 0 failed, 0 skipped | PASS |
| Original resume checksum unchanged | PASS |
| Fixture HTML forms + Playwright browser submit test | PASS |
| CSRF protection (double-submit cookie, 4 tests) | PASS |
| Worker integration tests (8 tests via fill_and_submit) | PASS |
| Nonce-to-application binding enforced | PASS |
| Outcome validated before nonce consumption | PASS |
| Search providers: Serper, Adzuna, Arbeitnow, Remotive (mocked HTTP tests) | PASS |
| Multi-provider discovery merges results | PASS |
| Provider failure isolation (one fails, others continue) | PASS |
| Cross-provider deduplication | PASS |
| Freshness filtering via search providers | PASS |
| Missing posted date from search rejected | PASS |
| Aggregator URL (LinkedIn/Indeed/etc.) never submitted | PASS |
| Official URL resolution from aggregator results | PASS |
| Unverified official URL → no submission | PASS |
| End-to-end: search provider → verification → scoring → approval | PASS |
| Free providers run without paid API keys | PASS |
| Dashboard shows discovery source, official URL, verification evidence | PASS |
| CandidateProfile parses resume + PROFILE.md | PASS |
| Evidence-based skills — no fabrication | PASS |
| Dynamic search queries from CandidateProfile | PASS |
| Profile refresh on checksum change | PASS |
| Job-to-profile matching stores match details | PASS |
| Dashboard shows match details before approval | PASS |
| Alembic migration preserves existing SQLite data | PASS |
| Alembic migration idempotent (double-upgrade) | PASS |
| Alembic fresh DB migration creates all tables | PASS |
| Profile gating: low match score blocks application | PASS |
| Profile gating: poor seniority blocks application | PASS |
| Profile gating: ineligible location blocks application | PASS |
| Profile gating runs before scoring/shortlisting | PASS |
| Combined checksum: PROFILE.md change refreshes profile | PASS |
| Evidence-based profile: unverified skill → profile_only | PASS |
| Verification: generic career homepage rejected | PASS |
| Verification: title/company mismatch blocks | PASS |
| MAX_RESULTS_PER_RUN globally enforced | PASS |
| Arbeitnow tokenized role matching (no exact phrase) | PASS |
| Arbeitnow fetches feed once per run (not per query) | PASS |
| Arbeitnow location filtered separately from role tokens | PASS |
| Remotive deduplicates identical role queries | PASS |
| Partial results survive single query failure | PASS |
| Provider errors include HTTP status / timeout detail | PASS |
| Provider diagnostics in orchestrator summary + dashboard | PASS |
| Retry with exponential backoff on 429/5xx/timeout | PASS |
| Remote eligibility: bare "Remote"/empty → not Pakistan-eligible | PASS |
| Remote eligibility: worldwide/global → accepted | PASS |
| Exact vacancy URL: generic careers/board homepage blocked | PASS |
| Exact vacancy URL: ATS-specific job URL passes verification | PASS |
| ATS probing resolves exact job posting, not board homepage | PASS |
| Automatic preparation after first APPROVED | PASS |
| Celery failure records Event for dashboard visibility | PASS |
| Dashboard retry-prepare works without Celery | PASS |
| Worker payload includes application_id, application_key, official_url | PASS |
| Path traversal protection on application_key | PASS |
| One pending FINAL per application (idempotent) | PASS |
| Recovery task re-enqueues stuck APPROVED, fails stuck PREPARING | PASS |
| Full automated chain: approve → prepare → final → submit | PASS |
| Local worker CLI: both `python -m local_worker` and `python -m local_worker.worker` | PASS |
| CLI --once exits cleanly, --help shows options | PASS |
| Configurable API_BASE_URL, WORKER_POLL_SECONDS, APPLICATIONS_DIR | PASS |
| Celery optional — tasks work without Redis/Celery installed | PASS |
| Collision-safe directories: application_key includes app ID | PASS |
| Two identical company/title → different directories | PASS |
| Worker report_result validates HTTP response, logs rejection | PASS |
| Connected integration: real task, real endpoints, real polling → SUBMITTED | PASS |
| Clean source archive created | NOT DONE |
