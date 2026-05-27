"""Load and validate declarative portal recipes (YAML / JSON — no code per state)."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

_STRATEGIES = frozenset(
    {
        "expand_internal_html_then_collect_files",
        "playwright_dropdown_submit",
    }
)


def _as_str_dict(d: Any) -> Dict[str, Any]:
    if not isinstance(d, dict):
        return {}
    return {str(k): v for k, v in d.items()}


def _validate(rec: Dict[str, Any]) -> Tuple[bool, str]:
    rid = str(rec.get("id") or "").strip()
    if not rid:
        return False, "recipe missing string id"
    strat = str(rec.get("strategy") or "").strip()
    if strat not in _STRATEGIES:
        return False, f"unknown strategy {strat!r} (allowed: {sorted(_STRATEGIES)})"
    match = _as_str_dict(rec.get("match"))
    if not match.get("host_contains") and not match.get("path_contains") and not match.get("url_regex"):
        return False, f"recipe {rid}: match must include host_contains, path_contains, and/or url_regex"
    if not isinstance(rec.get("params"), dict):
        return False, f"recipe {rid}: params must be a mapping"
    return True, ""


def load_recipes_from_dir(recipes_dir: Path) -> List[Dict[str, Any]]:
    """Load ``*.yaml`` / ``*.yml`` / ``*.json`` from directory; skip invalid entries."""
    out: List[Dict[str, Any]] = []
    if not recipes_dir.is_dir():
        logger.warning("portal recipes directory missing: %s", recipes_dir)
        return out

    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None  # noqa: N816

    for path in sorted(recipes_dir.iterdir()):
        if path.suffix.lower() == ".json":
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                items = raw if isinstance(raw, list) else [raw]
            except Exception as ex:
                logger.warning("recipe json %s: %s", path, ex)
                continue
            for rec in items:
                rec = _as_str_dict(rec)
                ok, err = _validate(rec)
                if not ok:
                    logger.warning("%s (%s)", err, path)
                    continue
                rec["_file"] = path.name
                out.append(rec)
            continue

        if path.suffix.lower() not in {".yaml", ".yml"}:
            continue
        if yaml is None:
            logger.warning("PyYAML not installed — skip %s", path.name)
            continue
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as ex:
            logger.warning("recipe yaml %s: %s", path, ex)
            continue
        items = raw if isinstance(raw, list) else [raw]
        for rec in items:
            rec = _as_str_dict(rec)
            ok, err = _validate(rec)
            if not ok:
                logger.warning("%s (%s)", err, path)
                continue
            rec["_file"] = path.name
            out.append(rec)
    return out


def recipes_matching_url(recipes: List[Dict[str, Any]], url: str) -> List[Dict[str, Any]]:
    import re
    from urllib.parse import urlparse

    u = (url or "").strip()
    pu = urlparse(u)
    host = (pu.netloc or "").lower()
    path = (pu.path or "").lower()
    full = u.lower()

    matched: List[Dict[str, Any]] = []
    for rec in recipes:
        m = _as_str_dict(rec.get("match"))
        hc = str(m.get("host_contains") or "").lower().strip()
        pc = str(m.get("path_contains") or "").lower().strip()
        rx = str(m.get("url_regex") or "").strip()

        ok = True
        if hc and hc not in host:
            ok = False
        if ok and pc and pc not in path and pc not in full:
            ok = False
        if ok and rx:
            try:
                if not re.search(rx, u, flags=re.I):
                    ok = False
            except re.error:
                ok = False
        if ok:
            matched.append(rec)
    return matched


def default_recipes_dir() -> Path:
    return Path(__file__).resolve().parent / "recipes"
