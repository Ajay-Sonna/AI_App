"""Human-readable fee schedule family names for mapping / bulk import."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

from app.app_db.artifacts_repo import get_current_artifact_for_logical_key

_EDITION_SPLIT_RE = re.compile(
    r"""
    \s*[·•]\s*|
    \s*—\s*|
    \s*–\s*|
    \s+-\s+(?=\d)|
    \s+\d{1,2}\s+[A-Za-z]{3,9}\s*,?\s*\d{4}\s*$
    """,
    re.VERBOSE,
)
_FEE_SCHEDULE_SUFFIX_RE = re.compile(r"\s+fee\s+schedule\s*$", re.IGNORECASE)


def norm_match_key(s: str) -> str:
    t = unicodedata.normalize("NFKC", (s or "").strip())
    t = t.rstrip("*").strip()
    return " ".join(t.split()).lower()


def strip_xlsx_suffix(s: str) -> str:
    t = (s or "").strip()
    low = t.lower()
    if low.endswith(".xlsx"):
        return t[:-5].strip()
    if low.endswith(".xls"):
        return t[:-4].strip()
    return t


def strip_fee_schedule_suffix(s: str) -> str:
    return _FEE_SCHEDULE_SUFFIX_RE.sub("", (s or "").strip()).strip()


def family_name_from_logical_key(lsk: str) -> str:
    s = strip_xlsx_suffix(lsk)
    s = s.replace("_", " ").replace("-", " ")
    s = strip_fee_schedule_suffix(s)
    return " ".join(s.split())


def family_name_from_source_label(label: str) -> str:
    s = (label or "").strip().rstrip("*").strip()
    if not s:
        return ""
    parts = _EDITION_SPLIT_RE.split(s, maxsplit=1)
    s = (parts[0] if parts else s).strip()
    s = re.sub(
        r"\s+[-–—]\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|\d).+$",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = strip_fee_schedule_suffix(s)
    return " ".join(s.split())


def primary_family_display_name(art: Dict[str, Any]) -> str:
    slabel = str(art.get("source_label") or "").strip()
    lsk = str(art.get("logical_schedule_key") or "").strip()
    if slabel:
        from_label = family_name_from_source_label(slabel)
        if from_label:
            return from_label.rstrip("*").strip()
    if lsk:
        from_lsk = family_name_from_logical_key(lsk)
        if from_lsk:
            return from_lsk
    aid = art.get("artifact_id")
    return f"Fee schedule {aid}" if aid is not None else "Fee schedule"


def match_keys_for_artifact(art: Dict[str, Any]) -> Set[str]:
    keys: Set[str] = set()
    slabel = str(art.get("source_label") or "").strip()
    lsk = str(art.get("logical_schedule_key") or "").strip()

    for raw in (
        primary_family_display_name(art),
        family_name_from_source_label(slabel) if slabel else "",
        slabel,
        family_name_from_logical_key(lsk) if lsk else "",
        strip_xlsx_suffix(lsk).replace("_", " ") if lsk else "",
        lsk,
    ):
        if raw:
            keys.add(norm_match_key(raw))
            keys.add(norm_match_key(strip_fee_schedule_suffix(raw)))

    return {k for k in keys if k}


def build_schedule_families(
    artifacts: List[Dict[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[str]]]:
    """
    Group artifacts by ``logical_schedule_key`` (schedule family).

    Returns ``(families_by_lsk, match_key_to_lsk_list)``.
    """
    families: Dict[str, Dict[str, Any]] = {}
    key_to_lsk: Dict[str, List[str]] = defaultdict(list)

    for art in artifacts:
        lsk = str(art.get("logical_schedule_key") or "").strip()
        if not lsk:
            aid = art.get("artifact_id")
            if aid is None:
                continue
            lsk = f"artifact:{int(aid)}"

        fam = families.setdefault(
            lsk,
            {
                "logical_schedule_key": lsk,
                "display_name": primary_family_display_name(art),
                "match_keys": set(),
                "artifact_ids": [],
            },
        )
        aid = art.get("artifact_id")
        if aid is not None:
            try:
                fam["artifact_ids"].append(int(aid))
            except (TypeError, ValueError):
                pass
        for mk in match_keys_for_artifact(art):
            fam["match_keys"].add(mk)
            key_to_lsk[mk].append(lsk)

        # Prefer the current artifact's display name when available.
        if art.get("is_current") in (True, 1, "1"):
            fam["display_name"] = primary_family_display_name(art)

    return families, key_to_lsk


def list_schedule_families_for_state(
    artifacts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Distinct schedule families sorted by display name (for bulk-import reference)."""
    families, _ = build_schedule_families(artifacts)
    by_name: Dict[str, List[str]] = defaultdict(list)
    for lsk, fam in families.items():
        disp = norm_match_key(str(fam.get("display_name") or lsk))
        by_name[disp].append(lsk)

    out: List[Dict[str, Any]] = []
    for disp, lsks in by_name.items():
        lsk = _pick_preferred_lsk(lsks, families)
        fam = families[lsk]
        out.append(
            {
                "logical_schedule_key": lsk,
                "schedule_name": str(fam.get("display_name") or lsk),
                "artifact_count": len(set(fam.get("artifact_ids") or [])),
            }
        )
    out.sort(key=lambda r: str(r.get("schedule_name") or "").lower())
    return out


