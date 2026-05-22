import json
import logging
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.app_db.artifacts_repo import (
    get_artifact_by_id,
    get_current_artifact_for_logical_key,
    list_artifacts,
    recompute_is_current_for_state,
)
from app.app_db.connection import app_db_configured
from app.app_db import fee_column_mappings_repo, state_links_repo
from app.dst_db.service import (
    _dst_configured,
    _resolve_state_code_column,
    fetch_dst_table_rows,
    list_dst_tables,
    validate_table_name,
)
from app.preview.preview_service import build_artifact_table_preview, build_preview_payload, streaming_fetch_resource
from app.preview.session_store import get_preview_authority

# Existing agents (kept for debug / internal use)
from app.agents.ingestion_agent import run_ingestion_agent
from app.agents.extraction_agent import run_catalog_extraction

# ✅ NEW unified pipeline (analyze + extract)
from app.agents.catalog_file_urls import (
    collect_file_urls_from_pipeline_result,
    normalize_persistable_url,
    summarize_artifact_link_availability,
)
from app.agents.run_agent import run_pipeline
from app.compare_fee_schedules import compare_artifact_to_dst
from app.storage.artifact_download import (
    delete_stored_artifact,
    download_fee_schedule_artifact,
    resolve_artifact_path,
)
from app.state_codes import resolve_us_state_code

app = FastAPI()

