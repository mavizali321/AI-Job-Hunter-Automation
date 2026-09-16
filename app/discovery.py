"""Async discovery orchestrator — runs search providers + ATS adapters, verifies, filters, ingests."""

import asyncio
import logging
from datetime import datetime, timezone, timedelta

from sqlalchemy.orm import Session

from app.adapters.base import DiscoveredJob
from app.adapters.greenhouse import GreenhouseAdapter
from app.adapters.lever import LeverAdapter
from app.adapters.ashby import AshbyAdapter
from app.adapters.generic import GenericCareerPageAdapter
from app.search.base import SearchResult, SearchProvider
from app.search.resolver import is_aggregator_url, resolve_official_url, is_official_url
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

DISCOVERY_ONLY_SOURCES = {"linkedin", "indeed", "serper", "adzuna", "arbeitnow", "remotive"}

AGGREGATOR_NEVER_SUBMIT = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "google.com", "adzuna.com", "arbeitnow.com", "remotive.com",
}


def _generate_search_queries() -> list[tuple[str, str]]:
    titles = settings.job_titles_list
    locations = settings.job_locations_list
    queries = []
    for title in titles:
        for loc in locations:
            queries.append((f"{title} {loc}", loc))
    return queries


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


def build_search_providers() -> list[SearchProvider]:
    providers: list[SearchProvider] = []
    if settings.serper_api_key:
        from app.search.serper import SerperProvider
        providers.append(SerperProvider(api_key=settings.serper_api_key))
    if settings.adzuna_app_id and settings.adzuna_api_key:
        from app.search.adzuna import AdzunaProvider
        providers.append(AdzunaProvider(
            app_id=settings.adzuna_app_id, api_key=settings.adzuna_api_key,
        ))
    from app.search.arbeitnow import ArbeitnowProvider
    providers.append(ArbeitnowProvider())
    from app.search.remotive import RemotiveProvider
    providers.append(RemotiveProvider())
    return providers


async def _run_adapter(adapter, search_terms: list[str]) -> tuple[str, list[DiscoveredJob], str | None]:
    try:
        results = await adapter.discover(search_terms=search_terms, location="Karachi")
        return adapter.name, results, None
    except Exception as e:
        logger.error("Adapter %s failed: %s", adapter.name, e)
        return adapter.name, [], str(e)


async def _run_search_provider(
    provider: SearchProvider,
    queries: list[tuple[str, str]],
    max_per_query: int = 10,
) -> tuple[str, list[SearchResult], str | None]:
    try:
        all_results: list[SearchResult] = []
        for query, location in queries:
            results = await provider.search(query, location=location, max_results=max_per_query)
            all_results.extend(results)
        return provider.name, all_results, None
    except Exception as e:
        logger.error("Search provider %s failed: %s", provider.name, e)
        return provider.name, [], str(e)


def _search_result_to_discovered(sr: SearchResult) -> DiscoveredJob:
    return DiscoveredJob(
        title=sr.title,
        company=sr.company,
        url=sr.url,
        source=sr.source,
        location=sr.location,
        remote_policy=sr.remote_policy,
        description=sr.snippet,
        salary=sr.salary,
        posted_date=sr.posted_date,
    )


async def _verify_via_adapter(adapter, url: str) -> tuple[bool, str]:
    try:
        result = await adapter.verify(url)
        if result and result.description:
            return True, result.description
        return result is not None, ""
    except Exception as e:
        logger.error("Verify failed for %s: %s", url, e)
        return False, ""


