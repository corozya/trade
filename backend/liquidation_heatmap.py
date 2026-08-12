"""Estimated Liquidation Heatmap (#195) — free alternative to a paid
Coinglass-style subscription, built entirely from data OkxClient already
exposes (open interest, funding) plus a new #195 addition (liquidation-orders,
position-tiers).

## This is ALWAYS an estimation, never real trader positions

No exchange publicly discloses other traders' actual entry prices, leverage,
or position sizes — not even Coinglass, whose own "liquidation heatmap" is
a model, not ground truth (see #192 research). This module is a cruder
version of the same kind of model: it does NOT read anyone's real position,
it infers *plausible* liquidation price clusters from public open-interest
behavior. Treat the output as one more input signal, not a fact.

## Algorithm (user decision, PM analysis on #195, 2026-08-09 — locked, do not
## redesign without a new conversation with the user)

1. Take an OI history series (`[{observed_at, open_interest}]`, from
   `OkxClient.get_open_interest_history` via crypto_backfill_open_interest.py's
   same endpoint) and find LOCAL EXTREMES (spikes) in open interest — points
   where OI jumps up or down relative to its recent neighborhood. A spike
   signals a burst of new position-opening (or mass closing), i.e. a cluster
   of traders likely entered around the corresponding price.
2. For each OI extreme, look up the candle CLOSE price at that timestamp
   (nearest OHLCV row) — this stands in for the "entry price" traders around
   that spike opened at (a simplification: real entries are spread across
   the whole spike, not a single instant, but the instant of the extreme is
   this model's proxy for it).
3. For 3 FIXED leverage tiers — 10x / 20x / 50x (user-decided fixed constants,
   see module docstring below for why these do NOT literally match OKX's own
   `position-tiers` schedule) — compute the estimated liquidation price for
   BOTH a long and a short opened at that entry price and that leverage,
   using the isolated-margin approximation:
       liq_price_long  = entry * (1 - 1/leverage + mmr)
       liq_price_short = entry * (1 + 1/leverage - mmr)
   `mmr` (maintenance margin ratio) is taken from position-tiers tier 1
   (smallest notional bracket, ~0.4% on BTC-USDT-SWAP as of 2026-08-09) as a
   conservative approximation — real mmr grows with position notional (see
   get_position_tiers docstring), but per-trader notional is unknowable here.
4. INTENSITY of a price zone = sum of the OI magnitude (`open_interest` at
   the spike, i.e. how big that burst of new positions was) of every
   (extreme, leverage-tier, side) combination whose estimated liquidation
   price falls into that zone. Zones are price buckets (see `_bucket_price`)
   so nearby liquidation price estimates from different spikes/tiers
   reinforce each other instead of each being its own isolated point.

## On leverage tiers: 10x/20x/50x vs OKX's real `position-tiers`

Verified empirically 2026-08-09 (BTC-USDT-SWAP, cross, via
`OkxClient.get_position_tiers`): OKX has NO fixed leverage menu. `maxLever`
degrades continuously from 100x (tier 1, notional 0-1,000 USDT) down to ~2x
at the largest notional brackets (>1.9M USDT), across ~99 tiers. So 10x/20x/
50x are not "OKX's tiers" in the API's own sense — they are simplified,
commonly-used-by-retail leverage CHOICES a trader can still pick manually up
to whatever `maxLever` their position size allows. This was flagged back to
the user's PM per task #195 point 4; the fixed-tier algorithm itself was
still the explicit, already-locked user decision (not renegotiated here).

## On-demand only (task #195 AC)

Computed fresh per request from already-fetched OI history + one fresh
`get_liquidation_orders` call (folded into the zones as a confirmation
signal, see `_realized_liquidation_weight`) — no CryptoDataLake writes, no
new crypto_backfill_cli.py data-kind, same style as risk_indicator.py's
`find_divergences` / candlestick_patterns.py's `detect_patterns`.
"""
from __future__ import annotations

from typing import Any

LEVERAGE_TIERS: tuple[float, ...] = (10.0, 20.0, 50.0)
"""Fixed leverage tiers (user decision, #195 PM analysis) — see module
docstring "On leverage tiers" section for why these are a simplification,
not OKX's own `position-tiers` schedule."""

DEFAULT_MMR = 0.004
"""Maintenance margin ratio approximation — OKX position-tiers tier 1 value
for BTC-USDT-SWAP cross (verified 2026-08-09). Real mmr grows with position
notional; unknowable per-trader here, so the smallest/most-common-for-retail
tier is used as a conservative floor."""


def _local_oi_extremes(
    oi_rows: list[dict[str, Any]],
    *,
    window: int = 3,
    min_relative_change: float = 0.02,
) -> list[dict[str, Any]]:
    """Points where `open_interest` is a local max or min within +/-`window`
    neighbors AND differs from the window's mean by at least
    `min_relative_change` (2% default) — filters out noise-level wiggles so
    only genuine OI bursts count as a "spike" (task's "skoki wolumenu
    otwartych pozycji"). Rows must already be sorted ascending by
    `observed_at` (same convention as `_read_series`)."""
    n = len(oi_rows)
    extremes: list[dict[str, Any]] = []
    for i in range(window, n - window):
        neighborhood = oi_rows[i - window : i + window + 1]
        values = [r["open_interest"] for r in neighborhood]
        current = oi_rows[i]["open_interest"]
        mean = sum(values) / len(values)
        if mean == 0:
            continue
        is_max = current == max(values)
        is_min = current == min(values)
        if not (is_max or is_min):
            continue
        relative_change = abs(current - mean) / mean
        if relative_change < min_relative_change:
            continue
        extremes.append({**oi_rows[i], "relative_change": relative_change})
    return extremes


