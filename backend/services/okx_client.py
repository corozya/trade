"""Klient OKX REST API — signing, credentials, odczyty (saldo/pozycje/zlecenia),
kontrakt place_order, retry tylko dla odczytów (#66).

Zakres (patrz task #66, rozszerzone o dane rynkowe #78):
- resolve_credentials(alias) — env OKX_{ALIAS}_API_KEY/API_SECRET/API_PASSPHRASE.
- sign_request — HMAC-SHA256 base64 wg spec OKX (timestamp+method+requestPath+body).
- OkxClient — get_balance/get_positions/get_orders/get_ticker/get_candles/
  get_orderbook/get_funding_rate/get_funding_rate_history/get_open_interest/
  get_liquidation_orders/get_position_tiers (#195)
  (retry+backoff) oraz place_order (kontrakt, BEZ retry — idempotencja zleceń
  to #68).
- Wyjątki domenowe: OkxCredentialsError, OkxSignatureError, OkxPermissionError,
  OkxRateLimitError, OkxApiError.

Bezpieczeństwo: żaden sekret (api_key/api_secret/api_passphrase) nie jest nigdy
logowany ani wstawiany do treści wyjątków w plaintext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

OKX_BASE_URL = "https://my.okx.com"
"""Konta OKX w regionie EEA (w tym Polska) uwierzytelniają się wyłącznie przez
my.okx.com — www.okx.com zwraca 50119 "API key doesn't exist" dla takich kluczy,
mimo że klucz jest poprawny. Zweryfikowane manualnie przy uruchomieniu #67."""

# Kody błędów OKX (sCode / HTTP-adjacent) mapowane na wyjątki domenowe.
# Referencja: OKX REST API error codes (podzbiór istotny dla read-only/trading).
_RATE_LIMIT_CODES = {"50011"}
_SIGNATURE_CODES = {"50113", "50104", "50105", "50106"}
_PERMISSION_CODES = {"50110", "50111", "50114"}


class OkxError(Exception):
    """Bazowy wyjątek klienta OKX."""


class OkxCredentialsError(OkxError):
    """Brak lub niekompletne credentials dla danego aliasu w env."""


class OkxSignatureError(OkxError):
    """OKX odrzucił podpis żądania (invalid signature / timestamp)."""


class OkxPermissionError(OkxError):
    """Klucz API nie ma uprawnień do wykonania operacji (np. read-only vs trading)."""


class OkxRateLimitError(OkxError):
    """OKX zwrócił rate limit — kandydat do retry na odczytach."""


class OkxApiError(OkxError):
    """Inny błąd zwrócony przez OKX API (kod + komunikat, bez treści żądania)."""

    def __init__(self, message: str, code: Optional[str] = None):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OkxCredentials:
    api_key: str
    api_secret: str
    api_passphrase: Optional[str] = None


def resolve_credentials(alias: str) -> OkxCredentials:
    """Rozwiązuje credentials OKX dla danego aliasu z os.environ.

    Konwencja: OKX_{ALIAS}_API_KEY, OKX_{ALIAS}_API_SECRET (wymagane),
    OKX_{ALIAS}_API_PASSPHRASE (opcjonalny — może nie istnieć dla kluczy
    tylko-odczyt zależnie od trybu API OKX).

    Brak wymaganej zmiennej -> OkxCredentialsError z czytelnym komunikatem
    (nazwa zmiennej, nigdy jej wartość).
    """
    if not alias or not alias.strip():
        raise OkxCredentialsError("exchange_credential_alias jest pusty/brak")

    prefix = f"OKX_{alias.strip().upper()}"
    key_var = f"{prefix}_API_KEY"
    secret_var = f"{prefix}_API_SECRET"
    passphrase_var = f"{prefix}_API_PASSPHRASE"

    api_key = os.environ.get(key_var)
    api_secret = os.environ.get(secret_var)
    api_passphrase = os.environ.get(passphrase_var)

    missing = [name for name, val in ((key_var, api_key), (secret_var, api_secret)) if not val]
    if missing:
        raise OkxCredentialsError(
            f"Brak wymaganych zmiennych środowiskowych dla aliasu '{alias}': "
            f"{', '.join(missing)}. Ustaw je w .env (root repo BOT)."
        )

    return OkxCredentials(
        api_key=api_key,
        api_secret=api_secret,
        api_passphrase=api_passphrase or None,
    )


