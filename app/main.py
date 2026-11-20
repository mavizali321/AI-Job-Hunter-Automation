"""FastAPI application — multi-user job hunter with phone+OTP auth."""

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    Body, FastAPI, Depends, HTTPException, Request, Form, Query,
    Response, UploadFile, File,
)
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel

import structlog

from app.config import settings
from app.database import get_db, engine, Base
from app.models import (
    Job, Application, Approval, Event, SourceRun, JobStatus,
    ApprovalDecision, ApprovalType, SubmissionNonce, User, CandidateProfile,
)
from app.csv_import import import_csv
from app.discovery import run_orchestrator
from app.pipeline import (
    ingest_discovered_job, verify_job, score_and_decide,
    create_application_for_shortlisted, application_slug,
)
from app.approval import (
    create_approval, validate_and_decide, decide_by_ref_code, ApprovalChannel,
    check_dual_approval, create_submission_nonce, consume_submission_nonce,
)
from app.state_machine import transition_job, InvalidTransitionError
from app.auth import (
    get_current_user, get_optional_user,
    register_user, verify_otp, resend_otp, login_with_otp,
    create_access_token,
)
from app.resume import process_resume
from app.whatsapp import WhatsAppClient

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(),
    cache_logger_on_first_use=True,
)
logger = structlog.get_logger()


class RateLimiter:
    def __init__(self, requests_per_minute: int = 60):
        self._rpm = requests_per_minute
        self._hits: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, key: str) -> bool:
        now = time.monotonic()
        window = now - 60.0
        hits = self._hits[key]
        self._hits[key] = [t for t in hits if t > window]
        if len(self._hits[key]) >= self._rpm:
            return False
        self._hits[key].append(now)
        return True


rate_limiter = RateLimiter(requests_per_minute=120)


@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(bind=engine)
    Path(settings.uploads_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.applications_dir).mkdir(parents=True, exist_ok=True)
    logger.info("app_started")
    yield
    logger.info("app_stopped")


app = FastAPI(title="Maviz AI Job Hunter", version="2.0.0", lifespan=lifespan)

templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))

signer = URLSafeTimedSerializer(settings.secret_key)

CSRF_COOKIE_NAME = "csrf_token"
VALID_OUTCOMES = {"CONFIRMED", "UNCERTAIN", "MANUAL_ACTION_REQUIRED", "FAILED"}


def _set_csrf_cookie(response, token: str):
    response.set_cookie(
        CSRF_COOKIE_NAME, token,
        httponly=True, samesite="strict", max_age=86400,
    )


def _verify_csrf(form_token: str, cookie_token: str) -> bool:
    if not form_token or not cookie_token:
        return False
    return hmac.compare_digest(form_token, cookie_token)


def _check_admin_auth(request: Request):
    auth_cookie = request.cookies.get("admin_auth")
    if not auth_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        signer.loads(auth_cookie, max_age=86400)
    except Exception:
        raise HTTPException(status_code=401, detail="Session expired")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    client_ip = request.client.host if request.client else "unknown"
    if not rate_limiter.is_allowed(client_ip):
        return JSONResponse(status_code=429, content={"detail": "Rate limit exceeded"})
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    logger.info(
        "request",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(duration_ms, 1),
    )
    return response


# ===========================================================================
# Health
# ===========================================================================

@app.get("/health")
def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.get("/ready")
def readiness(db: Session = Depends(get_db)):
    try:
        from sqlalchemy import text
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        raise HTTPException(status_code=503, detail="Database not ready")


# ===========================================================================
# Auth API — Register, OTP verify, Login
# ===========================================================================

class RegisterRequest(BaseModel):
    phone: str
    name: str
    email: str = ""

class OTPVerifyRequest(BaseModel):
    phone: str
    otp: str

class PhoneRequest(BaseModel):
    phone: str


@app.post("/api/auth/register")
def api_register(body: RegisterRequest, db: Session = Depends(get_db)):
    user, otp = register_user(db, body.phone, body.name, body.email)

    client = WhatsAppClient()
    whatsapp_result = None
    if client.configured:
        try:
            whatsapp_result = client.send_otp(user.whatsapp_number or user.phone, otp)
        except Exception as e:
            logger.warning("Failed to send OTP via WhatsApp: %s", e)

    return {
        "status": "otp_sent",
        "phone": user.phone,
        "message": "OTP sent to your WhatsApp. Verify to complete registration.",
        "whatsapp_configured": client.configured,
        "otp_debug": otp if not client.configured else None,
    }