def _pick_preferred_lsk(lsks: List[str], families: Dict[str, Dict[str, Any]]) -> str:
    """When several DB keys share one human name (e.g. ``aca_*`` vs main portal slug), pick one."""

    def score(lsk: str) -> Tuple[int, int, str]:
        fam = families.get(lsk) or {}
        pts = 0
        low = lsk.lower()
        if low.startswith("aca_"):
            pts -= 100
        if low.endswith(".xlsx"):
            pts += 5
        pts += len(set(fam.get("artifact_ids") or []))
        return (pts, len(lsk), lsk)

    return max(lsks, key=score)


def _collapse_matched_lsks(
    matched: Set[str],
    families: Dict[str, Dict[str, Any]],
) -> Set[str]:
    """Merge families that normalize to the same display name."""
    by_name: Dict[str, List[str]] = defaultdict(list)
    for lsk in matched:
        disp = norm_match_key(str((families.get(lsk) or {}).get("display_name") or lsk))
        by_name[disp].append(lsk)
    out: Set[str] = set()
    for group in by_name.values():
        out.add(_pick_preferred_lsk(group, families))
    return out


def _key_prefix_match(key: str, q: str) -> bool:
    if key == q:
        return True
    if key.startswith(q + " ") or key.startswith(q + " and"):
        return True
    return False


def _family_matches_query(q: str, fam: Dict[str, Any]) -> bool:
    disp = norm_match_key(str(fam.get("display_name") or ""))
    if not disp:
        return False
    if _key_prefix_match(disp, q):
        return True
    if q.startswith(disp):
        return True
    for mk in fam.get("match_keys") or ():
        if _key_prefix_match(mk, q):
            return True
    return False


def resolve_schedule_family_by_name(
    query: str,
    *,
    state_code: str,
    artifacts: List[Dict[str, Any]],
) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Resolve a user-typed schedule name to ``logical_schedule_key``.

    Returns ``(logical_schedule_key, display_name, error_message)``.
    """
    q = norm_match_key(query)
    if not q:
        return None, None, "StateSchedule is empty."

    families, _key_to_lsk = build_schedule_families(artifacts)

    matched: Set[str] = set()
    for lsk, fam in families.items():
        if _family_matches_query(q, fam):
            matched.add(lsk)

    matched = _collapse_matched_lsks(matched, families)

    if len(matched) == 1:
        lsk = next(iter(matched))
        return lsk, str(families[lsk]["display_name"]), None

    if not matched:
        suggestions = [
            str(f["display_name"])
            for f in sorted(families.values(), key=lambda x: str(x.get("display_name") or "").lower())
        ]
        close = [s for s in suggestions if q in norm_match_key(s) or norm_match_key(s).startswith(q[: min(8, len(q))])]
        hint = f" Similar names: {', '.join(close[:5])}." if close else ""
        return None, None, f"No fee schedule named {query!r} for {state_code}.{hint}"

    names = sorted({str(families[m]["display_name"]) for m in matched})
    return (
        None,
        None,
        f"Multiple schedules match {query!r}: {', '.join(names[:6])}"
        f"{'…' if len(names) > 6 else ''}. Use a fuller name or ArtifactId.",
    )


def representative_artifact_for_family(
    *,
    state_code: str,
    logical_schedule_key: str,
    artifacts: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Current artifact for the family, else newest row from the loaded list."""
    lsk = (logical_schedule_key or "").strip()
    if not lsk:
        return None
    cur = get_current_artifact_for_logical_key(state_code=state_code, logical_schedule_key=lsk)
    if cur:
        return cur
    ids_in_family: List[int] = []
    for art in artifacts:
        if str(art.get("logical_schedule_key") or "").strip() == lsk:
            try:
                ids_in_family.append(int(art["artifact_id"]))
            except (TypeError, ValueError, KeyError):
                pass
    if not ids_in_family:
        return None
    best_id = max(ids_in_family)
    for art in artifacts:
        if art.get("artifact_id") == best_id:
            return art
    return None
