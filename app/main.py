"""FastAPI application with dashboard and API endpoints."""

import hashlib
import hmac
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends, HTTPException, Request, Form, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from itsdangerous import URLSafeTimedSerializer

from app.config import settings
from app.database import get_db, engine, Base
from app.models import Job, Application, Approval, Event, SourceRun, JobStatus, ApprovalDecision
from app.csv_import import import_csv
from app.discovery import run_orchestrator
from app.pipeline import ingest_discovered_job, verify_job, score_and_decide, create_application_for_shortlisted
from app.approval import create_approval, validate_and_decide, decide_by_ref_code, ApprovalChannel
from app.state_machine import InvalidTransitionError

app = FastAPI(title="Maviz AI Job Hunter", version="1.0.0")

templates_dir = Path(__file__).parent / "templates"
templates_dir.mkdir(exist_ok=True)
templates = Jinja2Templates(directory=str(templates_dir))

signer = URLSafeTimedSerializer(settings.secret_key)

CSRF_TOKEN_FIELD = "csrf_token"


def _generate_csrf() -> str:
    return secrets.token_urlsafe(32)


def _verify_csrf(request_token: str, session_token: str) -> bool:
    return hmac.compare_digest(request_token, session_token)


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
        db.query(Job).filter(Job.status == JobStatus.WAITING_APPROVAL)
        .order_by(Job.score_total.desc()).limit(10).all()
    )
    return templates.TemplateResponse(request=request, name="dashboard.html", context={
        "total_jobs": total_jobs,
        "by_status": by_status,
        "total_apps": total_apps,
        "recent_events": recent_events,
        "last_run": last_run,
        "blocked_jobs": blocked_jobs,
        "pending_approvals": pending_approvals,
        "waiting_jobs": waiting_jobs,
        "csrf_token": _generate_csrf(),
    })


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


# --- Worker Endpoint ---

@app.get("/api/worker/jobs")
def worker_pending_jobs(request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Worker-Token", "")
    if not hmac.compare_digest(token, settings.worker_token):
        raise HTTPException(status_code=403, detail="Invalid worker token")
    jobs = (
        db.query(Job)
        .filter(Job.status == JobStatus.READY_TO_SUBMIT)
        .order_by(Job.score_total.desc())
        .limit(10)
        .all()
    )
    return [
        {
            "id": j.id,
            "company": j.company,
            "title": j.title,
            "url": j.canonical_url,
            "score": j.score_total,
        }
        for j in jobs
    ]


@app.post("/api/worker/submit/{job_id}")
def worker_submit_result(job_id: int, request: Request, db: Session = Depends(get_db)):
    token = request.headers.get("X-Worker-Token", "")
    if not hmac.compare_digest(token, settings.worker_token):
        raise HTTPException(status_code=403, detail="Invalid worker token")
    job = db.query(Job).filter(Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return {"status": "ok", "job_id": job.id}
