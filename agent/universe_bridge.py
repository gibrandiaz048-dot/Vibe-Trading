"""Vibe-Universe Bridge client.

Run from the Vibe-Trading repository:
    python agent/universe_bridge.py

This client forwards completed Vibe Swarm reports to Universe Core for
ANALYSIS/RISK review. It never submits broker orders or executes trades.
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


def _load_env_file(path: Path) -> None:
    """Load non-Core credentials without overriding process environment.

    The Universe bridge credential is intentionally excluded from dotenv:
    UNIVERSE_TOKEN_FILE is the only permitted source for that credential.
    """
    if not path.is_file():
        return
    try:
        raw = path.read_bytes()
        if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in raw[:256]:
            content = raw.decode("utf-16")
        else:
            content = raw.decode("utf-8-sig")
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in {"UNIVERSE_VIBE_TOKEN", "VIBE_TOKEN"}:
                continue
            if value.startswith(("'", '"')) and value.endswith(("'", '"')) and len(value) >= 2:
                value = value[1:-1]
            if key and key.replace("_", "").isalnum():
                os.environ.setdefault(key, value)
    except (OSError, UnicodeError) as exc:
        logging.getLogger("vibe_universe_bridge").warning(
            "Could not read dotenv configuration (%s).", type(exc).__name__
        )


_load_env_file(Path.home() / ".vibe-trading" / ".env")
_load_env_file(Path(__file__).resolve().parents[1] / ".env")
_load_env_file(Path(__file__).resolve().parent / ".env")

LOG = logging.getLogger("vibe_universe_bridge")
CORE_API = os.getenv("UNIVERSE_CORE_API_URL", "http://127.0.0.1:8766").rstrip("/")
CORE_WS = os.getenv("UNIVERSE_CORE_WS_URL", "ws://127.0.0.1:8766/ws/integrations/vibe")
VIBE_API = os.getenv("VIBE_TRADING_API_URL", "http://127.0.0.1:8000").rstrip("/")
VIBE_API_TOKEN = os.getenv("VIBE_TRADING_API_TOKEN", "")
BRIDGE_ENABLED = os.getenv("UNIVERSE_BRIDGE_ENABLED", "false").strip().lower() == "true"
SYNC_SECONDS = max(5, int(os.getenv("UNIVERSE_BRIDGE_SYNC_SECONDS", "15")))
SWARM_POLL_SECONDS = max(10, int(os.getenv("UNIVERSE_BRIDGE_SWARM_POLL_SECONDS", "20")))
RUNTIME_DIR = Path(os.getenv("VIBE_TRADING_HOME", str(Path.home() / ".vibe-trading"))) / "universe-core"
STATE_FILE = RUNTIME_DIR / "bridge_state.json"
SENT_FILE = RUNTIME_DIR / "forwarded_swarm_runs.json"
MAX_EVENTS = 500
TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")


class BridgeAuthenticationError(RuntimeError):
    """Core rejected the token after the one permitted file reread."""


def token_file_path() -> Path:
    configured = os.getenv("UNIVERSE_TOKEN_FILE", "").strip()
    if not configured:
        raise RuntimeError("UNIVERSE_TOKEN_FILE is required; bridge authentication is unavailable.")
    return Path(configured).expanduser().resolve()


def read_bridge_token() -> str:
    """Read the Core token only from the protected file; never generate or persist it."""
    path = token_file_path()
    try:
        token = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise RuntimeError("Could not read the protected Universe token file.") from exc
    if not TOKEN_PATTERN.fullmatch(token):
        raise RuntimeError("Protected Universe token file is malformed.")
    return token


def _extract_trade_proposal(report: str) -> dict[str, Any] | None:
    """Read only an explicitly tagged JSON proposal; narrative text is never traded."""
    match = re.search(r"\x60\x60\x60trade_proposal\s*(\{.*?\})\s*\x60\x60\x60", report, re.DOTALL | re.IGNORECASE)
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


def _build_swarm_payload(run_id: str, run: dict[str, Any], report: str) -> dict[str, Any]:
    """Build the bridge payload; only explicit proposals reach Universe risk review."""
    payload: dict[str, Any] = {
        "swarm_id": run_id,
        "report": report[:30000],
        "report_type": "CRYPTO_NEWS" if "news" in str(run.get("preset_name", "")).lower() else "SWARM_REPORT",
    }
    proposal = run.get("trade_proposal")
    if not isinstance(proposal, dict):
        proposal = _extract_trade_proposal(report)
    if proposal is not None:
        required = {"trade_id", "strategy", "asset", "direction", "amount", "risk_score"}
        if required.issubset(proposal):
            payload["trade_proposal"] = proposal
        else:
            LOG.warning("Ignoring incomplete native Vibe trade proposal.")
    return payload


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
        read_bridge_token()
        self.state: dict[str, Any] = _read_json(STATE_FILE, {"connected": False, "snapshot": None, "events": []})
        self.sent: set[str] = set(_read_json(SENT_FILE, []))
        self.http = httpx.AsyncClient(timeout=20.0)
        if VIBE_API_TOKEN:
            self.http.headers["Authorization"] = f"Bearer {VIBE_API_TOKEN}"
        self.stop = asyncio.Event()

    def persist(self) -> None:
        _write_json(STATE_FILE, self.state)
        _write_json(SENT_FILE, sorted(self.sent)[-5000:])

    async def core_request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Use Bearer auth and reread the protected file for one bounded 401 retry."""
        supplied_headers = dict(kwargs.pop("headers", {}) or {})
        for attempt in range(2):
            token = read_bridge_token()
            headers = {**supplied_headers, "Authorization": f"Bearer {token}"}
            response = await self.http.request(method, f"{CORE_API}{path}", headers=headers, **kwargs)
            if response.status_code == 401 and attempt == 0:
                continue
            if response.status_code == 401:
                raise BridgeAuthenticationError(
                    "Universe Core rejected the bridge credential after one token-file reread (HTTP 401)."
                )
            response.raise_for_status()
            return response
        raise BridgeAuthenticationError("Universe Core bridge authentication failed (HTTP 401).")

    async def sync_snapshot(self) -> None:
        response = await self.core_request("GET", "/api/integrations/vibe/state")
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
                async with websockets.connect(
                    CORE_WS, ping_interval=20, ping_timeout=20, close_timeout=5, max_size=2_000_000
                ) as ws:
                    await ws.send(json.dumps({"type": "AUTH", "token": read_bridge_token()}))
                    auth = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if auth.get("type") != "AUTH_OK":
                        raise BridgeAuthenticationError("Universe Core rejected WebSocket bridge authentication.")
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
            except BridgeAuthenticationError:
                self.stop.set()
                raise
            except Exception as exc:
                self.state["connected"] = False
                self.state["last_error"] = type(exc).__name__[:100]
                self.persist()
                LOG.warning("Universe stream disconnected (%s); retrying in %ss", type(exc).__name__, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30)

    async def snapshot_loop(self) -> None:
        while not self.stop.is_set():
            try:
                await self.sync_snapshot()
            except asyncio.CancelledError:
                raise
            except BridgeAuthenticationError:
                self.stop.set()
                raise
            except Exception as exc:
                self.state["last_sync_error"] = type(exc).__name__
                self.persist()
                LOG.warning("Initial-state sync failed (%s).", type(exc).__name__)
            await asyncio.sleep(SYNC_SECONDS)

    async def forward_completed_swarm_runs(self) -> None:
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
                    payload = _build_swarm_payload(run_id, run, report)
                    if "trade_proposal" in payload:
                        LOG.info("Vibe run %s produced a structured proposal for Universe RISK review.", run_id)
                    forwarded = await self.core_request(
                        "POST", "/api/integrations/vibe/swarm-report", json=payload
                    )
                    self.sent.add(run_id)
                    self.persist()
                    LOG.info("Forwarded completed Vibe run %s to Universe Core (HTTP %s).", run_id, forwarded.status_code)
            except asyncio.CancelledError:
                raise
            except BridgeAuthenticationError:
                self.stop.set()
                raise
            except Exception as exc:
                LOG.warning("Vibe Swarm forwarding cycle failed (%s).", type(exc).__name__)
            await asyncio.sleep(SWARM_POLL_SECONDS)

    async def run(self) -> None:
        LOG.info("Starting Vibe-Universe Bridge (paper-only; no broker execution).")
        tasks = [
            asyncio.create_task(self.universe_stream(), name="universe-stream"),
            asyncio.create_task(self.snapshot_loop(), name="core-snapshot"),
            asyncio.create_task(self.forward_completed_swarm_runs(), name="swarm-forwarder"),
        ]
        try:
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            for task in done:
                task.result()
        finally:
            self.stop.set()
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.http.aclose()


async def main() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
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
