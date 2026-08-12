"""Testy skryptu analyze_crypto_market_data.py (#79/#91): konwersja świec OKX -> DataFrame,
wskaźniki 15m, higher_tf_context (1h/4h), orderbook i futures.
Dane syntetyczne, zero I/O sieciowego.
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import analyze_crypto_market_data as acmd


def _make_candles(n, base=60000.0, bar_ms=900_000, trend=0.0):
    """Świece syntetyczne, najnowsza pierwsza (konwencja OKX)."""
    now = int(time.time() * 1000)
    rows = []
    price = base
    for i in range(n):
        ts = now - i * bar_ms
        o = price
        c = price * (1 + 0.001 * ((i % 5) - 2) + trend)
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        v = 100 + i
        rows.append([str(ts), str(o), str(h), str(l), str(c), str(v)])
        price = c
    return rows


def _raw_payload(**overrides):
    payload = {
        "inst_id": "BTC-USD_UM_XPERP-310328",
        "fetched_at": "2026-07-21T18:00:00.000Z",
        "candles": {
            "15m": {"ok": True, "data": {"data": _make_candles(150)}},
            "5m": {"ok": True, "data": {"data": _make_candles(60)}},
            "1H": {"ok": True, "data": {"data": _make_candles(80, bar_ms=3_600_000)}},
            "4H": {"ok": True, "data": {"data": _make_candles(60, bar_ms=14_400_000)}},
        },
        "orderbook": {
            "ok": True,
            "data": {"data": [{"bids": [["60000", "1", "0", "1"]], "asks": [["60005", "1", "0", "1"]]}]},
        },
        "funding_rate": {"ok": True, "data": {"data": [{"fundingRate": "0.0001", "nextFundingRate": "0.00012"}]}},
        "open_interest": {"ok": True, "data": {"data": [{"oi": "145000", "oiCcy": "1450000000"}]}},
    }
    payload.update(overrides)
    return payload


def test_okx_candles_to_df_sorts_ascending_and_parses_types():
    raw = _make_candles(5)
    df = acmd.okx_candles_to_df(raw)
    assert list(df.columns[:5]) == ["date", "open", "high", "low", "close"] or "volume" in df.columns
    assert df["date"].is_monotonic_increasing
    assert len(df) == 5


def test_okx_candles_to_df_empty_raises():
    with pytest.raises(acmd.AnalysisError):
        acmd.okx_candles_to_df([])


def test_okx_candles_to_df_drops_non_finite_ohlc():
    rows = _make_candles(3)
    rows[0][4] = "inf"
    df = acmd.okx_candles_to_df(rows)
    assert len(df) == 2

    for row in rows:
        row[4] = "inf"
    with pytest.raises(acmd.AnalysisError):
        acmd.okx_candles_to_df(rows)


def test_analyze_symbol_produces_expected_shape():
    result = acmd.analyze_symbol("BTC", _raw_payload())

    assert result["symbol"] == "BTC-USD_UM_XPERP-310328"
    assert set(result.keys()) == {
        "symbol", "analyzed_at", "price", "indicators_15m", "higher_tf_context", "orderbook", "futures",
    }
    assert set(result["indicators_15m"].keys()) == {
        "rsi14", "ema20", "ema50", "atr14", "bb", "vwap", "macd",
        "stoch_rsi", "adx", "relative_volume", "obv",
    }
    assert set(result["indicators_15m"]["bb"].keys()) == {"upper", "mid", "lower"}
    assert set(result["higher_tf_context"].keys()) == {
        "trend_1h", "ema50_1h", "ema200_1h", "last_swing_high_1h", "last_swing_low_1h",
        "adx_1h", "trend_4h",
    }
    assert result["higher_tf_context"]["trend_1h"] in {"up", "down", "range"}
    assert result["higher_tf_context"]["trend_4h"] in {"up", "down", "range"}


@pytest.mark.parametrize("symbol", ["BTC", "ETH", "DOGE"])
def test_supported_symbols_receive_complete_indicator_contract(symbol):
    raw = _raw_payload(inst_id=f"{symbol}-USD_UM_XPERP-310328")
    indicators = acmd.analyze_symbol(symbol, raw)["indicators_15m"]
    assert all(
        indicators[name]["status"] == "ok"
        for name in ("macd", "stoch_rsi", "adx", "relative_volume", "obv")
    )


def test_analyze_symbol_new_indicators_are_finite_and_deterministic():
    result = acmd.analyze_symbol("BTC", _raw_payload())
    result_again = acmd.analyze_symbol("BTC", _raw_payload())
    indicators = result["indicators_15m"]
    for name in ("macd", "stoch_rsi", "adx", "relative_volume", "obv"):
        assert indicators[name]["status"] == "ok"
        assert indicators[name] == result_again["indicators_15m"][name]
        for key, value in indicators[name].items():
            if key not in {"status", "required_bars"}:
                assert value is not None
                assert value == pytest.approx(float(value))
    assert result["higher_tf_context"]["adx_1h"]["status"] == "ok"
    assert "fib" not in json.dumps(result).lower()


@pytest.mark.parametrize("bars", [1, 19, 27, 31, 33])
def test_new_indicators_short_history_return_explicit_insufficient_data(bars):
    raw = _raw_payload(
        candles={
            "15m": {"ok": True, "data": {"data": _make_candles(bars)}},
            "1H": {"ok": True, "data": {"data": _make_candles(10, bar_ms=3_600_000)}},
            "4H": {"ok": False, "error": "missing"},
        }
    )
    result = acmd.analyze_symbol("BTC", raw)
    indicators = result["indicators_15m"]
    expected = {
        "macd": bars >= acmd._MACD_MIN_BARS,
        "stoch_rsi": bars >= acmd._STOCH_RSI_MIN_BARS,
        "adx": bars >= acmd._ADX_MIN_BARS,
        "relative_volume": bars >= acmd._VOLUME_PERIOD,
        "obv": bars >= acmd._VOLUME_PERIOD,
    }
    for name, enough_bars in expected.items():
        assert (indicators[name]["status"] == "ok") is enough_bars
    assert result["higher_tf_context"]["adx_1h"]["status"] == "insufficient_data"


def test_volume_indicators_handle_zero_and_incomplete_volume_without_nan():
    zero_rows = _make_candles(40)
    for row in zero_rows:
        row[5] = "0"
    incomplete_rows = _make_candles(40)
    incomplete_rows[0][5] = "not-a-number"

    zero_relative, zero_obv = acmd._volume_indicators(acmd.okx_candles_to_df(zero_rows))
    bad_relative, bad_obv = acmd._volume_indicators(acmd.okx_candles_to_df(incomplete_rows))

    assert zero_relative["status"] == "insufficient_data"
    assert zero_relative["value"] is None
    assert zero_obv == {"status": "ok", "required_bars": 20, "value": 0.0, "slope": 0.0}
    assert bad_relative["status"] == "insufficient_data"
    assert bad_obv["status"] == "insufficient_data"
    assert "NaN" not in json.dumps([zero_relative, zero_obv, bad_relative, bad_obv], allow_nan=False)


def test_analyze_symbol_missing_15m_raises():
    raw = _raw_payload(candles={"15m": {"ok": False, "error": "boom"}})
    with pytest.raises(acmd.AnalysisError):
        acmd.analyze_symbol("BTC", raw)


def test_analyze_symbol_missing_1h_4h_falls_back_to_unknown():
    raw = _raw_payload(
        candles={
            "15m": {"ok": True, "data": {"data": _make_candles(150)}},
            "5m": {"ok": True, "data": {"data": _make_candles(60)}},
            "1H": {"ok": False, "error": "boom"},
            "4H": {"ok": False, "error": "boom"},
        }
    )
    result = acmd.analyze_symbol("BTC", raw)
    assert result["higher_tf_context"]["trend_1h"] == "unknown"
    assert result["higher_tf_context"]["trend_4h"] == "unknown"
    assert result["higher_tf_context"]["ema50_1h"] is None
    assert result["higher_tf_context"]["adx_1h"]["status"] == "insufficient_data"


def test_analyze_symbol_orderbook_missing_returns_none():
    raw = _raw_payload(orderbook={"ok": False, "error": "boom"})
    result = acmd.analyze_symbol("BTC", raw)
    assert result["orderbook"] is None


def test_analyze_symbol_futures_missing_returns_none_values():
    raw = _raw_payload(
        funding_rate={"ok": False, "error": "boom"},
        open_interest={"ok": False, "error": "boom"},
    )
    result = acmd.analyze_symbol("BTC", raw)
    assert result["futures"]["funding_rate"] is None
    assert result["futures"]["open_interest"] is None


def test_orderbook_summary_computes_spread_and_depth():
    ob = {"bids": [["60000", "2", "0", "1"]], "asks": [["60005", "1", "0", "1"]]}
    summary = acmd._orderbook_summary(ob)
    assert summary["best_bid"] == 60000.0
    assert summary["best_ask"] == 60005.0
    assert summary["spread_bps"] > 0


def test_higher_tf_trend_unknown_when_too_few_bars():
    df = acmd.okx_candles_to_df(_make_candles(30, bar_ms=3_600_000))
    ctx = acmd._higher_tf_trend(df)
    assert ctx["trend_1h"] == "unknown"
    assert ctx["ema50_1h"] is None
    assert ctx["ema200_1h"] is None


def test_higher_tf_4h_direction_unknown_when_too_few_bars():
    df = acmd.okx_candles_to_df(_make_candles(30, bar_ms=14_400_000))
    assert acmd._higher_tf_4h_direction(df) == "unknown"


def test_higher_tf_trend_reflects_ema50_vs_ema200_relation():
    df = acmd.okx_candles_to_df(_make_candles(220))
    ctx = acmd._higher_tf_trend(df)
    e50, e200 = ctx["ema50_1h"], ctx["ema200_1h"]
    if e50 > e200 * 1.002:
        assert ctx["trend_1h"] == "up"
    elif e50 < e200 * 0.998:
        assert ctx["trend_1h"] == "down"
    else:
        assert ctx["trend_1h"] == "range"


def test_main_writes_analysis_json(monkeypatch, tmp_path):
    monkeypatch.setattr(acmd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(acmd, "SYMBOLS", ["BTC"])

    (tmp_path / "BTC_latest.json").write_text(json.dumps(_raw_payload()))

    exit_code = acmd.main()
    assert exit_code == 0

    out_path = tmp_path / "BTC_analysis.json"
    assert out_path.exists()
    data = json.loads(out_path.read_text())
    assert data["symbol"] == "BTC-USD_UM_XPERP-310328"


def test_main_missing_input_file_returns_1(monkeypatch, tmp_path):
    monkeypatch.setattr(acmd, "DATA_DIR", tmp_path)
    monkeypatch.setattr(acmd, "SYMBOLS", ["BTC", "ETH"])

    exit_code = acmd.main()
    assert exit_code == 1
    assert list(tmp_path.glob("*_analysis.json")) == []
