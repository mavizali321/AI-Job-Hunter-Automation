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
            decision, ref_code = WhatsAppClient.parse_approval_response(text)
            assert decision == "APPROVED"
            assert ref_code is None

    def test_reject_variants(self):
        for text in ["REJECT", "no", "N", "rejected", "decline"]:
            decision, ref_code = WhatsAppClient.parse_approval_response(text)
            assert decision == "REJECTED"
            assert ref_code is None

    def test_approve_with_ref_code(self):
        decision, ref_code = WhatsAppClient.parse_approval_response("APPROVE ABC12345")
        assert decision == "APPROVED"
        assert ref_code == "ABC12345"

    def test_reject_with_ref_code(self):
        decision, ref_code = WhatsAppClient.parse_approval_response("REJECT XYZ99999")
        assert decision == "REJECTED"
        assert ref_code == "XYZ99999"

    def test_unknown(self):
        decision, ref_code = WhatsAppClient.parse_approval_response("maybe")
        assert decision is None
        assert ref_code is None

    def test_empty(self):
        decision, ref_code = WhatsAppClient.parse_approval_response("")
        assert decision is None
        assert ref_code is None


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