def _okx_timestamp() -> str:
    """ISO8601 UTC z milisekundami, format wymagany przez OKX (np. 2020-12-08T09:08:57.715Z)."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def sign_request(
    secret: str,
    timestamp: str,
    method: str,
    request_path: str,
    body: str = "",
) -> str:
    """HMAC-SHA256 base64 wg spec OKX:

    OK-ACCESS-SIGN = base64(HMAC-SHA256(secret, timestamp + method.upper() + requestPath + body))
    """
    prehash = f"{timestamp}{method.upper()}{request_path}{body}"
    digest = hmac.new(secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    return base64.b64encode(digest).decode("utf-8")


def build_headers(
    credentials: OkxCredentials,
    method: str,
    request_path: str,
    body: str = "",
    simulated_trading: bool = False,
) -> dict[str, str]:
    """Buduje nagłówki OKX (auth + opcjonalnie x-simulated-trading).

    x-simulated-trading: 1 jest ustawiane tylko gdy simulated_trading=True
    (portfele execution_mode='trading' / demo) — nieobecne dla real/read_only.
    """
    timestamp = _okx_timestamp()
    signature = sign_request(credentials.api_secret, timestamp, method, request_path, body)

    headers = {
        "OK-ACCESS-KEY": credentials.api_key,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "Content-Type": "application/json",
    }
    if credentials.api_passphrase:
        headers["OK-ACCESS-PASSPHRASE"] = credentials.api_passphrase
    if simulated_trading:
        headers["x-simulated-trading"] = "1"
    return headers


def _map_okx_error(status_code: int, payload: Any) -> OkxError:
    """Mapuje odpowiedź błędu OKX (kod sCode + msg) na wyjątek domenowy.

    Nigdy nie wstawia treści żądania (sekretów) do komunikatu — tylko kod/msg
    zwrócone przez OKX.
    """
    code = None
    msg = ""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and data:
            first = data[0]
            if isinstance(first, dict):
                code = first.get("sCode") or payload.get("code")
        code = code or payload.get("code")
        msg = payload.get("msg", "")

    if code in _RATE_LIMIT_CODES or status_code == 429:
        return OkxRateLimitError(f"OKX rate limit (code={code}): {msg}")
    if code in _SIGNATURE_CODES:
        return OkxSignatureError(f"OKX odrzucił podpis żądania (code={code}): {msg}")
    if code in _PERMISSION_CODES or status_code == 401:
        return OkxPermissionError(f"OKX: brak uprawnień (code={code}): {msg}")
    return OkxApiError(f"OKX API error (code={code}): {msg}", code=code)


def _retry_read(fn, *, max_attempts: int = 3, backoff_seconds: float = 0.5):
    """Retry z prostym liniowym backoffem — WYŁĄCZNIE dla operacji odczytu.

    Retry tylko na OkxRateLimitError i httpx.TransportError (błędy sieciowe
    przejściowe). Inne wyjątki (signature, permission, walidacja) propagują
    natychmiast — nie mają sensu do ponawiania.
    """
    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt < max_attempts:
        try:
            return fn()
        except (OkxRateLimitError, httpx.TransportError) as exc:
            last_exc = exc
            attempt += 1
            if attempt >= max_attempts:
                break
            time.sleep(backoff_seconds * attempt)
    raise last_exc


class OkxClient:
    """Klient OKX REST API dla odczytów (real i demo) oraz kontraktu place_order.

    simulated_trading=True ustawia x-simulated-trading:1 na każdym żądaniu
    (portfele execution_mode='trading'). Dla execution_mode='read_only'/'real'
    zostaw simulated_trading=False (domyślnie).
    """

    def __init__(
        self,
        alias: str,
        simulated_trading: bool = False,
        base_url: str = OKX_BASE_URL,
        http_client: Optional[httpx.Client] = None,
        max_read_attempts: int = 3,
        retry_backoff_seconds: float = 0.5,
    ):
        self.alias = alias
        self.simulated_trading = simulated_trading
        self.base_url = base_url.rstrip("/")
        self._http = http_client or httpx.Client(base_url=self.base_url, timeout=10.0)
        self._owns_http = http_client is None
        self.max_read_attempts = max_read_attempts
        self.retry_backoff_seconds = retry_backoff_seconds
        self._credentials: Optional[OkxCredentials] = None

    def close(self):
        if self._owns_http:
            self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        self.close()

    @property
    def credentials(self) -> OkxCredentials:
        if self._credentials is None:
            self._credentials = resolve_credentials(self.alias)
        return self._credentials

    # -- warstwa transportu --------------------------------------------

    def _request(
        self,
        method: str,
        request_path: str,
        body_obj: Optional[dict] = None,
        *,
        authenticated: bool = True,
    ) -> Any:
        """Pojedyncze żądanie HTTP do OKX (bez retry). Rzuca wyjątek domenowy przy błędzie."""
        import json as _json

        body = _json.dumps(body_obj, separators=(",", ":")) if body_obj else ""
        if authenticated:
            headers = build_headers(
                self.credentials, method, request_path, body, simulated_trading=self.simulated_trading
            )
        else:
            # Public market endpoints must remain usable without credentials.
            # Deliberately do not touch ``self.credentials`` in this branch.
            headers = {"Content-Type": "application/json"}
        try:
            response = self._http.request(method, request_path, content=body or None, headers=headers)
        except httpx.TransportError:
            raise
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        # OKX zwraca code="0" dla sukcesu nawet z HTTP 200; code!="0" -> błąd domenowy.
        if response.status_code >= 400 or (isinstance(payload, dict) and payload.get("code") not in (None, "0")):
            raise _map_okx_error(response.status_code, payload)
        return payload

    def _get(self, request_path: str, *, authenticated: bool = True) -> Any:
        return _retry_read(
            lambda: self._request("GET", request_path, authenticated=authenticated),
            max_attempts=self.max_read_attempts,
            backoff_seconds=self.retry_backoff_seconds,
        )

    # -- odczyty (retry włączony) ----------------------------------------

    def get_balance(self, ccy: Optional[str] = None) -> Any:
        """Saldo konta (GET /api/v5/account/balance)."""
        path = "/api/v5/account/balance"
        if ccy:
            path += f"?ccy={ccy}"
        return self._get(path)

    def get_ticker(self, inst_id: str) -> Any:
        """Ostatnia cena rynkowa instrumentu (GET /api/v5/market/ticker), odczyt
        publiczny ale wołany przez ten sam podpisany klient dla spójności (#68:
        potrzebne PRZED place_order do przeliczenia limitu 100 USDT/zlecenie)."""
        path = f"/api/v5/market/ticker?instId={inst_id}"
        return self._get(path)

    def get_tickers(self, inst_type: str = "SWAP", *, public: bool = True) -> Any:
        """Bulk tickers for all instruments of ``inst_type``.

        Read-only wrapper for ``GET /api/v5/market/tickers``.  Unlike
        :meth:`get_ticker`, this makes one request and is intended for market
        scans/rankings; it never places or modifies an order.
        """
        return self._get(f"/api/v5/market/tickers?instType={inst_type}", authenticated=not public)

    def get_positions(self, inst_type: Optional[str] = None) -> Any:
        """Otwarte pozycje (GET /api/v5/account/positions)."""
        path = "/api/v5/account/positions"
        if inst_type:
            path += f"?instType={inst_type}"
        return self._get(path)

    def get_positions_history(self, inst_type: Optional[str] = None, inst_id: Optional[str] = None, limit: int = 100) -> Any:
        """Zamknięte pozycje z realnym P&L (GET /api/v5/account/positions-history) —
        pnl/openAvgPx/closeAvgPx/uTime per pozycja, w przeciwieństwie do
        orders-history, które jest per-zlecenie, nie per-pozycja."""
        path = f"/api/v5/account/positions-history?limit={limit}"
        if inst_type:
            path += f"&instType={inst_type}"
        if inst_id:
            path += f"&instId={inst_id}"
        return self._get(path)

    def get_algo_orders_pending(self, inst_id: Optional[str] = None, ord_type: str = "oco") -> Any:
        """Oczekujące algo-zlecenia (GET /api/v5/trade/orders-algo-pending) —
        źródło algoId dla attachAlgoOrds (SL/TP) dołączonych przy otwarciu
        pozycji, potrzebne do amend_algo_orders."""
        path = f"/api/v5/trade/orders-algo-pending?ordType={ord_type}"
        if inst_id:
            path += f"&instId={inst_id}"
        return self._get(path)

    def get_order(
        self,
        inst_id: str,
        ord_id: Optional[str] = None,
        *,
        cl_ord_id: Optional[str] = None,
    ) -> Any:
        """Status/szczegóły pojedynczego zlecenia (GET /api/v5/trade/order).

        Wołane po place_order (#68) żeby ustalić rzeczywistą cenę wykonania
        (avgPx) i stan wypełnienia (state/fillSz) — market order fill price
        może różnić się od estymaty użytej do limitu 100 USDT/zlecenie."""
        if not ord_id and not cl_ord_id:
            raise ValueError("ord_id lub cl_ord_id jest wymagany")
        lookup = f"ordId={ord_id}" if ord_id else f"clOrdId={cl_ord_id}"
        path = f"/api/v5/trade/order?instId={inst_id}&{lookup}"
        return self._get(path)

    def get_orders(self, inst_type: Optional[str] = None, state: Optional[str] = None) -> Any:
        """Lista/status zleceń (GET /api/v5/trade/orders-pending lub historia).

        Bez filtrów zwraca zlecenia oczekujące (orders-pending) — odczyt idempotentny.
        """
        path = "/api/v5/trade/orders-pending"
        params = []
        if inst_type:
            params.append(f"instType={inst_type}")
        if state:
            params.append(f"state={state}")
        if params:
            path += "?" + "&".join(params)
        return self._get(path)

    def get_instruments(self, inst_type: str, inst_family: Optional[str] = None) -> Any:
        """Metadane instrumentów (GET /api/v5/public/instruments) — odczyt publiczny,
        potrzebny do znalezienia aktualnego instId dla dany instFamily (np. X-Perps
        futures z terminem wygaśnięcia, alias/instFamily stabilne, instId nie)."""
        path = f"/api/v5/public/instruments?instType={inst_type}"
        if inst_family:
            path += f"&instFamily={inst_family}"
        return self._get(path, authenticated=False)

    def get_account_instruments(self, inst_type: str, inst_family: Optional[str] = None) -> Any:
        """Instrumenty dostępne dla bieżącego konta/trading mode.

        W przeciwieństwie do katalogu publicznego ten endpoint jest
        uwierzytelniony, więc przy ``simulated_trading=True`` uwzględnia
        nagłówek Demo i zwraca faktycznie tradowalne instId tego konta.
        """
        path = f"/api/v5/account/instruments?instType={inst_type}"
        if inst_family:
            path += f"&instFamily={inst_family}"
        return self._get(path)

    def get_candles(
        self,
        inst_id: str,
        bar: str = "15m",
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None,
        history: bool = False,
    ) -> Any:
        """Świece OHLCV (GET /api/v5/market/candles, lub /history-candles dla danych
        starszych niż dostępne w candles — history=True dla zasięgu >1-2 dni na 15m).

        bar: "1m"/"5m"/"15m"/"1H"/"4H"/... (spec OKX). limit max 300 wg API OKX.
        """
        endpoint = "/api/v5/market/history-candles" if history else "/api/v5/market/candles"
        path = f"{endpoint}?instId={inst_id}&bar={bar}&limit={limit}"
        if after:
            path += f"&after={after}"
        if before:
            path += f"&before={before}"
        return self._get(path)

    def get_orderbook(self, inst_id: str, sz: int = 20) -> Any:
        """Order book (GET /api/v5/market/books) — best bid/ask + głębokość.

        sz: liczba poziomów po każdej stronie (max 400 wg API OKX).
        """
        path = f"/api/v5/market/books?instId={inst_id}&sz={sz}"
        return self._get(path)

    def get_funding_rate(self, inst_id: str) -> Any:
        """Bieżący + przewidywany funding rate (GET /api/v5/public/funding-rate),
        odczyt publiczny — tylko dla instrumentów SWAP/futures perpetual."""
        path = f"/api/v5/public/funding-rate?instId={inst_id}"
        return self._get(path)

    def get_funding_rate_history(
        self,
        inst_id: str,
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> Any:
        """Historia funding rate (GET /api/v5/public/funding-rate-history), odczyt
        publiczny — tylko dla instrumentów SWAP/futures perpetual. limit max 100
        wg API OKX (#163).

        UWAGA semantyka odwrotna od nazwy parametru (zweryfikowane empirycznie
        2026-08-06, patrz #161/#163): ``after=<ts>`` zwraca rekordy STARSZE niż
        ts (paginacja wstecz w historii), ``before=<ts>`` zwraca NOWSZE. Zasięg
        REST: ok. 3 miesiące wstecz, potem pusta strona — twardy limit API, nie
        obchodzić.
        """
        path = f"/api/v5/public/funding-rate-history?instId={inst_id}&limit={limit}"
        if after:
            path += f"&after={after}"
        if before:
            path += f"&before={before}"
        return self._get(path)

    def get_open_interest(self, inst_id: Optional[str] = None, inst_type: str = "SWAP") -> Any:
        """Open Interest — poziom bieżący (GET /api/v5/public/open-interest),
        odczyt publiczny. instId opcjonalny (zawęża do jednego instrumentu)."""
        path = f"/api/v5/public/open-interest?instType={inst_type}"
        if inst_id:
            path += f"&instId={inst_id}"
        return self._get(path)

    def get_mark_price(self, inst_id: str, inst_type: str = "FUTURES") -> Any:
        """Mark price — cena referencyjna do likwidacji, różna od 'last'
        (GET /api/v5/public/mark-price), odczyt publiczny (#155/#156 advisory)."""
        path = f"/api/v5/public/mark-price?instType={inst_type}&instId={inst_id}"
        return self._get(path)

    def get_trades(self, inst_id: str, limit: int = 100) -> Any:
        """Ostatnie transakcje rynkowe — side/sz/px/ts (GET /api/v5/market/trades),
        odczyt publiczny (#155/#156 advisory). limit max 500 wg API OKX."""
        path = f"/api/v5/market/trades?instId={inst_id}&limit={limit}"
        return self._get(path)

    def get_open_interest_history(
        self,
        ccy: str,
        period: str = "5m",
        limit: Optional[int] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> Any:
        """Historyczny Open Interest + wolumen (GET /api/v5/rubik/stat/contracts/open-interest-volume),
        odczyt publiczny. Zwraca ``[ts, oi, vol]`` per okres w wartości quote-currency,
        agregowane per waluta bazowa (wszystkie kontrakty ``ccy`` razem, NIE per instId).

        Uwaga: to jest właściwy endpoint dla historii OI — endpoint z dokumentacji
        ``contract-open-interest-history`` jest martwy (zawsze zwraca dane puste),
        zweryfikowane empirycznie #164/#161. Parametr to ``ccy`` (waluta bazowa,
        np. "BTC"), NIE instId — jak get_taker_volume/get_long_short_account_ratio.
        period wspiera tylko 5m/1H/1D. Zweryfikowany zasięg wstecz: period=1D ->
        ~180 punktów (~6 mies., twardy sufit REST); period=5m -> ~575 punktów
        (~2 dni) — im drobniejszy period, tym krótszy zasięg wstecz niezależnie
        od paginacji."""
        path = f"/api/v5/rubik/stat/contracts/open-interest-volume?ccy={ccy}&period={period}"
        if limit:
            path += f"&limit={limit}"
        if after:
            path += f"&after={after}"
        if before:
            path += f"&before={before}"
        return self._get(path)

    def get_taker_volume(self, ccy: str, inst_type: str = "CONTRACTS", period: str = "5m") -> Any:
        """Agresywny wolumen kupna/sprzedaży taker (GET /api/v5/rubik/stat/taker-volume),
        odczyt publiczny. Zwraca [ts, sellVol, buyVol] per okres — pozwala odróżnić
        akumulację od słabego odbicia (#153/#155/#156 advisory).

        Uwaga: parametr to ccy (waluta bazowa, np. "BTC"), NIE instId — dane są
        zagregowane dla całej waluty, nie per konkretny kontrakt. period wspiera
        tylko 5m/1H/1D (zweryfikowane manualnie 2026-08-04, NIE 15m mimo że
        candles wspiera)."""
        path = f"/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType={inst_type}&period={period}"
        return self._get(path)

    def get_taker_volume_history(
        self,
        ccy: str,
        inst_type: str = "CONTRACTS",
        period: str = "5m",
        limit: Optional[int] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> Any:
        """Historyczny taker volume (GET /api/v5/rubik/stat/taker-volume) z paginacją
        after/before — sam endpoint co get_taker_volume, ale dla backfillu (#165/#161)
        zamiast pojedynczego bieżącego odczytu. Zwraca ``[ts, sellVol, buyVol]`` per
        okres w quote-currency, agregowane per waluta bazowa (ccy), NIE per instId.

        Parametr to ``ccy`` (waluta bazowa, np. "BTC"), NIE instId. period wspiera
        tylko 5m/1H/1D (zweryfikowane manualnie 2026-08-04, NIE 15m mimo że candles
        wspiera). Zweryfikowany zasięg wstecz: period=1D -> ~180 punktów (~6 mies.,
        twardy sufit REST); period=5m -> ~575 punktów (~2 dni) — analogicznie do
        get_open_interest_history (#164/#161)."""
        path = f"/api/v5/rubik/stat/taker-volume?ccy={ccy}&instType={inst_type}&period={period}"
        if limit:
            path += f"&limit={limit}"
        if after:
            path += f"&after={after}"
        if before:
            path += f"&before={before}"
        return self._get(path)

    def get_long_short_account_ratio(self, ccy: str, period: str = "5m") -> Any:
        """Stosunek liczby kont long/short (GET /api/v5/rubik/stat/contracts/long-short-account-ratio),
        odczyt publiczny. Zwraca [ts, ratio] per okres — pozycjonowanie rynku
        (#153/#155/#156 advisory).

        Uwaga: parametr to ccy (waluta bazowa), NIE instId — jak get_taker_volume.
        period wspiera tylko 5m/1H/1D (zweryfikowane manualnie 2026-08-04)."""
        path = f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period={period}"
        return self._get(path)

    def get_liquidation_orders(
        self,
        inst_type: str = "SWAP",
        *,
        inst_family: Optional[str] = None,
        uly: Optional[str] = None,
        inst_id: Optional[str] = None,
        state: str = "filled",
        limit: Optional[int] = None,
    ) -> Any:
        """Zrealizowane likwidacje (GET /api/v5/public/liquidation-orders),
        odczyt publiczny — #195 (estymowana liquidation heatmap).

        Zweryfikowane empirycznie 2026-08-09: dla instType=SWAP wymagany jest
        albo ``instFamily`` albo ``uly`` (OKX zwraca sCode 50015 "Either
        parameter uly or instFamily is required" gdy brak obu) — instId sam w
        sobie NIE wystarcza, w przeciwieństwie do większości innych publicznych
        endpointów tego klienta. state domyślnie "filled" (zrealizowane
        likwidacje) — inna wartość to "unfilled" (OKX spec).
        """
        if not inst_family and not uly:
            raise ValueError("get_liquidation_orders wymaga inst_family lub uly dla instType=SWAP")
        path = f"/api/v5/public/liquidation-orders?instType={inst_type}&state={state}"
        if inst_family:
            path += f"&instFamily={inst_family}"
        if uly:
            path += f"&uly={uly}"
        if inst_id:
            path += f"&instId={inst_id}"
        if limit:
            path += f"&limit={limit}"
        return self._get(path)

    def get_position_tiers(
        self,
        inst_type: str = "SWAP",
        *,
        inst_family: str,
        td_mode: str = "cross",
    ) -> Any:
        """Poziomy dźwigni/marginu per tier wielkości pozycji (GET
        /api/v5/public/position-tiers), odczyt publiczny — #195.

        Zweryfikowane empirycznie 2026-08-09 dla BTC-USDT (SWAP, cross): OKX
        NIE ma stałego menu dźwigni (np. 10x/20x/50x) — ``maxLever`` maleje
        stopniowo z każdym tier'em wielkości pozycji (notional), od 100x przy
        tier 1 (0-1000 USDT) w dół do ok. 2x przy najwyższych tier'ach
        (>1.9M USDT), ~99 tierów łącznie. ``mmr`` (maintenance margin ratio)
        rośnie analogicznie z każdym tier'em. Traderzy nadal mogą ręcznie
        wybrać dowolną dźwignię do ``maxLever`` danego tier'a — 10x/20x/50x
        pozostają sensownymi, powszechnie wybieranymi wartościami w praktyce,
        ale nie są to natywne "tiery" OKX-a w sensie tej odpowiedzi.
        """
        path = f"/api/v5/public/position-tiers?instType={inst_type}&instFamily={inst_family}&tdMode={td_mode}"
        return self._get(path)

    def get_long_short_account_ratio_history(
        self,
        ccy: str,
        period: str = "5m",
        limit: Optional[int] = None,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> Any:
        """Historyczny stosunek liczby kont long/short
        (GET /api/v5/rubik/stat/contracts/long-short-account-ratio), odczyt publiczny.
        Zwraca ``[ts, ratio]`` per okres — stosunek liczby kont long do short (nie
        wolumenu/wartości pozycji), pozycjonowanie rynku (#166/#161).

        Uwaga: parametr to ``ccy`` (waluta bazowa, np. "BTC"), NIE instId — jak
        get_taker_volume/get_open_interest_history. period wspiera tylko 5m/1H/1D.
        Zweryfikowany zasięg wstecz identyczny jak open-interest-volume: period=1D ->
        ~180 punktów (~6 mies., twardy sufit REST); period=5m -> ~575 punktów
        (~2 dni) — im drobniejszy period, tym krótszy zasięg wstecz niezależnie
        od paginacji. Ta metoda dodaje ``limit``/``after``/``before`` do istniejącego
        ``get_long_short_account_ratio`` (który zostaje jako pojedynczy bieżący odczyt
        używany przez agenta na żywo, bez zmian kontraktu)."""
        path = f"/api/v5/rubik/stat/contracts/long-short-account-ratio?ccy={ccy}&period={period}"
        if limit:
            path += f"&limit={limit}"
        if after:
            path += f"&after={after}"
        if before:
            path += f"&before={before}"
        return self._get(path)

    # -- zapisy (BEZ retry — idempotencja zleceń to #68) ------------------

    def place_order(
        self,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        px: Optional[str] = None,
        **extra_fields: Any,
    ) -> Any:
        """Składa zlecenie (POST /api/v5/trade/order).

        Kontrakt na potrzeby #66: metoda woła OKX REST bez retry (ryzyko
        duplikatu zlecenia). Pełna logika fill'i, statusów i idempotencji
        (client order id / dedup) należy do #68 — tu tylko wywołanie API.
        """
        body: dict[str, Any] = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }
        if px is not None:
            body["px"] = px
        body.update(extra_fields)
        return self._request("POST", "/api/v5/trade/order", body_obj=body)

    def set_leverage(self, inst_id: str, lever: str, mgn_mode: str) -> Any:
        """Ustawia dźwignię per-instrument (POST /api/v5/account/set-leverage) —
        wymagane PRZED pierwszym zleceniem futures na danym instId/mgnMode,
        inaczej OKX używa poprzednio ustawionej (lub domyślnej) dźwigni."""
        body = {"instId": inst_id, "lever": lever, "mgnMode": mgn_mode}
        return self._request("POST", "/api/v5/account/set-leverage", body_obj=body)

    def close_positions(self, inst_id: str, mgn_mode: str) -> Any:
        """Zamyka CAŁĄ pozycję netto na instId po rynku (POST
        /api/v5/trade/close-position) — konto w net_mode (brak posSide),
        więc nie trzeba podawać kierunku. Ubija też powiązane algo-zlecenia
        (SL/TP) automatycznie po stronie OKX."""
        body = {"instId": inst_id, "mgnMode": mgn_mode}
        return self._request("POST", "/api/v5/trade/close-position", body_obj=body)

    def amend_algo_orders(self, inst_id: str, algo_id: str, *, new_sl_trigger_px: Optional[str] = None, new_tp_trigger_px: Optional[str] = None) -> Any:
        """Zmienia trigger SL/TP istniejącego algo-zlecenia OCO bez zamykania
        pozycji (POST /api/v5/trade/amend-algos). Co najmniej jedno z
        new_sl_trigger_px/new_tp_trigger_px musi być podane."""
        body: dict[str, Any] = {"instId": inst_id, "algoId": algo_id}
        if new_sl_trigger_px is not None:
            body["newSlTriggerPx"] = new_sl_trigger_px
            body["newSlOrdPx"] = "-1"
        if new_tp_trigger_px is not None:
            body["newTpTriggerPx"] = new_tp_trigger_px
            body["newTpOrdPx"] = "-1"
        return self._request("POST", "/api/v5/trade/amend-algos", body_obj=body)
