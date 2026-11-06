"""Contract tests for the Vibe-Universe paper-trading bridge."""
from __future__ import annotations

import importlib.util
from pathlib import Path

MODULE_PATH = Path(__file__).resolve().parents[1] / "universe_bridge.py"
SPEC = importlib.util.spec_from_file_location("universe_bridge_under_test", MODULE_PATH)
bridge = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bridge)


def _proposal():
    return {
        "trade_id": "vibe-btc-001",
        "strategy": "momentum-confirmation",
        "asset": "BTC-USD",
        "direction": "LONG",
        "amount": 100.0,
        "risk_score": 0.25,
        "entry_price": 65000.0,
        "stop_loss_price": 64000.0,
        "take_profit_price": 68000.0,
    }


def test_extracts_only_explicit_tagged_proposal():
    report = "Desk agrees.\\n\x60\x60\x60trade_proposal\\n" + __import__("json").dumps(_proposal()) + "\\n\x60\x60\x60"
    assert bridge._extract_trade_proposal(report) == _proposal()


def test_narrative_without_tag_never_becomes_proposal():
    assert bridge._extract_trade_proposal("BUY BTC now, target 70000") is None


def test_malformed_or_incomplete_tag_is_ignored():
    assert bridge._extract_trade_proposal("\x60\x60\x60trade_proposal\\n{bad json}\\n\x60\x60\x60") is None
    assert bridge._extract_trade_proposal("\x60\x60\x60trade_proposal\\n{\"asset\":\"BTC-USD\"}\\n\x60\x60\x60") is None


def test_payload_forwards_explicit_proposal_to_risk_contract():
    proposal = _proposal()
    payload = bridge._build_swarm_payload("run-123", {}, "report",)
    assert payload["swarm_id"] == "run-123"
    assert "trade_proposal" not in payload

    payload = bridge._build_swarm_payload("run-124", {}, "report\\n\x60\x60\x60trade_proposal\\n" + __import__("json").dumps(proposal) + "\\n\x60\x60\x60")
    assert payload["trade_proposal"] == proposal


def test_native_incomplete_proposal_is_not_forwarded():
    payload = bridge._build_swarm_payload("run-125", {"trade_proposal": {"asset": "BTC-USD"}}, "report")
    assert "trade_proposal" not in payload


def test_report_is_bounded():
    payload = bridge._build_swarm_payload("run-126", {}, "x" * 31000)
    assert len(payload["report"]) == 30000
