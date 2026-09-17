"""Local browser worker that polls approved jobs and fills ATS forms."""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx


class SubmissionGuard:
    @staticmethod
    def compute_manifest_hash(job_snapshot: dict, resume_path: str, answers: dict) -> str:
        payload = json.dumps({
            "job": {k: job_snapshot.get(k) for k in sorted(job_snapshot.keys())},
            "resume_sha256": _file_hash(resume_path) if Path(resume_path).exists() else "",
            "answers": {k: answers.get(k) for k in sorted(answers.keys())},
        }, sort_keys=True)
        return hashlib.sha256(payload.encode()).hexdigest()

    @staticmethod
    def verify_manifest(current_hash: str, approved_hash: str) -> bool:
        return current_hash == approved_hash


def _file_hash(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _detect_ats(url: str) -> str:
    host = urlparse(url).netloc.lower()
    if "greenhouse.io" in host or "boards.greenhouse" in host:
        return "greenhouse"
    if "lever.co" in host or "jobs.lever" in host:
        return "lever"
    if "ashbyhq.com" in host or "jobs.ashby" in host:
        return "ashby"
    return "generic"


class LocalWorker:
    def __init__(self, api_url: str, worker_token: str,
                 browser_profile: str = "./browser_profile",
                 headless: bool = False,
                 applications_dir: str = "./applications"):
        self.api_url = api_url.rstrip("/")
        self.worker_token = worker_token
        self.browser_profile = browser_profile
        self.headless = headless
        self.applications_dir = applications_dir
        self.guard = SubmissionGuard()

    def _safe_app_dir(self, application_key: str) -> Path | None:
        """Resolve application directory, rejecting path traversal."""
        if not application_key:
            return None
        if "/" in application_key or "\\" in application_key or ".." in application_key:
            return None
        base = Path(self.applications_dir).resolve()
        target = (base / application_key).resolve()
        try:
            target.relative_to(base)
        except ValueError:
            return None
        return target

    def run_once(self) -> dict | None:
        """Poll for one job, process it, return the result (or None if no jobs)."""
        import asyncio
        import logging
        log = logging.getLogger("local_worker")

        jobs = self.poll_jobs()
        if not jobs:
            return None

        job = jobs[0]
        app_dir = self._safe_app_dir(job.get("application_key", ""))
        if not app_dir:
            log.warning("Invalid application_key for job %d, skipping", job["id"])
            return None
        if not app_dir.exists():
            log.warning("Application dir %s not found for job %d", app_dir, job["id"])
            return None

        log.info("Processing job %d: %s at %s", job["id"], job.get("title", ""), job.get("company", ""))
        result = asyncio.run(self.fill_and_submit(job, app_dir))
        log.info("Job %d outcome: %s", job["id"], result.get("outcome"))
        return result

    def poll_jobs(self) -> list[dict]:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{self.api_url}/api/worker/jobs",
                headers={"X-Worker-Token": self.worker_token},
            )
            if resp.status_code == 200:
                return resp.json()
        return []

    def authorize_submission(self, job_id: int, manifest_hash: str) -> dict:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self.api_url}/api/worker/authorize/{job_id}",
                headers={"X-Worker-Token": self.worker_token},
                json={"manifest_hash": manifest_hash},
            )
            if resp.status_code == 200:
                return resp.json()
            return {"error": resp.text, "status_code": resp.status_code}

    async def fill_and_submit(self, job: dict, app_dir: Path) -> dict:
        snapshot_path = app_dir / "job_snapshot.json"
        answers_path = app_dir / "answers.json"
        resume_path = app_dir / "resume.pdf"

        if not all(p.exists() for p in [snapshot_path, answers_path, resume_path]):
            return {"outcome": "FAILED", "reason": "Missing application files"}

        snapshot = json.loads(snapshot_path.read_text())
        answers = json.loads(answers_path.read_text())

        manifest_hash = self.guard.compute_manifest_hash(
            snapshot, str(resume_path), answers
        )

        auth = self.authorize_submission(job["id"], manifest_hash)
        if "error" in auth:
            return {
                "outcome": "FAILED",
                "reason": f"Authorization failed: {auth.get('error', '')}",
            }

        nonce = auth["nonce"]
        url = job.get("url", "")
        if not url:
            return {"outcome": "FAILED", "reason": "No job URL"}

        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"outcome": "FAILED", "reason": "Playwright not installed"}

        ats = _detect_ats(url)
        playwright_instance = None
        browser = None

        try:
            playwright_instance = await async_playwright().start()
            browser = await playwright_instance.chromium.launch_persistent_context(
                self.browser_profile, headless=self.headless,
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()

            await page.goto(url, timeout=30000)
            await page.wait_for_load_state("networkidle", timeout=15000)

            captcha = await page.query_selector(
                "[class*='captcha'], [id*='captcha'], iframe[src*='captcha']"
            )
            if captcha:
                self.report_result(job["id"], {
                    "nonce": nonce, "manifest_hash": manifest_hash,
                    "outcome": "MANUAL_ACTION_REQUIRED",
                    "reason": "CAPTCHA detected",
                })
                return {
                    "outcome": "MANUAL_ACTION_REQUIRED",
                    "reason": "CAPTCHA detected",
                }

            login_form = await page.query_selector("input[type='password']")
            if login_form:
                self.report_result(job["id"], {
                    "nonce": nonce, "manifest_hash": manifest_hash,
                    "outcome": "MANUAL_ACTION_REQUIRED",
                    "reason": "Login required",
                })
                return {
                    "outcome": "MANUAL_ACTION_REQUIRED",
                    "reason": "Login required",
                }

            await self._fill_ats_fields(page, ats, answers, str(resume_path))

            result = await self._click_submit(page, ats, job)
            result["nonce"] = nonce
            result["manifest_hash"] = manifest_hash
            self.report_result(job["id"], result)
            return result

        except Exception as e:
            return {"outcome": "FAILED", "reason": str(e)}
        finally:
            if browser:
                try:
                    await browser.close()
                except Exception:
                    pass
            if playwright_instance:
                try:
                    await playwright_instance.stop()
                except Exception:
                    pass

    async def _fill_ats_fields(self, page, ats: str, answers: dict,
                               resume_path: str):
        if ats == "greenhouse":
            await self._fill_greenhouse(page, answers, resume_path)
        elif ats == "lever":
            await self._fill_lever(page, answers, resume_path)
        elif ats == "ashby":
            await self._fill_ashby(page, answers, resume_path)
        else:
            await self._fill_generic(page, answers, resume_path)

    async def _fill_greenhouse(self, page, answers: dict, resume_path: str):
        field_map = {
            "full_name": "#first_name",
            "first_name": "#first_name",
            "last_name": "#last_name",
            "email": "#email",
            "phone": "#phone",
            "linkedin": "input[name*='linkedin' i], input[id*='linkedin' i]",
        }
        for key, selector in field_map.items():
            value = answers.get(key)
            if value:
                el = await page.query_selector(selector)
                if el:
                    await el.fill(str(value))

        file_input = await page.query_selector("input[type='file']")
        if file_input and answers.get("resume"):
            await file_input.set_input_files(resume_path)

    async def _fill_lever(self, page, answers: dict, resume_path: str):
        field_map = {
            "full_name": "input[name='name']",
            "email": "input[name='email']",
            "phone": "input[name='phone']",
            "linkedin": "input[name='urls[LinkedIn]'], input[name*='linkedin' i]",
        }
        for key, selector in field_map.items():
            value = answers.get(key)
            if value:
                el = await page.query_selector(selector)
                if el:
                    await el.fill(str(value))

        file_input = await page.query_selector("input[type='file']")
        if file_input and answers.get("resume"):
            await file_input.set_input_files(resume_path)

    async def _fill_ashby(self, page, answers: dict, resume_path: str):
        field_map = {
            "full_name": "input[name*='name' i]",
            "email": "input[name*='email' i], input[type='email']",
            "phone": "input[name*='phone' i], input[type='tel']",
            "linkedin": "input[name*='linkedin' i]",
        }
        for key, selector in field_map.items():
            value = answers.get(key)
            if value:
                el = await page.query_selector(selector)
                if el:
                    await el.fill(str(value))

        file_input = await page.query_selector("input[type='file']")
        if file_input and answers.get("resume"):
            await file_input.set_input_files(resume_path)

    async def _fill_generic(self, page, answers: dict, resume_path: str):
        for field_name, value in answers.items():
            if field_name == "resume":
                file_inputs = await page.query_selector_all("input[type='file']")
                for fi in file_inputs:
                    await fi.set_input_files(resume_path)
                    break
                continue

            selectors = [
                f"input[name*='{field_name}' i]",
                f"input[id*='{field_name}' i]",
                f"input[placeholder*='{field_name}' i]",
                f"textarea[name*='{field_name}' i]",
            ]
            for sel in selectors:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(str(value))
                    break

    async def _click_submit(self, page, ats: str, job: dict) -> dict:
        submit_selectors = {
            "greenhouse": "input[type='submit'][value*='Submit' i], button[type='submit']",
            "lever": "button[type='submit'], button.postings-btn-submit",
            "ashby": "button[type='submit'], button[data-testid='submit-application']",
            "generic": "button[type='submit'], input[type='submit']",
        }
        selector = submit_selectors.get(ats, submit_selectors["generic"])
        submit_btn = await page.query_selector(selector)

        if not submit_btn:
            return {
                "outcome": "MANUAL_ACTION_REQUIRED",
                "reason": "Submit button not found",
            }

        await submit_btn.click()

        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        content = await page.content()
        content_lower = content.lower()

        confirmed_markers = [
            "thank you", "application received", "successfully submitted",
            "application has been submitted", "we received your application",
        ]
        if any(m in content_lower for m in confirmed_markers):
            return {
                "outcome": "CONFIRMED",
                "confirmation_reference": (
                    f"{ats}-{job.get('id', 'unknown')}-"
                    f"{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
                ),
            }

        return {
            "outcome": "UNCERTAIN",
            "reason": "No confirmation detected after submit",
        }

    def report_result(self, job_id: int, result: dict):
        import logging
        log = logging.getLogger("local_worker")
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                f"{self.api_url}/api/worker/submit/{job_id}",
                headers={"X-Worker-Token": self.worker_token},
                json=result,
            )
            if resp.status_code != 200:
                log.error(
                    "Server rejected submission for job %d: %d %s",
                    job_id, resp.status_code, resp.text[:200],
                )
                return {"error": resp.text, "status_code": resp.status_code}
            return resp.json()


if __name__ == "__main__":
    from local_worker.__main__ import main
    main()
