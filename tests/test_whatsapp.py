"""Tests for WhatsApp webhook handling."""

from app.whatsapp import WhatsAppClient


class TestWebhookParsing:
    def test_parse_valid_message(self):
        data = {
            "entry": [{
                "changes": [{
                    "value": {
                        "messages": [{
                            "from": "923361321040",
                            "text": {"body": "APPROVE"},
                            "timestamp": "1694736000",
                            "id": "msg123",
                        }]
                    }
                }]
            }]
        }
        msg = WhatsAppClient.parse_webhook_message(data)
        assert msg is not None
        assert msg["from"] == "923361321040"
        assert msg["text"] == "APPROVE"
        assert msg["message_id"] == "msg123"

    def test_parse_empty_message(self):
        msg = WhatsAppClient.parse_webhook_message({"entry": [{"changes": [{"value": {}}]}]})
        assert msg is None

    def test_parse_malformed(self):
        msg = WhatsAppClient.parse_webhook_message({})
        assert msg is None


class TestApprovalResponse:
    def test_approve_variants(self):
        for text in ["APPROVE", "yes", "Y", "approved", "ok"]:
            assert WhatsAppClient.parse_approval_response(text) == "APPROVED"

    def test_reject_variants(self):
        for text in ["REJECT", "no", "N", "rejected", "decline"]:
            assert WhatsAppClient.parse_approval_response(text) == "REJECTED"

    def test_unknown(self):
        assert WhatsAppClient.parse_approval_response("maybe") is None
        assert WhatsAppClient.parse_approval_response("") is None


class TestIdempotency:
    def test_duplicate_message_detected(self, db):
        from app.models import Event
        event = Event(
            entity="whatsapp", action="webhook_received",
            metadata_={"message_id": "msg123"},
        )
        db.add(event)
        db.commit()

        existing = db.query(Event).filter(
            Event.metadata_.contains({"message_id": "msg123"})
        ).first()
        assert existing is not None


class TestSignatureVerification:
    def test_valid_signature(self):
        import hashlib, hmac
        secret = "test_secret"
        payload = b'{"test": true}'
        sig = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
        assert WhatsAppClient.verify_webhook_signature(payload, sig, secret)

    def test_invalid_signature(self):
        assert not WhatsAppClient.verify_webhook_signature(b"data", "sha256=wrong", "secret")
