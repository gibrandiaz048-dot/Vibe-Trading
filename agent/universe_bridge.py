"""Vibe-Universe Bridge client.

Run from the Vibe-Trading repository:
    python agent/universe_bridge.py

The process is a read-only consumer of Universe Core market/trade events. It
forwards completed Vibe Swarm reports to Universe Core for ANALYSIS/RISK review.
It never submits broker orders or executes trades.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
import websockets
from dotenv import load_dotenv

# Match Vibe-Trading's normal runtime environment locations. Explicit process
# variables win; no credentials are copied into source files.
load_dotenv(Path.home() / ".vibe-trading" / ".env")
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

LOG = logging.getLogger("vibe_universe_bridge")
CORE_API = os.getenv("UNIVERSE_CORE_API_URL", "http://127.0.0.1:8767").rstrip("/")
CORE_WS = os.getenv("UNIVERSE_CORE_WS_URL", "ws://127.0.0.1:8767/ws/integrations/vibe")
VIBE_API = os.getenv("VIBE_TRADING_API_URL", "http://127.0.0.1:8000").rstrip("/")
BRIDGE_TOKEN = os.getenv("UNIVERSE_VIBE_TOKEN", "")
VIBE_API_TOKEN = os.getenv("VIBE_TRADING_API_TOKEN", "")
BRIDGE_ENABLED = os.getenv("UNIVERSE_BRIDGE_ENABLED", "false").strip().lower() == "true"
SYNC_SECONDS = max(5, int(os.getenv("UNIVERSE_BRIDGE_SYNC_SECONDS", "15")))
SWARM_POLL_SECONDS = max(10, int(os.getenv("UNIVERSE_BRIDGE_SWARM_POLL_SECONDS", "20")))
RUNTIME_DIR = Path(os.getenv("VIBE_TRADING_HOME", str(Path.home() / ".vibe-trading"))) / "universe-core"
STATE_FILE = RUNTIME_DIR / "bridge_state.json"
SENT_FILE = RUNTIME_DIR / "forwarded_swarm_runs.json"
MAX_EVENTS = 500


def _extract_trade_proposal(report: str) -> dict[str, Any] | None:
    """Read only an explicitly tagged JSON proposal; narrative text is never traded."""
    match = re.search(r"```trade_proposal\s*(\{.*?\})\s*```", report, re.DOTALL | re.IGNORECASE)
    if not match:
        return None
    try:
        proposal = json.loads(match.group(1))
    except (TypeError, ValueError):
        LOG.warning("Vibe report contained an invalid trade_proposal JSON block.")
        return None
    if not isinstance(proposal, dict):
        LOG.warning("Vibe trade_proposal must be a JSON object.")
        return None
    required = {"trade_id", "strategy", "asset", "direction", "amount", "risk_score"}
    missing = required.difference(proposal)
    if missing:
        LOG.warning("Vibe trade_proposal missing required fields: %s", ", ".join(sorted(missing)))
        return None
    return proposal


def _read_json(path: Path, fallback: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


class VibeUniverseBridge:
    def __init__(self) -> None:
        if not BRIDGE_TOKEN:
            raise RuntimeError("UNIVERSE_VIBE_TOKEN is required; bridge remains disabled without shared authentication.")
        self.state: dict[str, Any] = _read_json(STATE_FILE, {"connected": False, "snapshot": None, "events": []})
        self.sent: set[str] = set(_read_json(SENT_FILE, []))
        self.http = httpx.AsyncClient(timeout=20.0, headers={"X-Vibe-Token": BRIDGE_TOKEN})
        if VIBE_API_TOKEN:
            self.http.headers["Authorization"] = f"Bearer {VIBE_API_TOKEN}"
        self.stop = asyncio.Event()

    def persist(self) -> None:
        _write_json(STATE_FILE, self.state)
        _write_json(SENT_FILE, sorted(self.sent)[-5000:])

    async def sync_snapshot(self) -> None:
        response = await self.http.get(f"{CORE_API}/api/integrations/vibe/state")
        response.raise_for_status()
        self.state["snapshot"] = response.json()
        self.state["last_sync_epoch"] = time.time()
        self.state["connected"] = True
        self.persist()

    async def record_event(self, event: dict[str, Any]) -> None:
        events = self.state.setdefault("events", [])
        events.append(event)
        del events[:-MAX_EVENTS]
        if event.get("type") == "COINBASE_MARKET_TICKER":
            ticker = event.get("ticker")
            if isinstance(ticker, dict) and ticker.get("product"):
                prices = self.state.setdefault("market_prices", {})
                prices[str(ticker["product"]).upper()] = {
                    **ticker,
                    "source": "COINBASE_EXCHANGE",
                    "received_epoch": time.time(),
                    "paper_only": True,
                    "real_orders_enabled": False,
                }
        self.state["last_event_epoch"] = time.time()
        self.state["connected"] = True
        self.persist()

    async def universe_stream(self) -> None:
        delay = 1
        while not self.stop.is_set():
            try:
                async with websockets.connect(CORE_WS, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2_000_000) as ws:
                    await ws.send(json.dumps({"type": "AUTH", "token": BRIDGE_TOKEN}))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if auth.get("type") != "AUTH_OK":
                        raise RuntimeError("Universe Core rejected Vibe bridge authentication.")
                    await self.sync_snapshot()
                    delay = 1
                    async for raw in ws:
                        try:
                            event = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if isinstance(event, dict):
                            await self.record_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["connected"] = False
                self.state["last_error"] = str(exc)[:300]
                self.persist()
                LOG.warning("Universe stream disconnected: %s; retrying in %ss", exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def snapshot_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.sync_snapshot()
            except Exception as exc:
                self.state["last_sync_error"] = str(exc)[:300]
                self.persist()
                LOG.warning("Initial-state sync failed: %s", exc)
            await asyncio.sleep(SYNC_SECONDS)

    async def forward_completed_swarm_runs(self) -> None:
        headers = {"X-Vibe-Token": BRIDGE_TOKEN}
        while not self.stop.is_set():
            try:
                response = await self.http.get(f"{VIBE_API}/swarm/runs")
                response.raise_for_status()
                runs = response.json()
                if not isinstance(runs, list):
                    raise ValueError("Vibe /swarm/runs did not return a list.")
                for item in runs:
                    if not isinstance(item, dict):
                        continue
                    run_id = str(item.get("id", "")).strip()
                    status = str(item.get("status", "")).lower()
                    if not run_id or run_id in self.sent or status not in {"completed", "complete", "success", "succeeded"}:
                        continue
                    detail = await self.http.get(f"{VIBE_API}/swarm/runs/{run_id}")
                    detail.raise_for_status()
                    run = detail.json()
                    report = run.get("final_report") or run.get("report")
                    if not isinstance(report, str) or not report.strip():
                        continue
                    payload = {
                        "swarm_id": run_id,
                        "report": report[:30000],
                        "report_type": "CRYPTO_NEWS" if "news" in str(run.get("preset_name", "")).lower() else "SWARM_REPORT",
                    }
                    proposal = run.get("trade_proposal")
                    if not isinstance(proposal, dict):
                        proposal = _extract_trade_proposal(report)
                    if proposal is not None:
                        payload["trade_proposal"] = proposal
                        LOG.info("Vibe run %s produced a structured proposal for Universe RISK review.", run_id)
                    forwarded = await self.http.post(
                        f"{CORE_API}/api/integrations/vibe/swarm-report",
                        json=payload,
                        headers=headers,
                    )
                    forwarded.raise_for_status()
                    self.sent.add(run_id)
                    self.persist()
                    LOG.info("Forwarded completed Vibe run %s to Universe Core.", run_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOG.warning("Vibe Swarm forwarding cycle failed: %s", exc)
            await asyncio.sleep(SWARM_POLL_SECONDS)

    async def run(self) -> None:
        LOG.info("Starting Vibe-Universe Bridge (paper-only; no broker execution).")
        try:
            await asyncio.gather(self.universe_stream(), self.snapshot_loop(), self.forward_completed_swarm_runs())
        finally:
            self.stop.set()
            await self.http.aclose()


async def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    if not BRIDGE_ENABLED:
        LOG.info("Bridge disabled; set UNIVERSE_BRIDGE_ENABLED=true to start it.")
        return
    bridge = VibeUniverseBridge()
    await bridge.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
