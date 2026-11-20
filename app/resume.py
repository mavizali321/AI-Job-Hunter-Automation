"""Resume upload, text extraction, and profile creation from resume."""

import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.config import settings
from app.models import User, CandidateProfile


UPLOAD_DIR = Path("uploads")

TECH_KEYWORDS = {
    "python", "javascript", "typescript", "react", "angular", "vue",
    "node", "express", "fastapi", "django", "flask", "sql", "postgresql",
    "mongodb", "redis", "docker", "kubernetes", "aws", "gcp", "azure",
    "git", "ci/cd", "rest", "graphql", "html", "css", "sass",
    "openai", "langchain", "llm", "gpt", "ai", "machine learning", "ml",
    "nlp", "rag", "vector", "embeddings", "prompt engineering",
    "automation", "selenium", "playwright", "scrapy",
    "java", "c#", "c++", "go", "rust", "php", "ruby",
    "tensorflow", "pytorch", "pandas", "numpy", "scikit-learn",
    "sap", "abap", "fiori", "hana", "erp",
    "agile", "scrum", "jira", "linux", "bash",
    "api", "microservices", "serverless", "lambda",
}

ROLE_KEYWORDS = [
    "ai engineer", "software engineer", "full stack developer",
    "backend developer", "frontend developer", "python developer",
    "data scientist", "ml engineer", "devops engineer",
    "forward deployed engineer", "solutions engineer",
    "web developer", "application developer", "technical consultant",
    "sap consultant", "sap developer",
]


def save_resume(user: User, file_content: bytes, filename: str) -> Path:
    user_dir = UPLOAD_DIR / str(user.id)
    user_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(filename).suffix.lower() or ".pdf"
    safe_name = f"resume{ext}"
    file_path = user_dir / safe_name

    file_path.write_bytes(file_content)
    return file_path


def extract_text_from_pdf(file_path: Path) -> str:
    try:
        import pdfplumber
        text_parts = []
        with pdfplumber.open(str(file_path)) as pdf:
            for page in pdf.pages:
                page_text = page.extract_text()
                if page_text:
                    text_parts.append(page_text)
        return "\n".join(text_parts)
    except Exception:
        return ""


def compute_checksum(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_skills_from_text(text: str) -> list[str]:
    text_lower = text.lower()
    found = []
    for skill in TECH_KEYWORDS:
        if skill in text_lower:
            found.append(skill)
    return sorted(set(found))


def extract_experience_years(text: str) -> float:
    text_lower = text.lower()
    patterns = [
        r"(\d+)\+?\s*years?\s*(?:of\s+)?experience",
        r"experience[:\s]+(\d+)\+?\s*years?",
        r"(\d+)\+?\s*years?\s*(?:of\s+)?(?:professional|work)",
    ]
    years = []
    for pattern in patterns:
        matches = re.findall(pattern, text_lower)
        years.extend(int(y) for y in matches)
    return max(years) if years else 0.0


def extract_name_from_text(text: str) -> str:
    lines = text.strip().split("\n")
    for line in lines[:5]:
        line = line.strip()
        if line and len(line) < 50 and not any(c.isdigit() for c in line):
            words = line.split()
            if 1 < len(words) <= 4 and all(w[0].isupper() for w in words if w):
                return line
    return ""


def extract_email_from_text(text: str) -> str:
    match = re.search(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", text)
    return match.group(0) if match else ""


def extract_phone_from_text(text: str) -> str:
    match = re.search(r"[\+]?[\d\s\-\(\)]{10,15}", text)
    return match.group(0).strip() if match else ""


def extract_linkedin_from_text(text: str) -> str:
    match = re.search(r"linkedin\.com/in/[\w-]+", text, re.I)
    return match.group(0) if match else ""


def guess_target_roles(skills: list[str], text: str) -> list[str]:
    text_lower = text.lower()
    roles = []
    for role in ROLE_KEYWORDS:
        if role in text_lower:
            roles.append(role.title())

    if not roles:
        skill_set = set(s.lower() for s in skills)
        if skill_set & {"llm", "openai", "langchain", "gpt", "ai", "rag"}:
            roles.append("AI Engineer")
        if skill_set & {"python", "fastapi", "django", "flask"}:
            roles.append("Python Developer")
        if skill_set & {"react", "angular", "vue", "javascript", "typescript"}:
            roles.append("Frontend Developer")
        if skill_set & {"sap", "abap", "fiori"}:
            roles.append("SAP Developer")
        if not roles:
            roles.append("Software Engineer")

    return roles[:5]


def process_resume(db: Session, user: User, file_content: bytes, filename: str) -> CandidateProfile:
    file_path = save_resume(user, file_content, filename)
    resume_text = extract_text_from_pdf(file_path)
    checksum = compute_checksum(file_content)

    user.resume_path = str(file_path)
    user.resume_text = resume_text
    user.resume_checksum = checksum

    skills = extract_skills_from_text(resume_text)
    experience_years = extract_experience_years(resume_text)
    name = extract_name_from_text(resume_text) or user.name
    email = extract_email_from_text(resume_text) or user.email or ""
    phone = extract_phone_from_text(resume_text) or user.phone
    linkedin = extract_linkedin_from_text(resume_text)
    target_roles = guess_target_roles(skills, resume_text)

    seniority = "junior"
    if experience_years >= 7:
        seniority = "senior"
    elif experience_years >= 4:
        seniority = "mid"

    existing = (
        db.query(CandidateProfile)
        .filter(CandidateProfile.user_id == user.id)
        .first()
    )

    if existing:
        existing.resume_checksum = checksum
        existing.name = name
        existing.email = email
        existing.phone = phone
        existing.linkedin = linkedin
        existing.seniority = seniority
        existing.total_experience_years = experience_years
        existing.skills = {"detected": skills}
        existing.target_roles = target_roles
        existing.raw_resume_text = resume_text
        profile = existing
    else:
        profile = CandidateProfile(
            user_id=user.id,
            resume_checksum=checksum,
            name=name,
            email=email,
            phone=phone,
            linkedin=linkedin,
            seniority=seniority,
            total_experience_years=experience_years,
            skills={"detected": skills},
            target_roles=target_roles,
            location_preferences=["Karachi", "Pakistan", "Remote"],
            raw_resume_text=resume_text,
        )
        db.add(profile)

    db.commit()
    return profile
