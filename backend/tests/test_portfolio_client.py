from __future__ import annotations

import httpx
import pytest

from services.portfolio_client import PortfolioClient, PortfolioUnavailable


def test_default_portfolio_url_uses_standalone_port(monkeypatch):
    monkeypatch.delenv("PORTFOLIO_API_URL", raising=False)

    assert PortfolioClient().base_url == "http://127.0.0.1:8422/api/v1"


def test_portfolio_unavailability_fails_closed(monkeypatch):
    request = httpx.Request("POST", "http://127.0.0.1:8422/api/v1/trade-intents")

    def unavailable(*_args, **_kwargs):
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx, "post", unavailable)

    with pytest.raises(PortfolioUnavailable, match="niedostępny"):
        PortfolioClient(timeout=0.01).submit_trade_intent(
            17,
            {
                "idempotency_key": "test-fail-closed",
                "symbol": "BTC",
                "action": "OPEN",
                "side": "BUY",
                "qty": "1",
            },
        )
