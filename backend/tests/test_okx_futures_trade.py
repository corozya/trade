"""#72: futures long/short z dźwignią x10 (portfel kind='game', exchange='okx',
execution_mode='trading') — market order z attachAlgoOrds TP/SL, mock, zero
HTTP realnego.

Kontekst odkryty manualnie na koncie demo OKX (2026-07-21) PRZED napisaniem
tych testów:
- Standardowe SWAP (BTC-USDT-SWAP) i FUTURES weekly/quarterly (BTC-USD_UM-*)
  są zablokowane dla kont EEA/Polska na my.okx.com (code=51155 "local
  compliance restrictions"), NAWET w trybie demo trading.
- Jedyny dostępny wariant: X-Perps (instFamily '{BASE}-USD_UM_XPERP', instType
  FUTURES) — MiCA-regulated, kontrakt z terminem 2031 (bez cotygodniowego
  rolowania), max dźwignia 10x. ETH ma TYLKO ten wariant (brak weekly/quarterly).
- net_mode (nie long_short_mode) na koncie demo — brak posSide w zleceniach.
- attachAlgoOrds z jednoczesnym tpTriggerPx+slTriggerPx działa w jednym
  zleceniu (zweryfikowane: state='live' jako OCO algo order po fillu).

Brak lokalnego ledgera (decyzja usera 2026-07-22, patrz docstring
services/okx_trade.py::execute_okx_futures_order): portfel trading+okx nie
zapisuje transakcji/pozycji do bazy — execute_okx_futures_order tylko składa
zlecenie na OKX i odświeża total_value przez sync_trading_okx_portfolio.
Testy poniżej weryfikują zlecenie/limity/TP-SL, NIE idempotencję per-transakcję
ani model long/short jako dwa symbole w ledgerze (oba usunięte razem z zapisem).
"""
import json
from decimal import Decimal

import pytest

from services import game
from services.okx_client import OkxApiError
from services.okx_trade import (
    ALLOWED_OKX_FUTURES_BASES,
    FUTURES_LEVERAGE,
    MAX_FUTURES_MARGIN_USDC,
    _resolve_futures_instrument,
    calculate_okx_futures_entry_size,
    execute_okx_futures_order,
)

_XPERP_INST_ID = "BTC-USD_UM_XPERP-310328"


def _make_futures_okx_portfolio(conn, name="FuturesOkx", alias="okx_demo_main", allowed_asset_types=None):
    player = game.create_player(name, "ai", conn=conn)
    pf = game.create_portfolio(
        player_id=player["id"],
        name=name,
        starting_capital=0.0,
        kind="game",
        managed_by="ai",
        mandate_md="demo trading futures OKX",
        strategy_profile={
            "allowed_asset_types": allowed_asset_types or ["krypto"],
            "max_position_pct": 20,
        },
        conn=conn,
    )
    pf = game.set_exchange_config(
        pf["id"], "okx", "trading", exchange_credential_alias=alias, conn=conn,
    )
    conn.execute(
        "INSERT INTO cash_entries (portfolio_id, date, type, amount, note) VALUES (?,?,?,?,?)",
        (pf["id"], "2026-07-01", "wplata", 100000.0, "seed demo futures"),
    )
    conn.commit()
    return pf


