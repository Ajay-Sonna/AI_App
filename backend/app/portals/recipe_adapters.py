"""Single registered adapter — runs all declarative YAML/JSON portal recipes."""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

from app.portals.recipe_expand import run_expand_internal_html_then_collect_files
from app.portals.recipe_loader import (
    default_recipes_dir,
    load_recipes_from_dir,
    recipes_matching_url,
)
from app.portals.recipe_playwright import run_playwright_dropdown_submit

logger = logging.getLogger(__name__)


def _recipes_enabled() -> bool:
    return os.getenv("PORTAL_RECIPES_ENABLED", "true").strip().lower() in ("1", "true", "yes", "")


def recipes_root() -> Path:
    raw = (os.getenv("PORTAL_RECIPES_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return default_recipes_dir()


@lru_cache(maxsize=1)
def _cached_load_recipes() -> List[Dict[str, Any]]:
    return load_recipes_from_dir(recipes_root())


def reload_recipes_cache() -> None:
    """Call from tests after mutating recipes on disk."""
    _cached_load_recipes.cache_clear()


class DeclarativeRecipesAdapter:
    adapter_id = "declarative_recipes"

    def matches(self, url: str) -> bool:
        if not _recipes_enabled():
            return False
        return len(recipes_matching_url(_cached_load_recipes(), url)) > 0

    def extend_catalog_tables(self, *, url: str, bundle: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        merged: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {"recipes_evaluated": 0}
        if not _recipes_enabled():
            return merged, meta

        recipes = _cached_load_recipes()
        for rec in recipes_matching_url(recipes, url):
            rid = str(rec.get("id") or "unnamed_recipe").strip()
            strat = str(rec.get("strategy") or "").strip()
            params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
            meta["recipes_evaluated"] = int(meta.get("recipes_evaluated") or 0) + 1
            try:
                if strat == "expand_internal_html_then_collect_files":
                    tab, sm = run_expand_internal_html_then_collect_files(
                        url, bundle=bundle, recipe_id=rid, params=params
                    )
                elif strat == "playwright_dropdown_submit":
                    tab, sm = run_playwright_dropdown_submit(url, bundle=bundle, recipe_id=rid, params=params)
                else:
                    logger.error("declarative_recipe unhandled strategy %s (%s)", strat, rid)
                    meta[rid] = {"ok": False, "error": f"unhandled strategy {strat}", "recipe_id": rid}
                    continue
                merged.append(tab)
                meta[rid] = sm
            except Exception as ex:
                logger.warning("recipe %s failed: %s", rid, ex)
                meta[rid] = {"ok": False, "error": str(ex), "recipe_id": rid}

        meta["recipe_files"] = [r.get("_file") for r in recipes_matching_url(recipes, url)]
        return merged, meta
