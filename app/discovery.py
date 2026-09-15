"""Async discovery orchestrator — runs all adapters, verifies, filters, ingests, and processes the full pipeline."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.adapters.base import DiscoveredJob
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.ashby import AshbyAdapter
from app.adapters.generic import GenericCareerPageAdapter
from app.config import settings
from app.models import Job, Application, JobStatus, SourceRun, ApprovalDecision
from app.pipeline import (
    ingest_discovered_job,
    verify_job,
    score_and_decide,
    create_application_for_shortlisted,
    collect_verification_evidence,
)
from app.scoring import hard_reject

logger = logging.getLogger(__name__)

SEARCH_TERMS = [
    "forward deployed engineer",
    "ai engineer",
    "llm engineer",
    "ai deployment",
    "applied ai",
    "ai solutions",
    "ai integration",
    "ai automation",
    "software engineer",
]

DISCOVERY_ONLY_SOURCES = {"linkedin", "indeed"}


def build_adapters() -> list:
    adapters = []
    if settings.greenhouse_tokens_list:
        adapters.append(GreenhouseAdapter(board_tokens=settings.greenhouse_tokens_list))
    if settings.lever_slugs_list:
        adapters.append(LeverAdapter(company_slugs=settings.lever_slugs_list))
    if settings.ashby_slugs_list:
        adapters.append(AshbyAdapter(company_slugs=settings.ashby_slugs_list))
    if settings.career_urls_list:
        adapters.append(GenericCareerPageAdapter(career_urls=settings.career_urls_list))
    return adapters


async def _run_adapter(adapter, search_terms: list[str]) -> tuple[str, list[DiscoveredJob], str | None]:
    try:
        results = await adapter.discover(search_terms=search_terms, location="Karachi")
        return adapter.name, results, None
    except Exception as e:
        logger.error("Adapter %s failed: %s", adapter.name, e)
        return adapter.name, [], str(e)


async def _verify_via_adapter(adapter, url: str) -> tuple[bool, str]:
    """Call adapter.verify(url). Returns (http_success, verified_content)."""
    try:
        result = await adapter.verify(url)
        if result and result.description:
            return True, result.description
        return result is not None, ""
    except Exception as e:
        logger.error("Verify failed for %s: %s", url, e)
        return False, ""


async def discover_all(adapters: list | None = None) -> tuple[list[DiscoveredJob], dict[str, str]]:
    if adapters is None:
        adapters = build_adapters()
    if not adapters:
        return [], {}

    tasks = [_run_adapter(adapter, SEARCH_TERMS) for adapter in adapters]
    results = await asyncio.gather(*tasks)

    all_jobs: list[DiscoveredJob] = []
    errors: dict[str, str] = {}
    for name, jobs, error in results:
        all_jobs.extend(jobs)
        if error:
            errors[name] = error
    return all_jobs, errors


async def discover_and_verify(adapters: list) -> tuple[list[tuple[DiscoveredJob, dict]], dict[str, str]]:
    """Run discovery then verify each job via its originating adapter."""
    all_jobs, adapter_errors = await discover_all(adapters)

    adapter_map = {a.name: a for a in adapters}

    verified_results: list[tuple[DiscoveredJob, dict]] = []
    for dj in all_jobs:
        adapter = adapter_map.get(dj.source)
        if adapter:
            http_success, verified_content = await _verify_via_adapter(adapter, dj.url)
        else:
            http_success, verified_content = False, ""

        evidence = collect_verification_evidence(
            url=dj.url,
            posted_date=dj.posted_date,
            location=dj.location,
            description=dj.description,
            verified_content=verified_content,
            http_success=http_success,
        )
        evidence["adapter"] = dj.source
        verified_results.append((dj, evidence))

    return verified_results, adapter_errors


def is_fresh(job: DiscoveredJob, window_days: int | None = None) -> bool:
    if not job.posted_date:
        return True
    window = window_days if window_days is not None else settings.search_window_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=window)
    posted = job.posted_date
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= cutoff


def is_discovery_only_source(source: str) -> bool:
    return source.lower() in DISCOVERY_ONLY_SOURCES


def _send_first_approval(db: Session, job: Job, application: Application) -> dict:
    """Create first approval and send WhatsApp message. Returns status dict."""
    from app.approval import create_approval, ApprovalChannel
    from app.whatsapp import WhatsAppClient

    channel = ApprovalChannel.WHATSAPP
    client = WhatsAppClient()
    if not client.configured:
        channel = ApprovalChannel.DASHBOARD

    approval, token = create_approval(db, application, job, channel=channel)

    if client.configured:
        try:
            result = client.send_approval_message_sync(
                company=job.company,
                role=job.title,
                location=job.location or "",
                score=job.score_total or 0,
                url=job.canonical_url,
                ref_code=approval.approval_ref_code,
                salary=job.salary,
                approval_type="first",
            )
            approval.whatsapp_delivery_status = "sent"
            db.commit()
            return {"status": "sent", "ref_code": approval.approval_ref_code, "whatsapp": result}
        except Exception as e:
            approval.whatsapp_delivery_status = f"failed: {str(e)[:80]}"
            db.commit()
            return {"status": "whatsapp_failed", "ref_code": approval.approval_ref_code, "error": str(e)}
    else:
        return {"status": "dashboard_only", "ref_code": approval.approval_ref_code}


def run_orchestrator(db: Session, adapters: list | None = None) -> dict:
    if adapters is None:
        adapters = build_adapters()

    run = SourceRun(source="orchestrator", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()

    try:
        verified_results, adapter_errors = asyncio.run(discover_and_verify(adapters))

        fresh_results = [(dj, ev) for dj, ev in verified_results if is_fresh(dj)]
        eligible_results = [(dj, ev) for dj, ev in fresh_results if not is_discovery_only_source(dj.source)]

        ingested = 0
        skipped_dedup = 0
        skipped_reject = 0

        ingested_jobs: list[tuple[Job, dict]] = []

        for dj, evidence in eligible_results:
            rejection = hard_reject(dj.title, dj.description, dj.location, dj.requirements)
            if rejection:
                skipped_reject += 1
                continue

            job = ingest_discovered_job(
                db,
                title=dj.title,
                company=dj.company,
                url=dj.url,
                source=dj.source,
                location=dj.location,
                remote_policy=dj.remote_policy,
                description=dj.description,
                requirements=dj.requirements,
                salary=dj.salary,
                posted_date=dj.posted_date,
                external_id=dj.external_id,
                requisition_id=dj.requisition_id,
            )
            if job:
                ingested += 1
                ingested_jobs.append((job, evidence))
            else:
                skipped_dedup += 1

        verified_count = 0
        blocked_count = 0
        for job, evidence in ingested_jobs:
            if job.status == JobStatus.DISCOVERED:
                if verify_job(db, job, evidence):
                    verified_count += 1
                else:
                    blocked_count += 1

        scored_count = 0
        shortlisted_count = 0
        verified_in_db = db.query(Job).filter(Job.status == JobStatus.VERIFIED).all()
        for job in verified_in_db:
            result = score_and_decide(db, job)
            if result:
                scored_count += 1
                if job.status == JobStatus.SHORTLISTED:
                    shortlisted_count += 1

        apps_created = 0
        approval_results = []
        shortlisted_in_db = db.query(Job).filter(Job.status == JobStatus.SHORTLISTED).all()
        for job in shortlisted_in_db:
            application = create_application_for_shortlisted(db, job)
            if application:
                apps_created += 1
                approval_result = _send_first_approval(db, job, application)
                approval_results.append(approval_result)

        run.jobs_found = len(verified_results)
        run.jobs_new = ingested
        run.status = "COMPLETED"
        run.ended_at = datetime.now(timezone.utc)

        summary = {
            "status": "COMPLETED",
            "discovered": len(verified_results),
            "fresh": len(fresh_results),
            "eligible": len(eligible_results),
            "ingested": ingested,
            "skipped_dedup": skipped_dedup,
            "skipped_reject": skipped_reject,
            "verified": verified_count,
            "blocked": blocked_count,
            "scored": scored_count,
            "shortlisted": shortlisted_count,
            "applications_created": apps_created,
            "approval_results": approval_results,
            "adapter_errors": adapter_errors,
        }

    except Exception as e:
        run.status = "FAILED"
        run.error = str(e)
        run.ended_at = datetime.now(timezone.utc)
        summary = {"status": "FAILED", "error": str(e)}

    db.commit()
    return summary
