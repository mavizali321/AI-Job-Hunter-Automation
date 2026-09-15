"""Pluggable OpenAI-compatible LLM client with deterministic fallback."""

import json
import hashlib
from functools import lru_cache

from app.config import settings

_CACHE: dict[str, dict] = {}


def _cache_key(content_hash: str, task: str) -> str:
    return hashlib.sha256(f"{content_hash}:{task}".encode()).hexdigest()[:16]


async def extract_job_details(description: str, content_hash: str) -> dict:
    cache_key = _cache_key(content_hash, "extract")
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    if not settings.llm_api_key:
        result = _deterministic_extract(description)
        _CACHE[cache_key] = result
        return result

    try:
        import openai
        client = openai.AsyncOpenAI(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
        )
        response = await client.chat.completions.create(
            model=settings.llm_model,
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
                {"role": "user", "content": description[:4000]},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=1000,
        )
        text = response.choices[0].message.content
        result = json.loads(text)
        _CACHE[cache_key] = result
        return result
    except Exception:
        result = _deterministic_extract(description)
        _CACHE[cache_key] = result
        return result


EXTRACT_SYSTEM_PROMPT = """Extract structured job details from the description. Return JSON:
{
  "title": "string",
  "company": "string",
  "location": "string",
  "remote_policy": "onsite|hybrid|remote|unknown",
  "requirements": ["string"],
  "years_required": 0,
  "salary": "string or null",
  "is_senior": false,
  "key_technologies": ["string"],
  "employment_type": "full-time|part-time|contract|internship|unknown",
  "sponsorship_available": false,
  "pakistan_eligible": false
}
Only include facts stated in the text. Do not infer or fabricate."""


def _deterministic_extract(description: str) -> dict:
    import re
    text = description.lower()
    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text)
    return {
        "title": "",
        "company": "",
        "location": "",
        "remote_policy": "remote" if "remote" in text else "unknown",
        "requirements": [],
        "years_required": max((int(y) for y in years_match), default=0),
        "salary": None,
        "is_senior": any(s in text for s in ["senior", "staff", "principal", "lead"]),
        "key_technologies": [],
        "employment_type": "unknown",
        "sponsorship_available": "sponsorship" in text,
        "pakistan_eligible": "pakistan" in text,
    }
