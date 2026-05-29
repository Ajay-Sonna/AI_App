"""Tests for compare join-key logic (duplicate codes, empty modifiers)."""

from app.compare_fee_schedules import _modifiers_disambiguate_duplicates


def test_empty_modifiers_do_not_disambiguate():
    rows = [
        {"code": "99213", "mod": ""},
        {"code": "99213", "mod": None},
        {"code": "99214", "mod": ""},
    ]
    assert _modifiers_disambiguate_duplicates(rows, "code", "mod") is False


def test_varying_modifiers_disambiguate():
    rows = [
        {"code": "99213", "mod": "26"},
        {"code": "99213", "mod": "TC"},
    ]
    assert _modifiers_disambiguate_duplicates(rows, "code", "mod") is True


def test_unique_codes_ignore_modifier_differences():
    rows = [
        {"code": "99213", "mod": "26"},
        {"code": "99214", "mod": "TC"},
    ]
    assert _modifiers_disambiguate_duplicates(rows, "code", "mod") is False


def test_coerce_compare_normalizes_datetime_strings():
    from app.compare_fee_schedules import _coerce_compare, _display_val

    same, a, b = _coerce_compare("2026-02-01 00:00:00", "2026-02-01T00:00:00")
    assert same is True
    assert a == "2026-02-01"
    assert b == "2026-02-01"
    assert _display_val("2026-01-07T00:00:00") == "2026-01-07"
    assert _display_val("9999-12-31 00:00:00") == "9999-12-31"
