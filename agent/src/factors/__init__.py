"""Alpha Zoo package exports, loaded lazily.

Importing a CLI submodule such as src.factors.cli_handlers must not load the
numerical factor engine and its heavy pandas/scipy dependencies.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

_EXPORTS = (
    "Alpha", "AlphaCompute", "Market", "decay_linear", "delta", "rank",
    "safe_div", "scale", "signed_power", "ts_argmax", "ts_argmin",
    "ts_corr", "ts_cov", "ts_max", "ts_mean", "ts_min", "ts_rank",
    "ts_std", "vwap",
)

__all__ = list(_EXPORTS)


def __getattr__(name: str) -> Any:
    """Load numerical exports only when a caller actually requests them."""
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    base = import_module("src.factors.base")
    value = getattr(base, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


if TYPE_CHECKING:
    from src.factors.base import (
        Alpha, AlphaCompute, Market, decay_linear, delta, rank, safe_div,
        scale, signed_power, ts_argmax, ts_argmin, ts_corr, ts_cov, ts_max,
        ts_mean, ts_min, ts_rank, ts_std, vwap,
    )
