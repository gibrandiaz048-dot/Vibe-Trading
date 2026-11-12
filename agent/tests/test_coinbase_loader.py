"""Tests for the public Coinbase Exchange OHLCV loader."""

from __future__ import annotations

from unittest.mock import MagicMock

import pandas as pd
import pytest
import requests

from backtest.loaders import coinbase_loader


def _response(payload, status=200):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_loader_is_public_and_registered():
    assert coinbase_loader.CoinbaseLoader.name == "coinbase"
    assert coinbase_loader.CoinbaseLoader.requires_auth is False
    assert "coinbase" in coinbase_loader.VALID_SOURCES if hasattr(coinbase_loader, "VALID_SOURCES") else True


def test_is_available_uses_public_btc_usd_ticker():
    loader = coinbase_loader.CoinbaseLoader()
    loader.session.get = MagicMock(return_value=_response({"price": "65000"}))
    assert loader.is_available() is True
    loader.session.get.assert_called_once()
    assert "/products/BTC-USD/ticker" in loader.session.get.call_args.args[0]


@pytest.mark.parametrize("interval,seconds", [
    ("1m", 60), ("5m", 300), ("15m", 900),
    ("1h", 3600), ("6h", 21600), ("1d", 86400),
])
def test_supported_interval_mapping(interval, seconds):
    assert coinbase_loader._INTERVALS[interval] == seconds


def test_unsupported_interval_fails_before_network():
    loader = coinbase_loader.CoinbaseLoader()
    loader.session.get = MagicMock()
    with pytest.raises(ValueError, match="does not support interval"):
        loader.fetch(["BTC-USD"], "2026-11-01", "2026-11-02", interval="2h")
    loader.session.get.assert_not_called()


def test_fetch_normalizes_coinbase_candle_order_and_preserves_quote(monkeypatch):
    loader = coinbase_loader.CoinbaseLoader()
    timestamp = int(pd.Timestamp("2026-11-02T00:00:00Z").timestamp())
    # Coinbase order is [time, low, high, open, close, volume].
    loader.session.get = MagicMock(return_value=_response([
        [timestamp, "99", "111", "101", "108", "12.5"]
    ]))
    monkeypatch.setattr(
        coinbase_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )

    result = loader.fetch(
        ["BTC-USD"], "2026-11-02", "2026-11-02", interval="1D"
    )
    frame = result["BTC-USD"]
    assert list(frame.columns) == ["open", "high", "low", "close", "volume"]
    assert frame.iloc[0].to_dict() == {
        "open": 101.0, "high": 111.0, "low": 99.0,
        "close": 108.0, "volume": 12.5,
    }
    assert frame.attrs["quote_currency"] == "USD"


def test_fetch_converts_slash_symbol_to_coinbase_product(monkeypatch):
    loader = coinbase_loader.CoinbaseLoader()
    timestamp = int(pd.Timestamp("2026-11-02T00:00:00Z").timestamp())
    loader.session.get = MagicMock(return_value=_response([
        [timestamp, "99", "111", "101", "108", "12.5"]
    ]))
    monkeypatch.setattr(
        coinbase_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )

    result = loader.fetch(["ETH/USD"], "2026-11-02", "2026-11-02")
    assert "ETH/USD" in result
    assert "/products/ETH-USD/candles" in loader.session.get.call_args.args[0]
    assert result["ETH/USD"].attrs["quote_currency"] == "USD"


def test_fetch_skips_invalid_product_symbol():
    loader = coinbase_loader.CoinbaseLoader()
    loader.session.get = MagicMock()
    result = loader.fetch(["BTC"], "2026-11-02", "2026-11-02")
    assert result == {}
    loader.session.get.assert_not_called()


def test_fetch_returns_empty_for_no_candles(monkeypatch):
    loader = coinbase_loader.CoinbaseLoader()
    loader.session.get = MagicMock(return_value=_response([]))
    monkeypatch.setattr(
        coinbase_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )
    assert loader.fetch(["BTC-USD"], "2026-11-02", "2026-11-02") == {}


def test_fetch_does_not_retry_non_request_errors(monkeypatch):
    loader = coinbase_loader.CoinbaseLoader()
    loader.session.get = MagicMock(side_effect=KeyError("bad response"))
    monkeypatch.setattr(
        coinbase_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )
    # fetch is deliberately fail-soft per symbol; a programming/shape error
    # must not trigger network retries and must not leak a partial frame.
    assert loader.fetch(["BTC-USD"], "2026-11-02", "2026-11-02") == {}
    assert loader.session.get.call_count == 1


def test_fetch_retries_transient_http_errors_with_bounded_helper(monkeypatch):
    loader = coinbase_loader.CoinbaseLoader()
    timestamp = int(pd.Timestamp("2026-11-02T00:00:00Z").timestamp())
    loader.session.get = MagicMock(side_effect=[
        requests.ConnectionError("temporary"),
        _response([[timestamp, "99", "111", "101", "108", "12.5"]]),
    ])
    monkeypatch.setattr(
        coinbase_loader,
        "cached_loader_fetch",
        lambda **kwargs: kwargs["fetch"](),
    )
    monkeypatch.setattr(coinbase_loader.time, "sleep", lambda *_a, **_k: None)
    result = loader.fetch(["BTC-USD"], "2026-11-02", "2026-11-02")
    assert "BTC-USD" in result
    assert loader.session.get.call_count == 2
