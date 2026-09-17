"""Parse PROFILE.md and resume PDF into a structured CandidateProfile."""

import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import CandidateProfile

PROFILE_MD_PATH = Path("profile/PROFILE.md")
RESUME_PDF_PATH = Path("profile/Maviz-Ali-Resume-Original.pdf")


def compute_file_checksum(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest().upper()


def compute_combined_checksum(*paths: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(str(pp) for pp in paths):
        path = Path(p)
        if path.exists():
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
    return h.hexdigest().upper()


def extract_resume_text(pdf_path: Path) -> str:
    try:
        import pdfplumber
        text_parts: list[str] = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)
    except ImportError:
        return ""
    except Exception:
        return ""


def parse_profile_md(text: str) -> dict:
    sections: dict[str, str] = {}
    current_header = ""
    current_lines: list[str] = []

    for line in text.split("\n"):
        h1 = re.match(r"^#\s+(.+)", line)
        h2 = re.match(r"^##\s+(.+)", line)
        h3 = re.match(r"^###\s+(.+)", line)
        if h1 or h2:
            if current_header:
                sections[current_header] = "\n".join(current_lines)
            current_header = (h1 or h2).group(1).strip().lower()
            current_lines = []
        elif h3:
            current_lines.append(line)
        else:
            current_lines.append(line)

    if current_header:
        sections[current_header] = "\n".join(current_lines)

    profile: dict = {}

    _parse_contact(sections, profile)
    _parse_target_roles(sections, profile)
    _parse_location_preferences(sections, profile)
    _parse_experience(sections, profile)
    _parse_education(sections, profile)
    _parse_skills(sections, profile)
    _parse_projects(sections, profile)
    _parse_do_not_claim(sections, profile)
    _parse_positioning(sections, profile)

    return profile


def _parse_contact(sections: dict, profile: dict) -> None:
    contact_text = sections.get("contact", "")
    profile["name"] = _extract_field(contact_text, "Name")
    profile["email"] = _extract_field(contact_text, "Email")
    profile["phone"] = _extract_field(contact_text, "Phone")
    profile["linkedin"] = _extract_field(contact_text, "LinkedIn")


def _extract_field(text: str, field: str) -> str:
    m = re.search(rf"[-*]\s*{field}\s*:\s*(.+)", text, re.IGNORECASE)
    return m.group(1).strip() if m else ""


def _parse_positioning(sections: dict, profile: dict) -> None:
    text = sections.get("current positioning", "")
    profile["positioning"] = text.strip()


def _parse_target_roles(sections: dict, profile: dict) -> None:
    text = sections.get("target roles", "")
    roles: list[dict] = []
    current_priority = "C"

    for line in text.split("\n"):
        pm = re.match(r"###\s+Priority\s+([ABC])", line, re.IGNORECASE)
        if pm:
            current_priority = pm.group(1).upper()
            continue
        rm = re.match(r"[-*]\s+(.+)", line)
        if rm:
            role_title = rm.group(1).strip()
            if role_title:
                roles.append({"title": role_title, "priority": current_priority})

    profile["target_roles"] = roles


def _parse_location_preferences(sections: dict, profile: dict) -> None:
    text = sections.get("location priority", "")
    prefs: list[dict] = []
    for line in text.split("\n"):
        m = re.match(r"\d+\.\s+(.+)", line)
        if m:
            loc = m.group(1).strip()
            prefs.append({"location": loc, "priority": len(prefs) + 1})
    profile["location_preferences"] = prefs


def _parse_experience(sections: dict, profile: dict) -> None:
    text = sections.get("experience", "")
    entries: list[dict] = []
    current_entry: dict | None = None

    for line in text.split("\n"):
        hm = re.match(r"###\s+(.+?)(?:\s*[-—]\s*(.+))?$", line)
        if hm:
            if current_entry:
                entries.append(current_entry)
            company = hm.group(1).strip()
            title = hm.group(2).strip() if hm.group(2) else ""
            current_entry = {"company": company, "title": title, "dates": "", "location": "", "bullets": []}
            continue
        if current_entry:
            dm = re.match(r"[-*]\s*(.+\d{4}.+\d{4}.+)", line)
            if dm and not current_entry["dates"]:
                date_line = dm.group(1).strip()
                parts = [p.strip() for p in re.split(r"[|]", date_line)]
                current_entry["dates"] = parts[0] if parts else date_line
                if len(parts) > 1:
                    current_entry["location"] = parts[1]
                continue
            bm = re.match(r"[-*]\s+(.+)", line)
            if bm:
                current_entry["bullets"].append(bm.group(1).strip())

    if current_entry:
        entries.append(current_entry)

    profile["experience"] = entries

    total_years = 0.0
    for entry in entries:
        dates = entry.get("dates", "")
        years_match = re.findall(r"(\d{4})", dates)
        if len(years_match) >= 2:
            start_year = int(years_match[0])
            end_year = int(years_match[-1])
            if end_year >= start_year:
                total_years += end_year - start_year
        elif "present" in dates.lower() and years_match:
            import datetime
            start_year = int(years_match[0])
            total_years += datetime.datetime.now().year - start_year

    profile["total_experience_years"] = max(total_years, 0)

    if total_years <= 2:
        profile["seniority"] = "junior"
    elif total_years <= 5:
        profile["seniority"] = "mid-junior"
    else:
        profile["seniority"] = "mid"


def _parse_education(sections: dict, profile: dict) -> None:
    text = sections.get("education", "")
    entries: list[dict] = []
    current: dict | None = None

    for line in text.split("\n"):
        hm = re.match(r"###\s+(.+)", line)
        if hm:
            if current:
                entries.append(current)
            current = {"institution": hm.group(1).strip(), "degree": "", "dates": ""}
            continue
        if current:
            dm = re.match(r"[-*]\s+(.+)", line)
            if dm:
                detail = dm.group(1).strip()
                parts = [p.strip() for p in re.split(r"[|]", detail)]
                if not current["degree"]:
                    current["degree"] = parts[0]
                    if len(parts) > 1:
                        current["dates"] = parts[1]
                elif not current["dates"]:
                    current["dates"] = detail

    if current:
        entries.append(current)
    profile["education"] = entries


def _parse_skills(sections: dict, profile: dict) -> None:
    text = sections.get("technical evidence", "")
    skills: dict[str, list[str]] = {}
    current_cat = ""

    for line in text.split("\n"):
        hm = re.match(r"###\s+(.+)", line)
        if hm:
            current_cat = hm.group(1).strip().lower()
            skills[current_cat] = []
            continue
        bm = re.match(r"[-*]\s+(.+)", line)
        if bm and current_cat:
            skills[current_cat].append(bm.group(1).strip())

    profile["skills"] = skills


def _parse_projects(sections: dict, profile: dict) -> None:
    text = sections.get("ai projects", "")
    projects: list[dict] = []
    current: dict | None = None

    for line in text.split("\n"):
        hm = re.match(r"###\s+(.+)", line)
        if hm:
            if current:
                projects.append(current)
            current = {"name": hm.group(1).strip(), "bullets": []}
            continue
        if current:
            bm = re.match(r"[-*]\s+(.+)", line)
            if bm:
                current["bullets"].append(bm.group(1).strip())

    if current:
        projects.append(current)
    profile["projects"] = projects


def _parse_do_not_claim(sections: dict, profile: dict) -> None:
    text = sections.get("do not claim without user confirmation", "")
    items: list[str] = []
    for line in text.split("\n"):
        bm = re.match(r"[-*]\s+(.+)", line)
        if bm:
            items.append(bm.group(1).strip())
    profile["do_not_claim"] = items


def _all_skill_keywords(skills: dict[str, list[str]]) -> set[str]:
    keywords: set[str] = set()
    for cat, items in skills.items():
        for item in items:
            for token in re.split(r"[,;/()\[\]]", item.lower()):
                cleaned = token.strip().strip(".")
                if len(cleaned) > 1:
                    keywords.add(cleaned)
    return keywords


def verify_claims_against_resume(profile_data: dict, resume_text: str) -> list[str]:
    """Return list of skills from profile that are NOT found in the resume text."""
    if not resume_text:
        return []
    resume_lower = resume_text.lower()
    unverified: list[str] = []
    skills = profile_data.get("skills", {})
    all_keywords = _all_skill_keywords(skills)
    for kw in sorted(all_keywords):
        if kw not in resume_lower and len(kw) > 3:
            unverified.append(kw)
    return unverified


def build_candidate_profile(db: Session, force: bool = False) -> CandidateProfile | None:
    if not RESUME_PDF_PATH.exists() or not PROFILE_MD_PATH.exists():
        return None

    checksum = compute_combined_checksum(RESUME_PDF_PATH, PROFILE_MD_PATH)

    if not force:
        existing = db.query(CandidateProfile).filter(
            CandidateProfile.resume_checksum == checksum
        ).first()
        if existing:
            return existing

    profile_text = PROFILE_MD_PATH.read_text(encoding="utf-8")
    profile_data = parse_profile_md(profile_text)

    resume_text = extract_resume_text(RESUME_PDF_PATH)

    all_keywords = _all_skill_keywords(profile_data.get("skills", {}))
    unverified = verify_claims_against_resume(profile_data, resume_text)
    unverified_set = set(unverified)
    confirmed = sorted(kw for kw in all_keywords if kw not in unverified_set)
    profile_only = sorted(unverified_set & all_keywords)

    db.query(CandidateProfile).delete()
    db.flush()

    cp = CandidateProfile(
        resume_checksum=checksum,
        name=profile_data.get("name", ""),
        email=profile_data.get("email", ""),
        phone=profile_data.get("phone", ""),
        linkedin=profile_data.get("linkedin", ""),
        seniority=profile_data.get("seniority", "junior"),
        total_experience_years=profile_data.get("total_experience_years"),
        skills=profile_data.get("skills", {}),
        confirmed_skills=confirmed,
        profile_only_skills=profile_only,
        experience=profile_data.get("experience", []),
        education=profile_data.get("education", []),
        projects=profile_data.get("projects", []),
        target_roles=profile_data.get("target_roles", []),
        location_preferences=profile_data.get("location_preferences", []),
        do_not_claim=profile_data.get("do_not_claim", []),
        raw_resume_text=resume_text[:10000] if resume_text else None,
    )
    db.add(cp)
    db.commit()
    return cp


def get_or_refresh_profile(db: Session) -> CandidateProfile | None:
    if not RESUME_PDF_PATH.exists():
        return db.query(CandidateProfile).first()

    current_checksum = compute_combined_checksum(RESUME_PDF_PATH, PROFILE_MD_PATH)
    existing = db.query(CandidateProfile).first()

    if existing and existing.resume_checksum == current_checksum:
        return existing

    return build_candidate_profile(db, force=True)


def generate_search_queries_from_profile(
    profile: CandidateProfile,
    locations: list[str] | None = None,
) -> list[tuple[str, str]]:
    if locations is None:
        from app.config import settings
        locations = settings.job_locations_list

    titles: list[str] = []
    priority_order = {"A": 0, "B": 1, "C": 2}
    sorted_roles = sorted(
        profile.target_roles or [],
        key=lambda r: priority_order.get(r.get("priority", "C"), 2),
    )
    for role in sorted_roles:
        t = role.get("title", "")
        if t and t not in titles:
            titles.append(t)

    if not titles:
        titles = ["Software Engineer"]

    queries: list[tuple[str, str]] = []
    for title in titles:
        for loc in locations:
            queries.append((title, loc))

    return queries


def match_job_to_profile(
    profile: CandidateProfile,
    title: str,
    description: str | None = None,
    requirements: str | None = None,
    location: str | None = None,
    remote_policy: str | None = None,
) -> dict:
    text = " ".join((t or "").lower() for t in [title, description, requirements])

    if profile.confirmed_skills:
        candidate_keywords = set(profile.confirmed_skills)
    else:
        candidate_keywords = _all_skill_keywords(profile.skills or {})

    job_tokens = set()
    for token in re.split(r"[\s,;/()\[\].]+", text):
        cleaned = token.strip().lower()
        if len(cleaned) > 1:
            job_tokens.add(cleaned)

    job_bigrams = set()
    words = text.split()
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i+1]}"
        job_bigrams.add(bigram)

    all_job_terms = job_tokens | job_bigrams

    matched: list[str] = []
    for kw in sorted(candidate_keywords):
        if kw in text:
            matched.append(kw)

    required_keywords = _extract_required_skills(text)
    missing: list[str] = []
    for req in required_keywords:
        if req not in candidate_keywords and not any(req in ck for ck in candidate_keywords):
            missing.append(req)

    seniority_fit = _assess_seniority_fit(profile, title, text)
    location_fit = _assess_location_fit(profile, location, remote_policy, description)

    do_not_claim = profile.do_not_claim or []
    blocked_fields: list[str] = []
    for dnc in do_not_claim:
        dnc_lower = dnc.lower()
        if any(term in text for term in dnc_lower.split(",")):
            blocked_fields.append(dnc)

    match_score = _compute_match_score(matched, missing, seniority_fit, location_fit, profile)

    return {
        "matched_skills": matched,
        "missing_skills": missing,
        "seniority_fit": seniority_fit,
        "location_fit": location_fit,
        "match_score": match_score,
        "blocked_fields": blocked_fields,
        "candidate_skill_count": len(candidate_keywords),
        "matched_count": len(matched),
        "missing_count": len(missing),
    }


