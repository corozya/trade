"""Support/Resistance levels (#233) — 1:1 port of TV/sr.pine ("Support and
Resistance (High Volume Boxes) [ChartPrime]", ChartPrime, MPL-2.0) from Pine
Script to Python. See PM analysis on ATS #233 (2026-08-08) for the full
element-by-element mapping — this module implements it without inventing a
new method: fractal pivots (fixed lookback, both sides), volume as an ENTRY
FILTER (not just a ranking weight), ATR-sized zone width, and forward state
tracking (breakout/hold/flip) per level.

## Why this is stateful, unlike rsi/atr/macd/stochastic (#228-#231)

Those indicators are a pure 1-value-per-bar transform. A S/R level is not: a
pivot confirmed at bar i creates a *zone* whose status (holding / broken /
flipped) keeps evolving on every later bar until the zone falls out of the
lookback window. This module returns one *event* per (level, bar-where-its-
status-changed) — see :func:`compute_level_events` — rather than one row per
input bar, matching the "S/R levels are a set of active levels with evolving
state, not a series" framing from the #233 brief. Each event is a complete,
immutable point-in-time snapshot of that level's state (id, zone bounds,
type, strength/volume, status, touch_count) — never mutated after being
produced, consistent with the lake's "immutable point-in-time records"
convention. The lake layer (services.crypto_indicator_backfill /
crypto_data_lake) republishing the same (symbol, timeframe, data_kind,
observed_at, available_at) key is a safe no-op, so re-running a full/-
incremental backfill over a level whose status changed again just adds a new
event row at the new observed_at — it does not rewrite history.

## Pine -> Python mapping

- ``ta.pivothigh(src, lookback, lookback)`` / ``ta.pivotlow`` -> a bar `i` is
  a confirmed pivot iff its high (low) is the strict max (min) over the
  `2*lookback+1`-bar window centered on it. Pine's pivot functions repaint
  forward by `lookback` bars (the confirmation needs `lookback` bars of
  *future* price) — modeled here by only ever adding a level once bar
  `i+lookback` exists, and to keep the lake honest, the level's own
  ``observed_at``/``available_at`` are set to that CONFIRMING bar
  (`i+lookback`), not the pivot bar itself. A caller reading the lake can
  therefore never see a level "from the future" relative to its own
  point-in-time cutoff — see ``_run_transform`` in
  services/crypto_indicator_backfill.py for the same convention on the
  indicator side.
- ``upAndDownVolume()`` -> :func:`_delta_volume`: `+volume` when
  `close > open`, `-volume` when `close < open`, and the LAST non-zero sign
  carried forward on a doji (`close == open`) — exact port of Pine's `var
  isBuyVolume` (a `switch` with no default branch leaves it unchanged).
- ``vol_hi = ta.highest(Vol/2.5, vol_len)`` / ``vol_lo = ta.lowest(...)`` ->
  :func:`_rolling_high`/:func:`_rolling_low` over the trailing `vol_len`
  bars (Pine's `ta.highest`/`ta.lowest` are inclusive of the current bar).
- Support requires ``Vol > vol_hi`` at the pivot-low bar; resistance requires
  ``Vol < vol_lo`` at the pivot-high bar — the volume FILTER from the PM
  analysis (levels that don't pass this never enter the output at all, they
  are not merely ranked lower).
- ``atr = ta.atr(200)``, ``withd = atr * box_width`` -> Wilder ATR(200) (see
  `indicators.atr`, box_width fixed at 1.0 — Pine's default and the only
  value the original indicator's author ships uncommented). This module
  reuses the ALREADY-BACKFILLED ``atr`` data_kind's *math* (imports
  `indicators.atr`, does not reimplement Wilder smoothing a third time) but
  computes locally at length=200, since the precomputed lake series is
  length=14 (a different lookback, used elsewhere for SL sizing).
- ``sup.set_right(bar_index+1)`` / adaptive box length -> not modeled as a
  drawing concern; the zone's right edge is implicitly "still open" until a
  status-changing event fires, exactly mirroring the state machine below.
- ``brekout_res``/``res_holds``/``sup_holds``/``brekout_sup`` (crossover/
  crossunder against the zone bounds) -> :func:`_level_state_events`,
  evaluated bar-by-bar for every bar after a level's confirmation, using the
  same low/high vs. zone-bound comparisons as Pine's ``ta.crossover``/
  ``ta.crossunder`` (previous bar on the far side of the threshold, current
  bar on the near/through side).
- ``res_is_sup``/``sup_is_res`` (flip tracking) -> ``status`` transitions to
  ``"flipped"`` on the SAME bar a breakout fires without an intervening hold
  — a level flips from resistance-broken to acting-as-support (or vice
  versa) exactly when Pine's ``brekout_res and res_is_sup[1]`` condition
  would light up on the NEXT bar; this module records the flip at the
  breakout bar itself (one canonical event per status change, no need for a
  second "confirmed one bar later" event since nothing about the zone's own
  bounds/type/volume changes between the two).

## Zone clustering (#233 "Do zrobienia" point 3 — no Pine analogue)

sr.pine draws every confirmed box independently (a human visually skims 50
of them on a chart). An API response should not return near-duplicate zones
a few ticks apart. :func:`cluster_levels` merges same-type zones whose price
ranges overlap (after ATR-width sizing) into one, keeping the higher-
strength (by `abs(volume)`) zone's id/bounds and summing touch counts —
applied as a final pass over already-computed level events, never inside the
Pine-ported detection logic itself.
"""
from __future__ import annotations

