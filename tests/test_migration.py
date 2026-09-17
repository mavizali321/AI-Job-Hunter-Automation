"""Tests for Alembic migrations: upgrade, idempotency, data preservation."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text, Column, Integer, String, Text, Float, DateTime, Enum, JSON, Boolean, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import DeclarativeBase, Session
import enum


class _Base(DeclarativeBase):
    pass


class _JS(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"


class _AD(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"


class _AC(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    DASHBOARD = "DASHBOARD"


class _AS(str, enum.Enum):
    NORMAL = "NORMAL"


class _OldJob(_Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    canonical_url = Column(String(2048), nullable=False, index=True)
    source = Column(String(100), nullable=False)
    external_id = Column(String(500), nullable=True)
    requisition_id = Column(String(500), nullable=True)
    company = Column(String(500), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    location = Column(String(500), nullable=True)
    remote_policy = Column(String(50), nullable=True)
    description = Column(Text, nullable=True)
    requirements = Column(Text, nullable=True)
    salary = Column(String(200), nullable=True)
    posted_date = Column(DateTime(timezone=True), nullable=True)
    closing_date = Column(DateTime(timezone=True), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    description_hash = Column(String(16), nullable=True)
    freshness_status = Column(String(50), nullable=True)
    eligibility = Column(String(200), nullable=True)
    score_role_relevance = Column(Float, nullable=True)
    score_technical_match = Column(Float, nullable=True)
    score_experience_fit = Column(Float, nullable=True)
    score_location_fit = Column(Float, nullable=True)
    score_company_quality = Column(Float, nullable=True)
    score_entry_level = Column(Float, nullable=True)
    score_total = Column(Float, nullable=True)
    score_reasons = Column(JSON, nullable=True)
    decision = Column(String(30), nullable=True)
    status = Column(Enum(_JS), nullable=False, default=_JS.DISCOVERED)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_job_canonical_url"),
        Index("ix_job_company_title_hash", "company", "title", "description_hash"),
    )


class _OldApplication(_Base):
    __tablename__ = "applications"
    id = Column(Integer, primary_key=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    resume_variant = Column(String(500), nullable=True)
    answer_set_version = Column(String(100), nullable=True)
    approval_version = Column(String(100), nullable=True)
    state = Column(Enum(_JS), nullable=False, default=_JS.DISCOVERED)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    confirmation = Column(Text, nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True))
    updated_at = Column(DateTime(timezone=True))


class _OldApproval(_Base):
    __tablename__ = "approvals"
    id = Column(Integer, primary_key=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    channel = Column(Enum(_AC), nullable=False)
    decision = Column(Enum(_AD), nullable=False, default=_AD.PENDING)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    payload_hash = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True))


class _OldCandidateAnswer(_Base):
    __tablename__ = "candidate_answers"
    id = Column(Integer, primary_key=True)
    question_key = Column(String(500), nullable=False, unique=True)
    answer = Column(Text, nullable=True)
    sensitivity = Column(Enum(_AS), nullable=False, default=_AS.NORMAL)
    source_evidence = Column(Text, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True))


class _OldEvent(_Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True)
    entity = Column(String(50), nullable=False)
    entity_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    action = Column(String(100), nullable=False)
    old_state = Column(String(50), nullable=True)
    new_state = Column(String(50), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    timestamp = Column(DateTime(timezone=True))


class _OldSourceRun(_Base):
    __tablename__ = "source_runs"
    id = Column(Integer, primary_key=True)
    source = Column(String(100), nullable=False)
    started_at = Column(DateTime(timezone=True))
    ended_at = Column(DateTime(timezone=True), nullable=True)
    jobs_found = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    jobs_updated = Column(Integer, default=0)
    status = Column(String(50), default="RUNNING")
    error = Column(Text, nullable=True)


def _create_pre_update_db(db_url: str) -> None:
    """Create a database with the original schema (before any migrations)."""
    engine = create_engine(db_url)
    _Base.metadata.create_all(bind=engine)
    with Session(engine) as s:
        j = _OldJob(
            canonical_url="https://example.com/old-job",
            source="test",
            company="PreUpdateCo",
            title="AI Engineer",
            description="An existing job from before the migration",
            score_total=85.0,
            decision="APPLY",
            status=_JS.DISCOVERED,
        )
        s.add(j)
        s.commit()
    engine.dispose()


class TestAlembicMigration:
    def _make_db(self, name: str) -> tuple[str, str]:
        db_dir = tempfile.mkdtemp()
        db_path = os.path.join(db_dir, name)
        return db_path, f"sqlite:///{db_path}"

    def _run_alembic(self, db_url: str, *commands):
        from alembic.config import Config
        from alembic import command
        from app.config import settings

        with patch.object(settings, "database_url", db_url):
            cfg = Config(os.path.join(os.path.dirname(__file__), "..", "alembic.ini"))
            cfg.set_main_option("sqlalchemy.url", db_url)
            for cmd, args in commands:
                getattr(command, cmd)(cfg, *args)

    def test_upgrade_preserves_existing_data(self):
        db_path, db_url = self._make_db("test_upgrade.db")
        _create_pre_update_db(db_url)

        self._run_alembic(db_url, ("upgrade", ("head",)))

        engine = create_engine(db_url)
        with engine.connect() as conn:
            row = conn.execute(text("SELECT company, title, score_total, decision FROM jobs")).first()
            assert row[0] == "PreUpdateCo"
            assert row[1] == "AI Engineer"
            assert row[2] == 85.0
            assert row[3] == "APPLY"

            cols = [r[1] for r in conn.execute(text("PRAGMA table_info(jobs)"))]
            assert "discovery_url" in cols
            assert "matched_skills" in cols
            assert "match_score" in cols

            tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
            assert "candidate_profiles" in tables
        engine.dispose()

    def test_migration_idempotent(self):
        db_path, db_url = self._make_db("test_idempotent.db")
        _create_pre_update_db(db_url)

        self._run_alembic(db_url, ("upgrade", ("head",)), ("upgrade", ("head",)))

        engine = create_engine(db_url)
        with engine.connect() as conn:
            count = conn.execute(text("SELECT count(*) FROM jobs")).scalar()
            assert count == 1
        engine.dispose()

    def test_fresh_database_migration(self):
        db_path, db_url = self._make_db("test_fresh.db")

        self._run_alembic(db_url, ("upgrade", ("head",)))

        engine = create_engine(db_url)
        with engine.connect() as conn:
            tables = [r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))]
            assert "jobs" in tables
            assert "candidate_profiles" in tables
            assert "submission_nonces" in tables
        engine.dispose()
