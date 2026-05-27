# Declarative portal recipes

Recipes add **extra** `catalog_tables` rows when plain HTML/table extraction misses files that require **multi-hop navigation** (`expand_internal_html_then_collect_files`) or **interactive UI** (`playwright_dropdown_submit`). They merge into the unified pipeline alongside Georgia postback resolution and ServiceNow rows.

## Files

- `*.yaml` / `*.yml` — one document per recipe, or YAML list root for multiple recipes in one file.
- `*.json` — alternate format (JSON array or single object).

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `PORTAL_RECIPES_ENABLED` | `true` | Disable all recipes (`false`). |
| `PORTAL_RECIPES_DIR` | `app/portals/recipes` directory | Alternate recipe folder. |

Recipe files are cached in-process (`lru_cache`); restart the API (or clear cache in tests via `reload_recipes_cache()` from `recipe_adapters`) after editing YAML.

## Matching

Each recipe declares `match` with at least one of:

- `host_contains`: substring compare on URL host (`michigan.gov` matches `www.michigan.gov`).
- `path_contains`: substring on path or whole URL lowercase.
- `url_regex`: full-URL regex (optional).

Multiple recipes may match one URL — all eligible strategies run sequentially.

## Strategies

### `expand_internal_html_then_collect_files`

Breadth-first, depth-limited crawl of same-site anchors; each visited HTML page contributes file rows via `extract_file_link_catalog`.

### `playwright_dropdown_submit`

Reloads start URL between option attempts; pairs each `<select>` with a nearby descendant `role=button`; iterates bounded options after clicking `"View Report"` (or configurable text). Captures the post-click navigation URL as the primary artifact pointer.

Tune selectors / limits per site inside `params`; keep **recipe data only** — no state-specific branches in Python.