from typing import Any


def _delta_volume(rows: list[dict[str, Any]]) -> list[float]:
    """Port of Pine's ``upAndDownVolume()``. `isBuyVolume` is a Pine `var`
    updated only by a `switch` with branches for `close > open` and
    `close < open` (no default) — on a doji (`close == open`) the PREVIOUS
    bar's direction carries forward unchanged. Starts `True` (Pine `var bool
    isBuyVolume = true` default)."""
    out = [0.0] * len(rows)
    is_buy = True
    for i, row in enumerate(rows):
        close, open_ = row["close"], row["open"]
        if close > open_:
            is_buy = True
        elif close < open_:
            is_buy = False
        volume = row["volume"]
        out[i] = volume if is_buy else -volume
    return out


def _rolling_high(values: list[float], length: int) -> list[float]:
    out = [float("nan")] * len(values)
    for i in range(len(values)):
        window = values[max(0, i - length + 1) : i + 1]
        out[i] = max(window)
    return out


def _rolling_low(values: list[float], length: int) -> list[float]:
    out = [float("nan")] * len(values)
    for i in range(len(values)):
        window = values[max(0, i - length + 1) : i + 1]
        out[i] = min(window)
    return out


def _pivot_highs_lows(
    highs: list[float], lows: list[float], lookback: int
) -> tuple[list[bool], list[bool]]:
    """Bar `i` is a pivot high (low) iff its high (low) is the STRICT max
    (min) over `[i-lookback, i+lookback]`. Only bars with a full window on
    both sides can be pivots (matches Pine's ``ta.pivothigh``/``pivotlow``
    returning `na` until `lookback` bars after the pivot)."""
    n = len(highs)
    pivot_high = [False] * n
    pivot_low = [False] * n
    for i in range(lookback, n - lookback):
        window_h = highs[i - lookback : i + lookback + 1]
        if highs[i] == max(window_h) and window_h.count(highs[i]) == 1:
            pivot_high[i] = True
        window_l = lows[i - lookback : i + lookback + 1]
        if lows[i] == min(window_l) and window_l.count(lows[i]) == 1:
            pivot_low[i] = True
    return pivot_high, pivot_low


