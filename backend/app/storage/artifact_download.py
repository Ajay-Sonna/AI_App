"""Download fee-schedule files to local disk (state / schedule folder layout) and register in DB."""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

from app.app_db.artifacts_repo import (
    delete_artifact_row,
    get_artifact_by_id,
    get_current_artifact_for_logical_key,
    insert_artifact_row,
    recompute_is_current_for_logical_key,
)
from app.preview.preview_service import (
    _DEFAULT_UA,
    _curl_warm_referrer_merge_cookies,
    _download_via_curl_cffi,
    _merge_accept_with_browser_headers,
    _maybe_warm_referrer,
    auto_preview_accept_attempts,
)
from app.state_codes import resolve_us_state_code

logger = logging.getLogger(__name__)

_MAX_BYTES = 50 * 1024 * 1024  # 50 MiB per file
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _default_local_root() -> Path:
    if os.name == "nt":
        return Path("C:/FeeScheduleVault")
    return Path.home() / "FeeScheduleVault"


def _artifact_root() -> Path:
    raw = (os.getenv("FEE_SCHEDULE_LOCAL_ROOT") or os.getenv("ARTIFACT_ROOT") or "").strip()
    if not raw:
        raw = str(_default_local_root())
    p = Path(raw).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _validate_public_http_url(url: str) -> str:
    u = url.strip()
    parsed = urlparse(u)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must be http or https")
    host = parsed.hostname
    if not host:
        raise ValueError("URL must include a host")
    try:
        infos = ipaddress.ip_address(host)
        if infos.is_private or infos.is_loopback or infos.is_link_local:
            raise ValueError("URL host must not be a private IP")
    except ValueError:
        pass  # hostname, not an IP literal
    return u


def _leaf_filename(url: str) -> str:
    path = urlparse(url).path or ""
    leaf = path.rstrip("/").rsplit("/", 1)[-1] or "download"
    leaf = _SAFE_NAME_RE.sub("_", leaf)[:180]
    return leaf or "download"


def _guess_ext(url: str, content_type: Optional[str]) -> str:
    low = url.lower()
    for ext in (".pdf", ".xlsx", ".xls", ".csv", ".zip", ".json"):
        if ext in low.split("?", 1)[0]:
            return ext
    ct = (content_type or "").split(";")[0].strip().lower()
    if "pdf" in ct:
        return ".pdf"
    if "spreadsheet" in ct or "excel" in ct:
        return ".xlsx"
    if "csv" in ct:
        return ".csv"
    return ""


def _slug_folder(logical_schedule_key: str) -> str:
    s = (logical_schedule_key or "").strip().lower() or "fee_schedule"
    s = _SAFE_NAME_RE.sub("_", s).strip("._-")[:96]
    return s or "fee_schedule"


def _http_last_modified_utc(val: Optional[str]) -> Optional[datetime]:
    if not val or not str(val).strip():
        return None
    try:
        dt = parsedate_to_datetime(str(val).strip())
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _norm_etag(val: Optional[str]) -> Optional[str]:
    if not val:
        return None
    s = str(val).strip()
    return s[:256] if s else None


def _referrer_for_sec_fetch(resource_url: str, referer_portal: Optional[str]) -> str:
    """Referrer URL used only for Sec-Fetch-* / merge logic (must be non-empty when possible)."""
    r = (referer_portal or "").strip()
    if r:
        return r
    try:
        p = urlparse(resource_url)
        if p.scheme in ("http", "https") and p.netloc:
            return f"{p.scheme}://{p.netloc}/"
    except Exception:
        pass
    return resource_url


def _browser_header_attempts(resource_url: str, referer_portal: Optional[str]) -> List[Dict[str, str]]:
    """Same Accept / Sec-Fetch strategy as preview downloads (many .gov WAFs block bare User-Agent)."""
    sec_ref = _referrer_for_sec_fetch(resource_url, referer_portal)
    out: List[Dict[str, str]] = []
    for accept_extra in auto_preview_accept_attempts(resource_url):
        merged = dict(_merge_accept_with_browser_headers(resource_url, sec_ref, accept_extra))
        merged["User-Agent"] = _DEFAULT_UA
        rp = (referer_portal or "").strip()
        if rp:
            merged["Referer"] = rp
        out.append(merged)
    return out


