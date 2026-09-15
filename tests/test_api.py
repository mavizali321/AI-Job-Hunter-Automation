"""Tests for the FastAPI application."""

import os
import pytest
from unittest.mock import patch

os.environ.setdefault("DATABASE_URL", "sqlite:///./test_api.db")

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, get_db
from app.main import app
from app.config import settings

test_engine = create_engine("sqlite:///./test_api.db", connect_args={"check_same_thread": False})
TestSession = sessionmaker(autocommit=False, autoflush=False, bind=test_engine)


def override_get_db():
    db = TestSession()
    try:
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db


@pytest.fixture(autouse=True, scope="module")
def setup_db():
    Base.metadata.create_all(bind=test_engine)
    yield
    Base.metadata.drop_all(bind=test_engine)
    import pathlib
    p = pathlib.Path("test_api.db")
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


client = TestClient(app)


class TestHealth:
    def test_health_endpoint(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    def test_login_page(self):
        resp = client.get("/login")
        assert resp.status_code == 200

    def test_dashboard_requires_auth(self):
        resp = client.get("/dashboard", follow_redirects=False)
        assert resp.status_code in (401, 403, 307)


class TestWhatsAppWebhook:
    def test_verify_endpoint(self):
        resp = client.get("/webhook/whatsapp", params={
            "hub.mode": "subscribe",
            "hub.verify_token": settings.whatsapp_verify_token,
            "hub.challenge": "test_challenge",
        })
        assert resp.status_code == 200
        assert resp.text == "test_challenge"

    def test_verify_rejects_bad_token(self):
        resp = client.get("/webhook/whatsapp", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "test",
        })
        assert resp.status_code == 403


class TestWorkerEndpoint:
    def test_worker_rejects_bad_token(self):
        resp = client.get("/api/worker/jobs", headers={"X-Worker-Token": "wrong"})
        assert resp.status_code == 403

    def test_worker_accepts_valid_token(self):
        from app.config import settings
        resp = client.get("/api/worker/jobs", headers={"X-Worker-Token": settings.worker_token})
        assert resp.status_code == 200


class TestDiscoveryEndpoint:
    def test_run_endpoint_requires_auth(self):
        resp = client.post("/api/run")
        assert resp.status_code == 401

    def test_run_endpoint_returns_discovery_result(self):
        from app.config import settings
        from itsdangerous import URLSafeTimedSerializer
        signer = URLSafeTimedSerializer(settings.secret_key)
        token = signer.dumps({"user": "admin"})
        with patch("app.main.run_orchestrator", return_value={"status": "COMPLETED", "discovered": 0, "ingested": 0}):
            resp = client.post("/api/run", cookies={"auth": token})
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "discovery" in data
            assert data["discovery"]["status"] == "COMPLETED"

    def test_csv_import_endpoint(self):
        from app.config import settings
        from itsdangerous import URLSafeTimedSerializer
        signer = URLSafeTimedSerializer(settings.secret_key)
        token = signer.dumps({"user": "admin"})
        resp = client.post("/api/csv-import", cookies={"auth": token})
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "csv_import" in data
