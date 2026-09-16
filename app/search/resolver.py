"""Official URL resolution — locate the company/ATS application URL from aggregator results."""

import logging
import re
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)

AGGREGATOR_DOMAINS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "google.com", "adzuna.com", "arbeitnow.com", "remotive.com",
    "jooble.org", "talent.com", "simplyhired.com", "careerjet.com",
}

ATS_DOMAINS = {
    "greenhouse.io", "boards.greenhouse.io",
    "lever.co", "jobs.lever.co",
    "ashbyhq.com", "jobs.ashbyhq.com",
    "workday.com", "myworkdayjobs.com",
    "smartrecruiters.com", "jobs.smartrecruiters.com",
    "icims.com",
    "bamboohr.com",
    "breezy.hr",
    "recruitee.com",
    "pinpointhq.com",
    "jazz.co", "applytojob.com",
}


def is_aggregator_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith(f".{d}") for d in AGGREGATOR_DOMAINS)
    except Exception:
        return False


def is_official_url(url: str) -> bool:
    if not url:
        return False
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return False
    if any(host == d or host.endswith(f".{d}") for d in AGGREGATOR_DOMAINS):
        return False
    return True


def is_ats_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return any(host == d or host.endswith(f".{d}") for d in ATS_DOMAINS)
    except Exception:
        return False


async def resolve_official_url(
    aggregator_url: str,
    title: str = "",
    company: str = "",
) -> tuple[str | None, str]:
    """Attempt to find the official company/ATS job URL from an aggregator link.

    Returns (official_url, method) or (None, reason).
    """
    if is_official_url(aggregator_url):
        return aggregator_url, "already_official"

    try:
        async with httpx.AsyncClient(
            timeout=15, follow_redirects=True, max_redirects=5,
        ) as client:
            resp = await client.get(aggregator_url)
            final_url = str(resp.url)
            if is_official_url(final_url) and final_url != aggregator_url:
                return final_url, "redirect"

            body = resp.text[:30000]
            apply_url = _extract_apply_link(body, company)
            if apply_url and is_official_url(apply_url):
                return apply_url, "page_link"
    except Exception as e:
        logger.debug("resolve_official_url failed for %s: %s", aggregator_url, e)

    if company and title:
        searched = await _search_ats_direct(company, title)
        if searched:
            return searched, "ats_search"

    return None, "unresolved"


def _extract_apply_link(html: str, company: str) -> str | None:
    patterns = [
        r'href=["\']([^"\']*(?:greenhouse|lever|ashby|workday|smartrecruiters|icims|bamboohr|breezy|recruitee)[^"\']*)["\']',
        r'href=["\']([^"\']*(?:/apply|/careers/|/jobs/)[^"\']*)["\']',
    ]
    for pattern in patterns:
        matches = re.findall(pattern, html, re.I)
        for m in matches:
            if m.startswith("http") and is_official_url(m):
                return m
    return None


async def _search_ats_direct(company: str, title: str) -> str | None:
    slug = re.sub(r'[^a-z0-9]', '', company.lower())
    ats_urls = [
        f"https://boards.greenhouse.io/{slug}",
        f"https://jobs.lever.co/{slug}",
        f"https://jobs.ashbyhq.com/{slug}",
    ]
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for url in ats_urls:
            try:
                resp = await client.get(url)
                if resp.status_code == 200 and title.split()[0].lower() in resp.text.lower():
                    return url
            except Exception:
                continue
    return None
