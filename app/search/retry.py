"""HTTP request retry with exponential backoff for transient errors."""

import asyncio
import logging

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3
BASE_DELAY = 1.0


async def request_with_retry(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_retries: int = MAX_RETRIES,
    base_delay: float = BASE_DELAY,
    **kwargs,
) -> httpx.Response:
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await getattr(client, method)(url, **kwargs)
            if resp.status_code in RETRYABLE_STATUS and attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                if resp.status_code == 429:
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after and retry_after.isdigit():
                        delay = max(delay, float(retry_after))
                logger.warning(
                    "Retry %d/%d for %s %s (HTTP %d), waiting %.1fs",
                    attempt + 1, max_retries, method.upper(), url,
                    resp.status_code, delay,
                )
                await asyncio.sleep(delay)
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(
                    "Retry %d/%d for %s %s (%s), waiting %.1fs",
                    attempt + 1, max_retries, method.upper(), url,
                    type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
                continue
            raise
    raise last_exc  # type: ignore[misc]


def format_provider_error(exc: Exception) -> str:
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, httpx.TimeoutException):
        return f"Timeout: {exc}"
    if isinstance(exc, httpx.ConnectError):
        return f"Connection error: {exc}"
    return f"{type(exc).__name__}: {exc}"
