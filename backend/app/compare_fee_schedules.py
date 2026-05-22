"""Compare a saved state artifact grid to DST table rows using fee_schedule_column_mapping."""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.app_db import fee_column_mappings_repo
from app.app_db.artifacts_repo import get_artifact_by_id
from app.dst_db.service import fetch_dst_table_rows, validate_table_name
from app.preview.preview_service import build_artifact_table_preview
from app.storage.artifact_download import resolve_artifact_path


def _norm_text(v: Any) -> str:
    return re.sub(r"\s+", " ", str(v if v is not None else "").strip())


def _norm_key(v: Any) -> str:
    return _norm_text(v).upper()


def _parse_column_map(raw: Any) -> Dict[str, str]:
    """state_column -> dst_column (as stored in DB)."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return {str(k).strip(): str(v).strip() for k, v in raw.items() if str(k).strip()}
    s = str(raw).strip()
    if not s:
        return {}
    try:
        obj = json.loads(s)
        return {str(k).strip(): str(v).strip() for k, v in obj.items()} if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _state_col_for_dst(column_map: Dict[str, str], dst_name: str) -> Optional[str]:
    target = str(dst_name).strip().upper()
    for sk, dv in column_map.items():
        if str(dv).strip().upper() == target:
            return sk
    return None


def _modifier_join_dst_candidates() -> Tuple[str, ...]:
    """DST keys used for CODE+modifier joins (first match wins with pick_dst_col + mapping)."""
    return ("MOD",) + tuple(f"MOD {i}" for i in range(1, 10))


def _modifier_join_pair_from_map(
    column_map: Dict[str, str],
    pick_dst_col: Callable[[str], Optional[str]],
) -> Tuple[Optional[str], Optional[str]]:
    """
    State column + physical DST column for duplicate-code joins.

    Matches Mapping tab realities: JSON blobs often expose ``MOD 1`` / ``MOD 2`` …
    rather than a single ``MOD`` column.
    """
    for cand in _modifier_join_dst_candidates():
        sc = _state_col_for_dst(column_map, cand)
        if not sc:
            continue
        phys = pick_dst_col(cand)
        if phys:
            return sc, phys
    return None, None


def _rows_to_dicts(columns: List[str], rows: List[List[Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for line in rows:
        d: Dict[str, Any] = {}
        for i, c in enumerate(columns):
            key = str(c).strip() if c is not None else f"col_{i + 1}"
            d[key] = line[i] if i < len(line) else None
        out.append(d)
    return out


def _quantize_money_two_dp(d: Decimal) -> Decimal:
    """Excel-style cents precision for mapped fee/rate comparisons."""
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _mapping_pair_is_money_field(state_column: Optional[str], dst_column: Optional[str]) -> bool:
    """
    Heuristic: rates/amount columns use 2‑dp quantized equality (aligned with workbook display).
    Excludes CODE/MOD/description/date-ish headers on either side.
    """

    def one_money(raw: Optional[str]) -> bool:
        t = str(raw or "").strip().lower()
        if not t:
            return False
        if re.search(r"\b(procedure\s*)?(code|cpt|hcpcs)\b", t):
            return False
        if re.search(r"\b(date|time|effective|expire|descr|modifier)\b", t):
            return False
        if re.search(r"\brate\b|\bprice\b|\bamount\b|\ballow\b|reimburs|\bpayment\b|\bcost\b|\bfee\b", t):
            return True
        if re.search(r"facility", t) and re.search(r"rate|amount|fee|price", t):
            return True
        if re.search(r"non[-.\s_]fac(?:ility)?", t):
            return True
        if re.fullmatch(r"fac|nfc|alw", t):
            return True
        return False

    return one_money(state_column or "") or one_money(dst_column or "")


def _coerce_compare(
    a: Any,
    b: Any,
    *,
    state_column: Optional[str] = None,
    dst_column: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """Return (same, display_a, display_b)."""
    sa, sb = _norm_text(a), _norm_text(b)
    if sa == sb:
        return True, sa, sb
    fa = _try_decimal(sa)
    fb = _try_decimal(sb)
    if fa is not None and fb is not None:
        if _mapping_pair_is_money_field(state_column, dst_column):
            qa = _quantize_money_two_dp(fa)
            qb = _quantize_money_two_dp(fb)
            same = qa == qb
            return same, format(qa, "f"), format(qb, "f")
        same = abs(fa - fb) <= Decimal("0.005")
        return same, sa, sb
    return False, sa, sb


def _try_decimal(s: str) -> Optional[Decimal]:
    if not s or not str(s).strip():
        return None
    cleaned = re.sub(r"[^\d.\-]", "", s.replace(",", ""))
    if cleaned in {"", "-", ".", "-.", ".-", "-.0"}:
        return None
    try:
        return Decimal(cleaned)
    except Exception:
        return None


def _display_val(v: Any) -> str:
    """Normalize cell values for JSON + UI tables."""
    if v is None:
        return ""
    if isinstance(v, Decimal):
        s = format(v, "f").rstrip("0").rstrip(".")
        return s if s else "0"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if isinstance(v, float) and v != v:
            return ""
        return str(v)
    if hasattr(v, "isoformat"):
        try:
            return v.isoformat()
        except Exception:
            pass
    return str(v).strip()


def _row_display(r: Optional[Dict[str, Any]]) -> Dict[str, str]:
    if not r:
        return {}
    return {str(k): _display_val(v) for k, v in r.items()}


def _triple_row_budget(cap: int, n_m: int, n_s: int, n_d: int) -> Tuple[int, int, int]:
    """
    Integer quotas totalling ``min(cap, n_m+n_s+n_d)`` for three buckets (typically
    mismatch / state_only / dst_only ROW OBJECT lengths). Keeps DST-only buckets from being
    starved when proportional rounding would squeeze them out.
    """
    n_m_i, n_s_i, n_d_i = int(max(0, n_m)), int(max(0, n_s)), int(max(0, n_d))
    diff = n_m_i + n_s_i + n_d_i
    cap_i = int(max(0, cap))
    if diff <= 0 or cap_i <= 0:
        return (0, 0, 0)
    if cap_i >= diff:
        return (n_m_i, n_s_i, n_d_i)
    quotas = [(n_m_i * cap_i) // diff, (n_s_i * cap_i) // diff, cap_i - (n_m_i * cap_i) // diff - (n_s_i * cap_i) // diff]
    lims = [n_m_i, n_s_i, n_d_i]
    quotas = [min(quotas[i], lims[i]) for i in range(3)]
    slack = cap_i - sum(quotas)
    while slack > 0:
        placed = False
        for i in range(3):
            if quotas[i] < lims[i]:
                quotas[i] += 1
                slack -= 1
                placed = True
                if slack <= 0:
                    break
        if not placed:
            break
    return (quotas[0], quotas[1], quotas[2])


def compare_artifact_to_dst(
    *,
    state_code: str,
    artifact_id: int,
    dst_fsname: str,
    dst_row_limit: int = 8000,
    max_result_rows: int = 5000,
) -> Dict[str, Any]:
    """
    Return summary + row-level comparison using mapping join keys CODE then CODE+MODIFIER if needed.
    """
    sc = str(state_code or "").strip().upper()[:8]
    if not sc:
        raise ValueError("state_code is required")
    dt = validate_table_name(dst_fsname)

    row_art = get_artifact_by_id(int(artifact_id))
    if not row_art:
        raise ValueError("Artifact not found")
    art_sc = str(row_art.get("state_code") or "").strip().upper()
    if art_sc and art_sc != sc:
        raise ValueError("Artifact state_code does not match the requested state")

    lsk = fee_column_mappings_repo.resolve_schedule_key_for_artifact(row_art)
    map_row = fee_column_mappings_repo.lookup_latest_mapping(
        state_code=sc,
        state_logical_schedule_key=lsk,
        dst_fsname=dt,
    )
    if not map_row:
        raise ValueError(
            f"No column mapping found for this file and DST table {dt}. Save a mapping on the Mapping tab."
        )
    column_map = _parse_column_map(map_row.get("column_map_json"))
    if not column_map:
        raise ValueError("Saved mapping is empty. Complete column pairings on the Mapping tab.")

    code_state_col = _state_col_for_dst(column_map, "CODE")
    if not code_state_col:
        raise ValueError("Mapping must pair a state column to DST column CODE (procedure code).")
    mod_preview_state_col: Optional[str] = None
    for cand in _modifier_join_dst_candidates():
        mod_preview_state_col = _state_col_for_dst(column_map, cand)
        if mod_preview_state_col:
            break

    rel = str(row_art.get("stored_rel_path") or "")
    if not rel:
        raise ValueError("Artifact has no stored file path")
    try:
        path = resolve_artifact_path(rel)
    except ValueError as e:
        raise ValueError(str(e)) from e
    if not path.is_file():
        raise ValueError("Artifact file is missing on disk")

    data = path.read_bytes()
    name = (row_art.get("original_filename") or path.name) or "artifact"
    mime = str(row_art.get("mime_type") or "")
    grid = build_artifact_table_preview(data, original_filename=name, mime_type=mime)
    if not grid.get("ok"):
        raise ValueError(str(grid.get("error") or "Could not parse artifact as a table"))
    st_cols: List[str] = [str(c) for c in grid["columns"]]
    st_rows_raw: List[List[Any]] = grid["rows"]
    for req in (code_state_col, mod_preview_state_col):
        if req and req not in st_cols:
            raise ValueError(f"State side has no column “{req}” from the saved mapping (file headers may have changed).")

    st_dicts = _rows_to_dicts(st_cols, st_rows_raw)

    dst_cols_tuple, dst_dicts = fetch_dst_table_rows(dt, limit=int(dst_row_limit), state_code=sc)

    def pick_dst_col(name: str) -> Optional[str]:
        """Resolve physical column name on DST row (case-insensitive)."""
        nup = name.strip().upper()
        for c in dst_cols_tuple:
            if str(c).strip().upper() == nup:
                return str(c)
        return None

    mod_state_col, mod_dst_col = _modifier_join_pair_from_map(column_map, pick_dst_col)

    missing_dst_targets = []
    for dst_target in {str(v).strip() for v in column_map.values() if str(v).strip()}:
        if pick_dst_col(dst_target) is None:
            missing_dst_targets.append(dst_target)
    if missing_dst_targets:
        raise ValueError(
            "Saved mapping references DST columns that are missing from the current DST sample: "
            + ", ".join(sorted(missing_dst_targets)[:12])
            + ("…" if len(missing_dst_targets) > 12 else "")
        )

    ordered_pair_specs: List[Tuple[str, str]] = []
    for st_col, dst_targ in column_map.items():
        phys = pick_dst_col(dst_targ)
        if phys:
            ordered_pair_specs.append((st_col, phys))

    column_pairs: List[Dict[str, str]] = [
        {"state_column": a, "dst_column": b} for a, b in ordered_pair_specs
    ]

    def build_field_diffs(sr: Optional[Dict[str, Any]], dr: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for st_col, phys in ordered_pair_specs:
            sv = sr.get(st_col) if sr else None
            dv = dr.get(phys) if dr else None
            same, va, vb = _coerce_compare(sv, dv, state_column=st_col, dst_column=phys)
            out.append(
                {
                    "state_column": st_col,
                    "dst_column": phys,
                    "state_value": va,
                    "dst_value": vb,
                    "same": same,
                }
            )
        return out

    # Decide join mode: duplicate code on either side → use code+modifier when MOD mapped.
    dst_code_col = pick_dst_col("CODE")
    if not dst_code_col:
        raise ValueError("DST result set has no CODE column for joining.")

    from collections import Counter

    st_ct = Counter(_norm_key(r.get(code_state_col)) for r in st_dicts if _norm_key(r.get(code_state_col)))
    dst_ct = Counter(_norm_key(r.get(dst_code_col)) for r in dst_dicts if _norm_key(r.get(dst_code_col)))
    dup_code = any(v > 1 for v in st_ct.values()) or any(v > 1 for v in dst_ct.values())
    use_mod = bool(dup_code)
    if use_mod:
        if not mod_state_col or not mod_dst_col:
            raise ValueError(
                "The same procedure code appears on multiple rows. Map one state modifier column to "
                "DST MOD (or MOD 1, MOD 2, …) and retry, or narrow the data so codes are unique."
            )

    def join_key_state(r: Dict[str, Any]) -> Tuple[str, ...]:
        c = _norm_key(r.get(code_state_col))
        if use_mod and mod_state_col:
            return (c, _norm_key(r.get(mod_state_col)))
        return (c,)

    def join_key_dst(r: Dict[str, Any]) -> Tuple[str, ...]:
        c = _norm_key(r.get(dst_code_col))
        if use_mod and mod_dst_col:
            return (c, _norm_key(r.get(mod_dst_col)))
        return (c,)

    dst_by_key: Dict[Tuple[str, ...], List[Dict[str, Any]]] = {}
    for r in dst_dicts:
        k = join_key_dst(r)
        if not k or not k[0]:
            continue
        dst_by_key.setdefault(k, []).append(r)

    matched_dst_keys: set = set()
    mismatch_n = 0
    state_only_n = 0
    match_n = 0
    dst_only_n = 0

    mismatch_rows: List[Dict[str, Any]] = []
    state_only_rows: List[Dict[str, Any]] = []
    match_rows: List[Dict[str, Any]] = []
    dst_only_rows: List[Dict[str, Any]] = []

    for sr in st_dicts:
        k = join_key_state(sr)
        if not k or not k[0]:
            continue
        dst_matches = dst_by_key.get(k) or []
        if not dst_matches:
            state_only_n += 1
            fds = build_field_diffs(sr, None)
            state_only_rows.append(
                {
                    "status": "state_only",
                    "join_key": {
                        "code": sr.get(code_state_col),
                        "modifier": sr.get(mod_state_col) if use_mod else None,
                    },
                    "field_diffs": fds,
                    "state_row": _row_display(sr),
                    "dst_row": {},
                }
            )
            continue
        dr = dst_matches[0]
        matched_dst_keys.add(tuple(k))
        match_n += 1
        field_diffs = build_field_diffs(sr, dr)
        all_same = all(x["same"] for x in field_diffs) if field_diffs else True
        st = "match" if all_same else "mismatch"
        if not all_same:
            mismatch_n += 1
        row_obj = {
            "status": st,
            "join_key": {
                "code": sr.get(code_state_col),
                "modifier": sr.get(mod_state_col) if use_mod else None,
            },
            "field_diffs": field_diffs,
            "state_row": _row_display(sr),
            "dst_row": _row_display(dr),
        }
        if st == "mismatch":
            mismatch_rows.append(row_obj)
        else:
            match_rows.append(row_obj)

    # DST-only keys
    for k, group in dst_by_key.items():
        if k in matched_dst_keys:
            continue
        if not k or not k[0]:
            continue
        dst_only_n += len(group)
        dr = group[0]
        fds = build_field_diffs(None, dr)
        dst_only_rows.append(
            {
                "status": "dst_only",
                "join_key": {"code": dr.get(dst_code_col), "modifier": dr.get(mod_dst_col) if use_mod else None},
                "field_diffs": fds,
                "state_row": {},
                "dst_row": _row_display(dr),
            }
        )

    _lm = len(mismatch_rows)
    _ls = len(state_only_rows)
    _ld = len(dst_only_rows)
    diff_n = _lm + _ls + _ld
    budget_nonmatch = min(max_result_rows, diff_n) if diff_n else 0
    bm, bso, bdo = _triple_row_budget(budget_nonmatch, _lm, _ls, _ld)
    out_diff = [*mismatch_rows[:bm], *state_only_rows[:bso], *dst_only_rows[:bdo]]
    leftover = max_result_rows - len(out_diff)
    match_take = match_rows[: max(0, leftover)]
    out_rows: List[Dict[str, Any]] = [*out_diff, *match_take]

    total_possible = diff_n + len(match_rows)
    result_rows_capped = total_possible > len(out_rows)

    summary = {
        "join_mode": "code_and_modifier" if use_mod else "code",
        "mapped_field_count": len(column_map),
        "state_row_count": len(st_dicts),
        "dst_row_count": len(dst_dicts),
        "matched_row_count": match_n,
        "mismatch_count": mismatch_n,
        "match_count": match_n - mismatch_n,
        "state_only_count": state_only_n,
        "dst_only_row_count": dst_only_n,
        "result_rows_returned": len(out_rows),
        "result_rows_capped": result_rows_capped,
    }

    inverted = {}
    for sk, dv in column_map.items():
        inverted[str(dv).strip().upper()] = sk

    return {
        "ok": True,
        "state_code": sc,
        "artifact_id": int(artifact_id),
        "dst_fsname": dt,
        "logical_schedule_key": lsk,
        "summary": summary,
        "mapping_dst_to_state": inverted,
        "column_pairs": column_pairs,
        "rows": out_rows,
    }

