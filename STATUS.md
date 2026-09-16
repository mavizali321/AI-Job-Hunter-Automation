# Status — Maviz AI Job Hunter

## Verified Working (no credentials required)

- **Database models & migrations**: 7 tables (jobs, applications, approvals, submission_nonces, candidate_answers, events, source_runs) with SQLAlchemy 2 + SQLite/PostgreSQL. Jobs have `verification_evidence` (JSON) and `verification_blockers` (Text). Approvals have `approval_type` (FIRST/FINAL), `approval_ref_code`, and `whatsapp_delivery_status`. Applications have `manifest_hash`, `confirmation_reference`, `screenshot_path`, `attempt_status`. SubmissionNonce stores one-time submission authorization tokens with expiry and consumption tracking.
- **State machine**: 17-state explicit FSM with enforced valid transitions, event logging on every transition. Path: DISCOVERED → VERIFIED → SCORED → SHORTLISTED → WAITING_APPROVAL → APPROVED → PREPARING → READY_TO_SUBMIT → AWAITING_FINAL_APPROVAL → FINAL_APPROVED → SUBMITTED. READY_TO_SUBMIT → SUBMITTED is **not valid** — must go through final approval states.
- **Two-stage approval**: FIRST approval validates payload hash (job + score + resume variant + answer set version). FINAL approval validates manifest hash (job snapshot + resume SHA256 + answers hash). Different hash computation for each. ApprovalType enum distinguishes FIRST vs FINAL. `_apply_decision` checks `approval_type` and uses correct hash comparison.
- **Submission nonce**: One-time authorization token created after dual approval passes. Includes manifest hash, short expiry (5 min), consumed flag. Worker must present valid unconsumed nonce to submit.
- **Scoring engine**: Deterministic 100-point rubric (role relevance, technical match, experience fit, location fit, company quality, entry-level accessibility) with hard-reject rules
- **Deduplication**: URL canonicalization + company/title/content hash; second runs produce zero duplicates
- **Evidence-based verification**: `verify_job` requires 6-point evidence. Missing evidence blocks as MANUAL_ACTION_REQUIRED.
- **Missing posting date**: `is_fresh()` returns `False` for missing `posted_date`. Jobs without posting date are excluded from discovery pipeline.
- **Discovery orchestrator**: Async orchestrator runs all configured adapters concurrently with verification.
- **Discovery adapters**: Greenhouse, Lever, Ashby, generic career page — all async with httpx
- **CSV import**: Available as separate `/api/csv-import` endpoint
- **Approval system**: Token-based with SHA256 hashing. Payload hash binding for FIRST, manifest hash binding for FINAL. Expiry enforcement. Dual-approval guard (`check_dual_approval`). Ref-code-based decisions.
- **Dashboard**: Real CSRF protection — token stored in secure httponly SameSite=strict cookie, hidden `csrf_token` form fields, validated server-side with `hmac.compare_digest` (double-submit cookie pattern). POST forms for approve/reject actions (not GET links). Shows pending approvals with ref codes and action buttons. Shows blocked/manual-action-required jobs with blockers.
- **Worker authorize endpoint**: `POST /api/worker/authorize/{job_id}` — atomically verifies: job is FINAL_APPROVED, dual approval satisfied, manifest hash matches, returns one-time submission nonce.
- **Worker submit endpoint**: `POST /api/worker/submit/{job_id}` — validates outcome BEFORE consuming nonce (invalid outcome cannot burn nonce), re-verifies `application.manifest_hash`, verifies nonce is bound to the correct application, then consumes nonce. Handles CONFIRMED/UNCERTAIN/MANUAL_ACTION_REQUIRED/FAILED outcomes. CONFIRMED → SUBMITTED. UNCERTAIN → MANUAL_ACTION_REQUIRED (never auto-retry). Records confirmation reference and screenshot path.
- **ATS submission adapters**: Greenhouse, Lever, Ashby, generic form fillers in local Playwright worker. One `async_playwright` + persistent browser context remains open across form fill, submit click, confirmation detection, then closes in `finally`. Never returns a page from an exited Playwright context. Detects ATS from URL hostname. Each adapter maps fields to ATS-specific selectors. CAPTCHA → MANUAL_ACTION_REQUIRED (no submit click). Login required → MANUAL_ACTION_REQUIRED.
- **WhatsApp webhook signature verification**: When `WHATSAPP_APP_SECRET` is configured, webhook validates `X-Hub-Signature-256` header before processing. Rejects unsigned/invalid in production mode.
- **Alembic migration**: Single migration adds all schema changes (submission_nonces table, new columns on applications/approvals/jobs). Tested against pre-update SQLite fixture — upgrade preserves existing data.
- **Pipeline**: Full discover → verify → score → shortlist → create application → auto-approval → prepare → AWAITING_FINAL_APPROVAL flow
- **Celery tasks**: Scheduled discovery (morning 09:00 / evening 18:00 Asia/Karachi)
- **Docker Compose**: API + worker + beat + PostgreSQL + Redis
- **Test suite**: 165 tests, 0 skipped, covering:
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
- **Resume integrity**: SHA256 checksum verified unchanged: `E47DD0E6E27E3FE906630219A1CA44D450BE5DC2F07186FF8637D17CCE59DD4E`

## Credential-Dependent (requires `.env` configuration)

- **Discovery adapters**: Set `GREENHOUSE_BOARD_TOKENS`, `LEVER_COMPANY_SLUGS`, `ASHBY_COMPANY_SLUGS`, `CAREER_PAGE_URLS`
- **WhatsApp Cloud API**: Requires `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_RECIPIENT`. Set `WHATSAPP_APP_SECRET` for webhook signature verification.
- **LLM extraction**: Requires `LLM_API_KEY`
- **PostgreSQL**: Docker Compose uses Postgres; local dev uses SQLite
- **Redis/Celery**: Requires Redis for scheduled tasks
- **Playwright browser worker**: Requires `playwright install chromium`

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
| All 165 tests pass, 0 skipped | PASS |
| Original resume checksum unchanged | PASS |
| Fixture HTML forms + Playwright browser submit test | PASS |
| CSRF protection (double-submit cookie, 4 tests) | PASS |
| Worker integration tests (8 tests via fill_and_submit) | PASS |
| Nonce-to-application binding enforced | PASS |
| Outcome validated before nonce consumption | PASS |
| Clean source archive created | NOT DONE |
