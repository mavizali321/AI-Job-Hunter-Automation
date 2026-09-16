"""Job processing pipeline: discover -> verify -> deduplicate -> score -> shortlist."""

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app.models import Job, Application, JobStatus, canonicalize_url, content_hash
from app.scoring import score_job
from app.state_machine import transition_job
from app.config import settings

AGGREGATOR_DOMAINS = {"linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "google.com"}

CLOSED_MARKERS = [
    "no longer accepting", "position filled", "position has been filled",
    "this job is closed", "this position is closed", "expired",
    "no longer available", "this role has been filled",
]

PAKISTAN_MARKERS = {"pakistan", "karachi", "lahore", "islamabad"}
REMOTE_MARKERS = {"remote", "worldwide", "global", "anywhere"}


def ingest_discovered_job(
    db: Session,
    title: str,
    company: str,
    url: str,
    source: str,
    location: str = "",
    remote_policy: str = "",
    description: str = "",
    requirements: str = "",
    salary: str = "",
    posted_date: datetime | None = None,
    external_id: str = "",
    requisition_id: str = "",
) -> Job | None:
    canonical = canonicalize_url(url)
    desc_hash = content_hash(f"{company}|{title}|{description[:500]}")

    existing = None
    if canonical:
        existing = db.query(Job).filter(Job.canonical_url == canonical).first()
    if not existing:
        existing = (
            db.query(Job)
            .filter(Job.company == company, Job.title == title, Job.description_hash == desc_hash)
            .first()
        )

    if existing:
        return None

    job = Job(
        canonical_url=canonical or f"discovered-{desc_hash}",
        source=source,
        external_id=external_id,
        requisition_id=requisition_id,
        company=company,
        title=title,
        location=location,
        remote_policy=remote_policy,
        description=description,
        requirements=requirements,
        salary=salary,
        posted_date=posted_date,
        description_hash=desc_hash,
        status=JobStatus.DISCOVERED,
    )
    db.add(job)
    db.commit()
    return job


def collect_verification_evidence(url: str, posted_date: datetime | None,
                                  location: str, description: str,
                                  verified_content: str | None = None,
                                  http_success: bool = False) -> dict:
    evidence = {"verified_at": datetime.now(timezone.utc).isoformat()}

    evidence["http_success"] = http_success

    parsed = urlparse(url)
    evidence["url_is_official"] = bool(parsed.scheme and parsed.netloc) and not any(
        parsed.netloc.endswith(d) for d in AGGREGATOR_DOMAINS
    )

    content = verified_content or description or ""
    evidence["has_content"] = len(content.strip()) > 50

    content_lower = content.lower()
    evidence["listing_closed"] = any(m in content_lower for m in CLOSED_MARKERS)

    evidence["has_posted_date"] = posted_date is not None

    all_text = content_lower + " " + (location or "").lower()
    evidence["location_eligible"] = (
        any(m in all_text for m in PAKISTAN_MARKERS) or
        any(m in all_text for m in REMOTE_MARKERS)
    )

    return evidence


def compute_verification_blockers(evidence: dict) -> list[str]:
    blockers = []
    if not evidence.get("http_success"):
        blockers.append("URL verification failed (HTTP error or unreachable)")
    if not evidence.get("url_is_official"):
        blockers.append("URL is not an official company/ATS page")
    if not evidence.get("has_content"):
        blockers.append("No job content found at URL")
    if evidence.get("listing_closed"):
        blockers.append("Listing appears closed or expired")
    if not evidence.get("has_posted_date"):
        blockers.append("No posting date evidence — cannot confirm freshness")
    if not evidence.get("location_eligible"):
        blockers.append("No Pakistan/remote eligibility evidence in listing")
    return blockers


def verify_job(db: Session, job: Job, evidence: dict | None = None) -> bool:
    if job.status != JobStatus.DISCOVERED:
        return False

    if evidence is None:
        evidence = collect_verification_evidence(
            url=job.canonical_url,
            posted_date=job.posted_date,
            location=job.location or "",
            description=job.description or "",
            http_success=False,
        )

    blockers = compute_verification_blockers(evidence)

    job.verification_evidence = evidence

    if blockers:
        job.verification_blockers = "; ".join(blockers)
        transition_job(db, job, JobStatus.MANUAL_ACTION_REQUIRED, {"blockers": blockers})
        db.commit()
        return False

    job.verification_blockers = None
    job.verified_at = datetime.now(timezone.utc)
    transition_job(db, job, JobStatus.VERIFIED)
    db.commit()
    return True


def score_and_decide(db: Session, job: Job) -> dict:
    if job.status != JobStatus.VERIFIED:
        return {}

    result = score_job(
        title=job.title,
        company=job.company,
        description=job.description,
        requirements=job.requirements,
        location=job.location,
        remote_policy=job.remote_policy,
    )

    breakdown = result.get("breakdown", {})
    job.score_role_relevance = breakdown.get("role_relevance", {}).get("score")
    job.score_technical_match = breakdown.get("technical_match", {}).get("score")
    job.score_experience_fit = breakdown.get("experience_fit", {}).get("score")
    job.score_location_fit = breakdown.get("location_fit", {}).get("score")
    job.score_company_quality = breakdown.get("company_quality", {}).get("score")
    job.score_entry_level = breakdown.get("entry_level", {}).get("score")
    job.score_total = result["total"]
    job.score_reasons = result["breakdown"]
    job.decision = result["decision"]

    transition_job(db, job, JobStatus.SCORED, {"score": result["total"], "decision": result["decision"]})

    if result["total"] >= settings.approval_threshold:
        transition_job(db, job, JobStatus.SHORTLISTED)
    elif result["decision"] == "SKIP" or result.get("rejection_reason"):
        transition_job(db, job, JobStatus.SKIPPED, {"reason": result.get("rejection_reason", "Below threshold")})

    db.commit()
    return result


def create_application_for_shortlisted(db: Session, job: Job) -> Application | None:
    if job.status != JobStatus.SHORTLISTED:
        return None

    existing = db.query(Application).filter(Application.job_id == job.id).first()
    if existing:
        return existing

    application = Application(
        job_id=job.id,
        resume_variant="profile/Maviz-Ali-Resume-Original.pdf",
        state=JobStatus.WAITING_APPROVAL,
    )
    db.add(application)
    transition_job(db, job, JobStatus.WAITING_APPROVAL)
    db.commit()
    return application


def prepare_application(db: Session, job: Job, application: Application) -> Path | None:
    if job.status != JobStatus.APPROVED:
        return None

    transition_job(db, job, JobStatus.PREPARING)
    application.state = JobStatus.PREPARING
    db.commit()

    slug = f"{job.company}-{job.title}".lower().replace(" ", "-")[:60]
    app_dir = Path("applications") / slug
    app_dir.mkdir(parents=True, exist_ok=True)

    snapshot = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "url": job.canonical_url,
        "score": job.score_total,
        "decision": job.decision,
        "description": (job.description or "")[:2000],
        "requirements": job.requirements,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }
    (app_dir / "job_snapshot.json").write_text(json.dumps(snapshot, indent=2))

    resume_src = Path("profile/Maviz-Ali-Resume-Original.pdf")
    if resume_src.exists():
        shutil.copy2(resume_src, app_dir / "resume.pdf")

    answers = {
        "full_name": "Maviz Ali",
        "email": "maviz.ali92@gmail.com",
        "phone": "+92 336 1321040",
        "linkedin": "linkedin.com/in/mavizali",
        "resume": str(app_dir / "resume.pdf"),
    }
    application.answer_set_version = content_hash(json.dumps(answers, sort_keys=True))
    (app_dir / "answers.json").write_text(json.dumps(answers, indent=2))

    transition_job(db, job, JobStatus.READY_TO_SUBMIT)
    application.state = JobStatus.READY_TO_SUBMIT
    db.commit()

    result = _send_final_approval(db, job, application, snapshot, answers)

    transition_job(db, job, JobStatus.AWAITING_FINAL_APPROVAL)
    application.state = JobStatus.AWAITING_FINAL_APPROVAL
    db.commit()

    return app_dir