class _FakeOkxClientFuturesOk:
    """get_instruments + set_leverage + place_order + get_ticker + get_order
    + get_balance/get_positions (wołane przez sync_trading_okx_portfolio po
    zleceniu, patrz execute_okx_futures_order) — happy path."""

    calls = {
        "get_instruments": 0, "set_leverage": 0, "place_order": 0, "get_ticker": 0,
        "get_order": 0, "get_balance": 0, "get_positions": 0,
    }
    last_place_order_kwargs = None

    def __init__(self, alias, simulated_trading=False, **kwargs):
        self.alias = alias
        self.simulated_trading = simulated_trading

    def get_instruments(self, inst_type, inst_family=None):
        type(self).calls["get_instruments"] += 1
        return {
            "code": "0", "msg": "",
            "data": [{
                "instId": _XPERP_INST_ID, "instFamily": inst_family, "state": "live",
                "ctVal": "0.01", "ctValCcy": "BTC", "lotSz": "1", "minSz": "1",
            }],
        }

    def get_account_instruments(self, inst_type, inst_family=None):
        return self.get_instruments(inst_type, inst_family=inst_family)

    def set_leverage(self, inst_id, lever, mgn_mode):
        type(self).calls["set_leverage"] += 1
        return {"code": "0", "msg": "", "data": [{"instId": inst_id, "lever": lever, "mgnMode": mgn_mode}]}

    def get_ticker(self, inst_id):
        type(self).calls["get_ticker"] += 1
        # BTC: qty=1, ctVal=0.01 -> notional=5, margin=0.5 przy cenie 500.
        return {"code": "0", "msg": "", "data": [{"instId": inst_id, "last": "500.0"}]}

    def place_order(self, inst_id, td_mode, side, ord_type, sz, px=None, **extra):
        type(self).calls["place_order"] += 1
        type(self).last_place_order_kwargs = extra
        return {"code": "0", "msg": "", "data": [{"ordId": "ORD-1", "sCode": "0", "sMsg": ""}]}

    def get_order(self, inst_id, ord_id):
        type(self).calls["get_order"] += 1
        return {
            "code": "0", "msg": "",
            "data": [{"ordId": ord_id, "avgPx": "500.5", "accFillSz": "1", "fillSz": "1", "state": "filled"}],
        }

    def get_balance(self, ccy=None):
        type(self).calls["get_balance"] += 1
        return {
            "code": "0", "msg": "",
            "data": [{"details": [{"ccy": "USDC", "eq": "1000.0", "availEq": "1000.0"}]}],
        }

    def get_positions(self, inst_type=None):
        type(self).calls["get_positions"] += 1
        return {"code": "0", "msg": "", "data": []}


class _FakeOkxClientFuturesApiError(_FakeOkxClientFuturesOk):
    """place_order rzuca OkxApiError — sprawdzamy brak stanu częściowego."""

    def place_order(self, inst_id, td_mode, side, ord_type, sz, px=None, **extra):
        type(self).calls["place_order"] += 1
        raise OkxApiError("insufficient margin", code="59XXX")


class _FakeOkxClientContract(_FakeOkxClientFuturesOk):
    base = "ETH"
    ct_val = "0.1"
    lot_sz = "1"
    min_sz = "1"
    last = "1920"

    def get_instruments(self, inst_type, inst_family=None):
        type(self).calls["get_instruments"] += 1
        return {
            "code": "0", "msg": "",
            "data": [{
                "instId": f"{self.base}-USD_UM_XPERP-310328",
                "instFamily": inst_family,
                "state": "live",
                "ctVal": self.ct_val,
                "ctValCcy": self.base,
                "lotSz": self.lot_sz,
                "minSz": self.min_sz,
            }],
        }

    def get_ticker(self, inst_id):
        type(self).calls["get_ticker"] += 1
        return {"code": "0", "msg": "", "data": [{"instId": inst_id, "last": self.last}]}


class _FakeOkxClientDogeContract(_FakeOkxClientContract):
    base = "DOGE"
    ct_val = "1000"
    last = "0.2"


class _FakeOkxClientBtcContract(_FakeOkxClientContract):
    base = "BTC"
    ct_val = "0.01"
    last = "100000"


class _FakeOkxClientLimitPending(_FakeOkxClientFuturesOk):
    """#198: get_order returns state='live' (not filled) — simulates a limit
    order still sitting on the book, unlike the base fixture's always-filled
    market response."""

    def get_order(self, inst_id, ord_id):
        type(self).calls["get_order"] += 1
        return {
            "code": "0", "msg": "",
            "data": [{"ordId": ord_id, "avgPx": "", "accFillSz": "0", "fillSz": "0", "state": "live"}],
        }


class _FakeOkxClientMissingCtVal(_FakeOkxClientContract):
    ct_val = None


class _FakeOkxClientInvalidLotSize(_FakeOkxClientContract):
    lot_sz = "0"


