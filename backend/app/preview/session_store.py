from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional


TTL_SECONDS_DEFAULT = 15 * 60

_lock = threading.Lock()
_sessions: Dict[str, "StoredPreviewSession"] = {}


@dataclass
class StoredPreviewSession:
    cookies: List[Dict[str, Any]]
    csrf_token: Optional[str]
    referrer_url: str
    expires_at: float


def register_preview_authority(
    *,
    referrer_url: str,
    cookies: Optional[List[Dict[str, Any]]] = None,
    csrf_token: Optional[str] = None,
    ttl_seconds: int = TTL_SECONDS_DEFAULT,
) -> str:
    """
    Store transient browser credentials from a scrape run (cookies + optional CSRF).
    Returns opaque id for `/preview` — not derivable without server memory.
    """
    sid = secrets.token_urlsafe(24)
    now = time.time()
    sess = StoredPreviewSession(
        cookies=list(cookies or []),
        csrf_token=(csrf_token or None),
        referrer_url=str(referrer_url or "").strip(),
        expires_at=now + ttl_seconds,
    )
    with _lock:
        _purge_expired_locked(now)
        _sessions[sid] = sess
    return sid


def get_preview_authority(session_id: str | None) -> StoredPreviewSession | None:
    if not session_id or not isinstance(session_id, str):
        return None
    now = time.time()
    with _lock:
        _purge_expired_locked(now)
        return _sessions.get(session_id)


def _purge_expired_locked(now: float) -> None:
    dead = [k for k, v in _sessions.items() if v.expires_at <= now]
    for k in dead:
        del _sessions[k]


def preview_authority_ttl_seconds() -> int:
    return TTL_SECONDS_DEFAULT
