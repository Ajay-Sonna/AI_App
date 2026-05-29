"""Tests for post-sync notification helpers."""

from app.notifications.sync_notifications import (
    _build_sync_status_email,
    _new_artifacts_from_run,
)


def test_new_artifacts_excludes_skipped():
    saved = [
        {"artifact_id": 1, "skipped": True},
        {"artifact_id": 2, "absolute_path": r"C:\vault\nc\a.pdf"},
    ]
    assert len(_new_artifacts_from_run(saved)) == 1
    assert _new_artifacts_from_run(saved)[0]["artifact_id"] == 2


def test_sync_email_no_new_files():
    subject, body = _build_sync_status_email(
        state_code="NC",
        run_label="19 May 2026, 2:00 PM IST",
        new_items=[],
        unmapped=[],
    )
    assert "no new files" in subject.lower()
    assert "No new fee schedules" in body


def test_sync_email_lists_multiple_paths():
    subject, body = _build_sync_status_email(
        state_code="NC",
        run_label="19 May 2026, 2:00 PM IST",
        new_items=[
            {"display_name": "Physician", "absolute_path": r"C:\OneDrive\FeeScheduleVault\nc\physician\a.xlsx"},
            {"display_name": "Dental", "absolute_path": r"C:\OneDrive\FeeScheduleVault\nc\dental\b.xlsx"},
        ],
        unmapped=[{"display_name": "Dental", "absolute_path": r"C:\OneDrive\FeeScheduleVault\nc\dental\b.xlsx"}],
    )
    assert "2 new file" in subject.lower()
    assert "Physician" in body
    assert "no column mapping" in body.lower()
