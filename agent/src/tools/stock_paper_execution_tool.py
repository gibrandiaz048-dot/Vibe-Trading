"""Swarm-facing adapter for the persistent, stock-only paper account.

This tool delegates every fill to the existing daily-bar PaperAccounts engine.
It never imports or calls a broker, exchange connector, or live-order service.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from src.agent.tools import BaseTool
from src.tools.paper_trade.accounts import AccountConflict, PaperAccounts

_ACCOUNT_LABEL = "Universe Core Paper"
_INITIAL_CASH = 1000.0
_MAX_RISK_FRACTION = 0.02


def _latest_completed_us_session() -> str:
    now = datetime.now(ZoneInfo("America/New_York"))
    day = now.date()
    if (now.hour, now.minute) < (16, 30):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day.isoformat()


class StockPaperExecutionTool(BaseTool):
    name = "stock_paper_execution"
    description = (
        "Manage and execute stock BUY/SELL orders in Vibe-Trading's persistent "
        "US paper-trading account. Operations: initialize (creates a $1,000 "
        "virtual USD account once), status (read account), execute (settle "
        "orders against completed US daily bars). Each order needs symbol, "
        "side, qty, limit and stop_loss. The stop_loss is used only to enforce "
        "a maximum 2% account-equity risk sizing gate; this daily-bar engine "
        "does NOT monitor or trigger live stops automatically. Fills are "
        "simulated at daily close under the existing engine rules. Never sends "
        "real orders. Use only US-listed stocks with symbols accepted by "
        "Vibe-Trading's US market data provider."
    )
    parameters = {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": ["initialize", "status", "execute"]},
            "account_id": {"type": "string"},
            "orders": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "side": {"type": "string", "enum": ["buy", "sell"]},
                        "qty": {"type": "number"},
                        "limit": {"type": "number"},
                        "stop_loss": {"type": "number"},
                    },
                    "required": ["symbol", "side", "qty", "limit"],
                    "additionalProperties": False,
                },
            },
            "to_date": {"type": "string", "description": "Optional YYYY-MM-DD completed US session."},
        },
        "required": ["operation"],
        "additionalProperties": False,
    }
    is_readonly = False
    repeatable = True

    def __init__(self) -> None:
        self.accounts = PaperAccounts()

    def execute(self, **kwargs: Any) -> str:
        operation = kwargs.get("operation")
        try:
            if operation == "initialize":
                existing = next(
                    (a for a in self.accounts.list() if a.get("label") == _ACCOUNT_LABEL and a.get("market") == "us"),
                    None,
                )
                account = existing or self.accounts.create(
                    label=_ACCOUNT_LABEL,
                    market="us",
                    account_state={"cash": _INITIAL_CASH, "positions": []},
                    start="today",
                )
                return json.dumps({"status": "ok", "created": existing is None, "account": account})

            account_id = kwargs.get("account_id")
            if not isinstance(account_id, str) or not account_id:
                return json.dumps({"status": "error", "error": "account_id is required"})
            account = self.accounts.get(account_id)

            if operation == "status":
                return json.dumps({"status": "ok", "account": account, "history": self.accounts.history(account_id)})
            if operation != "execute":
                return json.dumps({"status": "error", "error": "Unsupported operation"})

            orders = kwargs.get("orders")
            if not isinstance(orders, list) or not orders:
                return json.dumps({"status": "error", "error": "execute requires at least one order"})
            if account.get("market") != "us":
                return json.dumps({"status": "error", "error": "Executor restricted to US stocks"})

            state = account["account_state"]
            positions = {str(p["symbol"]).strip().upper(): p for p in state.get("positions", [])}
            equity = float(state.get("cash", 0.0)) + sum(
                float(p["qty"]) * float(p["avg_cost"]) for p in positions.values()
            )
            clean_orders = []
            for index, order in enumerate(orders):
                symbol = order.get("symbol")
                side = order.get("side")
                qty = float(order.get("qty", 0))
                limit = float(order.get("limit", 0))
                stop_raw = order.get("stop_loss")
                stop = float(stop_raw) if stop_raw is not None else 0.0
                if not isinstance(symbol, str) or not symbol.strip():
                    raise ValueError(f"orders[{index}].symbol is required")
                if side not in ("buy", "sell") or not all(math.isfinite(v) and v > 0 for v in (qty, limit)):
                    raise ValueError(f"orders[{index}] has invalid side, qty, or limit")
                if side == "buy":
                    if not math.isfinite(stop) or stop <= 0 or stop >= limit:
                        raise ValueError(f"{symbol}: BUY requires a positive stop_loss below buy limit")
                    risk = qty * abs(limit - stop)
                    if risk > equity * _MAX_RISK_FRACTION + 1e-8:
                        raise ValueError(
                            f"{symbol}: estimated stop risk ${risk:.2f} exceeds 2% account equity (${equity * _MAX_RISK_FRACTION:.2f})"
                        )
                if side == "sell" and (symbol not in positions or qty > float(positions[symbol]["qty"])):
                    raise ValueError(f"{symbol}: sell quantity exceeds held paper position")
                clean_orders.append({"symbol": symbol.strip().upper(), "side": side, "qty": qty, "limit": limit})

            result = self.accounts.advance(
                account_id,
                expected_revision=account["revision"],
                to_date=kwargs.get("to_date") or _latest_completed_us_session(),
                orders=clean_orders,
            )
            return json.dumps({"status": "ok" if result.get("committed") else "not_committed", **result}, ensure_ascii=False)
        except (KeyError, ValueError, AccountConflict) as exc:
            return json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False)
