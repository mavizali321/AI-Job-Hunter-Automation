"""Shared test fixtures."""

import os
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ["DATABASE_URL"] = "sqlite://"

from app.database import Base, get_db
from app.models import Job, Application, Approval, CandidateAnswer, CandidateProfile, Event, SourceRun, SubmissionNonce  # noqa


@pytest.fixture
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    from fastapi.testclient import TestClient
    from app.main import app

    def override_get_db():
        try:
            yield db
        finally:
            pass

    app.dependency_overrides[get_db] = override_get_db
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    app.dependency_overrides.clear()
