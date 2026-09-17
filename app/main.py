"""FastAPI application with dashboard and API endpoints."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Body, FastAPI, Depends, HTTPException, Request, Form, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.database import get_db, engine, Base
from app.models import (
    Job, Application, Approval, Event, SourceRun, JobStatus,
    ApprovalDecision, ApprovalType, SubmissionNonce,
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

app = FastAPI(title="Maviz AI Job Hunter", version="1.0.0")

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


def _check_auth(request: Request):
    auth_cookie = request.cookies.get("auth")
    if not auth_cookie:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        signer.loads(auth_cookie, max_age=86400)
    except Exception:
        raise HTTPException(status_code=401, detail="Session expired")


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)


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


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request=request, name="login.html")


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if not hmac.compare_digest(password, settings.dashboard_password):
        raise HTTPException(status_code=401, detail="Invalid password")
    token = signer.dumps({"user": "admin"})
    response = RedirectResponse(url="/dashboard", status_code=303)
    response.set_cookie("auth", token, httponly=True, max_age=86400)
    return response


@app.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    total_jobs = db.query(Job).count()
    by_status = {}
    for status in JobStatus:
        count = db.query(Job).filter(Job.status == status).count()
        if count > 0:
            by_status[status.value] = count
    total_apps = db.query(Application).count()
    recent_events = db.query(Event).order_by(Event.timestamp.desc()).limit(20).all()
    last_run = db.query(SourceRun).order_by(SourceRun.started_at.desc()).first()
    blocked_jobs = db.query(Job).filter(
        Job.status.in_([JobStatus.MANUAL_ACTION_REQUIRED, JobStatus.BLOCKED])
    ).order_by(Job.created_at.desc()).limit(10).all()
    pending_approvals = (
        db.query(Approval).filter(Approval.decision == ApprovalDecision.PENDING)
        .order_by(Approval.created_at.desc()).limit(10).all()
    )
    waiting_jobs = (
        db.query(Job).filter(Job.status.in_([
            JobStatus.WAITING_APPROVAL, JobStatus.AWAITING_FINAL_APPROVAL,
        ]))
        .order_by(Job.score_total.desc()).limit(10).all()
    )
    csrf = secrets.token_urlsafe(32)
    resp = templates.TemplateResponse(request=request, name="dashboard.html", context={
        "total_jobs": total_jobs,
        "by_status": by_status,
        "total_apps": total_apps,
        "recent_events": recent_events,
        "last_run": last_run,
        "blocked_jobs": blocked_jobs,
        "pending_approvals": pending_approvals,
        "waiting_jobs": waiting_jobs,
        "csrf_token": csrf,
    })
    _set_csrf_cookie(resp, csrf)
    return resp


@app.get("/jobs", response_class=HTMLResponse)
def jobs_list(request: Request, status: str = Query(None), db: Session = Depends(get_db)):
    _check_auth(request)
    query = db.query(Job).order_by(Job.score_total.desc().nullslast(), Job.created_at.desc())
    if status:
        query = query.filter(Job.status == status)
    jobs = query.limit(100).all()
    return templates.TemplateResponse(request=request, name="jobs.html", context={"jobs": jobs, "filter_status": status})


@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    jobs = (
        db.query(Job)
        .filter(Job.status.in_([JobStatus.SCORED, JobStatus.SHORTLISTED, JobStatus.WAITING_APPROVAL]))
        .order_by(Job.score_total.desc())
        .all()
    )
    return templates.TemplateResponse(request=request, name="review.html", context={"jobs": jobs})


@app.get("/approvals", response_class=HTMLResponse)
def approvals_page(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    approvals = db.query(Approval).order_by(Approval.created_at.desc()).limit(50).all()
    return templates.TemplateResponse(request=request, name="approvals.html", context={"approvals": approvals})


@app.get("/applications", response_class=HTMLResponse)
def applications_page(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    applications = db.query(Application).order_by(Application.created_at.desc()).limit(50).all()
    return templates.TemplateResponse(request=request, name="applications.html", context={"applications": applications})


@app.get("/audit", response_class=HTMLResponse)
def audit_log(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    events = db.query(Event).order_by(Event.timestamp.desc()).limit(200).all()
    return templates.TemplateResponse(request=request, name="audit.html", context={"events": events})


@app.get("/source-runs", response_class=HTMLResponse)
def source_runs_page(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    runs = db.query(SourceRun).order_by(SourceRun.started_at.desc()).limit(50).all()
    return templates.TemplateResponse(request=request, name="source_runs.html", context={"runs": runs})


# --- API Endpoints ---

@app.post("/api/run")
def manual_run(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    result = run_orchestrator(db)
    return {"status": "ok", "discovery": result}


@app.post("/api/csv-import")
def csv_import_endpoint(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
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
    _check_auth(request)
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _verify_csrf(csrf_token, cookie_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    success, msg = decide_by_ref_code(db, ref_code, ApprovalDecision.APPROVED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/api/reject-ref/{ref_code}")
def api_reject_by_ref(
    ref_code: str, request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_auth(request)
    cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME, "")
    if not _verify_csrf(csrf_token, cookie_csrf):
        raise HTTPException(status_code=403, detail="CSRF validation failed")
    success, msg = decide_by_ref_code(db, ref_code, ApprovalDecision.REJECTED)
    if not success:
        raise HTTPException(status_code=400, detail=msg)
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/api/export")
def export_csv(request: Request, db: Session = Depends(get_db)):
    _check_auth(request)
    import csv
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "company", "title", "location", "score", "decision", "status", "url", "created_at"])
    for job in db.query(Job).order_by(Job.created_at.desc()).all():
        writer.writerow([
            job.id, job.company, job.title, job.location,
            job.score_total, job.decision, job.status.value if job.status else "",
            job.canonical_url, job.created_at,
        ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=jobs_export.csv"},
    )


# --- WhatsApp Webhook ---

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
        from app.whatsapp import WhatsAppClient
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not WhatsAppClient.verify_webhook_signature(body, sig, settings.whatsapp_app_secret):
            raise HTTPException(status_code=403, detail="Invalid webhook signature")

    from app.whatsapp import WhatsAppClient
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
        metadata_={"message_id": message_id, "text": msg.get("text", ""), "decision": decision_text, "ref_code": ref_code},
    )
    db.add(event)
    db.commit()

    if decision_text and ref_code:
        decision_enum = ApprovalDecision.APPROVED if decision_text == "APPROVED" else ApprovalDecision.REJECTED
        success, approval_msg = decide_by_ref_code(db, ref_code, decision_enum)
        return {"status": "processed", "decision": decision_text, "ref_code": ref_code, "approval_result": approval_msg, "success": success}
    elif decision_text and not ref_code:
        return {"status": "ambiguous", "decision": decision_text, "error": "No ref code provided — reply with APPROVE <ref_code> or REJECT <ref_code>"}

    return {"status": "processed", "decision": decision_text}


@app.post("/api/retry-prepare/{job_id}")
def retry_prepare(
    job_id: int, request: Request,
    csrf_token: str = Form(""),
    db: Session = Depends(get_db),
):
    _check_auth(request)
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

    return RedirectResponse(url="/dashboard", status_code=303)


# --- Worker Endpoints ---

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
        app = db.query(Application).filter(Application.job_id == j.id).first()
        result.append({
            "id": j.id,
            "company": j.company,
            "title": j.title,
            "url": j.official_url or j.canonical_url,
            "score": j.score_total,
            "application_id": app.id if app else None,
            "application_key": application_slug(j.company, j.title, app.id if app else None),
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
