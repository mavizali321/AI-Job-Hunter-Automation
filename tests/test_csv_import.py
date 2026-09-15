"""Tests for CSV import and migration."""

import csv
import tempfile
from pathlib import Path

from app.models import Job
from app.csv_import import import_csv


class TestCSVImport:
    def test_imports_from_fixture(self, db):
        csv_path = Path(__file__).parent.parent / "tracker" / "applications.csv"
        if not csv_path.exists():
            return
        result = import_csv(db, csv_path)
        assert result["imported"] > 0

    def test_no_duplicates_on_reimport(self, db):
        csv_path = Path(__file__).parent.parent / "tracker" / "applications.csv"
        if not csv_path.exists():
            return
        result1 = import_csv(db, csv_path)
        first_count = result1["imported"]
        first_skipped = result1["skipped"]
        total_rows = first_count + first_skipped
        result2 = import_csv(db, csv_path)
        assert result2["imported"] == 0
        assert result2["skipped"] == total_rows
        assert db.query(Job).count() == first_count

    def test_import_custom_csv(self, db):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "date_found", "company", "role", "location", "remote_policy",
                "employment_type", "salary", "score", "priority", "url",
                "source", "status", "notes",
            ])
            writer.writeheader()
            writer.writerow({
                "date_found": "2026-09-15", "company": "TestCorp",
                "role": "AI Engineer", "location": "Karachi",
                "remote_policy": "onsite", "employment_type": "full-time",
                "salary": "", "score": "85", "priority": "APPLY",
                "url": "https://testcorp.com/jobs/1", "source": "test",
                "status": "discovered", "notes": "Test job",
            })
            path = Path(f.name)

        result = import_csv(db, path)
        assert result["imported"] == 1
        job = db.query(Job).first()
        assert job.company == "TestCorp"
        assert job.title == "AI Engineer"
        path.unlink()

    def test_missing_csv(self, db):
        result = import_csv(db, Path("/nonexistent.csv"))
        assert result["error"] == "CSV file not found"
