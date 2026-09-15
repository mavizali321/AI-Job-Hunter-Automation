"""Tests for state machine transitions."""

import pytest
from app.models import Job, JobStatus
from app.state_machine import transition_job, InvalidTransitionError, VALID_TRANSITIONS


class TestStateMachine:
    def test_valid_transition_discovered_to_verified(self, db):
        job = Job(
            canonical_url="https://example.com/job1",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        transition_job(db, job, JobStatus.VERIFIED)
        db.commit()
        assert job.status == JobStatus.VERIFIED

    def test_invalid_transition_fails(self, db):
        job = Job(
            canonical_url="https://example.com/job2",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.SUBMITTED)

    def test_submitted_is_terminal(self, db):
        job = Job(
            canonical_url="https://example.com/job3",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.SUBMITTED,
        )
        db.add(job)
        db.commit()

        with pytest.raises(InvalidTransitionError):
            transition_job(db, job, JobStatus.DISCOVERED)

    def test_full_happy_path(self, db):
        job = Job(
            canonical_url="https://example.com/job4",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        path = [
            JobStatus.VERIFIED, JobStatus.SCORED, JobStatus.SHORTLISTED,
            JobStatus.WAITING_APPROVAL, JobStatus.APPROVED,
            JobStatus.PREPARING, JobStatus.READY_TO_SUBMIT, JobStatus.SUBMITTED,
        ]
        for next_status in path:
            transition_job(db, job, next_status)
            db.commit()
            assert job.status == next_status

    def test_event_recorded(self, db):
        from app.models import Event
        job = Job(
            canonical_url="https://example.com/job5",
            source="test", company="TestCo", title="Engineer",
            status=JobStatus.DISCOVERED,
        )
        db.add(job)
        db.commit()

        transition_job(db, job, JobStatus.VERIFIED)
        db.commit()

        events = db.query(Event).filter(Event.entity_id == job.id).all()
        assert len(events) == 1
        assert events[0].old_state == "DISCOVERED"
        assert events[0].new_state == "VERIFIED"

    def test_all_transitions_have_entries(self):
        for status in JobStatus:
            assert status in VALID_TRANSITIONS