@pytest.fixture(autouse=True)
def _reset_fake_calls():
    _FakeOkxClientFuturesOk.calls = {
        "get_instruments": 0, "set_leverage": 0, "place_order": 0, "get_ticker": 0,
        "get_order": 0, "get_balance": 0, "get_positions": 0,
    }
    _FakeOkxClientFuturesOk.last_place_order_kwargs = None
    yield


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_demo_resolver_uses_account_catalog_when_public_inst_id_differs():
    class DivergedCatalogClient:
        simulated_trading = True
        public_calls = 0
        account_calls = 0

        @staticmethod
        def _instrument(inst_id):
            return {
                "code": "0", "data": [{
                    "instId": inst_id, "state": "live", "ctVal": "0.1",
                    "ctValCcy": "ETH", "lotSz": "1", "minSz": "1",
                }],
            }

        def get_instruments(self, *_args, **_kwargs):
            type(self).public_calls += 1
            return self._instrument("ETH-USD_UM_XPERP-310404")

        def get_account_instruments(self, *_args, **_kwargs):
            type(self).account_calls += 1
            return self._instrument("ETH-USD_UM_XPERP-310328")

    client = DivergedCatalogClient()
    instrument = _resolve_futures_instrument("ETH", client)

    assert instrument["instId"] == "ETH-USD_UM_XPERP-310328"
    assert client.account_calls == 1
    assert client.public_calls == 0


def test_demo_resolver_does_not_fallback_to_public_catalog():
    class DemoCatalogMissingClient:
        simulated_trading = True
        public_calls = 0

        def get_account_instruments(self, *_args, **_kwargs):
            return {"code": "0", "data": []}

        def get_instruments(self, *_args, **_kwargs):
            type(self).public_calls += 1
            return {"code": "0", "data": [{"instId": "ETH-USD_UM_XPERP-310404", "state": "live"}]}

    client = DemoCatalogMissingClient()
    with pytest.raises(ValueError, match="brak instrumentu futures"):
        _resolve_futures_instrument("ETH", client)
    assert client.public_calls == 0


def test_non_demo_resolver_keeps_using_public_catalog():
    class PublicClient:
        simulated_trading = False
        account_calls = 0

        def get_instruments(self, *_args, **_kwargs):
            return {
                "code": "0", "data": [{
                    "instId": "ETH-USD_UM_XPERP-310404", "state": "live",
                    "ctVal": "0.1", "ctValCcy": "ETH", "lotSz": "1", "minSz": "1",
                }],
            }

        def get_account_instruments(self, *_args, **_kwargs):
            type(self).account_calls += 1
            raise AssertionError("non-demo resolver must use public catalog")

    client = PublicClient()
    instrument = _resolve_futures_instrument("ETH", client)

    assert instrument["instId"] == "ETH-USD_UM_XPERP-310404"
    assert client.account_calls == 0


def _eth_sizing(**overrides):
    params = {
        "available_usdc_equity": "1000",
        "entry_price": "2000",
        "stop_loss_price": "1950",
        "side": "BUY",
        "instrument": {"ctVal": "0.001", "lotSz": "1", "minSz": "1"},
        "max_position_pct": "20",
    }
    params.update(overrides)
    return calculate_okx_futures_entry_size(**params)


def test_risk_sizing_supports_live_style_eth_contract_value():
    result = _eth_sizing()
    assert result["qty"] == 50
    assert result["binding_cap"] == "risk"


def test_risk_sizing_rounds_down_to_lot_size():
    result = _eth_sizing(instrument={"ctVal": "0.001", "lotSz": "3", "minSz": "3"})
    assert result["qty"] == 48


def test_risk_sizing_target_notional_cap_is_enforced():
    result = _eth_sizing(stop_loss_price="1999", max_position_pct="30")
    assert result["qty"] == 50
    assert result["binding_cap"] == "target_notional"


def test_risk_sizing_mandate_cap_is_enforced():
    result = _eth_sizing(stop_loss_price="1999", max_position_pct="5")
    assert result["qty"] == 25
    assert result["binding_cap"] == "mandate"


def test_growth_sizing_targets_ten_percent_notional_above_legacy_100_margin_cap():
    result = _eth_sizing(
        available_usdc_equity="89563",
        entry_price="1874",
        stop_loss_price="1879",
        side="SELL",
    )
    assert result["qty"] == 4779
    assert result["target_notional_usdc"] == Decimal("8956.30")
    assert result["binding_cap"] == "target_notional"