def _download_public_file_to_path(
    safe_url: str,
    referer_portal: Optional[str],
    destination: Path,
) -> Tuple[int, str, Optional[str], Optional[str], Optional[datetime]]:
    """
    Browser-like GET (multi-Accept + Sec-Fetch-*) then curl-cffi TLS impersonation (preview parity).
    Writes ``destination`` and returns ``(size, sha256_hex, raw_content_type, etag, last_modified_utc)``.
    """
    if destination.exists():
        destination.unlink()

    attempts = _browser_header_attempts(safe_url, referer_portal)
    session = requests.Session()
    session.headers.update({"User-Agent": _DEFAULT_UA})
    rp = (referer_portal or "").strip()
    if rp:
        session.headers["Referer"] = rp
    _maybe_warm_referrer(session, safe_url, rp, None)

    last_status = -1
    for hdr in attempts:
        try:
            with session.get(safe_url, stream=True, timeout=120, headers=hdr, allow_redirects=True) as resp:
                last_status = resp.status_code
                if resp.status_code >= 400:
                    continue
                ctype = resp.headers.get("content-type")
                etag = _norm_etag(resp.headers.get("ETag"))
                lm = _http_last_modified_utc(resp.headers.get("Last-Modified"))
                h = hashlib.sha256()
                total = 0
                with open(destination, "wb") as out:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > _MAX_BYTES:
                            raise ValueError(f"Download exceeds {_MAX_BYTES} bytes")
                        h.update(chunk)
                        out.write(chunk)
                return total, h.hexdigest(), ctype, etag, lm
        except Exception:
            if destination.exists():
                destination.unlink(missing_ok=True)
            continue

    sec_ref = _referrer_for_sec_fetch(safe_url, referer_portal)
    ref_curl = (referer_portal or "").strip() or sec_ref
    attempt_log: List[Dict[str, Any]] = []
    ck: Dict[str, str] = {}
    ck = _curl_warm_referrer_merge_cookies(safe_url, ref_curl, ck, attempt_log)

    ul = safe_url.lower()
    curl_accepts: List[Optional[Dict[str, str]]] = [
        None,
        {
            "Accept": (
                "application/pdf,application/x-pdf,"
                "application/octet-stream;q=0.95,*/*;q=0.05"
            )
        },
    ]
    if any(x in ul for x in (".xlsx", ".xls", ".xlsm")) or "spreadsheet" in ul:
        curl_accepts.append(
            {
                "Accept": (
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
                    "application/vnd.ms-excel,"
                    "application/octet-stream;q=0.95,*/*;q=0.05"
                )
            }
        )

    for caccept in curl_accepts:
        hit = _download_via_curl_cffi(
            safe_url,
            ref_curl,
            ck,
            caccept,
            attempt_log,
            max_response_bytes=_MAX_BYTES,
        )
        if not hit:
            continue
        data, ctype_hdr, st = hit
        if st >= 400 or not data:
            continue
        if len(data) > _MAX_BYTES:
            raise ValueError(f"Download exceeds {_MAX_BYTES} bytes")
        h2 = hashlib.sha256()
        h2.update(data)
        destination.write_bytes(data)
        return len(data), h2.hexdigest(), ctype_hdr, None, None

    raise RuntimeError(
        f"HTTP {last_status} download failed for url: {safe_url}"
        if last_status > 0
        else f"Download failed for url: {safe_url}",
    )


def _parse_portal_effective_date(hint: Optional[str]) -> Optional[date]:
    """``YYYY-MM-DD`` from catalog / API hint."""
    if not hint or not str(hint).strip():
        return None
    s = str(hint).strip()[:32]
    try:
        y, mo, d = s.split("-", 2)
        return date(int(y), int(mo), int(d))
    except (ValueError, TypeError):
        return None


def _skip_response(
    *,
    row: Dict[str, Any],
    root: Path,
    reason: str,
) -> Dict[str, Any]:
    rel = str(row.get("stored_rel_path") or "").replace("\\", "/")
    return {
        "artifact_id": int(row["artifact_id"]),
        "content_sha256": row.get("content_sha256"),
        "bytes_size": int(row.get("bytes_size") or 0),
        "stored_rel_path": rel,
        "absolute_path": str((root / rel).resolve()) if rel else "",
        "skipped": True,
        "skip_reason": reason,
    }


