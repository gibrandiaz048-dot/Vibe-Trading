"""Read-only REST surface for the local Vibe-Universe Bridge cache."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from fastapi import Depends, FastAPI, HTTPException


def _state_file() -> Path:
    from src.config.paths import get_runtime_root
    return get_runtime_root() / "universe-core" / "bridge_state.json"


def _load_state() -> dict[str, Any]:
    path = _state_file()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"connected": False, "snapshot": None, "events": [], "message": "Bridge client has not started."}
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Bridge cache is unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise HTTPException(status_code=503, detail="Bridge cache has an invalid shape.")
    return value


def register_universe_bridge_routes(
    app: FastAPI,
    require_local_or_auth: Callable[..., Any],
) -> None:
    """Expose cached Universe Core state without granting any execution capability."""

    @app.get("/universe/core/state", dependencies=[Depends(require_local_or_auth)])
    async def universe_core_state():
        state = _load_state()
        return {
            "connected": bool(state.get("connected")),
            "snapshot": state.get("snapshot"),
            "last_sync_epoch": state.get("last_sync_epoch"),
            "last_error": state.get("last_error") or state.get("last_sync_error"),
            "paper_only": True,
            "real_orders_enabled": False,
        }

    @app.get("/universe/core/events", dependencies=[Depends(require_local_or_auth)])
    async def universe_core_events(limit: int = 100):
        if limit < 1 or limit > 500:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
        state = _load_state()
        events = state.get("events", [])
        if not isinstance(events, list):
            events = []
        return {
            "connected": bool(state.get("connected")),
            "events": events[-limit:],
            "paper_only": True,
            "real_orders_enabled": False,
        }
