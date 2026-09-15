"""WhatsApp Cloud API client for approval messages and webhook handling."""

import hashlib
import hmac
import json
from datetime import datetime, timezone

import httpx

from app.config import settings


WHATSAPP_API_URL = "https://graph.facebook.com/v18.0"


class WhatsAppClient:
    def __init__(self):
        self.phone_number_id = settings.whatsapp_phone_number_id
        self.access_token = settings.whatsapp_access_token
        self.recipient = settings.whatsapp_recipient
        self.verify_token = settings.whatsapp_verify_token

    @property
    def configured(self) -> bool:
        return bool(self.phone_number_id and self.access_token and self.recipient)

    def _build_approval_body(
        self,
        company: str,
        role: str,
        location: str,
        score: float,
        salary: str | None,
        url: str,
        ref_code: str,
        approval_type: str = "first",
        manifest_hash: str | None = None,
    ) -> str:
        if approval_type == "final":
            lines = [
                f"*Final Submission Approval*",
                f"Ref: {ref_code}",
                f"Company: {company}",
                f"Role: {role}",
                f"Location: {location}",
                f"Score: {score}/100",
            ]
            if manifest_hash:
                lines.append(f"Manifest: {manifest_hash[:12]}...")
            lines.append(f"URL: {url}")
            lines.append("")
            lines.append(f"Reply: APPROVE {ref_code} or REJECT {ref_code}")
        else:
            lines = [
                f"*Job Approval Request*",
                f"Ref: {ref_code}",
                f"Company: {company}",
                f"Role: {role}",
                f"Location: {location}",
                f"Score: {score}/100",
            ]
            if salary:
                lines.append(f"Salary: {salary}")
            lines.append(f"URL: {url}")
            lines.append("")
            lines.append(f"Reply: APPROVE {ref_code} or REJECT {ref_code}")

        return "\n".join(lines)

    async def send_approval_message(
        self,
        company: str,
        role: str,
        location: str,
        score: float,
        url: str,
        ref_code: str,
        salary: str | None = None,
        approval_type: str = "first",
        manifest_hash: str | None = None,
    ) -> dict:
        if not self.configured:
            return {"status": "skipped", "reason": "WhatsApp not configured"}

        body = self._build_approval_body(
            company=company, role=role, location=location, score=score,
            salary=salary, url=url, ref_code=ref_code,
            approval_type=approval_type, manifest_hash=manifest_hash,
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{WHATSAPP_API_URL}/{self.phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": self.recipient,
                    "type": "text",
                    "text": {"body": body},
                },
                timeout=30,
            )
            return response.json()

    def send_approval_message_sync(self, **kwargs) -> dict:
        if not self.configured:
            return {"status": "skipped", "reason": "WhatsApp not configured"}

        body = self._build_approval_body(**kwargs)

        with httpx.Client() as client:
            response = client.post(
                f"{WHATSAPP_API_URL}/{self.phone_number_id}/messages",
                headers={
                    "Authorization": f"Bearer {self.access_token}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "to": self.recipient,
                    "type": "text",
                    "text": {"body": body},
                },
                timeout=30,
            )
            return response.json()

    @staticmethod
    def verify_webhook_signature(payload: bytes, signature: str, app_secret: str) -> bool:
        expected = hmac.new(app_secret.encode(), payload, hashlib.sha256).hexdigest()
        return hmac.compare_digest(f"sha256={expected}", signature)

    @staticmethod
    def parse_webhook_message(data: dict) -> dict | None:
        try:
            entry = data.get("entry", [{}])[0]
            changes = entry.get("changes", [{}])[0]
            value = changes.get("value", {})
            messages = value.get("messages", [])
            if not messages:
                return None
            msg = messages[0]
            return {
                "from": msg.get("from", ""),
                "text": msg.get("text", {}).get("body", ""),
                "timestamp": msg.get("timestamp", ""),
                "message_id": msg.get("id", ""),
            }
        except (IndexError, KeyError):
            return None

    @staticmethod
    def parse_approval_response(text: str) -> tuple[str | None, str | None]:
        """Parse approval response. Returns (decision, ref_code)."""
        parts = text.strip().split()
        if not parts:
            return None, None

        keyword = parts[0].upper()
        ref_code = parts[1].upper() if len(parts) > 1 else None

        if keyword in ("APPROVE", "YES", "Y", "APPROVED", "OK"):
            return "APPROVED", ref_code
        if keyword in ("REJECT", "NO", "N", "REJECTED", "DECLINE"):
            return "REJECTED", ref_code
        return None, None
