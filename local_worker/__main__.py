"""CLI entry point: python -m local_worker"""

import argparse
import asyncio
import logging
import signal
import sys
import time

from app.config import settings


def main():
    parser = argparse.ArgumentParser(
        description="Local browser worker — polls for FINAL_APPROVED jobs and fills ATS forms",
    )
    parser.add_argument(
        "--once", action="store_true",
        help="Process one eligible job and exit (exit 0 even if none found)",
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Run browser in headless mode",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("local_worker")

    from local_worker.worker import LocalWorker

    worker = LocalWorker(
        api_url=settings.api_base_url,
        worker_token=settings.worker_token,
        browser_profile=settings.browser_profile_dir,
        headless=args.headless,
        applications_dir=settings.applications_dir,
    )

    shutdown_requested = False

    def _handle_signal(signum, frame):
        nonlocal shutdown_requested
        shutdown_requested = True
        log.info("Shutdown requested (signal %s)", signum)

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (OSError, ValueError):
        pass

    if args.once:
        try:
            worker.run_once()
        except Exception as exc:
            log.error("Worker --once failed: %s", exc)
        return

    processing: set[int] = set()

    while not shutdown_requested:
        try:
            jobs = worker.poll_jobs()
        except Exception as exc:
            log.error("Poll failed: %s", exc)
            time.sleep(settings.worker_poll_seconds)
            continue

        for job in jobs:
            if shutdown_requested:
                break
            job_id = job["id"]
            if job_id in processing:
                continue

            app_dir = worker._safe_app_dir(job.get("application_key", ""))
            if not app_dir:
                log.warning("Invalid application_key for job %d, skipping", job_id)
                continue
            if not app_dir.exists():
                log.warning("Application dir %s not found for job %d", app_dir, job_id)
                continue

            processing.add(job_id)
            try:
                log.info(
                    "Processing job %d: %s at %s",
                    job_id, job.get("title", ""), job.get("company", ""),
                )
                result = asyncio.run(worker.fill_and_submit(job, app_dir))
                log.info("Job %d outcome: %s", job_id, result.get("outcome"))
            except Exception as exc:
                log.error("Job %d failed: %s", job_id, exc)
            finally:
                processing.discard(job_id)

        time.sleep(settings.worker_poll_seconds)

    log.info("Worker stopped")


if __name__ == "__main__":
    main()