logger = logging.getLogger(__name__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# -------------------------------------------------------------------
# Request Models
# -------------------------------------------------------------------

class URLRequest(BaseModel):
    url: str
    discover_links: bool = True
    max_discovered_links: int = 25


class CatalogExtractRequest(BaseModel):
    url: str
    paginate: bool = True
    max_pages: int = 200


class RunRequest(BaseModel):
    """Run ingestion. Provide ``url`` and/or ``state_code`` (uses saved portal URL for state)."""

    url: str = ""
    state_code: str | None = None
    persist_artifacts: bool = True
    paginate: bool = True
    max_pages: int = 50
    max_tables: int = 12


class PreviewSnippetBody(BaseModel):
    resource_url: str
    referrer_url: str | None = None
    session_id: str | None = None
    document_hint: str | None = None


class PreviewProxyBody(BaseModel):
    resource_url: str
    referrer_url: str | None = None
    session_id: str | None = None
    document_hint: str | None = None


class ArtifactDownloadBody(BaseModel):
    url: str
    state_code: str | None = None
    logical_schedule_key: str | None = None
    source_label: str | None = None
    referer_url: str | None = None
    portal_date_hint: str | None = None
    effective_date_source: str | None = None
    is_superseded_hint: bool = False


class StatePortalLinkCreate(BaseModel):
    state_code: str
    display_label: str
    portal_url: str
    sort_order: int = 0


class FeeColumnMappingUpsert(BaseModel):
    """Save or update dbo.fee_schedule_column_mapping for a saved artifact + DST table."""

    state_code: str
    artifact_id: int = Field(..., ge=1)
    dst_fsname: str
    column_map_json: Any = Field(default_factory=dict)
    updated_by: str | None = None


class StatePortalLinkPatch(BaseModel):
    display_label: str | None = None
    portal_url: str | None = None
    sort_order: int | None = None


class FeeScheduleCompareRequest(BaseModel):
    """Run server-side compare: saved artifact grid vs DST table using column mapping."""

    state_code: str
    artifact_id: int = Field(..., ge=1)
    dst_fsname: str


def _json_safe_value(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


def _json_safe_row(row: dict) -> dict:
    return {k: _json_safe_value(v) for k, v in row.items()}


def _column_map_object(cell: Any) -> Dict[str, Any]:
    """Parse dbo.fee_schedule_column_mapping.column_map_json into a JSON object."""
    if cell is None:
        return {}
    if isinstance(cell, dict):
        return cell
    s = str(cell).strip()
    if not s:
        return {}
    try:
        out = json.loads(s)
        return out if isinstance(out, dict) else {}
    except json.JSONDecodeError:
        return {}


def _artifact_schedule_title_row(art: Dict[str, Any]) -> Tuple[Optional[int], str]:
    """Return (artifact_id, short label) mirroring SPA ``artifactFeeScheduleDisplayName`` roughly."""
    try:
        aid = int(art.get("artifact_id")) if art.get("artifact_id") is not None else None
    except (TypeError, ValueError):
        aid = None
    slabel = str(art.get("source_label") or "").strip()
    lsk = str(art.get("logical_schedule_key") or "").strip().replace("_", " ")
    fn = str(art.get("original_filename") or "").strip()
    if slabel:
        title = slabel
    elif lsk:
        title = lsk
    elif fn:
        base = fn.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
        base = base.rsplit(".", 1)[0].replace("_", " ") if "." in fn else base.replace("_", " ")
        title = base or fn
    else:
        title = f"artifact {aid}" if aid is not None else "Fee schedule"
    return aid, title


def _mapping_list_identity(state_code: str, logical_schedule_key: str) -> Dict[str, Any]:
    """Resolve ``artifact_id`` + readable schedule label from stored logical schedule key."""
    sk = str(logical_schedule_key or "").strip()
    artifact_id_val: Optional[int] = None
    label_raw = sk.replace("_", " ") if sk else "Unknown schedule"
    lowered = sk.lower()
    sc = str(state_code or "").strip().upper()[:8]
    if lowered.startswith("artifact:"):
        try:
            artifact_id_val = int(sk.split(":", 1)[1])
        except (ValueError, IndexError):
            artifact_id_val = None
        if artifact_id_val is not None:
            art_row = get_artifact_by_id(artifact_id_val)
            if art_row:
                artifact_id_val, label_raw = _artifact_schedule_title_row(art_row)
            else:
                label_raw = sk
    else:
        art_cur = get_current_artifact_for_logical_key(
            state_code=sc if sc else "",
            logical_schedule_key=sk[:256],
        )
        if art_cur:
            artifact_id_val, label_raw = _artifact_schedule_title_row(art_cur)
    return {"artifact_id": artifact_id_val, "schedule_label": label_raw}


def _optional_resolved_state(code: str | None) -> str | None:
    """Normalize query/body ``state_code`` to a 2-letter USPS code, or None if absent."""
    if code is None or not str(code).strip():
        return None
    try:
        return resolve_us_state_code(str(code))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


# -------------------------------------------------------------------
# ✅ USER-FACING ENDPOINT (FRONTEND SHOULD USE THIS)
# -------------------------------------------------------------------

@app.post("/run")
def run(request: RunRequest):
    """
    Single, user-facing endpoint.

    If ``url`` is empty but ``state_code`` is set, loads the single saved portal URL for that state.
    When ``state_code`` + ``persist_artifacts`` and the app DB are configured, downloads discovered
    fee files (pdf/xlsx/…) to local artifact storage after a successful run.
    """
    url = (request.url or "").strip()
    sc = _optional_resolved_state(request.state_code)

    if not url:
        if sc and app_db_configured():
            url = (state_links_repo.get_portal_url_for_state(sc) or "").strip()
        if not url:
            raise HTTPException(
                status_code=400,
                detail="Provide a portal url, or select a state with a saved URL under State URLs.",
            )

    result = run_pipeline(
        url,
        paginate=request.paginate,
        max_pages=request.max_pages,
        max_tables=request.max_tables,
    )
    result["resolved_url"] = url
    if sc:
        result["state_code"] = sc

    if not result.get("blocked"):
        result["artifact_discovery"] = summarize_artifact_link_availability(result, base_url=url)

    if (
        not result.get("blocked")
        and sc
        and request.persist_artifacts
        and app_db_configured()
    ):
        saved: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        file_items = list(collect_file_urls_from_pipeline_result(result, base_url=url))
        result["artifact_download_candidates"] = len(file_items)
        for item in file_items:
            u = item.get("url") or ""
            try:
                sup_raw = str(item.get("superseded_hint") or "").strip().lower()
                is_sup = sup_raw in ("1", "true", "yes")
                meta = download_fee_schedule_artifact(
                    url=u,
                    state_code=sc,
                    logical_schedule_key=(item.get("logical_schedule_key") or "").strip() or None,
                    source_label=item.get("label") or None,
                    referer=url,
                    portal_date_hint=(item.get("portal_date") or "").strip() or None,
                    effective_date_source=(item.get("effective_date_source") or "").strip() or None,
                    is_superseded_hint=is_sup,
                    defer_recompute=True,
                )
                saved.append({"url": u, **meta})
            except Exception as ex:
                logger.warning("Artifact download failed for %s: %s", u, ex)
                errors.append({"url": u, "error": str(ex)})
        result["artifacts_saved"] = saved
        result["artifacts_errors"] = errors
        try:
            recompute_is_current_for_state(state_code=sc)
        except Exception as ex:
            logger.warning("recompute_is_current_for_state failed after run: %s", ex)
        portal_norm = {normalize_persistable_url(item.get("url") or "") for item in file_items}
        portal_norm.discard("")
        pruned = 0
        try:
            # If this run found **no** downloadable URLs, ``portal_norm`` is empty. Pruning would
            # incorrectly delete every stored artifact for the state (nothing is "in" an empty set).
            if not portal_norm:
                logger.info(
                    "Skipping artifact prune for %s: no file URLs matched download heuristics this run.",
                    sc,
                )
                result["artifacts_pruned"] = 0
            else:
                existing = list_artifacts(state_code=sc, current_only=False, limit=5000)
                to_drop: List[int] = []
                for row in existing:
                    raw_u = (row.get("source_url") or "").strip()
                    if not raw_u:
                        continue
                    nu = normalize_persistable_url(raw_u)
                    if nu and nu not in portal_norm:
                        to_drop.append(int(row["artifact_id"]))
                for aid in to_drop:
                    delete_stored_artifact(aid)
                    pruned += 1
                result["artifacts_pruned"] = pruned
        except Exception as ex:
            logger.warning("Artifact prune failed for %s: %s", sc, ex)
            result["artifacts_pruned_error"] = str(ex)

    if sc and app_db_configured():
        try:
            state_links_repo.touch_last_agent_run(sc)
        except Exception as ex:
            logger.warning("Could not update last_agent_run_at_utc for %s: %s", sc, ex)

    return result


@app.post("/preview/snippet")
def preview_snippet(body: PreviewSnippetBody):
    """
    Portable preview manifest: spreadsheets as small tables, small PDF/text as inlined
    base64 payloads, SSRF-checked URLs, optional ephemeral session replay.
    """
    authority = get_preview_authority(body.session_id)
    ref_ov = body.referrer_url.strip() if body.referrer_url else None
    return build_preview_payload(
        body.resource_url.strip(),
        authority=authority,
        referer_override=ref_ov,
        document_hint=body.document_hint.strip() if body.document_hint else None,
    )


@app.post("/preview/proxy")
def preview_proxy(body: PreviewProxyBody):
    """Stream-through download with the same SSRF/session rules as /preview/snippet."""
    authority = get_preview_authority(body.session_id)
    ref_ov = body.referrer_url.strip() if body.referrer_url else None

    resp, err = streaming_fetch_resource(
        body.resource_url.strip(),
        authority,
        referer_override=ref_ov,
        document_hint=body.document_hint.strip() if body.document_hint else None,
    )
    if err:
        code = 502 if isinstance(err, str) and err.startswith("upstream_") else 400
        raise HTTPException(status_code=code, detail=err)
    assert resp is not None

    raw_ct = resp.headers.get("content-type") or ""
    media = raw_ct.split(";")[0].strip() or "application/octet-stream"
    path_leaf = urlparse(body.resource_url).path.rstrip("/").rsplit("/", 1)[-1] or "download"
    ascii_name = path_leaf.encode("ascii", "ignore").decode("ascii") or "download"

    def gen():
        try:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    yield chunk
        finally:
            resp.close()

    return StreamingResponse(
        gen(),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"',
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )


# -------------------------------------------------------------------
# DST (SSMS / SQL Server) — dbo tables in configured database
# -------------------------------------------------------------------


@app.get("/dst/tables")
def dst_tables_list(
    state_code: str | None = Query(
        None,
        description="When set, only dbo tables that contain at least one row with this state_code (and define state_code).",
    ),
):
    if not _dst_configured():
        raise HTTPException(
            status_code=503,
            detail="DST database not configured: set MSSQL_ODBC_CONN or MSSQL_SERVER in the environment.",
        )
    sf = _optional_resolved_state(state_code)
    try:
        tables = list_dst_tables(state_filter=sf)
    except Exception as ex:
        logger.exception("DST list tables failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    return {"tables": tables, "state_filter": sf}


@app.get("/dst/rows")
def dst_table_rows(
    table: str = Query(..., min_length=1, max_length=128),
    limit: int = Query(2000, ge=1, le=10_000),
    state_code: str | None = Query(None, description="When table has state_code column, filter rows"),
    response_row_limit: int | None = Query(
        None,
        ge=0,
        le=10_000,
        description="Cap rows returned after flattening (e.g. 0 for Mapping column-only); "
        "the full TOP(limit) slice is still read for JSON-key union.",
    ),
):
    if not _dst_configured():
        raise HTTPException(
            status_code=503,
            detail="DST database not configured: set MSSQL_ODBC_CONN or MSSQL_SERVER in the environment.",
        )
    try:
        validate_table_name(table)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid table name") from None

    sc = _optional_resolved_state(state_code)
    try:
        columns, rows = fetch_dst_table_rows(table, limit=limit, state_code=sc)
        if response_row_limit is not None:
            rows = rows[: response_row_limit]
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as ex:
        logger.exception("DST fetch rows failed for %s", table)
        raise HTTPException(status_code=503, detail=str(ex)) from ex

    meta: Dict[str, Any] = {"table": table.strip(), "limit": limit, "columns": columns, "rows": rows}
    if response_row_limit is not None:
        meta["response_row_limit_applied"] = response_row_limit
    if sc:
        col = _resolve_state_code_column(table.strip())
        meta["state_filter_applied"] = bool(col)
        if not col:
            meta["state_filter_note"] = "Table has no state_code column; rows are unfiltered."
    return meta


# -------------------------------------------------------------------
# Fee schedule app DB: local artifacts + configured state URLs (not DST)
# -------------------------------------------------------------------


@app.get("/app/health")
def app_companion_health():
    """Whether companion DB + artifact root are configured (DST unchanged)."""
    from app.storage.artifact_download import _artifact_root

    root = str(_artifact_root())
    return {
        "app_database_configured": app_db_configured(),
        "artifact_root_default_or_env": root,
    }


@app.post("/app/artifacts/download")
def app_download_artifact(body: ArtifactDownloadBody):
    if not app_db_configured():
        raise HTTPException(
            status_code=503,
            detail="App database not configured: set MSSQL_APP_DATABASE or MSSQL_APP_ODBC_CONN.",
        )
    try:
        result = download_fee_schedule_artifact(
            url=body.url.strip(),
            state_code=body.state_code,
            logical_schedule_key=body.logical_schedule_key,
            source_label=body.source_label,
            referer=(body.referer_url.strip() if body.referer_url else None),
            portal_date_hint=(body.portal_date_hint.strip() if body.portal_date_hint else None),
            effective_date_source=(body.effective_date_source.strip() if body.effective_date_source else None),
            is_superseded_hint=bool(body.is_superseded_hint),
            defer_recompute=False,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as ex:
        logger.exception("Artifact download failed")
        raise HTTPException(status_code=502, detail=str(ex)) from ex
    return {"ok": True, **result}


@app.get("/app/artifacts")
def app_list_artifacts(
    state_code: str | None = Query(None),
    current_only: bool = Query(True),
    limit: int = Query(200, ge=1, le=5000),
):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    try:
        sc = _optional_resolved_state(state_code)
        rows = list_artifacts(state_code=sc, current_only=current_only, limit=limit)
    except Exception as ex:
        logger.exception("List artifacts failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    return {"artifacts": [_json_safe_row(r) for r in rows]}


@app.get("/app/artifacts/{artifact_id}/file")
def app_serve_artifact_file(artifact_id: int):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    row = get_artifact_by_id(artifact_id)
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    rel = str(row.get("stored_rel_path") or "")
    if not rel:
        raise HTTPException(status_code=404, detail="Missing stored path")
    try:
        path = resolve_artifact_path(rel)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stored path") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    media = (row.get("mime_type") or "application/octet-stream").split(";")[0].strip()
    name = (row.get("original_filename") or path.name) or "download"
    return FileResponse(path, media_type=media, filename=name)


@app.get("/app/artifacts/{artifact_id}/preview-table")
def app_artifact_preview_table(artifact_id: int):
    """Return a small column/row grid for Excel or CSV artifacts (Fee Schedules inline preview)."""
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    row = get_artifact_by_id(artifact_id)
    if not row:
        raise HTTPException(status_code=404, detail="Artifact not found")
    rel = str(row.get("stored_rel_path") or "")
    if not rel:
        raise HTTPException(status_code=404, detail="Missing stored path")
    try:
        path = resolve_artifact_path(rel)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid stored path") from None
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File missing on disk")
    data = path.read_bytes()
    name = (row.get("original_filename") or path.name) or "download"
    mime = str(row.get("mime_type") or "")
    out = build_artifact_table_preview(data, original_filename=name, mime_type=mime)
    if not out.get("ok"):
        raise HTTPException(status_code=415, detail=str(out.get("error") or "preview_failed"))
    return {"columns": out["columns"], "rows": out["rows"]}


@app.get("/app/state-portal-links")
def app_list_state_portal_links(
    state_code: str | None = Query(None, description="If set, only links for this state (e.g. NC)"),
    limit: int = Query(200, ge=1, le=500),
):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    try:
        sf = _optional_resolved_state(state_code)
        rows = state_links_repo.list_state_portal_links(state_code=sf, limit=limit)
    except Exception as ex:
        logger.exception("List state portal links failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    return {"links": [_json_safe_row(r) for r in rows]}


@app.post("/app/state-portal-links")
def app_create_state_portal_link(body: StatePortalLinkCreate):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    if not body.state_code.strip() or not body.portal_url.strip():
        raise HTTPException(status_code=400, detail="state_code and portal_url are required.")
    try:
        lid, inserted = state_links_repo.upsert_state_portal_link(
            state_code=body.state_code,
            display_label=body.display_label or body.portal_url[:80],
            portal_url=body.portal_url,
            sort_order=body.sort_order,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as ex:
        logger.exception("Create state portal link failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    return {"ok": True, "link_id": lid, "inserted": inserted}


@app.patch("/app/state-portal-links/{link_id}")
def app_patch_state_portal_link(link_id: int, body: StatePortalLinkPatch):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    ok = state_links_repo.update_state_portal_link(
        link_id,
        display_label=body.display_label,
        portal_url=body.portal_url,
        sort_order=body.sort_order,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="Link not found or nothing to update")
    return {"ok": True}


@app.delete("/app/state-portal-links/by-state/{state_code}")
def app_delete_state_portal_for_state(state_code: str):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    try:
        sc = resolve_us_state_code(state_code)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    n = state_links_repo.delete_state_portal_link_for_state(sc)
    return {"ok": True, "deleted": n}


@app.delete("/app/state-portal-links/{link_id}")
def app_delete_state_portal_link(link_id: int):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    if not state_links_repo.delete_state_portal_link(link_id):
        raise HTTPException(status_code=404, detail="Link not found")
    return {"ok": True}


@app.get("/app/fee-column-mappings")
def app_list_fee_column_mappings(
    state_code: str = Query(..., description="USPS state code (must match saved mapping rows)"),
    limit: int = Query(500, ge=1, le=2000),
):
    """List saved column mappings for a state (composer + inventory)."""
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    sc = _optional_resolved_state(state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    try:
        rows_raw = fee_column_mappings_repo.list_mappings_for_state(sc, limit=limit)
    except Exception as ex:
        logger.exception("fee-column-mappings list failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    summaries: List[Dict[str, Any]] = []
    for row in rows_raw:
        cm = _column_map_object(row.get("column_map_json"))
        paired = sum(1 for k, v in cm.items() if str(k).strip() and str(v).strip())
        ident = _mapping_list_identity(sc, str(row.get("state_logical_schedule_key") or ""))
        summaries.append(
            {
                "mapping_id": int(row["mapping_id"]),
                "state_logical_schedule_key": str(row.get("state_logical_schedule_key") or ""),
                "dst_fsname": str(row.get("dst_fsname") or ""),
                "paired_column_count": paired,
                "artifact_id": ident["artifact_id"],
                "schedule_label": ident["schedule_label"],
                "updated_at_utc": _json_safe_value(row.get("updated_at_utc") or row.get("created_at_utc")),
                "updated_by": ((str(row.get("updated_by") or "").strip() or None)),
            }
        )
    return {"ok": True, "state_code": sc, "mappings": summaries}


@app.get("/app/fee-column-mappings/latest")
def app_get_latest_fee_column_mapping(
    artifact_id: int = Query(..., ge=1),
    state_code: str = Query(..., description="USPS state code (normalized to match artifact)"),
    dst_fsname: str | None = Query(
        None,
        description="If set, fetch mapping for this exact DST table/view (dbo name).",
    ),
):
    """Load a saved dbo.fee_schedule_column_mapping row for an artifact-centric workflow."""
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    sc = _optional_resolved_state(state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    row_art = get_artifact_by_id(artifact_id)
    if not row_art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    art_sc = str(row_art.get("state_code") or "").strip().upper()
    if art_sc and art_sc != sc:
        raise HTTPException(
            status_code=400,
            detail="Artifact state_code does not match the requested state_code.",
        )
    try:
        lsk = fee_column_mappings_repo.resolve_schedule_key_for_artifact(row_art)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    dst_trim = (dst_fsname or "").strip() or None
    if dst_trim:
        try:
            validate_table_name(dst_trim)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid dst_fsname") from None
    try:
        found_row = fee_column_mappings_repo.lookup_latest_mapping(
            state_code=sc,
            state_logical_schedule_key=lsk,
            dst_fsname=dst_trim,
        )
    except Exception as ex:
        logger.exception("fee-column-mappings latest lookup failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    if not found_row:
        return {"found": False, "mapping": None, "column_map": {}}
    cm = _column_map_object(found_row.get("column_map_json"))
    return {
        "found": True,
        "mapping": _json_safe_row(found_row),
        "column_map": cm,
    }


@app.get("/app/fee-column-mappings/{mapping_id:int}")
def app_get_fee_column_mapping_by_id(
    mapping_id: int,
    state_code: str = Query(..., description="Must match mapping row"),
):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    sc = _optional_resolved_state(state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    try:
        found_row = fee_column_mappings_repo.get_mapping_by_id_for_state(mapping_id=int(mapping_id), state_code=sc)
    except Exception as ex:
        logger.exception("fee-column-mappings get failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    if not found_row:
        raise HTTPException(status_code=404, detail="Mapping not found for this state.")
    cm = _column_map_object(found_row.get("column_map_json"))
    ident = _mapping_list_identity(sc, str(found_row.get("state_logical_schedule_key") or ""))
    paired = sum(1 for k, v in cm.items() if str(k).strip() and str(v).strip())
    return {
        "ok": True,
        "mapping": _json_safe_row(dict(found_row)),
        "column_map": cm,
        "paired_column_count": paired,
        "artifact_id": ident["artifact_id"],
        "schedule_label": ident["schedule_label"],
    }


@app.delete("/app/fee-column-mappings/{mapping_id:int}")
def app_delete_fee_column_mapping(
    mapping_id: int,
    state_code: str = Query(..., description="Must match mapping row"),
):
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    sc = _optional_resolved_state(state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    try:
        ok = fee_column_mappings_repo.delete_mapping_by_id(mapping_id=int(mapping_id), state_code=sc)
    except Exception as ex:
        logger.exception("fee-column-mappings delete failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    if not ok:
        raise HTTPException(status_code=404, detail="Mapping not found or already deleted.")
    return {"ok": True}


@app.put("/app/fee-column-mappings")
def app_upsert_fee_column_mapping(body: FeeColumnMappingUpsert):
    """Upsert dbo.fee_schedule_column_mapping for an artifact ID + DST table name."""
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    sc = _optional_resolved_state(body.state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    row_art = get_artifact_by_id(body.artifact_id)
    if not row_art:
        raise HTTPException(status_code=404, detail="Artifact not found")
    art_sc = (str(row_art.get("state_code") or "").strip().upper())
    if art_sc and art_sc != sc:
        raise HTTPException(
            status_code=400,
            detail="Artifact state_code does not match the requested state_code.",
        )
    try:
        dst = validate_table_name(body.dst_fsname)
        lsk = fee_column_mappings_repo.resolve_schedule_key_for_artifact(row_art)
        saved = fee_column_mappings_repo.upsert_fee_column_mapping(
            state_code=sc,
            state_logical_schedule_key=lsk,
            dst_fsname=dst,
            column_map_json=body.column_map_json,
            updated_by=body.updated_by,
        )
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as ex:
        logger.exception("fee-column-mappings upsert failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex
    cm = _column_map_object(saved.get("column_map_json"))
    return {"ok": True, "mapping": _json_safe_row(saved), "column_map": cm}


@app.post("/app/fee-schedules/compare")
def app_fee_schedule_compare(body: FeeScheduleCompareRequest):
    """Compare the selected saved artifact to DST rows using the saved column mapping (Mapping tab)."""
    if not app_db_configured():
        raise HTTPException(status_code=503, detail="App database not configured.")
    if not _dst_configured():
        raise HTTPException(
            status_code=503,
            detail="DST database not configured: set MSSQL_ODBC_CONN or MSSQL_SERVER in the environment.",
        )
    sc = _optional_resolved_state(body.state_code)
    if sc is None:
        raise HTTPException(status_code=400, detail="state_code is required.")
    try:
        return compare_artifact_to_dst(state_code=sc, artifact_id=body.artifact_id, dst_fsname=body.dst_fsname)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve)) from ve
    except Exception as ex:
        logger.exception("fee-schedules compare failed")
        raise HTTPException(status_code=503, detail=str(ex)) from ex


# -------------------------------------------------------------------
# 🔍 DEBUG / INTERNAL ENDPOINTS (OPTIONAL, KEEP FOR DEV)
# -------------------------------------------------------------------

@app.post("/analyze")
def analyze(request: URLRequest):
    """Debug endpoint: returns analysis only."""
    return run_ingestion_agent(
        request.url,
        discover_links=request.discover_links,
        max_discovered_links=request.max_discovered_links,
    )


@app.post("/extract")
def extract_catalog(request: CatalogExtractRequest):
    """Internal extraction endpoint (legacy / debug)."""
    return run_catalog_extraction(
        request.url,
        paginate=request.paginate,
        max_pages=request.max_pages,
    )




# # backend/app/main.py

# from fastapi import FastAPI
# from pydantic import BaseModel
# from app.agents.ingestion_agent import run_ingestion_agent
# from app.agents.extraction_agent import run_catalog_extraction
# from fastapi.middleware.cors import CORSMiddleware

# app = FastAPI()

# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )


# class URLRequest(BaseModel):
#     url: str
#     discover_links: bool = True
#     max_discovered_links: int = 25


# class CatalogExtractRequest(BaseModel):
#     url: str
#     paginate: bool = True
#     max_pages: int = 200


# @app.post("/extract")
# def extract_catalog(request: CatalogExtractRequest):
#     """Structured rows from selected HTML table blocks (fee schedule catalogs)."""
#     return run_catalog_extraction(
#         request.url,
#         paginate=request.paginate,
#         max_pages=request.max_pages,
#     )


# @app.post("/analyze")
# def analyze(request: URLRequest):
#     return run_ingestion_agent(
#         request.url,
#         discover_links=request.discover_links,
#         max_discovered_links=request.max_discovered_links,
#     )