async def discover_all(
    adapters: list | None = None,
    search_providers: list[SearchProvider] | None = None,
) -> tuple[list[DiscoveredJob], dict[str, str]]:
    if adapters is None:
        adapters = build_adapters()
    if search_providers is None:
        search_providers = build_search_providers()

    adapter_search_terms = [t for t in settings.job_titles_list[:5]]

    adapter_tasks = [_run_adapter(a, adapter_search_terms) for a in adapters]

    queries = _generate_search_queries()
    max_per_query = max(1, settings.max_results_per_run // max(len(queries), 1))
    provider_tasks = [
        _run_search_provider(p, queries, max_per_query=max_per_query)
        for p in search_providers
    ]

    all_tasks = adapter_tasks + provider_tasks
    if not all_tasks:
        return [], {}

    results = await asyncio.gather(*all_tasks)

    all_jobs: list[DiscoveredJob] = []
    errors: dict[str, str] = {}
    for name, items, error in results:
        if isinstance(items, list) and items:
            if isinstance(items[0], SearchResult):
                all_jobs.extend(_search_result_to_discovered(sr) for sr in items)
            else:
                all_jobs.extend(items)
        if error:
            errors[name] = error
    return all_jobs, errors


async def discover_and_verify(
    adapters: list,
    search_providers: list[SearchProvider] | None = None,
) -> tuple[list[tuple[DiscoveredJob, dict]], dict[str, str]]:
    all_jobs, adapter_errors = await discover_all(adapters, search_providers)

    adapter_map = {a.name: a for a in adapters}

    verified_results: list[tuple[DiscoveredJob, dict]] = []
    for dj in all_jobs:
        verify_url = dj.url
        official_url = None
        official_method = None

        if is_aggregator_url(dj.url):
            resolved, method = await resolve_official_url(dj.url, dj.title, dj.company)
            if resolved:
                official_url = resolved
                official_method = method
                verify_url = resolved
            else:
                official_method = "unresolved"

        adapter = adapter_map.get(dj.source)
        if adapter:
            http_success, verified_content = await _verify_via_adapter(adapter, verify_url)
        else:
            http_success, verified_content = await _verify_url_directly(verify_url)

        evidence = collect_verification_evidence(
            url=verify_url,
            posted_date=dj.posted_date,
            location=dj.location,
            description=dj.description,
            verified_content=verified_content,
            http_success=http_success,
        )
        evidence["adapter"] = dj.source
        evidence["discovery_url"] = dj.url
        if official_url:
            evidence["official_url"] = official_url
            evidence["official_url_method"] = official_method
        elif official_method == "unresolved":
            evidence["official_url_unresolved"] = True

        verified_results.append((dj, evidence))

    return verified_results, adapter_errors


async def _verify_url_directly(url: str) -> tuple[bool, str]:
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                return True, resp.text[:5000]
    except Exception:
        pass
    return False, ""


def is_fresh(job: DiscoveredJob, window_days: int | None = None) -> bool:
    if not job.posted_date:
        return False
    window = window_days if window_days is not None else settings.search_window_days
    cutoff = datetime.now(timezone.utc) - timedelta(days=window)
    posted = job.posted_date
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=timezone.utc)
    return posted >= cutoff


def is_discovery_only_source(source: str) -> bool:
    return source.lower() in DISCOVERY_ONLY_SOURCES


def _send_first_approval(db: Session, job: Job, application: Application) -> dict:
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


def run_orchestrator(
    db: Session,
    adapters: list | None = None,
    search_providers: list[SearchProvider] | None = None,
) -> dict:
    if adapters is None:
        adapters = build_adapters()

    run = SourceRun(source="orchestrator", started_at=datetime.now(timezone.utc))
    db.add(run)
    db.commit()

    try:
        verified_results, adapter_errors = asyncio.run(
            discover_and_verify(adapters, search_providers)
        )

        fresh_results = [(dj, ev) for dj, ev in verified_results if is_fresh(dj)]

        ingested = 0
        skipped_dedup = 0
        skipped_reject = 0
        skipped_aggregator = 0

        ingested_jobs: list[tuple[Job, dict]] = []

        for dj, evidence in fresh_results:
            rejection = hard_reject(dj.title, dj.description, dj.location, dj.requirements)
            if rejection:
                skipped_reject += 1
                continue

            official_url = evidence.get("official_url")
            unresolved = evidence.get("official_url_unresolved", False)
            use_url = official_url if official_url else dj.url

            if is_aggregator_url(use_url):
                skipped_aggregator += 1
                continue

            job = ingest_discovered_job(
                db,
                title=dj.title,
                company=dj.company,
                url=use_url,
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
                job.discovery_url = dj.url
                job.discovery_source = dj.source
                if official_url:
                    job.official_url = official_url
                    job.official_url_method = evidence.get("official_url_method")
                if unresolved:
                    job.official_url_method = "unresolved"
                db.commit()
                ingested += 1
                ingested_jobs.append((job, evidence))
            else:
                skipped_dedup += 1

        verified_count = 0
        blocked_count = 0
        for job, evidence in ingested_jobs:
            if job.status == JobStatus.DISCOVERED:
                if evidence.get("official_url_unresolved"):
                    evidence["url_is_official"] = False
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
            "ingested": ingested,
            "skipped_dedup": skipped_dedup,
            "skipped_reject": skipped_reject,
            "skipped_aggregator": skipped_aggregator,
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