def test_risk_sizing_fails_when_rounded_qty_is_below_minimum():
    with pytest.raises(ValueError, match="mniejsze niż minSz"):
        _eth_sizing(
            available_usdc_equity="10",
            instrument={"ctVal": "0.001", "lotSz": "10", "minSz": "10"},
        )


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"entry_price": None}, "entry_price"),
        ({"stop_loss_price": None}, "stop_loss_price"),
        ({"available_usdc_equity": None}, "available_usdc_equity"),
    ],
)
def test_risk_sizing_fails_closed_on_missing_sl_or_balance(overrides, message):
    with pytest.raises(ValueError, match=message):
        _eth_sizing(**overrides)


def test_explicit_exit_qty_is_not_risk_resized(conn, price_env, monkeypatch):
    import services.okx_trade as okx_trade

    monkeypatch.setattr(
        okx_trade,
        "calculate_okx_futures_entry_size",
        lambda **_kwargs: pytest.fail("explicit exit qty must not be resized"),
    )
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="SELL", qty=1,
        reason="mock exit", idempotency_key="exit-not-resized", conn=conn,
        okx_client_factory=_FakeOkxClientFuturesOk,
    )
    assert result["qty_filled"] == pytest.approx(1.0)


def test_automatic_entry_sizing_reaches_demo_execution_path(conn, price_env):
    class AutoSizedEthClient(_FakeOkxClientContract):
        ct_val = "0.001"
        last = "2000"
        submitted_size = None

        def place_order(self, inst_id, td_mode, side, ord_type, sz, px=None, **extra):
            type(self).submitted_size = sz
            return super().place_order(inst_id, td_mode, side, ord_type, sz, px=px, **extra)

    pf = _make_futures_okx_portfolio(conn)
    profile = dict(pf["strategy_profile"])
    profile["max_position_pct"] = 20
    conn.execute(
        "UPDATE portfolios SET strategy_profile=? WHERE id=?",
        (json.dumps(profile), pf["id"]),
    )
    conn.commit()
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="ETH", side="BUY", qty=None,
        reason="mock auto sized entry", stop_loss_price=1950,
        take_profit_price=2100, idempotency_key="auto-sized-entry", conn=conn,
        okx_client_factory=AutoSizedEthClient,
    )
    assert AutoSizedEthClient.submitted_size == "50"


def test_auto_sized_growth_entry_can_exceed_legacy_100_margin_cap(conn, price_env):
    class GrowthEthClient(_FakeOkxClientContract):
        ct_val = "0.001"
        last = "1874"
        submitted_size = None

        def get_balance(self, ccy=None):
            return {
                "code": "0", "data": [{"details": [{
                    "ccy": "USDC", "eq": "89563", "availEq": "89563",
                }]}],
            }

        def place_order(self, inst_id, td_mode, side, ord_type, sz, px=None, **extra):
            type(self).submitted_size = sz
            return super().place_order(inst_id, td_mode, side, ord_type, sz, px=px, **extra)

    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="ETH", side="SELL", qty=None,
        reason="mock growth sizing", stop_loss_price=1879,
        take_profit_price=1864, idempotency_key="growth-auto", conn=conn,
        okx_client_factory=GrowthEthClient,
    )
    assert GrowthEthClient.submitted_size == "4779"
    assert result["qty_requested"] == 4779


def test_explicit_growth_sized_qty_still_hits_legacy_100_margin_cap(conn, price_env):
    class GrowthEthClient(_FakeOkxClientContract):
        ct_val = "0.001"
        last = "1874"

    pf = _make_futures_okx_portfolio(conn)
    with pytest.raises(game.ValidationError, match="100.0 USDC margin"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="ETH", side="SELL", qty=4779,
            reason="mock explicit sizing", stop_loss_price=1879,
            take_profit_price=1864, idempotency_key="growth-explicit", conn=conn,
            okx_client_factory=GrowthEthClient,
        )