@app.post("/api/auth/verify-otp")
def api_verify_otp(body: OTPVerifyRequest, db: Session = Depends(get_db)):
    user, token = verify_otp(db, body.phone, body.otp)
    return {
        "status": "verified",
        "token": token,
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "email": user.email,
            "has_resume": bool(user.resume_path),
        },
    }


@app.post("/api/auth/resend-otp")
def api_resend_otp(body: PhoneRequest, db: Session = Depends(get_db)):
    user, otp = resend_otp(db, body.phone)

    client = WhatsAppClient()
    if client.configured:
        try:
            client.send_otp(user.whatsapp_number or user.phone, otp)
        except Exception as e:
            logger.warning("Failed to resend OTP: %s", e)

    return {
        "status": "otp_sent",
        "phone": user.phone,
        "otp_debug": otp if not client.configured else None,
    }


@app.post("/api/auth/login")
def api_login(body: PhoneRequest, db: Session = Depends(get_db)):
    user, otp = login_with_otp(db, body.phone)

    client = WhatsAppClient()
    if client.configured:
        try:
            client.send_otp(user.whatsapp_number or user.phone, otp)
        except Exception as e:
            logger.warning("Failed to send login OTP: %s", e)

    return {
        "status": "otp_sent",
        "phone": user.phone,
        "message": "OTP sent. Verify to login.",
        "otp_debug": otp if not client.configured else None,
    }


# ===========================================================================
# User profile & resume
# ===========================================================================

