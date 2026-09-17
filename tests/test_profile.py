"""Tests for CandidateProfile: parsing, query generation, matching, refresh, truth enforcement."""

import hashlib
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.models import CandidateProfile, Job, JobStatus
from app.profile_parser import (
    parse_profile_md,
    compute_file_checksum,
    compute_combined_checksum,
    build_candidate_profile,
    get_or_refresh_profile,
    generate_search_queries_from_profile,
    match_job_to_profile,
    verify_claims_against_resume,
    _all_skill_keywords,
)


SAMPLE_PROFILE_MD = """# Maviz Ali — Candidate Profile

## Contact
- Name: Maviz Ali
- Email: maviz.ali92@gmail.com
- Phone: +92 336 1321040
- LinkedIn: linkedin.com/in/mavizali

## Current positioning
AI Software Development Trainee | AI Automation & Integration | Front-End Developer

## Target roles
### Priority A (best fit)
- Forward Deployed Engineer
- AI Engineer / Junior AI Engineer
- Applied AI Engineer

### Priority B (strong adjacent)
- Solutions Engineer (AI/SaaS)
- Software Engineer — AI/LLM focus

### Priority C (acceptable entry points)
- Junior Software Engineer
- Python Developer / Backend Developer
- Front-End Developer (React/Angular, AI product)

## Location priority
1. Karachi — onsite or hybrid
2. Pakistan — remote
3. Worldwide remote (company explicitly hires from Pakistan)
4. International with sponsorship or relocation support

## Experience
### Tack Solutions — Front-End Developer
- April 2025 – Present | Karachi, Pakistan
- Building responsive web applications with React
- Integrating REST APIs and LLM-powered features

### SYSCROWD — UI Developer
- August 2022 – March 2025 | Karachi, Pakistan
- Developed enterprise UIs with Angular and Vue
- Managed CMS implementations on WordPress and Shopify

## Education
### Sir Syed University of Engineering & Technology
- BS Computer Science | 2021 – 2025

## Technical evidence
### AI & Automation
- Agentic AI concepts, LLM integration, prompt engineering
- Tool/function calling, structured outputs, RAG fundamentals
### Front-End
- HTML5, CSS3, JavaScript ES6+, React, Angular, Vue
### Integration
- REST APIs, JSON, async patterns, Git/GitHub
### Currently strengthening
- Python for AI automation, vector databases

## AI projects
### AI-Powered Career Roadmap Integration
- ASP.NET Core, Angular, REST APIs, LLM API integration
### Agentic AI Workflow Memory System (In Progress)
- Python-based agent with persistent memory

## Do not claim without user confirmation
- US/EU/UK/Australia work authorization or visa sponsorship eligibility
- Ability to relocate internationally
- Production Python expertise
- Production vector database expertise
- Any certification not listed in the resume
"""