def _send_final_approval(db: Session, job: Job, application: Application, snapshot: dict, answers: dict) -> dict:
    """Auto-create second (final) approval after preparation and send WhatsApp."""
    from app.approval import create_final_approval, ApprovalChannel
    from app.whatsapp import WhatsAppClient
    from local_worker.worker import SubmissionGuard

    guard = SubmissionGuard()
    manifest_hash = guard.compute_manifest_hash(
        snapshot, application.resume_variant or "profile/Maviz-Ali-Resume-Original.pdf", answers,
    )

    client = WhatsAppClient()
    channel = ApprovalChannel.WHATSAPP if client.configured else ApprovalChannel.DASHBOARD

    approval, token = create_final_approval(db, application, job, manifest_hash, channel=channel)

    if client.configured:
        try:
            result = client.send_approval_message_sync(
                company=job.company,
                role=job.title,
                location=job.location or "",
                score=job.score_total or 0,
                url=job.canonical_url,
                ref_code=approval.approval_ref_code,
                approval_type="final",
                manifest_hash=manifest_hash,
            )
            approval.whatsapp_delivery_status = "sent"
            db.commit()
            return {"status": "sent", "ref_code": approval.approval_ref_code, "whatsapp": result}
        except Exception as e:
            approval.whatsapp_delivery_status = f"failed: {str(e)[:80]}"
            db.commit()
            return {"status": "whatsapp_failed", "ref_code": approval.approval_ref_code, "error": str(e)}
    return {"status": "dashboard_only", "ref_code": approval.approval_ref_code}
