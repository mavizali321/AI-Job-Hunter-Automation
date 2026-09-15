"""Seed the database: create tables and import CSV."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import engine, Base, SessionLocal
from app.models import Job, Application, Approval, CandidateAnswer, Event, SourceRun  # noqa
from app.csv_import import import_csv


def main():
    print("Creating tables...")
    Base.metadata.create_all(bind=engine)
    print("Tables created.")

    db = SessionLocal()
    print("Importing CSV...")
    result = import_csv(db)
    print(f"Import result: {result}")
    db.close()
    print("Done.")


if __name__ == "__main__":
    main()