def test_execute_okx_futures_order_happy_path_sets_leverage_and_places_order(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="demo futures test",
        idempotency_key="idem-futures-happy", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    assert _FakeOkxClientFuturesOk.calls["get_instruments"] == 1
    assert _FakeOkxClientFuturesOk.calls["set_leverage"] == 1
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 1
    assert result["ok"] is True
    assert result["order_id"] == "ORD-1"
    assert result["symbol"] == "BTC"
    assert result["side"] == "BUY"
    assert result["qty_filled"] == pytest.approx(1.0)
    # fill_price = realPrice fillu (avgPx).
    assert result["fill_price"] == pytest.approx(500.5)
    # sync_trading_okx_portfolio wołane po zleceniu (get_balance) — total_value w wyniku.
    assert _FakeOkxClientFuturesOk.calls["get_balance"] == 1
    assert result["total_value_pln"] is not None


def test_futures_leverage_set_to_10x_isolated(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="lever check",
        idempotency_key="idem-lever", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    # set_leverage wołane z lever='10' i mgnMode='isolated' — sprawdzamy przez
    # brak wyjątku (fake zwraca sukces niezależnie od parametrów) i licznik wywołań.
    assert _FakeOkxClientFuturesOk.calls["set_leverage"] == 1


# ---------------------------------------------------------------------------
# TP/SL attachAlgoOrds
# ---------------------------------------------------------------------------


def test_take_profit_and_stop_loss_attached_to_order(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="tp/sl test",
        take_profit_price=55000.0, stop_loss_price=48000.0,
        idempotency_key="idem-tpsl", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    extra = _FakeOkxClientFuturesOk.last_place_order_kwargs
    assert "attachAlgoOrds" in extra
    attach = extra["attachAlgoOrds"][0]
    assert attach["tpTriggerPx"] == "55000.0"
    assert attach["tpOrdPx"] == "-1"
    assert attach["slTriggerPx"] == "48000.0"
    assert attach["slOrdPx"] == "-1"


def test_no_tp_sl_omits_attach_algo_ords(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="no tp/sl",
        idempotency_key="idem-no-tpsl", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    extra = _FakeOkxClientFuturesOk.last_place_order_kwargs
    assert "attachAlgoOrds" not in extra


def test_only_take_profit_no_stop_loss(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="tp only",
        take_profit_price=55000.0,
        idempotency_key="idem-tp-only", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    attach = _FakeOkxClientFuturesOk.last_place_order_kwargs["attachAlgoOrds"][0]
    assert attach["tpTriggerPx"] == "55000.0"
    assert "slTriggerPx" not in attach


# ---------------------------------------------------------------------------
# Limit margin (MAX_FUTURES_MARGIN_USDC)
# ---------------------------------------------------------------------------


def test_eth_one_contract_uses_ct_val_and_fits_margin_limit(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="ETH", side="BUY", qty=1, reason="ctVal sizing",
        conn=conn, okx_client_factory=_FakeOkxClientContract,
    )
    # 1 * 0.1 ETH * 1920 / 10 = 19.20 USDC margin.
    assert result["ok"] is True
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 1


def test_fractional_contract_rejected_by_lot_size_before_place_order(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    with pytest.raises(game.ValidationError, match="minSz|lotSz"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="ETH", side="BUY", qty=0.5, reason="bad step",
            conn=conn, okx_client_factory=_FakeOkxClientContract,
        )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 0


def test_eth_contract_value_enforces_margin_limit(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    with pytest.raises(game.ValidationError, match=r"115\.20 USDC"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="ETH", side="BUY", qty=6, reason="too large",
            conn=conn, okx_client_factory=_FakeOkxClientContract,
        )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 0


@pytest.mark.parametrize(
    ("symbol", "qty", "client_factory"),
    [
        ("BTC", 1, _FakeOkxClientBtcContract),   # 0.01 BTC * 100000 / 10 = 100
        ("DOGE", 5, _FakeOkxClientDogeContract),  # 5 * 1000 DOGE * 0.2 / 10 = 100
    ],
)
def test_contract_values_for_btc_doge_are_applied(
    conn, price_env, symbol, qty, client_factory,
):
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol=symbol, side="BUY", qty=qty, reason="boundary",
        conn=conn, okx_client_factory=client_factory,
    )
    assert result["ok"] is True
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 1


# ---------------------------------------------------------------------------
# SOL: próbowane w #136, COFNIĘTE 2026-07-24, re-zweryfikowane 2026-08-07
# (#198, nadal COFNIĘTE — 0 kontraktów perpetual w tamtym momencie), i
# ponownie re-zweryfikowane 2026-08-09 (#240) — TYM RAZEM ma działający
# perpetual (SOL-USD_UM_XPERP, ctValCcy=SOL) na demo_main_full, więc dodany
# z powrotem do ALLOWED_OKX_FUTURES_BASES. To nie znaczy że wcześniejsze
# weryfikacje były błędne — OKX dodał instrument między 2026-08-07 a
# 2026-08-09, patrz komentarz przy ALLOWED_OKX_FUTURES_BASES w okx_trade.py.
# XRP re-zweryfikowany 2026-08-07 (#198): DZIAŁA jako perpetual
# (XRP-USD_UM_XPERP, ctValCcy=XRP) na demo_main_full — coś się zmieniło na
# OKX od 2026-07-24 (albo dotyczyło innego stanu konta), więc dodany z
# powrotem do ALLOWED_OKX_FUTURES_BASES. Patrz komentarz tam. LTC dodany
# 2026-08-09 (#240) jako zupełnie nowy symbol, zweryfikowany live tak samo.
# ---------------------------------------------------------------------------


def test_sol_is_in_allowlist(conn, price_env):
    assert "SOL" in ALLOWED_OKX_FUTURES_BASES


def test_xrp_is_in_allowlist(conn, price_env):
    assert "XRP" in ALLOWED_OKX_FUTURES_BASES


def test_ltc_is_in_allowlist(conn, price_env):
    assert "LTC" in ALLOWED_OKX_FUTURES_BASES


@pytest.mark.parametrize("client_factory", [_FakeOkxClientMissingCtVal, _FakeOkxClientInvalidLotSize])
def test_missing_or_invalid_contract_metadata_rejected_safely(conn, price_env, client_factory):
    pf = _make_futures_okx_portfolio(conn)
    with pytest.raises(game.ValidationError, match="instrumentu futures|nieprawidłowe"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="ETH", side="BUY", qty=1, reason="bad metadata",
            conn=conn, okx_client_factory=client_factory,
        )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 0


def test_order_above_margin_limit_rejected_before_place_order(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    # BTC ctVal=0.01, last=500, qty=201 -> margin=100.5 > limit 100.
    with pytest.raises(game.ValidationError, match="margin"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=201, reason="too big margin",
            idempotency_key="idem-margin-toobig", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
        )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 0


def test_order_at_or_below_margin_limit_allowed(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    # BTC ctVal=0.01, last=500, qty=200 -> margin=100 == limit, dozwolone.
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=200, reason="at margin limit",
        idempotency_key="idem-margin-atlimit", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 1
    assert result["order_id"] == "ORD-1"


# ---------------------------------------------------------------------------
# Allowlist symboli bazowych
# ---------------------------------------------------------------------------


def test_disallowed_base_symbol_rejected_before_place_order(conn, price_env):
    # ADA (poza BTC/ETH/DOGE/SOL/XRP) użyty jako symbol na pewno spoza
    # allowlisty także po #136 (który dodał SOL/XRP) — SOL przestał być
    # dobrym przykładem "niedozwolonego" symbolu, bo teraz jest w allowlist.
    pf = _make_futures_okx_portfolio(conn)
    assert "ADA" not in ALLOWED_OKX_FUTURES_BASES
    with pytest.raises(game.ValidationError, match="ALLOWED_OKX_FUTURES_BASES|nie jest dozwolony"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="ADA", side="BUY", qty=1, reason="not allowed",
            idempotency_key="idem-notallowed", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
        )
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 0


# ---------------------------------------------------------------------------
# side=SELL składa zlecenie sprzedaży (short) bez zmian modelu ledgera —
# brak lokalnego ledgera oznacza że nie ma już nic do sprawdzenia poza tym,
# że side realny trafia do OKX niezmieniony.
# ---------------------------------------------------------------------------


def test_sell_side_places_sell_order_on_okx(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="SELL", qty=1, reason="short test",
        idempotency_key="idem-short", conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    assert result["side"] == "SELL"
    assert _FakeOkxClientFuturesOk.calls["place_order"] == 1


# ---------------------------------------------------------------------------
# Błąd OKX -> brak wywołania sync (zlecenie w ogóle nie doszło do skutku)
# ---------------------------------------------------------------------------


def test_okx_error_during_place_order_leaves_no_transaction_recorded(conn, price_env):
    pf = _make_futures_okx_portfolio(conn)
    with pytest.raises(game.ValidationError, match="OKX odrzucił zlecenie"):
        execute_okx_futures_order(
            portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="will fail",
            idempotency_key="idem-futures-fail", conn=conn, okx_client_factory=_FakeOkxClientFuturesApiError,
        )
    # Brak lokalnego ledgera w ogóle (patrz docstring modułu) — nic nie ma się
    # zapisać do transactions niezależnie od sukcesu/porażki zlecenia.
    txs = conn.execute(
        "SELECT COUNT(*) AS n FROM transactions WHERE portfolio_id=?", (pf["id"],)
    ).fetchone()
    assert txs["n"] == 0


# ---------------------------------------------------------------------------
# Dispatch z execute_trade (game.py)
# ---------------------------------------------------------------------------


def test_execute_trade_dispatches_futures_with_tp_sl(conn, price_env, monkeypatch):
    """execute_trade przekazuje take_profit_price/stop_loss_price dalej do
    execute_okx_futures_order — weryfikacja pełnego wiringu MCP -> game ->
    okx_trade (nie tylko wywołanie execute_okx_futures_order wprost)."""
    import services.okx_trade as okx_trade_module

    monkeypatch.setattr(okx_trade_module, "OkxClient", _FakeOkxClientFuturesOk)

    pf = _make_futures_okx_portfolio(conn)
    result = game.execute_trade(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="dispatch tp/sl",
        take_profit_price=55000.0, stop_loss_price=48000.0,
        idempotency_key="idem-dispatch-tpsl", conn=conn,
    )
    assert result["ok"] is True
    attach = _FakeOkxClientFuturesOk.last_place_order_kwargs["attachAlgoOrds"][0]
    assert attach["tpTriggerPx"] == "55000.0"
    assert attach["slTriggerPx"] == "48000.0"


# ---------------------------------------------------------------------------
# Limit orders (#198, 2026-08-07)
# ---------------------------------------------------------------------------


def test_limit_order_sends_ord_type_limit_and_px(conn, price_env):
    """limit_price set -> ordType='limit' + px, not market."""
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="limit entry",
        limit_price=480.0, take_profit_price=550.0, stop_loss_price=420.0,
        conn=conn, okx_client_factory=_FakeOkxClientLimitPending,
    )
    assert result["ok"] is True
    assert result["limit_price"] == 480.0
    assert result["order_state"] == "live"
    assert result["qty_filled"] == 0.0


def test_limit_order_does_not_wait_for_fill(conn, price_env):
    """#198 user decision: confirm ACCEPTANCE only — a single get_order call
    (not _get_order_until_terminal's up-to-5-attempt poll loop, which market
    orders use to wait out the fill race documented at the top of
    okx_trade.py)."""
    pf = _make_futures_okx_portfolio(conn)
    execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="limit entry",
        limit_price=480.0, conn=conn, okx_client_factory=_FakeOkxClientLimitPending,
    )
    assert _FakeOkxClientLimitPending.calls["get_order"] == 1


def test_limit_price_used_for_margin_check_not_last_price(conn, price_env):
    """Margin computed from limit_price (480), not the fixture's last=500 —
    a limit far from the market shouldn't be sized off a price it won't
    actually fill at."""
    pf = _make_futures_okx_portfolio(conn)
    # qty=1, ctVal=0.01: at limit_price=480 -> notional=4.8, margin=0.48 (under 100 USDC, passes)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="limit entry",
        limit_price=480.0, conn=conn, okx_client_factory=_FakeOkxClientLimitPending,
    )
    assert result["ok"] is True


def test_market_order_still_waits_for_terminal_state(conn, price_env):
    """No limit_price -> unchanged market behavior, still ord_type='market'
    with no px, and still polls for a terminal fill state (regression guard
    for #198's changes to the same function)."""
    pf = _make_futures_okx_portfolio(conn)
    result = execute_okx_futures_order(
        portfolio_id=pf["id"], symbol="BTC", side="BUY", qty=1, reason="market entry",
        conn=conn, okx_client_factory=_FakeOkxClientFuturesOk,
    )
    assert result["ok"] is True
    assert result["limit_price"] is None
    assert result["order_state"] == "filled"