def _extract_required_skills(text: str) -> list[str]:
    known_tech = {
        "python", "java", "c++", "c#", "go", "golang", "rust", "ruby",
        "kubernetes", "k8s", "docker", "aws", "azure", "gcp",
        "tensorflow", "pytorch", "scikit-learn", "pandas", "numpy",
        "react", "angular", "vue", "node", "express", "django", "flask",
        "sql", "postgresql", "mongodb", "redis", "elasticsearch",
        "machine learning", "deep learning", "computer vision", "nlp",
        "llm", "ai", "ml", "api", "rest", "graphql",
        "typescript", "javascript", "html", "css",
        "agile", "scrum", "ci/cd", "devops",
        "prompt engineering", "rag", "langchain", "openai",
        "figma", "sketch", "adobe",
    }
    found: list[str] = []
    for tech in sorted(known_tech):
        if tech in text:
            found.append(tech)
    return found


def _assess_seniority_fit(profile: CandidateProfile, title: str, text: str) -> str:
    t_lower = title.lower()
    senior_markers = {"senior", "staff", "principal", "lead", "director", "head of", "vp", "manager"}

    has_senior = any(s in t_lower for s in senior_markers)
    years_match = re.findall(r"(\d+)\+?\s*(?:years?|yrs?)", text)
    required_years = max((int(y) for y in years_match), default=0)

    candidate_years = profile.total_experience_years or 0

    if has_senior and required_years > 5:
        return "poor — senior role, candidate is " + profile.seniority
    if required_years > candidate_years + 2:
        return f"stretch — requires {required_years}yr, candidate has ~{candidate_years:.0f}yr"
    if any(k in t_lower for k in ["junior", "entry", "intern", "trainee", "graduate"]):
        return "strong — entry-level role"
    if required_years <= 3:
        return "good — experience within range"
    return f"moderate — requires {required_years}yr"