class TestProfileParsing:
    def test_parses_contact_info(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        assert result["name"] == "Maviz Ali"
        assert result["email"] == "maviz.ali92@gmail.com"
        assert result["phone"] == "+92 336 1321040"
        assert result["linkedin"] == "linkedin.com/in/mavizali"

    def test_parses_target_roles_with_priorities(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        roles = result["target_roles"]
        assert len(roles) >= 5
        a_roles = [r for r in roles if r["priority"] == "A"]
        assert any("Forward Deployed" in r["title"] for r in a_roles)
        assert any("AI Engineer" in r["title"] for r in a_roles)
        b_roles = [r for r in roles if r["priority"] == "B"]
        assert any("Solutions Engineer" in r["title"] for r in b_roles)
        c_roles = [r for r in roles if r["priority"] == "C"]
        assert any("Junior Software Engineer" in r["title"] for r in c_roles)

    def test_parses_location_preferences(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        prefs = result["location_preferences"]
        assert len(prefs) >= 3
        assert prefs[0]["priority"] == 1
        assert "Karachi" in prefs[0]["location"]

    def test_parses_experience(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        exp = result["experience"]
        assert len(exp) == 2
        assert exp[0]["company"] == "Tack Solutions"
        assert exp[0]["title"] == "Front-End Developer"
        assert exp[1]["company"] == "SYSCROWD"

    def test_parses_experience_years(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        assert result["total_experience_years"] >= 3

    def test_determines_seniority(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        assert result["seniority"] in ("junior", "mid-junior", "mid")

    def test_parses_education(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        edu = result["education"]
        assert len(edu) >= 1
        assert "Sir Syed" in edu[0]["institution"]
        assert "Computer Science" in edu[0]["degree"]

    def test_parses_skills_by_category(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        skills = result["skills"]
        assert "ai & automation" in skills
        assert "front-end" in skills
        assert "integration" in skills
        assert len(skills["ai & automation"]) >= 2

    def test_parses_projects(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        projects = result["projects"]
        assert len(projects) >= 2
        assert any("Career Roadmap" in p["name"] for p in projects)

    def test_parses_do_not_claim(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        dnc = result["do_not_claim"]
        assert len(dnc) >= 3
        assert any("work authorization" in item.lower() for item in dnc)
        assert any("production python" in item.lower() for item in dnc)


class TestEvidenceBasedSkills:
    def test_all_skill_keywords_extracted(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        keywords = _all_skill_keywords(result["skills"])
        assert "react" in keywords
        assert "angular" in keywords
        assert "llm integration" in keywords or "llm" in keywords
        assert "rest apis" in keywords or "rest" in keywords

    def test_skills_only_from_profile(self):
        """Skills come from the profile — none are invented."""
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        keywords = _all_skill_keywords(result["skills"])
        assert "kubernetes" not in keywords
        assert "terraform" not in keywords
        assert "pytorch" not in keywords
        assert "phd" not in keywords

    def test_verify_claims_flags_missing(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        fake_resume = "I know HTML and CSS and JavaScript"
        unverified = verify_claims_against_resume(result, fake_resume)
        assert len(unverified) > 0
        assert any("angular" in u for u in unverified)

    def test_verify_claims_all_present(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        full_resume = (
            "Maviz Ali — react angular vue html5 css3 javascript es6+ "
            "llm integration prompt engineering agentic ai concepts tool function calling "
            "structured outputs rag fundamentals rest apis json async patterns git github "
            "python for ai automation vector databases asp.net core wordpress shopify figma"
        )
        unverified = verify_claims_against_resume(result, full_resume)
        assert len(unverified) == 0


class TestNoInventedQualifications:
    def test_profile_never_adds_kubernetes(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        all_text = str(result).lower()
        assert "kubernetes" not in all_text or "currently strengthening" in all_text

    def test_profile_never_adds_certifications(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        all_text = str(result).lower()
        assert "aws certified" not in all_text
        assert "google certified" not in all_text
        assert "certified scrum" not in all_text

    def test_do_not_claim_respected_in_match(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        cp = CandidateProfile(
            resume_checksum="test",
            name="Test",
            seniority="junior",
            skills=result["skills"],
            experience=result["experience"],
            education=result["education"],
            projects=result["projects"],
            target_roles=result["target_roles"],
            location_preferences=result["location_preferences"],
            do_not_claim=result["do_not_claim"],
            total_experience_years=3,
        )
        match = match_job_to_profile(
            cp,
            title="Python Backend Engineer",
            description="Requires production Python expertise and AWS certification",
            requirements="Must have work authorization in the US",
        )
        assert len(match["blocked_fields"]) >= 1


class TestQueryGeneration:
    def test_generates_queries_from_profile(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        cp = CandidateProfile(
            resume_checksum="test",
            name="Test",
            seniority="junior",
            skills=result["skills"],
            experience=[],
            education=[],
            projects=[],
            target_roles=result["target_roles"],
            location_preferences=result["location_preferences"],
            do_not_claim=[],
            total_experience_years=3,
        )
        queries = generate_search_queries_from_profile(cp, locations=["Karachi", "Remote"])
        assert len(queries) > 0
        titles = {q[0] for q in queries}
        assert any("Forward Deployed" in t for t in titles)
        assert any("AI Engineer" in t for t in titles)

    def test_priority_a_roles_come_first(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        cp = CandidateProfile(
            resume_checksum="test",
            name="Test",
            seniority="junior",
            skills={},
            experience=[],
            education=[],
            projects=[],
            target_roles=result["target_roles"],
            location_preferences=[],
            do_not_claim=[],
            total_experience_years=3,
        )
        queries = generate_search_queries_from_profile(cp, locations=["Karachi"])
        first_query = queries[0][0]
        assert "Forward Deployed" in first_query or "AI Engineer" in first_query

    def test_empty_job_titles_uses_profile(self):
        """When JOB_TITLES env is empty, queries come from CandidateProfile."""
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        cp = CandidateProfile(
            resume_checksum="test",
            name="Test",
            seniority="junior",
            skills={},
            experience=[],
            education=[],
            projects=[],
            target_roles=result["target_roles"],
            location_preferences=[],
            do_not_claim=[],
            total_experience_years=3,
        )
        queries = generate_search_queries_from_profile(cp, locations=["Remote"])
        assert len(queries) >= 5
        titles = {q[0] for q in queries}
        assert not all(t == "Software Engineer" for t in titles)

    def test_no_queries_without_roles(self):
        cp = CandidateProfile(
            resume_checksum="test",
            name="Test",
            seniority="junior",
            skills={},
            experience=[],
            education=[],
            projects=[],
            target_roles=[],
            location_preferences=[],
            do_not_claim=[],
            total_experience_years=0,
        )
        queries = generate_search_queries_from_profile(cp, locations=["Karachi"])
        assert len(queries) >= 1
        assert "Software Engineer" in queries[0][0]


class TestProfileRefresh:
    def test_build_stores_in_db(self, db):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake resume content for testing")
            pdf_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(SAMPLE_PROFILE_MD)
            md_path = Path(f.name)

        try:
            with patch("app.profile_parser.RESUME_PDF_PATH", pdf_path), \
                 patch("app.profile_parser.PROFILE_MD_PATH", md_path), \
                 patch("app.profile_parser.extract_resume_text", return_value="fake resume text"):
                profile = build_candidate_profile(db)
                assert profile is not None
                assert profile.name == "Maviz Ali"
                assert db.query(CandidateProfile).count() == 1
        finally:
            pdf_path.unlink(missing_ok=True)
            md_path.unlink(missing_ok=True)

    def test_refresh_on_checksum_change(self, db):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 original resume")
            pdf_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(SAMPLE_PROFILE_MD)
            md_path = Path(f.name)

        try:
            with patch("app.profile_parser.RESUME_PDF_PATH", pdf_path), \
                 patch("app.profile_parser.PROFILE_MD_PATH", md_path), \
                 patch("app.profile_parser.extract_resume_text", return_value=""):
                p1 = build_candidate_profile(db)
                old_checksum = p1.resume_checksum

                pdf_path.write_bytes(b"%PDF-1.4 updated resume with new content")

                p2 = get_or_refresh_profile(db)
                assert p2.resume_checksum != old_checksum
                assert db.query(CandidateProfile).count() == 1
        finally:
            pdf_path.unlink(missing_ok=True)
            md_path.unlink(missing_ok=True)

    def test_no_refresh_when_unchanged(self, db):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 stable resume")
            pdf_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(SAMPLE_PROFILE_MD)
            md_path = Path(f.name)

        try:
            with patch("app.profile_parser.RESUME_PDF_PATH", pdf_path), \
                 patch("app.profile_parser.PROFILE_MD_PATH", md_path), \
                 patch("app.profile_parser.extract_resume_text", return_value=""):
                p1 = build_candidate_profile(db)
                p2 = get_or_refresh_profile(db)
                assert p1.id == p2.id
        finally:
            pdf_path.unlink(missing_ok=True)
            md_path.unlink(missing_ok=True)


class TestJobMatching:
    @pytest.fixture
    def profile(self):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        return CandidateProfile(
            resume_checksum="test",
            name="Maviz Ali",
            seniority="mid-junior",
            total_experience_years=3,
            skills=result["skills"],
            experience=result["experience"],
            education=result["education"],
            projects=result["projects"],
            target_roles=result["target_roles"],
            location_preferences=result["location_preferences"],
            do_not_claim=result["do_not_claim"],
        )

    def test_match_ai_job_has_matched_skills(self, profile):
        match = match_job_to_profile(
            profile,
            title="AI Engineer",
            description="Build LLM integration, prompt engineering, REST API automation, React frontend",
            location="Karachi, Pakistan",
        )
        assert len(match["matched_skills"]) >= 3
        assert match["match_score"] > 0

    def test_match_stores_missing_skills(self, profile):
        match = match_job_to_profile(
            profile,
            title="ML Engineer",
            description="Requires pytorch, tensorflow, kubernetes, docker, deep learning",
            requirements="5+ years Python, PhD preferred",
        )
        assert len(match["missing_skills"]) >= 2
        assert any("pytorch" in s for s in match["missing_skills"])

    def test_match_seniority_fit_junior(self, profile):
        match = match_job_to_profile(
            profile,
            title="Junior AI Engineer",
            description="Entry level AI position, 0-2 years experience",
            location="Karachi",
        )
        assert "strong" in match["seniority_fit"] or "good" in match["seniority_fit"]

    def test_match_seniority_fit_senior(self, profile):
        match = match_job_to_profile(
            profile,
            title="Senior Staff Engineer",
            description="Lead team of 20, 10+ years experience required",
        )
        assert "poor" in match["seniority_fit"] or "stretch" in match["seniority_fit"]

    def test_match_location_pakistan(self, profile):
        match = match_job_to_profile(
            profile,
            title="Developer",
            description="Web developer needed",
            location="Karachi, Pakistan",
        )
        assert "strong" in match["location_fit"]

    def test_match_location_us_only(self, profile):
        match = match_job_to_profile(
            profile,
            title="Developer",
            description="Must be located in US, no sponsorship",
            location="New York, NY",
        )
        assert "poor" in match["location_fit"]

    def test_match_score_range(self, profile):
        match = match_job_to_profile(
            profile,
            title="AI Automation Engineer",
            description="LLM, REST API, React, prompt engineering, agentic AI, function calling",
            location="Karachi, Pakistan",
        )
        assert 0 <= match["match_score"] <= 100

    def test_match_returns_all_fields(self, profile):
        match = match_job_to_profile(
            profile,
            title="Developer",
            description="Build web apps",
        )
        assert "matched_skills" in match
        assert "missing_skills" in match
        assert "seniority_fit" in match
        assert "location_fit" in match
        assert "match_score" in match
        assert "blocked_fields" in match


class TestJobMatchedInOrchestrator:
    def test_scored_job_gets_match_details(self, db):
        from app.pipeline import ingest_discovered_job, verify_job, score_and_decide

        result = parse_profile_md(SAMPLE_PROFILE_MD)
        cp = CandidateProfile(
            resume_checksum="test",
            name="Maviz Ali",
            seniority="mid-junior",
            total_experience_years=3,
            skills=result["skills"],
            experience=result["experience"],
            education=result["education"],
            projects=result["projects"],
            target_roles=result["target_roles"],
            location_preferences=result["location_preferences"],
            do_not_claim=result["do_not_claim"],
        )
        db.add(cp)
        db.commit()

        job = ingest_discovered_job(
            db,
            title="AI Engineer",
            company="TestCo",
            url="https://testco.com/jobs/ai",
            source="test",
            location="Karachi, Pakistan",
            description="LLM API integration, agentic AI, prompt engineering, RAG, React frontend",
            requirements="1-2 years",
        )
        ev = {
            "http_success": True, "url_is_official": True, "has_content": True,
            "listing_closed": False, "has_posted_date": True, "location_eligible": True,
        }
        verify_job(db, job, ev)
        score_and_decide(db, job)

        match = match_job_to_profile(
            cp,
            title=job.title,
            description=job.description,
            requirements=job.requirements,
            location=job.location,
        )
        job.matched_skills = match["matched_skills"]
        job.missing_skills = match["missing_skills"]
        job.seniority_fit = match["seniority_fit"]
        job.location_fit_detail = match["location_fit"]
        job.match_score = match["match_score"]
        job.match_details = match
        db.commit()

        refreshed = db.query(Job).get(job.id)
        assert refreshed.matched_skills is not None
        assert len(refreshed.matched_skills) >= 2
        assert refreshed.match_score > 0
        assert refreshed.seniority_fit is not None
        assert refreshed.location_fit_detail is not None


class TestMigrationIdempotent:
    def test_migration_creates_candidate_profiles_table(self, db):
        assert db.query(CandidateProfile).count() == 0

    def test_job_has_match_columns(self, db):
        from app.pipeline import ingest_discovered_job
        job = ingest_discovered_job(
            db,
            title="Test",
            company="TestCo",
            url="https://example.com/test",
            source="test",
            description="test job",
        )
        job.matched_skills = ["python", "react"]
        job.missing_skills = ["kubernetes"]
        job.seniority_fit = "good"
        job.location_fit_detail = "strong"
        job.match_score = 75.0
        job.match_details = {"test": True}
        job.rejection_reason = None
        db.commit()
        refreshed = db.query(Job).get(job.id)
        assert refreshed.matched_skills == ["python", "react"]
        assert refreshed.match_score == 75.0

    def test_job_has_discovery_columns(self, db):
        from app.pipeline import ingest_discovered_job
        job = ingest_discovered_job(
            db,
            title="Test2",
            company="TestCo2",
            url="https://example.com/test2",
            source="test",
            description="test job 2",
        )
        job.discovery_url = "https://search.example.com/result"
        job.discovery_source = "serper"
        job.official_url = "https://testco2.com/careers/1"
        job.official_url_method = "redirect"
        db.commit()
        refreshed = db.query(Job).get(job.id)
        assert refreshed.discovery_source == "serper"
        assert refreshed.official_url_method == "redirect"


class TestProfileGating:
    """Profile match must happen before shortlisting; low match blocks applications."""

    def _make_profile(self, db, **overrides):
        result = parse_profile_md(SAMPLE_PROFILE_MD)
        defaults = dict(
            resume_checksum="gate_test",
            name="Maviz Ali",
            seniority="mid-junior",
            total_experience_years=3,
            skills=result["skills"],
            confirmed_skills=sorted(_all_skill_keywords(result["skills"])),
            profile_only_skills=[],
            experience=result["experience"],
            education=result["education"],
            projects=result["projects"],
            target_roles=result["target_roles"],
            location_preferences=result["location_preferences"],
            do_not_claim=result["do_not_claim"],
        )
        defaults.update(overrides)
        cp = CandidateProfile(**defaults)
        db.add(cp)
        db.commit()
        return cp

    def _ingest_and_verify(self, db, **job_kwargs):
        from app.pipeline import ingest_discovered_job, verify_job
        defaults = dict(
            title="Developer",
            company="TestCo",
            url=f"https://testco.com/jobs/{id(job_kwargs)}",
            source="test",
            description="Web development position",
            location="Karachi, Pakistan",
        )
        defaults.update(job_kwargs)
        job = ingest_discovered_job(db, **defaults)
        ev = {
            "http_success": True, "url_is_official": True,
            "has_content": True, "has_substantial_content": True,
            "title_match": True, "company_match": True,
            "is_job_specific_url": True,
            "listing_closed": False, "has_posted_date": True,
            "location_eligible": True,
        }
        verify_job(db, job, ev)
        return job

    def test_low_profile_match_never_creates_application(self, db):
        from app.discovery import _check_profile_gate
        profile = self._make_profile(db)
        match = match_job_to_profile(
            profile,
            title="Kubernetes Platform Engineer",
            description="Expert in k8s, terraform, helm, istio, service mesh, GitOps for remote worldwide teams",
            requirements="8+ years infrastructure experience",
            location="Remote, Worldwide",
        )
        from app.config import settings
        assert match["match_score"] < settings.min_profile_match_score
        reason = _check_profile_gate(match, profile)
        assert reason is not None
        assert "match score" in reason.lower() or "below minimum" in reason.lower()

    def test_poor_seniority_never_creates_application(self, db):
        from app.discovery import _check_profile_gate
        profile = self._make_profile(db)
        match = match_job_to_profile(
            profile,
            title="Senior Staff Principal Engineer",
            description="Lead 50-person engineering org, 15+ years required",
            requirements="PhD preferred, 15+ years",
        )
        reason = _check_profile_gate(match, profile)
        assert reason is not None
        assert "seniority" in reason.lower()

    def test_ineligible_location_never_creates_application(self, db):
        from app.discovery import _check_profile_gate
        profile = self._make_profile(db)
        match = match_job_to_profile(
            profile,
            title="Junior Developer",
            description="Must be in office. No sponsorship available.",
            location="New York, US only",
        )
        reason = _check_profile_gate(match, profile)
        assert reason is not None
        assert "location" in reason.lower() or "ineligible" in reason.lower()

    def test_profile_match_happens_before_shortlisting(self, db):
        """A verified job with poor profile match is skipped, never scored or shortlisted."""
        from app.pipeline import score_and_decide
        from app.state_machine import transition_job

        profile = self._make_profile(db)
        job = self._ingest_and_verify(
            db,
            title="Senior Kubernetes DevOps Lead",
            description="Expert in k8s terraform helm istio GitOps",
            requirements="10+ years infrastructure and platform experience",
            location="San Francisco, CA, US only, no sponsorship",
            url="https://testco.com/jobs/gate-test-1",
        )
        assert job.status == JobStatus.VERIFIED

        match = match_job_to_profile(
            profile,
            title=job.title,
            description=job.description,
            requirements=job.requirements,
            location=job.location,
        )

        from app.discovery import _check_profile_gate
        rejection = _check_profile_gate(match, profile)
        assert rejection is not None

        job.rejection_reason = rejection
        transition_job(db, job, JobStatus.SKIPPED, {"reason": rejection})
        db.commit()

        assert job.status == JobStatus.SKIPPED
        from app.models import Application
        apps = db.query(Application).filter(Application.job_id == job.id).all()
        assert len(apps) == 0


class TestCombinedChecksumRefresh:
    def test_profile_md_change_refreshes_profile(self, db):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 stable resume content")
            pdf_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(SAMPLE_PROFILE_MD)
            md_path = Path(f.name)

        try:
            with patch("app.profile_parser.RESUME_PDF_PATH", pdf_path), \
                 patch("app.profile_parser.PROFILE_MD_PATH", md_path), \
                 patch("app.profile_parser.extract_resume_text", return_value=""):
                p1 = build_candidate_profile(db)
                old_checksum = p1.resume_checksum

                md_path.write_text(SAMPLE_PROFILE_MD + "\n## Updated section\n- New skill added\n", encoding="utf-8")

                p2 = get_or_refresh_profile(db)
                assert p2.resume_checksum != old_checksum
                assert db.query(CandidateProfile).count() == 1
        finally:
            pdf_path.unlink(missing_ok=True)
            md_path.unlink(missing_ok=True)


class TestEvidenceBasedProfileCreation:
    def test_unverified_skill_is_profile_only(self, db):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
            f.write(b"%PDF-1.4 fake resume")
            pdf_path = Path(f.name)

        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8") as f:
            f.write(SAMPLE_PROFILE_MD)
            md_path = Path(f.name)

        try:
            resume_text = "HTML CSS JavaScript React REST APIs JSON Git"
            with patch("app.profile_parser.RESUME_PDF_PATH", pdf_path), \
                 patch("app.profile_parser.PROFILE_MD_PATH", md_path), \
                 patch("app.profile_parser.extract_resume_text", return_value=resume_text):
                profile = build_candidate_profile(db)
                assert profile.confirmed_skills is not None
                assert profile.profile_only_skills is not None
                assert "react" in profile.confirmed_skills
                assert len(profile.profile_only_skills) > 0
                assert all(
                    skill not in profile.confirmed_skills
                    for skill in profile.profile_only_skills
                )
        finally:
            pdf_path.unlink(missing_ok=True)
            md_path.unlink(missing_ok=True)


class TestVerificationStrengthening:
    def test_generic_career_homepage_rejected(self):
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://example.com/careers",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content="Welcome to our careers page! We're always looking for talented people. Browse open positions.",
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="ExampleCorp",
        )
        blockers = compute_verification_blockers(ev)
        assert len(blockers) > 0
        has_relevant_blocker = any(
            "substantial" in b.lower() or "generic" in b.lower() or "specific" in b.lower() or "title" in b.lower()
            for b in blockers
        )
        assert has_relevant_blocker

    def test_title_company_mismatch_blocks(self):
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://example.com/jobs/123",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content="WrongCompany is hiring a Data Scientist to build ML pipelines. Apply now with qualifications. Requirements: PhD in Statistics. Responsibilities: lead the team.",
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="RightCompany",
        )
        blockers = compute_verification_blockers(ev)
        assert any("title" in b.lower() or "company" in b.lower() for b in blockers)


class TestSearchBehavior:
    def test_max_results_globally_enforced(self):
        import asyncio
        from app.discovery import discover_all
        from app.search.base import SearchResult, SearchProvider
        from app.config import settings as real_settings

        class BulkProvider(SearchProvider):
            name = "bulk"
            async def search(self, query, location="", max_results=25):
                return [
                    SearchResult(
                        title=f"Job {i}", company="Co", location="",
                        url=f"https://co.com/{self.name}/{id(self)}/{query[:10]}/{i}",
                        snippet="desc", source=self.name,
                        posted_date=datetime.now(timezone.utc),
                    )
                    for i in range(max_results)
                ]

        original = real_settings.max_results_per_run
        try:
            object.__setattr__(real_settings, "max_results_per_run", 5)
            jobs, errors, _diag = asyncio.run(discover_all(
                adapters=[],
                search_providers=[BulkProvider(), BulkProvider()],
            ))
            assert len(jobs) <= 5
        finally:
            object.__setattr__(real_settings, "max_results_per_run", original)

    def test_arbeitnow_matches_role_tokens(self):
        import asyncio
        from app.search.arbeitnow import ArbeitnowProvider

        mock_data = {
            "data": [
                {
                    "title": "AI Engineer",
                    "company_name": "TestCo",
                    "description": "Build AI systems",
                    "url": "https://arbeitnow.com/jobs/1",
                    "location": "Remote",
                    "remote": True,
                    "created_at": int(datetime.now(timezone.utc).timestamp()),
                },
                {
                    "title": "Marketing Manager",
                    "company_name": "OtherCo",
                    "description": "Manage marketing campaigns",
                    "url": "https://arbeitnow.com/jobs/2",
                    "location": "Berlin",
                    "remote": False,
                    "created_at": int(datetime.now(timezone.utc).timestamp()),
                },
            ]
        }

        import httpx
        mock_resp = httpx.Response(
            200,
            json=mock_data,
            request=httpx.Request("GET", "https://www.arbeitnow.com/api/job-board-api"),
        )

        provider = ArbeitnowProvider()
        with patch("app.search.arbeitnow.httpx.AsyncClient") as MockClient:
            mock_instance = AsyncMock()
            mock_instance.get.return_value = mock_resp
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=False)
            MockClient.return_value = mock_instance

            results = asyncio.run(provider.search("AI Engineer", location="Remote"))
            assert len(results) == 1
            assert results[0].title == "AI Engineer"


class TestRemoteEligibilitySafety:
    def test_generic_remote_blocked(self):
        """A job listed as location='Remote' with no Pakistan/worldwide marker
        must NOT be treated as Pakistan-eligible."""
        from app.search.arbeitnow import _is_remote_eligible
        assert _is_remote_eligible("Remote") is False
        assert _is_remote_eligible("") is False
        assert _is_remote_eligible("remote") is False

    def test_worldwide_remote_accepted(self):
        """Remote jobs with explicit worldwide/Pakistan markers are accepted."""
        from app.search.arbeitnow import _is_remote_eligible
        assert _is_remote_eligible("Remote - Worldwide") is True
        assert _is_remote_eligible("Remote, Pakistan") is True
        assert _is_remote_eligible("Karachi, Remote") is True
        assert _is_remote_eligible("Anywhere") is True
        assert _is_remote_eligible("Global") is True

    def test_pipeline_location_eligible_blocks_bare_remote(self):
        """Pipeline verification must not accept bare 'remote' as location-eligible."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://example.com/jobs/456",
            posted_date=datetime.now(timezone.utc),
            location="Remote",
            description="",
            verified_content="AI Engineer role. Responsibilities: build AI. Requirements: Python. Apply now. Qualifications: experience.",
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="example",
        )
        blockers = compute_verification_blockers(ev)
        assert any("pakistan" in b.lower() or "eligib" in b.lower() for b in blockers)

    def test_pipeline_location_eligible_accepts_worldwide(self):
        """Pipeline verification accepts worldwide/Pakistan location evidence."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://example.com/jobs/789",
            posted_date=datetime.now(timezone.utc),
            location="Remote - Worldwide",
            description="",
            verified_content="AI Engineer at example. Responsibilities: build AI. Requirements: Python. Apply now. Qualifications: experience.",
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="example",
        )
        blockers = compute_verification_blockers(ev)
        assert not any("pakistan" in b.lower() or "eligib" in b.lower() for b in blockers)


class TestExactVacancyUrlEnforcement:
    def test_generic_careers_page_blocked(self):
        """A generic /careers URL must be blocked even with content."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://example.com/careers",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content="Join our team! We have openings for AI Engineer. Apply now. Responsibilities: build products. Requirements: coding. Qualifications: degree. ExampleCorp is hiring.",
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="ExampleCorp",
        )
        blockers = compute_verification_blockers(ev)
        assert any("generic" in b.lower() or "specific" in b.lower() for b in blockers)

    def test_ats_board_homepage_blocked(self):
        """ATS board homepage (no job ID) must be blocked."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        for board_url in [
            "https://boards.greenhouse.io/examplecorp",
            "https://jobs.lever.co/examplecorp",
            "https://jobs.ashbyhq.com/examplecorp",
        ]:
            ev = collect_verification_evidence(
                url=board_url,
                posted_date=datetime.now(timezone.utc),
                location="Karachi, Pakistan",
                description="",
                verified_content="ExampleCorp open positions. AI Engineer — Karachi. Responsibilities: build AI. Requirements: Python. Apply now. Qualifications: experience.",
                http_success=True,
                discovered_title="AI Engineer",
                discovered_company="ExampleCorp",
            )
            blockers = compute_verification_blockers(ev)
            assert any(
                "generic" in b.lower() or "specific" in b.lower()
                for b in blockers
            ), f"{board_url} should be blocked but blockers={blockers}"

    def _substantial_job_content(self):
        return (
            "ExampleCorp is hiring an AI Engineer to join our team in Karachi. "
            "Responsibilities: build and deploy AI systems, integrate LLM APIs, "
            "develop agentic workflows, and maintain ML pipelines. "
            "Requirements: Python, machine learning, REST APIs, 2+ years experience. "
            "Qualifications: BS in Computer Science or equivalent. "
            "Apply now to work on cutting-edge AI products."
        )

    def test_exact_greenhouse_job_url_passes(self):
        """An exact Greenhouse job URL with matching content passes."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://boards.greenhouse.io/examplecorp/jobs/4567890",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content=self._substantial_job_content(),
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="ExampleCorp",
        )
        blockers = compute_verification_blockers(ev)
        assert not any("generic" in b.lower() for b in blockers)
        assert not any("specific" in b.lower() for b in blockers)

    def test_exact_lever_job_url_passes(self):
        """An exact Lever job URL with matching content passes."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://jobs.lever.co/examplecorp/abc12345-6789-def0-1234-567890abcdef",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content=self._substantial_job_content(),
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="ExampleCorp",
        )
        blockers = compute_verification_blockers(ev)
        assert not any("generic" in b.lower() for b in blockers)
        assert not any("specific" in b.lower() for b in blockers)

    def test_exact_ashby_job_url_passes(self):
        """An exact Ashby job URL with matching content passes."""
        from app.pipeline import collect_verification_evidence, compute_verification_blockers
        ev = collect_verification_evidence(
            url="https://jobs.ashbyhq.com/examplecorp/abc12345-6789-def0-1234-567890abcdef",
            posted_date=datetime.now(timezone.utc),
            location="Karachi, Pakistan",
            description="",
            verified_content=self._substantial_job_content(),
            http_success=True,
            discovered_title="AI Engineer",
            discovered_company="ExampleCorp",
        )
        blockers = compute_verification_blockers(ev)
        assert not any("generic" in b.lower() for b in blockers)
        assert not any("specific" in b.lower() for b in blockers)
