"""Tests for fee schedule family name resolution."""

from app.fee_schedule_identity import (
    family_name_from_logical_key,
    family_name_from_source_label,
    norm_match_key,
    resolve_schedule_family_by_name,
)


def test_family_name_from_logical_key_strips_xlsx():
    assert family_name_from_logical_key("physician_assistant_fee_schedule.xlsx") == "physician assistant"


def test_family_name_from_source_label_strips_date():
    assert family_name_from_source_label("Physician Assistant · May 4, 2026") == "Physician Assistant"
    assert family_name_from_source_label("Physician Assistant - May 4, 2026") == "Physician Assistant"
    assert family_name_from_source_label("Physician Assistant*") == "Physician Assistant"


def test_resolve_partial_schedule_name():
    arts = [
        {
            "artifact_id": 100,
            "logical_schedule_key": "physician_assistant_fee_schedule.xlsx",
            "source_label": "Physician Assistant · May 4, 2026",
            "is_current": True,
        },
        {
            "artifact_id": 99,
            "logical_schedule_key": "physician_assistant_fee_schedule.xlsx",
            "source_label": "Physician Assistant · Jan 1, 2025",
            "is_current": False,
        },
        {
            "artifact_id": 200,
            "logical_schedule_key": "nurse_practitioner_and_cma_fee_schedule.xlsx",
            "source_label": "Nurse Practitioner · May 4, 2026",
            "is_current": True,
        },
    ]
    lsk, name, err = resolve_schedule_family_by_name("Physician Assistant", state_code="NC", artifacts=arts)
    assert err is None
    assert lsk == "physician_assistant_fee_schedule.xlsx"
    assert name == "Physician Assistant"

    lsk2, _, err2 = resolve_schedule_family_by_name(
        "physician assistant fee schedule",
        state_code="NC",
        artifacts=arts,
    )
    assert err2 is None
    assert lsk2 == "physician_assistant_fee_schedule.xlsx"


def test_resolve_ambiguous_prefix():
    arts = [
        {
            "artifact_id": 1,
            "logical_schedule_key": "a.xlsx",
            "source_label": "Alpha Fee Schedule",
            "is_current": True,
        },
        {
            "artifact_id": 2,
            "logical_schedule_key": "b.xlsx",
            "source_label": "Alpha Plus Fee Schedule",
            "is_current": True,
        },
    ]
    _, _, err = resolve_schedule_family_by_name("Alpha", state_code="NC", artifacts=arts)
    assert err is not None
    assert "Multiple" in err


def test_norm_match_key():
    assert norm_match_key("  Physician   Assistant  ") == "physician assistant"
