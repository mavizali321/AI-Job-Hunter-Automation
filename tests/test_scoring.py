"""Tests for the scoring engine."""

from app.scoring import (
    score_job, score_role_relevance, score_technical_match,
    score_experience_fit, score_location_fit, hard_reject,
)


class TestRoleRelevance:
    def test_fde_exact_match(self):
        score, _ = score_role_relevance("Forward Deployed Engineer")
        assert score == 30

    def test_ai_integration(self):
        score, _ = score_role_relevance("AI Integration Engineer")
        assert score == 25

    def test_applied_ai(self):
        score, _ = score_role_relevance("Applied AI Engineer")
        assert score == 20

    def test_software_with_ai(self):
        score, _ = score_role_relevance("Software Engineer", "Work on AI and LLM features")
        assert score == 15

    def test_unrelated(self):
        score, _ = score_role_relevance("Marketing Manager")
        assert score == 0


class TestTechnicalMatch:
    def test_strong_ai_overlap(self):
        desc = "LLM integration, API automation, agentic workflows, prompt engineering, RAG"
        score, _ = score_technical_match(desc)
        assert score >= 20

    def test_frontend_only(self):
        desc = "React, Angular, Vue, JavaScript, CSS, HTML, responsive design, UI components"
        score, _ = score_technical_match(desc)
        assert score <= 15

    def test_no_description(self):
        score, _ = score_technical_match(None)
        assert score == 10


class TestExperienceFit:
    def test_intern(self):
        score, _ = score_experience_fit("AI Intern", "Intern position")
        assert score == 15

    def test_within_range(self):
        score, _ = score_experience_fit("AI Engineer", "2 years experience required")
        assert score >= 10

    def test_too_senior(self):
        score, _ = score_experience_fit("Senior Staff Engineer", "8+ years required")
        assert score <= 5


class TestLocationFit:
    def test_karachi(self):
        score, _ = score_location_fit("Karachi, Pakistan", "onsite")
        assert score == 15

    def test_pakistan_remote(self):
        score, _ = score_location_fit("Pakistan", "remote")
        assert score == 15

    def test_worldwide_ambiguous(self):
        score, _ = score_location_fit("Worldwide", "remote")
        assert score == 6

    def test_no_route(self):
        score, _ = score_location_fit("New York, NY", "onsite")
        assert score == 0


class TestHardReject:
    def test_us_only(self):
        result = hard_reject("Engineer", "US only position", "United States")
        assert result is not None

    def test_senior_too_many_years(self):
        result = hard_reject("Senior Staff Engineer", "8+ years required", None, "8+ years of experience")
        assert result is not None

    def test_karachi_no_reject(self):
        result = hard_reject("AI Engineer", "Great role", "Karachi, Pakistan")
        assert result is None


class TestScoreJob:
    def test_high_score_fde_karachi(self):
        result = score_job(
            title="Forward Deployed Engineer",
            company="Palantir",
            description="LLM integration, API automation, agentic workflows, prompt engineering, RAG pipeline",
            location="Karachi, Pakistan",
            remote_policy="onsite",
        )
        assert result["total"] >= 70
        assert result["decision"] in ("APPLY", "PRIORITY_APPLY", "REVIEW")

    def test_skip_unrelated(self):
        result = score_job(
            title="Marketing Manager",
            company="Random Co",
            description="Social media management",
            location="New York, NY",
            remote_policy="onsite",
        )
        assert result["decision"] == "SKIP"

    def test_skip_too_senior(self):
        result = score_job(
            title="Senior Staff Principal Engineer",
            company="BigCo",
            description="10+ years of distributed systems, Kubernetes, DevOps",
            requirements="10+ years of experience",
            location="United States only",
            remote_policy="onsite",
        )
        assert result["total"] == 0 or result["decision"] == "SKIP"

    def test_breakdown_present(self):
        result = score_job(
            title="AI Engineer Intern",
            company="StartupCo",
            description="LLM API integration, prompt engineering",
            location="Karachi",
        )
        assert "breakdown" in result
        assert "role_relevance" in result["breakdown"]
        assert "technical_match" in result["breakdown"]
