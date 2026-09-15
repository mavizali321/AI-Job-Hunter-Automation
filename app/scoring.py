"""Deterministic job scoring engine based on rules/SCORING.md rubric."""

import re

ROLE_KEYWORDS_A = [
    "forward deployed", "fde", "ai deployment",
]
ROLE_KEYWORDS_B = [
    "ai integration", "ai automation", "ai solutions", "solutions engineer",
    "llm engineer", "ai engineer",
]
ROLE_KEYWORDS_C = [
    "applied ai", "ai software",
]
ROLE_KEYWORDS_D = [
    "software engineer", "full stack", "fullstack", "backend", "frontend",
]

TECH_STRONG = {
    "llm", "gpt", "openai", "langchain", "prompt", "agent", "agentic",
    "api", "rest", "integration", "automation", "rag", "vector",
    "function calling", "tool use", "structured output",
}
TECH_FRONTEND = {
    "react", "angular", "vue", "javascript", "typescript", "html", "css",
    "frontend", "front-end", "ui", "ux",
}
TECH_NEGATIVE = {
    "kubernetes", "k8s", "devops", "mlops", "pytorch", "tensorflow",
    "deep learning", "computer vision", "phd", "doctorate",
}

PAKISTAN_LOCATIONS = {"karachi", "lahore", "islamabad", "pakistan"}
SENIOR_MARKERS = {"senior", "staff", "principal", "lead", "director", "manager", "head of", "vp"}

SENSITIVE_FIELDS = {
    "work authorization", "visa", "sponsorship", "disability", "gender",
    "race", "ethnicity", "veteran", "criminal", "background check",
    "salary expectation", "compensation", "relocation",
}


def _lower_text(*texts: str | None) -> str:
    return " ".join((t or "").lower() for t in texts)


def score_role_relevance(title: str, description: str | None = None) -> tuple[float, str]:
    t = _lower_text(title)
    d = _lower_text(description)
    combined = t + " " + d

    if any(k in t for k in ROLE_KEYWORDS_A):
        return 30.0, "Exact/near-exact FDE or AI deployment role"
    if any(k in t for k in ROLE_KEYWORDS_C):
        return 20.0, "AI software/applied AI role"
    if any(k in t for k in ROLE_KEYWORDS_B):
        return 25.0, "AI integration/automation/solutions role"
    if any(k in t for k in ROLE_KEYWORDS_D) and any(k in combined for k in ["ai", "llm", "ml", "machine learning"]):
        return 15.0, "Adjacent software role with substantial AI work"
    if any(k in t for k in ROLE_KEYWORDS_D):
        return 10.0, "Software role with limited AI relevance"
    return 0.0, "Unrelated role"


def score_technical_match(description: str | None, requirements: str | None = None) -> tuple[float, str]:
    text = _lower_text(description, requirements)
    if not text.strip():
        return 10.0, "No description available for matching"

    strong_hits = sum(1 for k in TECH_STRONG if k in text)
    frontend_hits = sum(1 for k in TECH_FRONTEND if k in text)
    negative_hits = sum(1 for k in TECH_NEGATIVE if k in text)

    if strong_hits >= 4:
        score = 25.0
        reason = "Strong overlap with LLM/API/agentic + software integration"
    elif strong_hits >= 2:
        score = 20.0
        reason = "Good overlap with AI integration, some gaps possible"
    elif strong_hits >= 1 and frontend_hits >= 2:
        score = 15.0
        reason = "Mixed match: some AI + frontend overlap"
    elif frontend_hits >= 3:
        score = 10.0
        reason = "Mostly frontend with limited AI relevance"
    else:
        score = 5.0
        reason = "Major mismatch with profile technologies"

    if negative_hits >= 2:
        score = max(0, score - 5)
        reason += "; heavy requirements outside profile"

    return score, reason


def score_experience_fit(title: str, description: str | None, requirements: str | None = None) -> tuple[float, str]:
    text = _lower_text(title, description, requirements)

    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text)
    required_years = max((int(y) for y in years_match), default=0)

    has_senior = any(s in text for s in SENIOR_MARKERS)

    if any(k in text for k in ["intern", "trainee", "entry", "junior", "new grad", "graduate"]):
        return 15.0, "Entry-level/intern — strong fit"
    if required_years <= 2 and not has_senior:
        return 13.0, f"Requires {required_years} years — within range"
    if required_years <= 3 and not has_senior:
        return 10.0, f"Requires {required_years} years — borderline fit"
    if required_years <= 5 and not has_senior:
        return 7.0, f"Requires {required_years} years — stretch"
    if has_senior or required_years > 5:
        return 2.0, f"Senior/staff level or {required_years}+ years required — likely too senior"
    return 10.0, "Experience requirements unclear"


