import enum
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlparse, urlunparse

from sqlalchemy import (
    Column, Integer, String, Text, Float, DateTime, Enum, ForeignKey,
    JSON, UniqueConstraint, Index, Boolean,
)
from sqlalchemy.orm import relationship

from app.database import Base


class JobStatus(str, enum.Enum):
    DISCOVERED = "DISCOVERED"
    VERIFIED = "VERIFIED"
    SCORED = "SCORED"
    SHORTLISTED = "SHORTLISTED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    PREPARING = "PREPARING"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    AWAITING_FINAL_APPROVAL = "AWAITING_FINAL_APPROVAL"
    FINAL_APPROVED = "FINAL_APPROVED"
    SUBMITTED = "SUBMITTED"
    REJECTED = "REJECTED"
    SKIPPED = "SKIPPED"
    EXPIRED = "EXPIRED"
    BLOCKED = "BLOCKED"
    MANUAL_ACTION_REQUIRED = "MANUAL_ACTION_REQUIRED"
    FAILED = "FAILED"


class ApprovalDecision(str, enum.Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ApprovalChannel(str, enum.Enum):
    WHATSAPP = "WHATSAPP"
    DASHBOARD = "DASHBOARD"


class ApprovalType(str, enum.Enum):
    FIRST = "FIRST"
    FINAL = "FINAL"


class AnswerSensitivity(str, enum.Enum):
    NORMAL = "NORMAL"
    SENSITIVE = "SENSITIVE"
    BLOCKED = "BLOCKED"


def canonicalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip().lower())
    path = parsed.path.rstrip("/")
    return urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _utcnow():
    return datetime.now(timezone.utc)


class Job(Base):
    __tablename__ = "jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    canonical_url = Column(String(2048), nullable=False, index=True)
    discovery_url = Column(String(2048), nullable=True)
    discovery_source = Column(String(100), nullable=True)
    official_url = Column(String(2048), nullable=True)
    official_url_method = Column(String(50), nullable=True)
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
    verification_evidence = Column(JSON, nullable=True)
    verification_blockers = Column(Text, nullable=True)

    score_role_relevance = Column(Float, nullable=True)
    score_technical_match = Column(Float, nullable=True)
    score_experience_fit = Column(Float, nullable=True)
    score_location_fit = Column(Float, nullable=True)
    score_company_quality = Column(Float, nullable=True)
    score_entry_level = Column(Float, nullable=True)
    score_total = Column(Float, nullable=True)
    score_reasons = Column(JSON, nullable=True)

    decision = Column(String(30), nullable=True)
    rejection_reason = Column(Text, nullable=True)

    matched_skills = Column(JSON, nullable=True)
    missing_skills = Column(JSON, nullable=True)
    seniority_fit = Column(String(100), nullable=True)
    location_fit_detail = Column(String(200), nullable=True)
    match_score = Column(Float, nullable=True)
    match_details = Column(JSON, nullable=True)

    status = Column(Enum(JobStatus), nullable=False, default=JobStatus.DISCOVERED)

    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    applications = relationship("Application", back_populates="job")
    events = relationship("Event", back_populates="job", foreign_keys="Event.entity_id")

    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_job_canonical_url"),
        Index("ix_job_company_title_hash", "company", "title", "description_hash"),
    )


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_id = Column(Integer, ForeignKey("jobs.id"), nullable=False)
    resume_variant = Column(String(500), nullable=True)
    answer_set_version = Column(String(100), nullable=True)
    approval_version = Column(String(100), nullable=True)
    manifest_hash = Column(String(64), nullable=True)
    state = Column(Enum(JobStatus), nullable=False, default=JobStatus.WAITING_APPROVAL)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    confirmation = Column(Text, nullable=True)
    confirmation_reference = Column(String(500), nullable=True)
    screenshot_path = Column(String(500), nullable=True)
    attempt_status = Column(String(50), nullable=True)
    failure_reason = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)

    job = relationship("Job", back_populates="applications")
    approvals = relationship("Approval", back_populates="application")


class Approval(Base):
    __tablename__ = "approvals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True)
    channel = Column(Enum(ApprovalChannel), nullable=False)
    approval_type = Column(Enum(ApprovalType), nullable=False, default=ApprovalType.FIRST)
    decision = Column(Enum(ApprovalDecision), nullable=False, default=ApprovalDecision.PENDING)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    decided_at = Column(DateTime(timezone=True), nullable=True)
    payload_hash = Column(String(64), nullable=True)
    approval_ref_code = Column(String(20), nullable=True, index=True)
    whatsapp_delivery_status = Column(String(100), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)

    application = relationship("Application", back_populates="approvals")


class SubmissionNonce(Base):
    __tablename__ = "submission_nonces"

    id = Column(Integer, primary_key=True, autoincrement=True)
    application_id = Column(Integer, ForeignKey("applications.id"), nullable=False, unique=True)
    nonce = Column(String(64), nullable=False, unique=True, index=True)
    manifest_hash = Column(String(64), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    consumed = Column(Boolean, default=False, nullable=False)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class CandidateAnswer(Base):
    __tablename__ = "candidate_answers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    question_key = Column(String(500), nullable=False, unique=True)
    answer = Column(Text, nullable=True)
    sensitivity = Column(Enum(AnswerSensitivity), nullable=False, default=AnswerSensitivity.NORMAL)
    source_evidence = Column(Text, nullable=True)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)


class Event(Base):
    __tablename__ = "events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity = Column(String(50), nullable=False)
    entity_id = Column(Integer, ForeignKey("jobs.id"), nullable=True)
    action = Column(String(100), nullable=False)
    old_state = Column(String(50), nullable=True)
    new_state = Column(String(50), nullable=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    timestamp = Column(DateTime(timezone=True), default=_utcnow)

    job = relationship("Job", back_populates="events", foreign_keys=[entity_id])


class CandidateProfile(Base):
    __tablename__ = "candidate_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    resume_checksum = Column(String(64), nullable=False, unique=True)
    name = Column(String(200), nullable=False)
    email = Column(String(200), nullable=True)
    phone = Column(String(50), nullable=True)
    linkedin = Column(String(500), nullable=True)
    seniority = Column(String(50), nullable=False, default="junior")
    total_experience_years = Column(Float, nullable=True)
    skills = Column(JSON, nullable=False, default=dict)
    confirmed_skills = Column(JSON, nullable=True)
    profile_only_skills = Column(JSON, nullable=True)
    experience = Column(JSON, nullable=False, default=list)
    education = Column(JSON, nullable=False, default=list)
    projects = Column(JSON, nullable=False, default=list)
    target_roles = Column(JSON, nullable=False, default=list)
    location_preferences = Column(JSON, nullable=False, default=list)
    do_not_claim = Column(JSON, nullable=False, default=list)
    raw_resume_text = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), default=_utcnow)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow)


class SourceRun(Base):
    __tablename__ = "source_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(100), nullable=False)
    started_at = Column(DateTime(timezone=True), default=_utcnow)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    jobs_found = Column(Integer, default=0)
    jobs_new = Column(Integer, default=0)
    jobs_updated = Column(Integer, default=0)
    status = Column(String(50), default="RUNNING")
    error = Column(Text, nullable=True)