def compute_level_events(
    rows: list[dict[str, Any]],
    *,
    lookback: int = 20,
    vol_len: int = 2,
    box_width: float = 1.0,
    atr_length: int = 200,
) -> list[dict[str, Any]]:
    """Port of ``calcSupportResistance`` + the flip-tracking switches below
    it in sr.pine. `rows` are ohlcv rows (dicts with open/high/low/close/
    volume/observed_at/available_at), sorted ascending by `observed_at`.

    Returns one dict per level-state-change event (creation or a later
    breakout/hold/flip), each a full snapshot:
    ``{level_id, type, price_top, price_bottom, status, volume, touch_count,
    created_at, last_touched_at, event, observed_at, available_at}``.
    `observed_at`/`available_at` on a creation event are the CONFIRMING bar
    (pivot bar + lookback, see module docstring) — never the pivot bar
    itself, so as-of reads never see a level before it could actually have
    been known.
    """
    from indicators import atr as _atr_fn  # crypto-dashboard/backend/indicators.py

    n = len(rows)
    if n <= 2 * lookback:
        return []

    closes = [float(r["close"]) for r in rows]
    highs = [float(r["high"]) for r in rows]
    lows = [float(r["low"]) for r in rows]

    delta_vol = _delta_volume(rows)
    vol_scaled = [v / 2.5 for v in delta_vol]
    vol_hi = _rolling_high(vol_scaled, vol_len)
    vol_lo = _rolling_low(vol_scaled, vol_len)

    atr_values = _atr_fn(highs, lows, closes, length=atr_length)

    pivot_high, pivot_low = _pivot_highs_lows(highs, lows, lookback)

    events: list[dict[str, Any]] = []
    # Active levels, most-recently-created first is irrelevant — keyed by id.
    active: list[dict[str, Any]] = []
    level_seq = 0

    for i in range(n):
        confirm_row = rows[i]
        # A pivot at bar (i - lookback) is confirmed exactly at bar i.
        pivot_bar = i - lookback
        if pivot_bar >= 0:
            atr_at_pivot = atr_values[pivot_bar]
            width = (atr_at_pivot * box_width) if atr_at_pivot == atr_at_pivot else None  # NaN check
            if width is not None:
                if pivot_low[pivot_bar] and delta_vol[pivot_bar] > vol_hi[pivot_bar]:
                    level_seq += 1
                    price_bottom = lows[pivot_bar] - width
                    price_top = lows[pivot_bar]
                    level = {
                        "level_id": f"sup-{level_seq}",
                        "type": "support",
                        "price_top": price_top,
                        "price_bottom": price_bottom,
                        "status": "holding",
                        "volume": delta_vol[pivot_bar],
                        "touch_count": 0,
                        "created_at": confirm_row["observed_at"],
                        "last_touched_at": confirm_row["observed_at"],
                    }
                    active.append(level)
                    events.append(
                        _event(level, event="created", row=confirm_row)
                    )
                if pivot_high[pivot_bar] and delta_vol[pivot_bar] < vol_lo[pivot_bar]:
                    level_seq += 1
                    price_top = highs[pivot_bar] + width
                    price_bottom = highs[pivot_bar]
                    level = {
                        "level_id": f"res-{level_seq}",
                        "type": "resistance",
                        "price_top": price_top,
                        "price_bottom": price_bottom,
                        "status": "holding",
                        "volume": delta_vol[pivot_bar],
                        "touch_count": 0,
                        "created_at": confirm_row["observed_at"],
                        "last_touched_at": confirm_row["observed_at"],
                    }
                    active.append(level)
                    events.append(
                        _event(level, event="created", row=confirm_row)
                    )

        # State machine for every already-active level, evaluated on bar i
        # (skip the bar a level was just created on — Pine's crossover/
        # crossunder need a PREVIOUS bar to compare against, and the box
        # is only drawn starting the bar after creation).
        if i == 0:
            continue
        prev_high, prev_low = highs[i - 1], lows[i - 1]
        cur_high, cur_low = highs[i], lows[i]

        for level in active:
            if level["created_at"] == confirm_row["observed_at"]:
                continue  # just created this bar, nothing to evaluate yet
            changed = False
            if level["type"] == "support":
                # sup_holds = crossover(low, supportLevel) i.e. low crosses
                # up through price_top (the pivot price, upper edge of the
                # support zone).
                sup_holds = prev_low <= level["price_top"] < cur_low
                # brekout_sup = crossunder(high, supportLevel_1) i.e. high
                # crosses down through price_bottom.
                brekout_sup = prev_high >= level["price_bottom"] > cur_high
                if brekout_sup:
                    was_flip_candidate = level["status"] == "broken"
                    level["status"] = "flipped" if was_flip_candidate else "broken"
                    level["last_touched_at"] = confirm_row["observed_at"]
                    level["touch_count"] += 1
                    changed = True
                elif sup_holds:
                    if level["status"] != "holding":
                        level["status"] = "holding"
                    level["last_touched_at"] = confirm_row["observed_at"]
                    level["touch_count"] += 1
                    changed = True
            else:  # resistance
                # brekout_res = crossover(low, resistanceLevel_1) i.e. low
                # crosses up through price_top (upper edge).
                brekout_res = prev_low <= level["price_top"] < cur_low
                # res_holds = crossunder(high, resistanceLevel) i.e. high
                # crosses down through price_bottom (the pivot price).
                res_holds = prev_high >= level["price_bottom"] > cur_high
                if brekout_res:
                    was_flip_candidate = level["status"] == "broken"
                    level["status"] = "flipped" if was_flip_candidate else "broken"
                    level["last_touched_at"] = confirm_row["observed_at"]
                    level["touch_count"] += 1
                    changed = True
                elif res_holds:
                    if level["status"] != "holding":
                        level["status"] = "holding"
                    level["last_touched_at"] = confirm_row["observed_at"]
                    level["touch_count"] += 1
                    changed = True
            if changed:
                events.append(_event(level, event=level["status"], row=confirm_row))

    return events