def _nearest_close_price(ohlcv_rows: list[dict[str, Any]], observed_at: str) -> float | None:
    """Closest OHLCV row (by `observed_at` string compare, ISO8601 timestamps
    sort lexicographically) to `observed_at` — proxy "entry price" for an OI
    extreme, see module docstring step 2."""
    if not ohlcv_rows:
        return None
    best_row = min(ohlcv_rows, key=lambda r: abs(_iso_to_sort_key(r["observed_at"]) - _iso_to_sort_key(observed_at)))
    return best_row["close"]


def _iso_to_sort_key(iso_ts: str) -> float:
    """Cheap monotonic sort key from an ISO8601 timestamp without pulling in
    datetime parsing edge cases — lexicographic ISO8601 strings already sort
    correctly, but subtraction below needs numeric distance, so this hashes
    to ordinal-ish position via char codes on the numeric portion only."""
    from datetime import datetime

    return datetime.fromisoformat(iso_ts.replace("Z", "+00:00")).timestamp()


def _liq_price(entry: float, leverage: float, *, side: str, mmr: float = DEFAULT_MMR) -> float:
    """Isolated-margin liquidation price approximation (fees/funding
    ignored — this is an estimate, see module docstring)."""
    if side == "long":
        return entry * (1.0 - 1.0 / leverage + mmr)
    return entry * (1.0 + 1.0 / leverage - mmr)


def _bucket_price(price: float, *, bucket_pct: float) -> float:
    """Round `price` to the nearest `bucket_pct` fraction of itself, so
    nearby liquidation-price estimates land in the same zone instead of each
    being its own isolated point (module docstring step 4)."""
    if price <= 0:
        return price
    bucket_size = price * bucket_pct
    if bucket_size <= 0:
        return price
    return round(price / bucket_size) * bucket_size


def estimate_liquidation_zones(
    oi_rows: list[dict[str, Any]],
    ohlcv_rows: list[dict[str, Any]],
    *,
    leverage_tiers: tuple[float, ...] = LEVERAGE_TIERS,
    mmr: float = DEFAULT_MMR,
    extreme_window: int = 3,
    min_relative_change: float = 0.02,
    bucket_pct: float = 0.0025,
) -> list[dict[str, Any]]:
    """Estimated liquidation price zones from OI history + OHLCV closes.

    `oi_rows`: `[{observed_at, open_interest}]` ascending by time (as read
    from the `open_interest` CryptoDataLake data_kind).
    `ohlcv_rows`: `[{observed_at, close}]` ascending by time, same symbol/
    timeframe range as `oi_rows` (used only to look up the price at each OI
    extreme's timestamp).

    Returns `[{price, intensity, side, leverage, extreme_count}]` sorted by
    `price` ascending — one row per (bucketed price, side) combination that
    received at least one contribution, `intensity` = sum of OI magnitude
    (see module docstring step 4), `leverage` = the dominant (largest single
    contribution) leverage tier for that bucket (informational only —
    buckets from different tiers/extremes can and do merge).
    """
    extremes = _local_oi_extremes(oi_rows, window=extreme_window, min_relative_change=min_relative_change)

    # zone_key -> accumulator
    zones: dict[tuple[float, str], dict[str, Any]] = {}
    for extreme in extremes:
        entry = _nearest_close_price(ohlcv_rows, extreme["observed_at"])
        if entry is None or entry <= 0:
            continue
        oi_weight = extreme["open_interest"]
        for leverage in leverage_tiers:
            for side in ("long", "short"):
                liq_price = _liq_price(entry, leverage, side=side, mmr=mmr)
                bucket = _bucket_price(liq_price, bucket_pct=bucket_pct)
                key = (bucket, side)
                acc = zones.setdefault(
                    key,
                    {"price": bucket, "side": side, "intensity": 0.0, "extreme_count": 0, "_max_contribution": 0.0, "leverage": leverage},
                )
                acc["intensity"] += oi_weight
                acc["extreme_count"] += 1
                if oi_weight > acc["_max_contribution"]:
                    acc["_max_contribution"] = oi_weight
                    acc["leverage"] = leverage

    result = [
        {
            "price": zone["price"],
            "side": zone["side"],
            "intensity": zone["intensity"],
            "extreme_count": zone["extreme_count"],
            "leverage": zone["leverage"],
        }
        for zone in zones.values()
    ]
    result.sort(key=lambda z: z["price"])
    return result


def realized_liquidation_weight(liquidation_orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reshapes `GET /api/v5/public/liquidation-orders` raw payload rows
    (`OkxClient.get_liquidation_orders`) into the same `{price, side,
    intensity}` shape as `estimate_liquidation_zones`, so a caller can merge
    real recently-realized liquidations alongside the estimated zones as a
    confirmation signal (task #195 scope point 1 — the new OkxClient method
    exists to feed this).

    OKX's liquidation-orders `details` entries carry `bkPx` (bankruptcy
    price) and `sz`/`side` (posSide semantics: "long"/"short" for the
    liquidated position, not the order's buy/sell side) — `posSide` used
    here as the zone side, `sz` as the intensity weight (contract count, a
    coarser unit than OI's quote-currency value but the only size OKX
    exposes on this endpoint)."""
    out: list[dict[str, Any]] = []
    for order in liquidation_orders:
        details = order.get("details", [])
        for detail in details:
            try:
                price = float(detail.get("bkPx", 0) or 0)
                size = float(detail.get("sz", 0) or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0 or size <= 0:
                continue
            side = detail.get("posSide") or detail.get("side") or "unknown"
            out.append({"price": price, "side": side, "intensity": size})
    out.sort(key=lambda z: z["price"])
    return out
