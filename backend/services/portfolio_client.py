"""HTTP boundary from Crypto Agent to Investment Portfolio Manager."""
from __future__ import annotations

import os
from typing import Any, Mapping

import httpx


class PortfolioUnavailable(RuntimeError):
    """Portfolio Manager could not authoritatively process the request."""


class PortfolioClient:
    def __init__(self, base_url: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or os.environ.get(
            "PORTFOLIO_API_URL", "http://127.0.0.1:8420/api/v1"
        )).rstrip("/")
        self.timeout = timeout
        api_key = os.environ.get("PORTFOLIO_API_KEY", "").strip()
        self.headers = {"X-API-Key": api_key} if api_key else {}

    def submit_trade_intent(
        self, portfolio_id: int, intent: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/portfolios/{portfolio_id}/trade-intents",
                json=dict(intent),
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise PortfolioUnavailable("Portfolio Manager zwrócił niepoprawny kontrakt")
            return payload
        except httpx.HTTPStatusError as exc:
            detail: Any = exc.response.text
            try:
                detail = exc.response.json().get("detail", detail)
            except (ValueError, AttributeError):
                pass
            raise PortfolioUnavailable(
                f"Portfolio Manager odrzucił TradeIntent ({exc.response.status_code}): {detail}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioUnavailable(f"Portfolio Manager niedostępny: {exc}") from exc

    def update_position_protection(
        self, portfolio_id: int, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            response = httpx.post(
                f"{self.base_url}/portfolios/{portfolio_id}/position-protection",
                json=dict(payload),
                headers=self.headers,
                timeout=self.timeout,
            )
            response.raise_for_status()
            result = response.json()
            if not isinstance(result, dict):
                raise PortfolioUnavailable("Portfolio Manager zwrócił niepoprawny kontrakt")
            return result
        except httpx.HTTPStatusError as exc:
            raise PortfolioUnavailable(
                f"Portfolio Manager odrzucił zmianę ochrony ({exc.response.status_code}): {exc.response.text}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise PortfolioUnavailable(f"Portfolio Manager niedostępny: {exc}") from exc
