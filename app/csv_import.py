"""Import existing tracker/applications.csv into the database without losing rows or creating duplicates."""

import csv
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Job, JobStatus, canonicalize_url, content_hash


CSV_PATH = Path(__file__).parent.parent / "tracker" / "applications.csv"

STATUS_MAP = {
    "discovered": JobStatus.DISCOVERED,
    "verified": JobStatus.VERIFIED,
    "scored": JobStatus.SCORED,
    "shortlisted": JobStatus.SHORTLISTED,
    "applied": JobStatus.SUBMITTED,
    "skipped": JobStatus.SKIPPED,
    "freshness_fail": JobStatus.EXPIRED,
    "rejected": JobStatus.REJECTED,
}


def _parse_date(val: str) -> datetime | None:
    if not val or not val.strip():
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(val.strip(), fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _parse_score(val: str) -> float | None:
    if not val or not val.strip():
        return None
    try:
        return float(val.strip())
    except ValueError:
        return None


def _map_status(val: str) -> JobStatus:
    return STATUS_MAP.get(val.strip().lower(), JobStatus.DISCOVERED)


def _decision_from_priority(priority: str) -> str:
    p = priority.strip().upper()
    if p in ("PRIORITY_APPLY", "PRIORITY APPLY"):
        return "PRIORITY_APPLY"
    if p == "APPLY":
        return "APPLY"
    if p == "REVIEW":
        return "REVIEW"
    if p in ("SKIP", "FRESHNESS_FAIL"):
        return "SKIP"
    return "SKIP"


def import_csv(db: Session, csv_path: Path | None = None) -> dict:
    path = csv_path or CSV_PATH
    if not path.exists():
        return {"imported": 0, "skipped": 0, "error": "CSV file not found"}

    imported = 0
    skipped = 0
    errors = []

    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader, start=2):
            try:
                company = row.get("company", "").strip()
                title = row.get("role", "").strip()
                url = row.get("url", "").strip()

                if not company or not title:
                    errors.append(f"Row {i}: missing company or role")
                    continue

                canonical = canonicalize_url(url) if url else ""
                desc_hash = content_hash(f"{company}|{title}|{url}")

                existing = None
                if canonical:
                    existing = db.query(Job).filter(Job.canonical_url == canonical).first()
                if not existing:
                    existing = (
                        db.query(Job)
                        .filter(Job.company == company, Job.title == title, Job.description_hash == desc_hash)
                        .first()
                    )

                if existing:
                    skipped += 1
                    continue

                job = Job(
                    canonical_url=canonical or f"csv-import-{i}-{desc_hash}",
                    source=row.get("source", "csv-import").strip(),
                    company=company,
                    title=title,
                    location=row.get("location", "").strip(),
                    remote_policy=row.get("remote_policy", "").strip(),
                    salary=row.get("salary", "").strip(),
                    posted_date=_parse_date(row.get("date_posted", "")),
                    description_hash=desc_hash,
                    freshness_status=row.get("freshness_status", "").strip(),
                    score_total=_parse_score(row.get("score", "")),
                    decision=_decision_from_priority(row.get("priority", "")),
                    status=_map_status(row.get("status", "discovered")),
                    eligibility=row.get("notes", "").strip()[:200],
                )
                db.add(job)
                try:
                    db.flush()
                    imported += 1
                except Exception:
                    db.rollback()
                    skipped += 1
            except Exception as e:
                errors.append(f"Row {i}: {e}")

    db.commit()
    return {"imported": imported, "skipped": skipped, "errors": errors}
