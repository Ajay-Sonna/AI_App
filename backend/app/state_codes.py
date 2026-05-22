"""US state / territory codes — canonical 2-letter USPS-style identifiers."""

from __future__ import annotations

import re
from typing import Dict, FrozenSet


def _normalize_name_key(name: str) -> str:
    s = re.sub(r"[^A-Za-z]+", " ", (name or "").upper()).strip()
    return " ".join(s.split())


# (code, common English name) — stored in DB and used for folders as uppercase 2-letter.
_US_PAIRS: tuple[tuple[str, str], ...] = (
    ("AL", "Alabama"),
    ("AK", "Alaska"),
    ("AZ", "Arizona"),
    ("AR", "Arkansas"),
    ("CA", "California"),
    ("CO", "Colorado"),
    ("CT", "Connecticut"),
    ("DE", "Delaware"),
    ("DC", "District of Columbia"),
    ("FL", "Florida"),
    ("GA", "Georgia"),
    ("HI", "Hawaii"),
    ("ID", "Idaho"),
    ("IL", "Illinois"),
    ("IN", "Indiana"),
    ("IA", "Iowa"),
    ("KS", "Kansas"),
    ("KY", "Kentucky"),
    ("LA", "Louisiana"),
    ("ME", "Maine"),
    ("MD", "Maryland"),
    ("MA", "Massachusetts"),
    ("MI", "Michigan"),
    ("MN", "Minnesota"),
    ("MS", "Mississippi"),
    ("MO", "Missouri"),
    ("MT", "Montana"),
    ("NE", "Nebraska"),
    ("NV", "Nevada"),
    ("NH", "New Hampshire"),
    ("NJ", "New Jersey"),
    ("NM", "New Mexico"),
    ("NY", "New York"),
    ("NC", "North Carolina"),
    ("ND", "North Dakota"),
    ("OH", "Ohio"),
    ("OK", "Oklahoma"),
    ("OR", "Oregon"),
    ("PA", "Pennsylvania"),
    ("RI", "Rhode Island"),
    ("SC", "South Carolina"),
    ("SD", "South Dakota"),
    ("TN", "Tennessee"),
    ("TX", "Texas"),
    ("UT", "Utah"),
    ("VT", "Vermont"),
    ("VA", "Virginia"),
    ("WA", "Washington"),
    ("WV", "West Virginia"),
    ("WI", "Wisconsin"),
    ("WY", "Wyoming"),
)

US_STATE_CODES: FrozenSet[str] = frozenset(c for c, _ in _US_PAIRS)

_NAME_TO_CODE: Dict[str, str] = {}
for _code, _name in _US_PAIRS:
    _NAME_TO_CODE[_normalize_name_key(_name)] = _code


def resolve_us_state_code(raw: str) -> str:
    """
    Return canonical 2-letter USPS code.

    Accepts ``GA``, ``ga``, ``Georgia``, ``NEW YORK``, etc.
    """
    s = (raw or "").strip()
    if not s:
        raise ValueError("state_code is required")

    compact = re.sub(r"[\s._-]+", "", s.upper())
    if len(compact) == 2 and compact in US_STATE_CODES:
        return compact

    key = _normalize_name_key(s)
    if key in _NAME_TO_CODE:
        return _NAME_TO_CODE[key]

    raise ValueError(
        f"Unknown US state or territory: {raw!r}. Use a 2-letter code (e.g. GA, NY, NC) or the full state name."
    )