def score_location_fit(location: str | None, remote_policy: str | None, description: str | None = None) -> tuple[float, str]:
    text = _lower_text(location, remote_policy, description)

    pakistan_match = any(loc in text for loc in PAKISTAN_LOCATIONS)
    is_remote = "remote" in text

    if pakistan_match and not is_remote:
        return 15.0, "Karachi/Pakistan onsite/hybrid"
    if pakistan_match and is_remote:
        return 15.0, "Pakistan remote"
    if is_remote and ("worldwide" in text or "global" in text or "anywhere" in text):
        if pakistan_match or "pakistan" in text:
            return 12.0, "Worldwide remote explicitly accepting Pakistan"
        return 6.0, "Ambiguous worldwide remote — Pakistan eligibility unclear"
    if is_remote:
        return 6.0, "Remote — Pakistan eligibility unclear"
    if "sponsorship" in text or "relocation" in text or "visa" in text:
        return 6.0, "Potential sponsorship/relocation route"
    return 0.0, "Location explicitly excludes Pakistan with no route"


def score_company_quality(company: str, description: str | None = None) -> tuple[float, str]:
    text = _lower_text(company, description)
    score = 5.0
    reasons = []

    if any(k in text for k in ["yc", "y combinator", "series a", "series b", "funded"]):
        score += 3
        reasons.append("well-funded/YC-backed")
    if any(k in text for k in ["startup", "early stage"]):
        score += 1
        reasons.append("startup opportunity")
    if any(k in text for k in ["fortune 500", "enterprise", "global"]):
        score += 2
        reasons.append("established company")

    return min(score, 10.0), "; ".join(reasons) if reasons else "Standard company"


def score_entry_level(title: str, description: str | None = None, requirements: str | None = None) -> tuple[float, str]:
    text = _lower_text(title, description, requirements)

    if any(k in text for k in ["intern", "trainee", "new grad", "graduate", "entry level", "entry-level", "junior"]):
        return 5.0, "Explicitly entry-level accessible"
    if any(k in text for k in ["no experience required", "0-1 year", "0-2 year"]):
        return 4.0, "Low experience bar"

    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text)
    required_years = max((int(y) for y in years_match), default=0)
    if required_years <= 2:
        return 3.0, "Low years requirement — accessible"
    if required_years <= 4:
        return 1.0, "Moderate experience required"
    return 0.0, "High experience bar"


def hard_reject(title: str, description: str | None, location: str | None, requirements: str | None = None) -> str | None:
    text = _lower_text(title, description, requirements)
    t = _lower_text(title)

    has_senior = any(s in t for s in SENIOR_MARKERS)
    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text)
    required_years = max((int(y) for y in years_match), default=0)
    if has_senior and required_years > 5:
        return "Senior/staff/principal requiring 5+ years — candidate not qualified"

    loc_text = _lower_text(location, description)
    pakistan_match = any(loc in loc_text for loc in PAKISTAN_LOCATIONS)
    is_remote = "remote" in loc_text
    has_sponsorship = "sponsorship" in loc_text or "relocation" in loc_text
    if not pakistan_match and not is_remote and not has_sponsorship:
        us_only = any(k in loc_text for k in ["united states", "us only", "usa only", "u.s. only"])
        if us_only:
            return "Location explicitly excludes Pakistan with no sponsorship/relocation route"

    return None


def score_job(
    title: str,
    company: str,
    description: str | None = None,
    requirements: str | None = None,
    location: str | None = None,
    remote_policy: str | None = None,
) -> dict:
    rejection = hard_reject(title, description, location, requirements)
    if rejection:
        return {
            "total": 0,
            "decision": "SKIP",
            "rejection_reason": rejection,
            "breakdown": {},
        }

    role_score, role_reason = score_role_relevance(title, description)
    tech_score, tech_reason = score_technical_match(description, requirements)
    exp_score, exp_reason = score_experience_fit(title, description, requirements)
    loc_score, loc_reason = score_location_fit(location, remote_policy, description)
    comp_score, comp_reason = score_company_quality(company, description)
    entry_score, entry_reason = score_entry_level(title, description, requirements)

    total = role_score + tech_score + exp_score + loc_score + comp_score + entry_score

    if total >= 90:
        decision = "PRIORITY_APPLY"
    elif total >= 80:
        decision = "APPLY"
    elif total >= 70:
        decision = "REVIEW"
    else:
        decision = "SKIP"

    return {
        "total": total,
        "decision": decision,
        "rejection_reason": None,
        "breakdown": {
            "role_relevance": {"score": role_score, "max": 30, "reason": role_reason},
            "technical_match": {"score": tech_score, "max": 25, "reason": tech_reason},
            "experience_fit": {"score": exp_score, "max": 15, "reason": exp_reason},
            "location_fit": {"score": loc_score, "max": 15, "reason": loc_reason},
            "company_quality": {"score": comp_score, "max": 10, "reason": comp_reason},
            "entry_level": {"score": entry_score, "max": 5, "reason": entry_reason},
        },
    }
