"""Local browser worker that polls approved jobs and fills ATS forms."""

import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx


class SubmissionGuard:
    """Ensures dual approval before any submission."""

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


class LocalWorker:
    def __init__(self, api_url: str, worker_token: str, browser_profile: str = "./browser_profile"):
        self.api_url = api_url.rstrip("/")
        self.worker_token = worker_token
        self.browser_profile = browser_profile
        self.guard = SubmissionGuard()

    def poll_jobs(self) -> list[dict]:
        with httpx.Client(timeout=30) as client:
            resp = client.get(
                f"{self.api_url}/api/worker/jobs",
                headers={"X-Worker-Token": self.worker_token},
            )
            if resp.status_code == 200:
                return resp.json()
        return []

    async def fill_and_submit(self, job: dict, app_dir: Path) -> dict:
        snapshot_path = app_dir / "job_snapshot.json"
        answers_path = app_dir / "answers.json"
        resume_path = app_dir / "resume.pdf"

        if not all(p.exists() for p in [snapshot_path, answers_path, resume_path]):
            return {"status": "FAILED", "reason": "Missing application files"}

        snapshot = json.loads(snapshot_path.read_text())
        answers = json.loads(answers_path.read_text())

        manifest_hash = self.guard.compute_manifest_hash(snapshot, str(resume_path), answers)

        result = await self._fill_form(job, answers, str(resume_path))
        if result.get("status") == "MANUAL_ACTION_REQUIRED":
            return result

        result["manifest_hash"] = manifest_hash
        result["requires_final_approval"] = True
        return result

    async def _fill_form(self, job: dict, answers: dict, resume_path: str) -> dict:
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            return {"status": "FAILED", "reason": "Playwright not installed"}

        url = job.get("url", "")
        if not url:
            return {"status": "FAILED", "reason": "No job URL"}

        async with async_playwright() as p:
            browser = await p.chromium.launch_persistent_context(
                self.browser_profile,
                headless=False,
            )
            page = browser.pages[0] if browser.pages else await browser.new_page()

            try:
                await page.goto(url, timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)

                captcha = await page.query_selector("[class*='captcha'], [id*='captcha'], iframe[src*='captcha']")
                if captcha:
                    return {"status": "MANUAL_ACTION_REQUIRED", "reason": "CAPTCHA detected"}

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

                return {
                    "status": "READY_TO_SUBMIT",
                    "fields_filled": list(answers.keys()),
                    "url": url,
                }
            except Exception as e:
                return {"status": "FAILED", "reason": str(e)}
            finally:
                await browser.close()

    def report_result(self, job_id: int, result: dict):
        with httpx.Client(timeout=30) as client:
            client.post(
                f"{self.api_url}/api/worker/submit/{job_id}",
                headers={"X-Worker-Token": self.worker_token},
                json=result,
            )
