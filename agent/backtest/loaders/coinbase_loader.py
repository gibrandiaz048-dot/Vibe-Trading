"""Public Coinbase Exchange OHLCV loader (no API credentials required)."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

import pandas as pd
import requests

from backtest.loaders.base import (
    cached_loader_fetch,
    check_budget,
    positive_env_float,
    positive_env_int,
    retry_with_budget,
    validate_date_range,
)
from backtest.loaders.registry import register

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.exchange.coinbase.com"
_TIMEOUT = positive_env_int("COINBASE_TIMEOUT_MS", 12_000) / 1000
_BUDGET = positive_env_float("COINBASE_FETCH_BUDGET_S", 45.0)
_INTERVALS = {
    "1m": 60, "1M": 60,
    "5m": 300, "5M": 300,
    "15m": 900, "15M": 900,
    "1h": 3600, "1H": 3600,
    "6h": 21600, "6H": 21600,
    "1d": 86400, "1D": 86400,
}


@register
class CoinbaseLoader:
    """Fetch public spot candles from Coinbase Exchange."""

    name = "coinbase"
    markets = {"crypto"}
    requires_auth = False

    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "Vibe-Trading/coinbase-loader"})

    def is_available(self) -> bool:
        try:
            response = self.session.get(
                f"{_BASE_URL}/products/BTC-USD/ticker", timeout=_TIMEOUT
            )
            return response.status_code == 200
        except requests.RequestException:
            return False

    def fetch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        *,
        interval: str = "1D",
        fields: Optional[List[str]] = None,
    ) -> Dict[str, pd.DataFrame]:
        validate_date_range(start_date, end_date)
        granularity = _INTERVALS.get(interval)
        if granularity is None:
            raise ValueError(
                f"Coinbase does not support interval {interval!r}; "
                f"supported: {', '.join(_INTERVALS)}"
            )

        start = pd.Timestamp(start_date, tz="UTC")
        end = pd.Timestamp(end_date, tz="UTC") + pd.Timedelta(days=1)
        result: Dict[str, pd.DataFrame] = {}

        for code in codes:
            product = str(code).strip().upper().replace("/", "-")
            if not product or "-" not in product:
                logger.warning("Coinbase skipped unsupported symbol %r", code)
                continue

            def fetch_frame(product_id: str = product) -> pd.DataFrame:
                rows: list[list[float]] = []
                cursor = start
                deadline = time.monotonic() + _BUDGET
                label = f"Coinbase candles for {product_id}"
                window = pd.Timedelta(seconds=granularity * 300)

                while cursor < end:
                    check_budget(deadline, label, budget_s=_BUDGET)
                    window_end = min(cursor + window, end)

                    def request_page():
                        response = self.session.get(
                            f"{_BASE_URL}/products/{product_id}/candles",
                            params={
                                "granularity": granularity,
                                "start": cursor.isoformat(),
                                "end": window_end.isoformat(),
                            },
                            timeout=_TIMEOUT,
                        )
                        response.raise_for_status()
                        payload = response.json()
                        if not isinstance(payload, list):
                            raise ValueError(
                                f"Unexpected Coinbase candle payload for {product_id}"
                            )
                        return payload

                    page = retry_with_budget(
                        request_page,
                        transient=requests.RequestException,
                        deadline=deadline,
                        label=label,
                    )
                    rows.extend(page)
                    cursor = window_end

                if not rows:
                    return pd.DataFrame(
                        columns=["open", "high", "low", "close", "volume"],
                        index=pd.DatetimeIndex([], name="trade_date"),
                    )

                # Coinbase order: [time, low, high, open, close, volume].
                frame = pd.DataFrame(
                    rows,
                    columns=["timestamp", "low", "high", "open", "close", "volume"],
                )
                frame["trade_date"] = pd.to_datetime(
                    frame["timestamp"], unit="s", utc=True
                ).dt.tz_localize(None)
                frame = frame.set_index("trade_date").sort_index()
                frame = frame[~frame.index.duplicated(keep="last")]
                for column in ("open", "high", "low", "close", "volume"):
                    frame[column] = pd.to_numeric(frame[column], errors="coerce")
                frame = frame[["open", "high", "low", "close", "volume"]].dropna(
                    subset=["open", "high", "low", "close"]
                )
                # Preserve quote provenance; never silently relabel USD as USDT.
                frame.attrs["quote_currency"] = product_id.rsplit("-", 1)[-1]
                return frame

            try:
                frame = cached_loader_fetch(
                    source=self.name,
                    symbol=code,
                    timeframe=interval,
                    start_date=start_date,
                    end_date=end_date,
                    fields=fields,
                    fetch=fetch_frame,
                )
                if frame is not None and not frame.empty:
                    result[code] = frame
            except requests.HTTPError as exc:
                logger.warning("Coinbase has no usable product %s: %s", product, exc)
            except Exception as exc:
                logger.warning("Coinbase failed for %s: %s", product, exc)

        return result
