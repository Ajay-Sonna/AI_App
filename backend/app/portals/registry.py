"""
Optional **portal adapters** — declarative recipes or tiny code hooks for portals where files sit
behind interactions (multi-page navigation, dropdown + submit, postbacks, …).

**Preferred path:** add or edit YAML recipes under ``app/portals/recipes/`` (see that folder's README).
The ``DeclarativeRecipesAdapter`` is registered by default and merges matching recipe output into
``catalog_tables`` — no per-state Python required for simple patterns.

The core pipeline (DOM tables, pagination, ServiceNow capture, file-link scan) stays generic.

Adapters only **add** tables; they do not remove or replace core extraction unless you
implement that inside ``extend_catalog_tables`` (e.g. return [] and document behavior).

Add or update ``*.yaml`` files under ``app/portals/recipes/`` for new sites; avoid state-specific
Python unless a recipe cannot express the behavior.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Protocol, Tuple, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class PortalCatalogAdapter(Protocol):
    """One URL family + optional extra catalog tables after the shared pipeline data exists."""

    adapter_id: str

    def matches(self, url: str) -> bool:
        ...

    def extend_catalog_tables(
        self, *, url: str, bundle: Dict[str, Any]
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        """
        Return (tables, meta). ``tables`` are appended to ``catalog_tables`` in ``run_pipeline``.
        ``bundle`` is the ``load_page_bundle`` dict (html, spa_bundle, page_analysis, …).
        """
        ...


from app.portals.recipe_adapters import DeclarativeRecipesAdapter


# Append instances here as you implement them (order = first match wins for meta keys only;
# all matching adapters may run and merge).
REGISTERED_PORTAL_ADAPTERS: List[PortalCatalogAdapter] = [DeclarativeRecipesAdapter()]


def collect_portal_catalog_extensions(
    url: str, bundle: Dict[str, Any]
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Run every registered adapter whose ``matches(url)`` is true; merge tables and collect meta.
    Safe no-op when the registry is empty.
    """
    merged: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    u = (url or "").strip()
    if not u or not REGISTERED_PORTAL_ADAPTERS:
        return merged, meta

    for adapter in REGISTERED_PORTAL_ADAPTERS:
        if not adapter.matches(u):
            continue
        aid = getattr(adapter, "adapter_id", type(adapter).__name__)
        try:
            tables, am = adapter.extend_catalog_tables(url=u, bundle=bundle)
        except Exception as ex:
            logger.warning("portal adapter %s failed: %s", aid, ex)
            meta[aid] = {"ok": False, "error": str(ex)}
            continue
        if not isinstance(tables, list):
            meta[aid] = {"ok": False, "error": "adapter did not return a list"}
            continue
        if not isinstance(am, dict):
            am = {}
        for t in tables:
            if isinstance(t, dict):
                t.setdefault("source", f"portal_adapter:{aid}")
                merged.append(t)
        meta[aid] = {**am, "ok": True, "tables_appended": len(tables)}
    return merged, meta