def download_fee_schedule_artifact(
    *,
    url: str,
    state_code: Optional[str] = None,
    logical_schedule_key: Optional[str] = None,
    source_label: Optional[str] = None,
    referer: Optional[str] = None,
    portal_date_hint: Optional[str] = None,
    effective_date_source: Optional[str] = None,
    is_superseded_hint: bool = False,
    defer_recompute: bool = False,
) -> Dict[str, Any]:
    """
    Download ``url`` under ``{ARTIFACT_ROOT}/{state}/{logical_folder}/``, register in DB.
    ``is_current`` is assigned by date-primary recompute (latest effective date per logical schedule).

    Skips full download when an existing current row's ETag matches, or same URL bytes (SHA256) as current.
    """
    safe_url = _validate_public_http_url(url)
    sc_raw = (state_code or "").strip()
    sc = resolve_us_state_code(sc_raw) if sc_raw else None

    leaf = _leaf_filename(safe_url)
    lsk_raw = (logical_schedule_key or "").strip()[:256]
    lsk = lsk_raw or _slug_folder(leaf.rsplit(".", 1)[0] if "." in leaf else leaf)

    root = _artifact_root()
    state_folder = (sc or "unknown").lower()
    folder_slug = _slug_folder(lsk)
    target_dir = root / state_folder / folder_slug
    target_dir.mkdir(parents=True, exist_ok=True)

    hdr_attempts = _browser_header_attempts(safe_url, referer)
    cur_row = get_current_artifact_for_logical_key(state_code=sc, logical_schedule_key=lsk)

    try:
        head = requests.head(safe_url, headers=hdr_attempts[0], allow_redirects=True, timeout=45)
        if head.ok and cur_row:
            etag = _norm_etag(head.headers.get("ETag"))
            db_etag = _norm_etag(cur_row.get("remote_etag"))
            if etag and db_etag and etag == db_etag:
                return _skip_response(row=cur_row, root=root, reason="etag_unchanged")
    except Exception as ex:
        logger.debug("HEAD skipped for %s: %s", safe_url, ex)

    tmp_path = target_dir / f".part_{os.getpid()}_{hashlib.md5(safe_url.encode('utf-8'), usedforsecurity=False).hexdigest()[:10]}"
    total, digest, ctype, etag_final, lm_final = _download_public_file_to_path(safe_url, referer, tmp_path)

    ext = _guess_ext(safe_url, ctype)
    if ext and not leaf.lower().endswith(ext):
        if "." not in leaf:
            leaf = leaf + ext
    if cur_row and str(cur_row.get("content_sha256") or "") == digest:
        tmp_path.unlink(missing_ok=True)
        return _skip_response(row=cur_row, root=root, reason="sha256_unchanged")

    portal_date = (portal_date_hint or "").strip()[:32]
    day_fetch = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_prefix = portal_date if portal_date else day_fetch
    final_name = f"{date_prefix}__{digest[:16]}_{leaf}"
    final_path = target_dir / final_name
    if final_path.exists():
        tmp_path.unlink(missing_ok=True)
    else:
        tmp_path.replace(final_path)

    rel_dir = Path(state_folder) / folder_slug
    rel_path = str(rel_dir / final_name).replace("\\", "/")

    lm_sql = lm_final.replace(tzinfo=None) if lm_final else None

    ped = _parse_portal_effective_date(portal_date_hint)
    src_eff = (effective_date_source or "").strip()[:32] or None
    if ped is not None and not src_eff:
        src_eff = "catalog"
    aid = insert_artifact_row(
        state_code=sc,
        logical_schedule_key=lsk,
        source_url=safe_url,
        content_sha256=digest,
        stored_rel_path=rel_path,
        original_filename=leaf,
        mime_type=(ctype or "").split(";")[0].strip()[:256] or None,
        bytes_size=total,
        source_label=source_label,
        remote_etag=etag_final,
        remote_last_modified_utc=lm_sql,
        portal_effective_date=ped,
        effective_date_source=src_eff,
        is_superseded_hint=is_superseded_hint,
    )

    if not defer_recompute:
        try:
            recompute_is_current_for_logical_key(state_code=sc, logical_schedule_key=lsk)
        except Exception as ex:
            logger.warning("recompute_is_current_for_logical_key failed: %s", ex)

    return {
        "artifact_id": aid,
        "content_sha256": digest,
        "bytes_size": total,
        "stored_rel_path": rel_path,
        "absolute_path": str((root / rel_path).resolve()),
        "logical_schedule_key": lsk,
    }


def resolve_artifact_path(stored_rel_path: str) -> Path:
    root = _artifact_root()
    p = (root / stored_rel_path).resolve()
    root_resolved = root.resolve()
    try:
        p.relative_to(root_resolved)
    except ValueError:
        raise ValueError("Invalid stored path") from None
    return p


def delete_stored_artifact(artifact_id: int) -> None:
    """Remove DB row and delete the on-disk file under ARTIFACT_ROOT (best-effort file delete)."""
    row = get_artifact_by_id(int(artifact_id))
    if not row:
        return
    rel = (row.get("stored_rel_path") or "").strip()
    if rel:
        try:
            p = resolve_artifact_path(rel)
            if p.is_file():
                p.unlink()
        except Exception as ex:
            logger.warning("Could not delete artifact file %s: %s", rel, ex)
    delete_artifact_row(int(artifact_id))
    lsk = (row.get("logical_schedule_key") or "").strip()
    sc_code = row.get("state_code")
    if lsk:
        try:
            recompute_is_current_for_logical_key(state_code=sc_code, logical_schedule_key=lsk)
        except Exception as ex:
            logger.warning("recompute after delete failed: %s", ex)
