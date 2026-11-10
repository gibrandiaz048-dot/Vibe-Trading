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
    report = "Desk agrees.\n\x60\x60\x60trade_proposal\n" + __import__("json").dumps(_proposal()) + "\n\x60\x60\x60"
    assert bridge._extract_trade_proposal(report) == _proposal()


def test_narrative_without_tag_never_becomes_proposal():
    assert bridge._extract_trade_proposal("BUY BTC now, target 70000") is None


def test_malformed_or_incomplete_tag_is_ignored():
    assert bridge._extract_trade_proposal("\x60\x60\x60trade_proposal\n{bad json}\n\x60\x60\x60") is None
    assert bridge._extract_trade_proposal("\x60\x60\x60trade_proposal\n{\"asset\":\"BTC-USD\"}\n\x60\x60\x60") is None


def test_payload_forwards_explicit_proposal_to_risk_contract():
    proposal = _proposal()
    payload = bridge._build_swarm_payload("run-123", {}, "report",)
    assert payload["swarm_id"] == "run-123"
    assert "trade_proposal" not in payload

    payload = bridge._build_swarm_payload("run-124", {}, "report\n\x60\x60\x60trade_proposal\n" + __import__("json").dumps(proposal) + "\n\x60\x60\x60")
    assert payload["trade_proposal"] == proposal


def test_native_incomplete_proposal_is_not_forwarded():
    payload = bridge._build_swarm_payload("run-125", {"trade_proposal": {"asset": "BTC-USD"}}, "report")
    assert "trade_proposal" not in payload


def test_report_is_bounded():
    payload = bridge._build_swarm_payload("run-126", {}, "x" * 31000)
    assert len(payload["report"]) == 30000


def test_reads_core_token_only_from_protected_file(tmp_path, monkeypatch):
    token_file = tmp_path / "protected.token"
    token_file.write_text("A" * 43, encoding="ascii")
    monkeypatch.setenv("UNIVERSE_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("UNIVERSE_VIBE_TOKEN", "dotenv-or-env-value-must-not-be-used")
    assert bridge.read_bridge_token() == "A" * 43


def test_missing_token_file_fails_without_generating_a_copy(tmp_path, monkeypatch):
    token_file = tmp_path / "missing" / "protected.token"
    monkeypatch.setenv("UNIVERSE_TOKEN_FILE", str(token_file))
    import pytest
    with pytest.raises(RuntimeError, match="Could not read"):
        bridge.read_bridge_token()
    assert not token_file.exists()


def test_dotenv_loader_skips_core_token_names_and_preserves_existing_environment(tmp_path, monkeypatch):
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "UNIVERSE_VIBE_TOKEN=dotenv-value-one\\n"
        "VIBE_TOKEN=dotenv-value-two\\n"
        "VIBE_TRADING_API_TOKEN=dotenv-api-value\\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("UNIVERSE_VIBE_TOKEN", raising=False)
    monkeypatch.delenv("VIBE_TOKEN", raising=False)
    monkeypatch.setenv("VIBE_TRADING_API_TOKEN", "process-api-value")

    bridge._load_env_file(dotenv)

    assert "UNIVERSE_VIBE_TOKEN" not in __import__("os").environ
    assert "VIBE_TOKEN" not in __import__("os").environ
    assert __import__("os").environ["VIBE_TRADING_API_TOKEN"] == "process-api-value"


def test_core_request_rereads_token_file_once_after_401(tmp_path, monkeypatch):
    import asyncio
    import httpx

    token_file = tmp_path / "protected.token"
    token_file.write_text("A" * 43, encoding="ascii")
    monkeypatch.setenv("UNIVERSE_TOKEN_FILE", str(token_file))

    class FakeHTTP:
        def __init__(self):
            self.calls = []

        async def request(self, method, url, *, headers, **kwargs):
            self.calls.append(dict(headers))
            if len(self.calls) == 1:
                token_file.write_text("B" * 43, encoding="ascii")
                return httpx.Response(401, request=httpx.Request(method, url))
            return httpx.Response(200, json={"ok": True}, request=httpx.Request(method, url))

    async def exercise():
        client = bridge.VibeUniverseBridge()
        original = client.http
        fake = FakeHTTP()
        client.http = fake
        try:
            response = await client.core_request("GET", "/test")
            assert response.status_code == 200
            assert fake.calls == [
                {"Authorization": "Bearer " + "A" * 43},
                {"Authorization": "Bearer " + "B" * 43},
            ]
        finally:
            await original.aclose()

    asyncio.run(exercise())


def test_second_core_401_stops_after_exactly_one_retry(tmp_path, monkeypatch):
    import asyncio
    import httpx
    import pytest

    token_file = tmp_path / "protected.token"
    token_file.write_text("C" * 43, encoding="ascii")
    monkeypatch.setenv("UNIVERSE_TOKEN_FILE", str(token_file))

    class FakeHTTP:
        def __init__(self):
            self.calls = 0

        async def request(self, method, url, *, headers, **kwargs):
            self.calls += 1
            return httpx.Response(401, request=httpx.Request(method, url))

    async def exercise():
        client = bridge.VibeUniverseBridge()
        original = client.http
        fake = FakeHTTP()
        client.http = fake
        try:
            with pytest.raises(bridge.BridgeAuthenticationError, match="HTTP 401"):
                await client.core_request("GET", "/test")
            assert fake.calls == 2
        finally:
            await original.aclose()

    asyncio.run(exercise())