def _event(level: dict[str, Any], *, event: str, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "level_id": level["level_id"],
        "type": level["type"],
        "price_top": level["price_top"],
        "price_bottom": level["price_bottom"],
        "status": level["status"],
        "volume": level["volume"],
        "touch_count": level["touch_count"],
        "created_at": level["created_at"],
        "last_touched_at": level["last_touched_at"],
        "event": event,
        "observed_at": row["observed_at"],
        "available_at": row["available_at"],
    }


def latest_level_states(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse an event stream (as returned by ``compute_level_events``, or
    read back from the lake) down to one row per `level_id` — its most
    recent state as of the last event present in `events`. Callers doing an
    as-of read should filter `events` to `available_at <= cutoff` BEFORE
    calling this, same convention as CryptoDataLake.read_as_of_duckdb."""
    latest: dict[str, dict[str, Any]] = {}
    for ev in sorted(events, key=lambda e: (e["observed_at"], e["level_id"])):
        latest[ev["level_id"]] = ev
    return list(latest.values())


def cluster_levels(
    levels: list[dict[str, Any]], *, overlap_tolerance: float = 0.0
) -> list[dict[str, Any]]:
    """Merge same-type levels whose [price_bottom, price_top] zones overlap
    (#233 "Do zrobienia" point 3 — Pine draws every box independently, an API
    consumer should not see near-duplicate zones a few ticks apart). Zones
    are merged transitively (a chain of overlaps collapses to one cluster);
    the surviving row keeps the HIGHEST-strength (`abs(volume)`) member's
    id/bounds/status, its `touch_count` is the SUM across the cluster (more
    retests = stronger, matching #233 point 4), and `volume` is the max
    abs(volume) member's own signed volume (keeps a meaningful sign instead
    of averaging supports/resistances together, which cannot happen anyway
    since clustering is same-type-only)."""
    by_type: dict[str, list[dict[str, Any]]] = {}
    for lvl in levels:
        by_type.setdefault(lvl["type"], []).append(lvl)

    out: list[dict[str, Any]] = []
    for _type, group in by_type.items():
        group = sorted(group, key=lambda l: l["price_bottom"])
        clusters: list[list[dict[str, Any]]] = []
        cluster_max_top: list[float] = []
        for lvl in group:
            # Compare against the RUNNING MAX price_top of the current
            # cluster (not just the most-recently-appended member's own
            # price_top) — a cluster's merged span can extend past any
            # single member once earlier zones have already widened it, so
            # comparing only against the last-appended zone would miss
            # transitive overlaps (zone C overlapping the cluster's earlier,
            # wider zone A even though C doesn't overlap the narrower zone B
            # appended just before it).
            if clusters and lvl["price_bottom"] <= cluster_max_top[-1] + overlap_tolerance:
                clusters[-1].append(lvl)
                cluster_max_top[-1] = max(cluster_max_top[-1], lvl["price_top"])
            else:
                clusters.append([lvl])
                cluster_max_top.append(lvl["price_top"])
        for cluster in clusters:
            best = max(cluster, key=lambda l: abs(l["volume"]))
            merged = dict(best)
            merged["touch_count"] = sum(l["touch_count"] for l in cluster)
            merged["price_top"] = max(l["price_top"] for l in cluster)
            merged["price_bottom"] = min(l["price_bottom"] for l in cluster)
            merged["merged_level_ids"] = sorted(l["level_id"] for l in cluster)
            out.append(merged)
    return out


def price_position_summary(
    zones: list[dict[str, Any]],
    reference_price: float,
    *,
    near: int = 3,
) -> dict[str, Any]:
    """Locate `reference_price` against an ALREADY-CLUSTERED, price_top-DESC
    sorted `zones` list (exactly the list /api/support_resistance returns) and
    return the decision-ready extras: where price sits relative to the zone
    map, and the `near` closest zones on each side with pre-computed distances.

    `zones` must be the SAME list object the endpoint returns, in the SAME
    order (sorted by ``price_top`` descending) — every reference below is a
    plain integer index into it (``zone_index``), never a duplicated zone
    object, so this summary stays tiny regardless of how many zones exist.

    Returned shape::

        {
          "price_position": {"status": <str>, "zone_index": <int|None>},
          "nearest_resistances": [
            {"zone_index": int, "distance_abs": float, "distance_pct": float}, ...
          ],
          "nearest_supports": [
            {"zone_index": int, "distance_abs": float, "distance_pct": float}, ...
          ],
        }

    `price_position.status` is one of:
      * ``inside_support_zone`` / ``inside_resistance_zone`` — price is WITHIN
        a zone's [price_bottom, price_top] band (actively testing it);
        ``zone_index`` points at that zone. If price sits inside more than one
        overlapping zone the tightest-band one wins (clustering makes this
        rare, but keep it deterministic).
      * ``above_all_zones`` / ``below_all_zones`` — price is beyond every known
        zone on that side; the corresponding ``nearest_*`` list on the far side
        is empty (NOT an error, see #233 follow-up brief).
      * ``between_zones`` — price sits in a gap between zones with zones on both
        sides; ``zone_index`` is None.

    ``nearest_resistances`` are zones strictly ABOVE price (a zone's whole band
    is above price, or price is below the zone), sorted nearest-first, capped at
    `near`. ``nearest_supports`` are zones strictly BELOW price, same rule.
    A zone price is currently INSIDE is reported via ``price_position`` and is
    excluded from both nearest lists (it is neither above nor below).

    ``distance_abs`` is ``edge - reference_price`` where `edge` is the NEAR edge
    of the zone (a resistance's ``price_bottom``, a support's ``price_top``) —
    positive for resistances (above), negative for supports (below), matching
    the follow-up brief's sign convention. ``distance_pct`` is that same signed
    gap as a percentage of ``reference_price`` (``distance_abs / price * 100``),
    rounded to 4 dp.
    """
    inside: list[tuple[int, dict[str, Any]]] = []
    for idx, z in enumerate(zones):
        if z["price_bottom"] <= reference_price <= z["price_top"]:
            inside.append((idx, z))

    inside_index: int | None = None
    inside_type: str | None = None
    if inside:
        # tightest band wins — most specific level price is actually testing
        idx, z = min(inside, key=lambda pair: pair[1]["price_top"] - pair[1]["price_bottom"])
        inside_index = idx
        inside_type = z["type"]

    def _dist(edge: float) -> tuple[float, float]:
        abs_d = round(edge - reference_price, 8)
        pct = round((edge - reference_price) / reference_price * 100, 4) if reference_price else 0.0
        return abs_d, pct

    resistances: list[dict[str, Any]] = []  # zones above price (near edge = price_bottom)
    supports: list[dict[str, Any]] = []     # zones below price (near edge = price_top)
    for idx, z in enumerate(zones):
        if idx == inside_index:
            continue
        if z["price_bottom"] > reference_price:
            abs_d, pct = _dist(z["price_bottom"])
            resistances.append({"zone_index": idx, "distance_abs": abs_d, "distance_pct": pct})
        elif z["price_top"] < reference_price:
            abs_d, pct = _dist(z["price_top"])
            supports.append({"zone_index": idx, "distance_abs": abs_d, "distance_pct": pct})

    resistances.sort(key=lambda r: r["distance_abs"])            # smallest positive gap first
    supports.sort(key=lambda s: s["distance_abs"], reverse=True)  # smallest magnitude (closest to 0) first
    nearest_resistances = resistances[:near]
    nearest_supports = supports[:near]

    if inside_index is not None:
        status = "inside_resistance_zone" if inside_type == "resistance" else "inside_support_zone"
        position_index: int | None = inside_index
    elif not supports and resistances:
        status = "below_all_zones"
        position_index = None
    elif not resistances and supports:
        status = "above_all_zones"
        position_index = None
    elif not supports and not resistances:
        # no zones at all on either side (empty map) — treat as between_zones
        # with no anchor rather than inventing an above/below claim.
        status = "between_zones"
        position_index = None
    else:
        status = "between_zones"
        position_index = None

    return {
        "price_position": {"status": status, "zone_index": position_index},
        "nearest_resistances": nearest_resistances,
        "nearest_supports": nearest_supports,
    }


__all__ = [
    "compute_level_events",
    "latest_level_states",
    "cluster_levels",
    "price_position_summary",
]
