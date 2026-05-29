"""Per-request Groq token accounting (context-local for FastAPI concurrency)."""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_tracker_ctx: contextvars.ContextVar[Optional["GroqUsageTracker"]] = contextvars.ContextVar(
    "groq_usage_tracker",
    default=None,
)


@dataclass
class GroqUsageTracker:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: List[Dict[str, Any]] = field(default_factory=list)

    def add(self, response: Any, *, purpose: str) -> None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        pt = int(getattr(usage, "prompt_tokens", 0) or 0)
        ct = int(getattr(usage, "completion_tokens", 0) or 0)
        tt = int(getattr(usage, "total_tokens", 0) or (pt + ct))
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt
        self.calls.append(
            {
                "purpose": purpose,
                "prompt_tokens": pt,
                "completion_tokens": ct,
                "total_tokens": tt,
            }
        )
        logger.info(
            "Groq usage [%s]: input=%s output=%s total=%s",
            purpose,
            pt,
            ct,
            tt,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "call_count": len(self.calls),
            "calls": list(self.calls),
        }


def reset_groq_usage() -> GroqUsageTracker:
    tracker = GroqUsageTracker()
    _tracker_ctx.set(tracker)
    return tracker


def get_groq_usage() -> GroqUsageTracker:
    tracker = _tracker_ctx.get()
    if tracker is None:
        tracker = GroqUsageTracker()
        _tracker_ctx.set(tracker)
    return tracker


def record_groq_completion(response: Any, *, purpose: str) -> None:
    get_groq_usage().add(response, purpose=purpose)