@app.get("/api/me")
def api_me(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    profile = db.query(CandidateProfile).filter(CandidateProfile.user_id == user.id).first()
    total_jobs = db.query(Job).filter(Job.user_id == user.id).count()
    total_apps = db.query(Application).filter(Application.user_id == user.id).count()
    submitted = db.query(Application).filter(
        Application.user_id == user.id,
        Application.state == JobStatus.SUBMITTED,
    ).count()

    return {
        "user": {
            "id": user.id,
            "name": user.name,
            "phone": user.phone,
            "email": user.email,
            "has_resume": bool(user.resume_path),
            "resume_checksum": user.resume_checksum,
            "created_at": user.created_at.isoformat() if user.created_at else None,
        },
        "profile": {
            "skills": profile.skills if profile else None,
            "target_roles": profile.target_roles if profile else None,
            "seniority": profile.seniority if profile else None,
            "experience_years": profile.total_experience_years if profile else None,
        } if profile else None,
        "stats": {
            "total_jobs": total_jobs,
            "total_applications": total_apps,
            "submitted": submitted,
        },
    }


@app.post("/api/resume/upload")
async def api_upload_resume(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are accepted")

    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large (max 10MB)")

    profile = process_resume(db, user, content, file.filename)

    return {
        "status": "uploaded",
        "resume_path": user.resume_path,
        "profile": {
            "name": profile.name,
            "skills": profile.skills,
            "target_roles": profile.target_roles,
            "seniority": profile.seniority,
            "experience_years": profile.total_experience_years,
        },
    }


# ===========================================================================
# User job endpoints
# ===========================================================================

@app.get("/api/my/jobs")
def api_my_jobs(
    status: str | None = Query(None),
    limit: int = Query(50, le=200),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    query = (
        db.query(Job)
        .filter(Job.user_id == user.id)
        .order_by(Job.score_total.desc().nullslast(), Job.created_at.desc())
    )
    if status:
        query = query.filter(Job.status == status)
    jobs = query.limit(limit).all()

    return [
        {
            "id": j.id,
            "company": j.company,
            "title": j.title,
            "location": j.location,
            "score": j.score_total,
            "decision": j.decision,
            "status": j.status.value if j.status else None,
            "url": j.official_url or j.canonical_url,
            "salary": j.salary,
            "created_at": j.created_at.isoformat() if j.created_at else None,
        }
        for j in jobs
    ]


@app.get("/api/my/applications")
def api_my_applications(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    applications = (
        db.query(Application)
        .filter(Application.user_id == user.id)
        .order_by(Application.created_at.desc())
        .limit(100)
        .all()
    )
    result = []
    for a in applications:
        job = db.query(Job).filter(Job.id == a.job_id).first()
        result.append({
            "id": a.id,
            "job_id": a.job_id,
            "company": job.company if job else None,
            "title": job.title if job else None,
            "state": a.state.value if a.state else None,
            "submitted_at": a.submitted_at.isoformat() if a.submitted_at else None,
            "created_at": a.created_at.isoformat() if a.created_at else None,
        })
    return result


@app.post("/api/my/run-hunt")
def api_run_hunt(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user.resume_path:
        raise HTTPException(status_code=400, detail="Upload your resume first")

    result = run_orchestrator(db)
    return {"status": "ok", "discovery": result}


# ===========================================================================
# Admin dashboard (legacy password-based for admin)
# ===========================================================================

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html")


@app.post("/admin/login")
def admin_login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password, settings.dashboard_password):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = signer.dumps({"user": "admin"})
    response = RedirectResponse(url="/admin/dashboard", status_code=303)
    response.set_cookie("admin_auth", token, httponly=True, max_age=86400)
    return response


@app.get("/admin/dashboard", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    _check_admin_auth(request)
    total_users = db.query(User).count()
    verified_users = db.query(User).filter(User.is_verified == True).count()
    total_jobs = db.query(Job).count()
    by_status = {}
    for status in JobStatus:
        count = db.query(Job).filter(Job.status == status).count()
        if count > 0:
            by_status[status.value] = count
    total_apps = db.query(Application).count()
    recent_events = db.query(Event).order_by(Event.timestamp.desc()).limit(20).all()
    last_run = db.query(SourceRun).order_by(SourceRun.started_at.desc()).first()
    pending_approvals = (
        db.query(Approval).filter(Approval.decision == ApprovalDecision.PENDING)
        .order_by(Approval.created_at.desc()).limit(10).all()
    )
    csrf = secrets.token_urlsafe(32)
    resp = templates.TemplateResponse(request=request, name="admin_dashboard.html", context={
        "total_users": total_users,
        "verified_users": verified_users,
        "total_jobs": total_jobs,
        "by_status": by_status,
        "total_apps": total_apps,
        "recent_events": recent_events,
        "last_run": last_run,
        "pending_approvals": pending_approvals,
        "csrf_token": csrf,
    })
    _set_csrf_cookie(resp, csrf)
    return resp


# ===========================================================================
# User-facing pages (phone+OTP auth)
# ===========================================================================

@app.get("/", response_class=HTMLResponse)
def home_page(request: Request):
    return templates.TemplateResponse(request=request, name="home.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="user_login.html")


@app.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse(request=request, name="user_register.html")


@app.get("/dashboard", response_class=HTMLResponse)
def user_dashboard_page(request: Request):
    return templates.TemplateResponse(request=request, name="user_dashboard.html")


# ===========================================================================
# Existing API endpoints (admin-protected)
# ===========================================================================

@app.post("/api/run")
def manual_run(request: Request, db: Session = Depends(get_db)):
    _check_admin_auth(request)
    result = run_orchestrator(db)
    return {"status": "ok", "discovery": result}


@app.post("/api/csv-import")
def csv_import_endpoint(request: Request, db: Session = Depends(get_db)):
    _check_admin_auth(request)
    result = import_csv(db)
    return {"status": "ok", "csv_import": result}


@app.post("/api/approve/{token}")
def api_approve(token: str, db: Session = Depends(get_db)):
    success, msg = validate_and_decide(db, token, ApprovalDecision.APPROVED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "approved", "message": msg}


@app.post("/api/reject/{token}")
def api_reject(token: str, db: Session = Depends(get_db)):
    success, msg = validate_and_decide(db, token, ApprovalDecision.REJECTED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return {"status": "rejected", "message": msg}


@app.post("/api/approve-ref/{ref_code}")
def api_approve_by_ref(
    ref_code: str, request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_admin_auth(request)
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _verify_csrf(csrf_token, cookie_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    success, msg = decide_by_ref_code(db, ref_code, ApprovalDecision.APPROVED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.post("/api/reject-ref/{ref_code}")
def api_reject_by_ref(
    ref_code: str, request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_admin_auth(request)
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _verify_csrf(csrf_token, cookie_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    success, msg = decide_by_ref_code(db, ref_code, ApprovalDecision.REJECTED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return RedirectResponse(url="/admin/dashboard", status_code=303)


@app.get("/api/export")
def export_csv(request: Request, db: Session = Depends(get_db)):
    _check_admin_auth(request)
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "user_id", "company", "title", "location", "score", "decision", "status", "url", "created_at"])
    for job in db.query(Job).order_by(Job.created_at.desc()).all():
        writer.writerow([
            job.id, job.user_id, job.company, job.title, job.location,
            job.score_total, job.decision, job.status.value if job.status else "",
            job.canonical_url, job.created_at,
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs_export.csv"},
    )


# ===========================================================================
# WhatsApp Webhook
# ===========================================================================

@app.get("/webhook/whatsapp")
def whatsapp_verify(
    request: Request,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == settings.whatsapp_verify_token:
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="Verification failed")


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.body()
    data = await request.json()

    if settings.whatsapp_app_secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not WhatsAppClient.verify_webhook_signature(body, sig, settings.whatsapp_app_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

    msg = WhatsAppClient.parse_webhook_message(data)
    if not msg:
        return {"status": "no_message"}

    message_id = msg.get("message_id", "")
    existing = db.query(Event).filter(Event.metadata_.contains({"message_id": message_id})).first()
    if existing:
        return {"status": "duplicate"}

    decision_text, ref_code = WhatsAppClient.parse_approval_response(msg.get("text", ""))

    event = Event(
        entity="whatsapp",
        entity_id=None,
        action="webhook_received",
        metadata_={"message_id": message_id, "text": msg.get("text", ""), "from": msg.get("from", ""),
                    "decision": decision_text, "ref_code": ref_code},
    )
    db.add(event)
    db.commit()

    if decision_text and ref_code:
        decision_enum = ApprovalDecision.APPROVED if decision_text == "APPROVED" else ApprovalDecision.REJECTED
        success, approval_msg = decide_by_ref_code(db, ref_code, decision_enum)
        return {"status": "processed", "decision": decision_text, "ref_code": ref_code,
                "approval_result": approval_msg, "success": success}
    elif decision_text and not ref_code:
        return {"status": "ambiguous", "decision": decision_text,
                "error": "No ref code provided — reply with APPROVE <ref_code> or REJECT <ref_code>"}

    return {"status": "processed", "decision": decision_text}


# ===========================================================================
# Worker Endpoints
# ===========================================================================

@app.get("/api/worker/jobs")
def worker_pending_jobs(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Worker-Token", "")
    if not hmac.compare_digest(token, settings.worker_token):
        raise HTTPException(status_code=403, detail="Invalid worker token")
    jobs = (
        db.query(Job)
        .filter(Job.status == JobStatus.FINAL_APPROVED)
        .order_by(Job.score_total.desc())
        .limit(10)
        .all()
    )
    result = []
    for j in jobs:
        a = db.query(Application).filter(Application.job_id == j.id).first()
        result.append({
            "id": j.id,
            "user_id": j.user_id,
            "company": j.company,
            "title": j.title,
            "url": j.official_url or j.canonical_url,
            "score": j.score_total,
            "application_id": a.id if a else None,
            "application_key": application_slug(j.company, j.title, a.id if a else None),
        })
    return result


@app.post("/api/worker/authorize/{job_id}")
def worker_authorize(job_id: int, request: Request, body: dict = Body(default={}), db: Session = Depends(get_db)):
    token = request.headers.get("X-Worker-Token", "")
    if not hmac.compare_digest(token, settings.worker_token):
        raise HTTPException(status_code=403, detail="Invalid worker token")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.FINAL_APPROVED:
        raise HTTPException(status_code=400, detail=f"Job not in FINAL_APPROVED state (current: {job.status.value})")

    application = db.query(Application).filter(Application.job_id == job.id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if not check_dual_approval(db, application.id):
        raise HTTPException(status_code=400, detail="Dual approval not satisfied")

    if not application.manifest_hash:
        raise HTTPException(status_code=400, detail="No manifest hash on application")

    incoming_hash = body.get("manifest_hash", "")
    if incoming_hash and incoming_hash != application.manifest_hash:
        raise HTTPException(status_code=400, detail="Manifest hash mismatch — content changed")

    nonce_value, nonce_hash = create_submission_nonce(db, application, application.manifest_hash)
    return {"nonce": nonce_value, "manifest_hash": nonce_hash, "job_id": job.id}


@app.post("/api/worker/submit/{job_id}")
def worker_submit_result(job_id: int, request: Request, body: dict = Body(default={}), db: Session = Depends(get_db)):
    token = request.headers.get("X-Worker-Token", "")
    if not hmac.compare_digest(token, settings.worker_token):
        raise HTTPException(status_code=403, detail="Invalid worker token")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    if job.status != JobStatus.FINAL_APPROVED:
        raise HTTPException(status_code=400, detail=f"Job not in FINAL_APPROVED state (current: {job.status.value})")

    nonce_value = body.get("nonce", "")
    manifest_hash = body.get("manifest_hash", "")
    outcome = body.get("outcome", "")
    confirmation_ref = body.get("confirmation_reference", "")
    screenshot_path = body.get("screenshot_path", "")

    if outcome not in VALID_OUTCOMES:
        raise HTTPException(status_code=400, detail=f"Unknown outcome: {outcome}")

    application = db.query(Application).filter(Application.job_id == job.id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    if application.manifest_hash and manifest_hash and application.manifest_hash != manifest_hash:
        raise HTTPException(status_code=400, detail="Manifest hash mismatch — content changed")

    if not nonce_value:
        raise HTTPException(status_code=400, detail="Submission nonce required")

    ok, nonce_msg = consume_submission_nonce(db, nonce_value, manifest_hash, application.id)
    if not ok:
        raise HTTPException(status_code=400, detail=nonce_msg)

    if outcome == "CONFIRMED":
        application.attempt_status = "CONFIRMED"
        application.confirmation_reference = confirmation_ref
        application.screenshot_path = screenshot_path
        application.submitted_at = datetime.now(timezone.utc)
        application.confirmation = f"Submitted: {confirmation_ref}"
        try:
            transition_job(db, job, JobStatus.SUBMITTED, {"confirmation": confirmation_ref})
            application.state = JobStatus.SUBMITTED
        except InvalidTransitionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif outcome == "UNCERTAIN":
        application.attempt_status = "UNCERTAIN"
        application.confirmation_reference = confirmation_ref
        application.screenshot_path = screenshot_path
        try:
            transition_job(db, job, JobStatus.MANUAL_ACTION_REQUIRED, {"reason": "uncertain_submission"})
            application.state = JobStatus.MANUAL_ACTION_REQUIRED
        except InvalidTransitionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif outcome == "MANUAL_ACTION_REQUIRED":
        reason = body.get("reason", "Manual action needed")
        application.attempt_status = "MANUAL_ACTION_REQUIRED"
        application.failure_reason = reason
        try:
            transition_job(db, job, JobStatus.MANUAL_ACTION_REQUIRED, {"reason": reason})
            application.state = JobStatus.MANUAL_ACTION_REQUIRED
        except InvalidTransitionError as e:
            raise HTTPException(status_code=400, detail=str(e))
    elif outcome == "FAILED":
        reason = body.get("reason", "Submission failed")
        application.attempt_status = "FAILED"
        application.failure_reason = reason
        try:
            transition_job(db, job, JobStatus.FAILED, {"reason": reason})
            application.state = JobStatus.FAILED
        except InvalidTransitionError as e:
            raise HTTPException(status_code=400, detail=str(e))

    db.commit()
    return {"status": "ok", "job_id": job.id, "outcome": outcome}


@app.post("/api/retry-prepare/{job_id}")
def retry_prepare(
    job_id: int, request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_admin_auth(request)
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _verify_csrf(csrf_token, cookie_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")

    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    from app.state_machine import VALID_TRANSITIONS
    if JobStatus.PREPARING not in VALID_TRANSITIONS.get(job.status, set()):
        raise HTTPException(
            status_code=400,
            detail=f"Cannot retry preparation from {job.status.value}",
        )

    application = db.query(Application).filter(Application.job_id == job.id).first()
    if not application:
        raise HTTPException(status_code=404, detail="Application not found")

    from app.pipeline import prepare_application
    result = prepare_application(db, job, application)
    if result is None:
        raise HTTPException(status_code=500, detail="Preparation failed")

    return RedirectResponse(url="/admin/dashboard", status_code=303)