def _assess_location_fit(
    profile: CandidateProfile,
    location: str | None,
    remote_policy: str | None,
    description: str | None,
) -> str:
    text = " ".join((t or "").lower() for t in [location, remote_policy, description])
    pakistan_markers = {"karachi", "lahore", "islamabad", "pakistan"}
    is_pakistan = any(m in text for m in pakistan_markers)
    is_remote = "remote" in text

    if is_pakistan and not is_remote:
        return "strong — Pakistan onsite/hybrid"
    if is_pakistan and is_remote:
        return "strong — Pakistan remote"
    if is_remote and ("worldwide" in text or "global" in text or "anywhere" in text):
        return "moderate — worldwide remote, Pakistan eligibility unclear"
    if is_remote:
        return "moderate — remote, Pakistan eligibility unclear"
    no_sponsorship = "no sponsorship" in text or "not sponsor" in text or "without sponsorship" in text
    if not no_sponsorship and ("sponsorship" in text or "relocation" in text):
        return "possible — sponsorship/relocation mentioned"
    return "poor — no Pakistan/remote eligibility"


def _compute_match_score(
    matched: list[str],
    missing: list[str],
    seniority_fit: str,
    location_fit: str,
    profile: CandidateProfile,
) -> float:
    score = 0.0

    skill_ratio = len(matched) / max(len(matched) + len(missing), 1)
    score += skill_ratio * 40

    if "strong" in seniority_fit:
        score += 25
    elif "good" in seniority_fit:
        score += 20
    elif "moderate" in seniority_fit:
        score += 10
    elif "stretch" in seniority_fit:
        score += 5

    if "strong" in location_fit:
        score += 25
    elif "moderate" in location_fit:
        score += 15
    elif "possible" in location_fit:
        score += 10

    if len(matched) >= 5:
        score += 10
    elif len(matched) >= 3:
        score += 5

    return min(round(score, 1), 100.0)
