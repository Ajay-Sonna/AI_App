"""Portal-specific interaction extensions (see ``registry``)."""

from app.portals.recipe_adapters import reload_recipes_cache as reload_portal_recipes_cache
from app.portals.registry import (
    PortalCatalogAdapter,
    REGISTERED_PORTAL_ADAPTERS,
    collect_portal_catalog_extensions,
)

__all__ = [
    "PortalCatalogAdapter",
    "REGISTERED_PORTAL_ADAPTERS",
    "collect_portal_catalog_extensions",
    "reload_portal_recipes_cache",
]
