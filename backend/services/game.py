"""Warstwa serwisowa gry: FIFO pozycji, wycena, walidacje strategy_profile,
CRUD graczy/portfeli/transakcji/rekomendacji/rund, leaderboard.

Współdzielona przez REST API i (w kolejnym zadaniu) MCP server — jedna logika,
dwa interfejsy, zgodnie z SPEC-V2.md.
"""
import json
import hashlib
import logging
import re
import sqlite3
from bisect import bisect_right
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from datetime import datetime, date as date_cls, timedelta
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

from services.db import get_conn, begin_immediate

_UNIQUE_NAME_RE = re.compile(r"UNIQUE constraint failed:\s*players\.name")
from services.mandates import MANDATE_PRESETS, DEFAULT_STRATEGY_PROFILE, ASSET_TYPES
from services.pricing import (
    load_fx,
    load_fx_as_of,
    fx_rate_for_currency,
    price_for,
    price_source_path,
    change_pct_1d,
    close_as_of,
    is_usd_ticker,
    currency_for_symbol,
    load_price_series,
)


class ValidationError(Exception):
    """Odrzucenie transakcji/operacji z czytelnym komunikatem (złamana reguła strategy_profile)."""


class FifoIntegrityError(ValidationError):
    """SELL (bieżący lub historyczny import) nie ma pełnego pokrycia w lotach FIFO
    dostępnych na dany moment — zamiast po cichu ignorować niedopasowaną część
    (TASK_026-B), zgłaszamy błąd z symbolem, datą i brakującą ilością."""


class NotFoundError(Exception):
    pass


class IdempotencyConflictError(ValidationError):
    """Klucz idempotencji istnieje, ale opisuje inną operację."""


# ---------- players ----------

def list_players(conn: Optional[sqlite3.Connection] = None, include_archived: bool = False) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        q = "SELECT * FROM players"
        if not include_archived:
            q += " WHERE active=1"
        q += " ORDER BY name"
        rows = conn.execute(q).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def create_player(name: str, type_: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Uwaga: gdy `conn` przekazane przez wołającego (np. `create_player_with_portfolio`),
    NIE commituje — wołający odpowiada za commit/rollback całej operacji nadrzędnej."""
    if type_ not in ("ai", "human", "benchmark"):
        raise ValidationError(f"type musi być jednym z ai/human/benchmark, otrzymano: {type_}")
    own = conn is None
    conn = conn or get_conn()
    try:
        try:
            cur = conn.execute(
                "INSERT INTO players (name, type) VALUES (?, ?)", (name, type_)
            )
        except sqlite3.IntegrityError as e:
            if own:
                conn.rollback()
            if _UNIQUE_NAME_RE.search(str(e)):
                raise ValidationError(f"gracz o nazwie '{name}' już istnieje") from e
            raise ValidationError(str(e)) from e
        if own:
            conn.commit()
        return dict(conn.execute("SELECT * FROM players WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        if own:
            conn.close()


def create_player_with_portfolio(
    name: str,
    type_: str,
    starting_capital: float,
    base_currency: str = "PLN",
    mandate_md: str = "",
    strategy_profile: Optional[dict] = None,
    preset: Optional[str] = None,
    managed_by: Optional[str] = None,
    accent_color: Optional[str] = None,
    idempotent: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """POST /api/players: player + portfolio (kind=game) powstają razem albo żaden
    rekord (TASK_026-C) — jedna transakcja BEGIN IMMEDIATE, wspólny commit/rollback.
    Konflikt nazwy gracza → ValidationError (kontrolowane 400/409 w main.py),
    nigdy surowy sqlite3.IntegrityError.

    Ta funkcja jest zawsze granicą transakcji (BEGIN IMMEDIATE/commit/rollback),
    niezależnie od tego, czy `conn` przyszło od wołającego (np. main.py otwiera
    `conn` per-request i przekazuje dalej, ale to WCIĄŻ ta funkcja decyduje o
    commit/rollback) — tylko `conn.close()` jest warunkowe (własność połączenia)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            if idempotent:
                existing = conn.execute(
                    "SELECT * FROM players WHERE name=?", (name,)
                ).fetchone()
                if existing is not None:
                    portfolios = conn.execute(
                        "SELECT * FROM portfolios WHERE player_id=? ORDER BY id",
                        (existing["id"],),
                    ).fetchall()

                    effective_mandate = mandate_md
                    effective_profile = strategy_profile
                    if preset:
                        if preset not in MANDATE_PRESETS:
                            raise ValidationError(
                                f"nieznany preset mandatu: {preset}. "
                                f"Dostępne: {list(MANDATE_PRESETS)}"
                            )
                        preset_data = MANDATE_PRESETS[preset]
                        effective_mandate = effective_mandate or preset_data["mandate_md"]
                        effective_profile = effective_profile or preset_data["strategy_profile"]
                    if effective_profile is None:
                        effective_profile = DEFAULT_STRATEGY_PROFILE
                    validate_strategy_profile(effective_profile)

                    effective_manager = managed_by
                    if effective_manager is None:
                        effective_manager = "user" if type_ == "human" else "ai"
                    if effective_manager not in MANAGED_BY_VALUES:
                        raise ValidationError(
                            f"managed_by musi być user/ai, otrzymano: {effective_manager}"
                        )
                    effective_color = normalize_accent_color(accent_color)
                    if effective_color is None:
                        effective_color = default_accent_for_name(name)

                    portfolio = portfolios[0] if len(portfolios) == 1 else None
                    same = (
                        existing["type"] == type_
                        and existing["active"] == 1
                        and portfolio is not None
                        and portfolio["name"] == name
                        and portfolio["base_currency"] == base_currency
                        and float(portfolio["starting_capital"]) == float(starting_capital)
                        and portfolio["kind"] == "game"
                        and portfolio["managed_by"] == effective_manager
                        and portfolio["mandate_md"] == effective_mandate
                        and json.loads(portfolio["strategy_profile"]) == effective_profile
                        and portfolio["accent_color"] == effective_color
                        and portfolio["archived"] == 0
                    )
                    if same:
                        conn.commit()
                        return {
                            "player": dict(existing),
                            "portfolio": get_portfolio_row(portfolio["id"], conn),
                            "replayed": True,
                        }
                    raise ValidationError(
                        f"konflikt odtworzenia: gracz o nazwie '{name}' "
                        "istnieje z innymi danymi"
                    )

            player = create_player(name, type_, conn=conn)
            portfolio = create_portfolio(
                player_id=player["id"],
                name=name,
                starting_capital=starting_capital,
                kind="game",
                base_currency=base_currency,
                mandate_md=mandate_md,
                strategy_profile=strategy_profile,
                preset=preset,
                managed_by=managed_by,
                accent_color=accent_color,
                conn=conn,
            )
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return {"player": player, "portfolio": portfolio, "replayed": False}
    finally:
        if own:
            conn.close()


def archive_player(player_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Soft-delete gracza (active=0). Kaskadowo archiwizuje też wszystkie jego
    portfele (portfolios.archived=1) — bez tego portfel „osierocony” dalej
    pojawiałby się w /api/portfolios i leaderboardzie mimo nieaktywnego gracza
    (TASK_026-C). Cała operacja w jednej transakcji: player + portfele razem.
    Zawsze granica transakcji, patrz `create_player_with_portfolio`."""
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            row = conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone()
            if row is None:
                raise NotFoundError(f"player {player_id} nie istnieje")
            conn.execute("UPDATE players SET active=0 WHERE id=?", (player_id,))
            conn.execute("UPDATE portfolios SET archived=1 WHERE player_id=?", (player_id,))
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return dict(conn.execute("SELECT * FROM players WHERE id=?", (player_id,)).fetchone())
    finally:
        if own:
            conn.close()


# ---------- portfolios ----------

CASH_ENTRY_TYPES = frozenset({
    "wplata",
    "wyplata",
    "odsetki",
    "podatek",
    "dywidenda",
    "blokada",
    "odblokowanie",
    "rozliczenie_transakcji",
})
PUBLIC_CASH_OPERATION_TYPES = frozenset({
    "wplata", "wyplata", "dywidenda", "odsetki", "podatek",
})
# Typy z eMakler cashflow — bez reguły lokaty (seed/sync realnych kont).
BROKER_CASHFLOW_ENTRY_TYPES = frozenset({
    "blokada",
    "odblokowanie",
    "rozliczenie_transakcji",
})
# dywidenda: equity i/lub lokaty (nie wymaga wyłącznie lokaty)
DIVIDEND_ALLOWED_ASSET_TYPES = frozenset({"akcje_pln", "akcje_zagraniczne", "lokaty"})
MANAGED_BY_VALUES = frozenset({"user", "ai"})
EXECUTION_MODE_VALUES = frozenset({"read_only", "trading"})
SUPPORTED_EXCHANGES = frozenset({"okx"})

# #67: portfele z exchange skonfigurowanym jako read_only (dziś: real+okx) mają
# saldo/pozycje pochodzące WYŁĄCZNIE z synchronizacji z giełdą. Żadna ścieżka
# mutująca ledger (transakcje, ręczne cash_entries) nie może ich dotknąć — to
# jedyne miejsce definiujące tę regułę, wołane zarówno z _record_trade jak i
# add_cash_operation (patrz #67 PM-analiza: execute_trade/_record_trade nie
# sprawdzały wcześniej kind='real' w ogóle, tylko `archived`).
READ_ONLY_EXECUTION_MODES = frozenset({"read_only"})

# Sensowne defaulty ramek (#15). IKZE_ZONA / IKZE-Dorota = dotychczasowy --family (#7a3fa0).
DEFAULT_ACCENT_BY_NAME = {
    "IKE": "#3d4fd8",
    "IKZE": "#0d9488",
    "IKZE_ZONA": "#7a3fa0",
    "IKZE-Dorota": "#7a3fa0",
    "VeloBank": "#b86a1e",
    "Lokata": "#b86a1e",
    "Claude-clone": "#6366f1",
    "Claude-free": "#157f4a",
    "Zagranica": "#c45c26",
}

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def normalize_accent_color(value: Optional[str]) -> Optional[str]:
    """None/'' → None (neutral). Inaczej #RRGGBB."""
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    if not _HEX_COLOR_RE.match(s):
        raise ValidationError("accent_color musi być hex #RRGGBB lub puste")
    return s.lower()


def default_accent_for_name(name: str) -> Optional[str]:
    return DEFAULT_ACCENT_BY_NAME.get(name)


_PERCENT_FIELDS = ("max_position_pct", "min_cash_pct", "max_us_pct")
_NUMERIC_FIELDS = ("max_trades_per_round",)
_BOOL_FIELDS = ("requires_fundamental_check", "stop_loss_required", "cash_only")


def validate_strategy_profile(profile: Optional[dict]) -> None:
    """Walidacja strukturalna strategy_profile (TASK_026-C) — odrzuca kształt/typy
    złe na wejściu (np. procent jako string, ujemna wartość, nieznany typ aktywa)
    z czytelnym ValidationError zamiast pozwolić im dotrwać do błędu arytmetycznego
    dopiero przy execute_trade."""
    if profile is None:
        return
    if not isinstance(profile, dict):
        raise ValidationError("strategy_profile musi być obiektem (dict)")
    for field in _PERCENT_FIELDS:
        if field not in profile or profile[field] is None:
            continue
        value = profile[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"strategy_profile.{field} musi być liczbą")
        if value < 0 or value > 100:
            raise ValidationError(f"strategy_profile.{field} musi być w zakresie 0-100, otrzymano: {value}")
    for field in _NUMERIC_FIELDS:
        if field not in profile or profile[field] is None:
            continue
        value = profile[field]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValidationError(f"strategy_profile.{field} musi być liczbą")
        if value < 0:
            raise ValidationError(f"strategy_profile.{field} nie może być ujemne, otrzymano: {value}")
    for field in _BOOL_FIELDS:
        if field not in profile or profile[field] is None:
            continue
        if not isinstance(profile[field], bool):
            raise ValidationError(f"strategy_profile.{field} musi być bool")
    allowed_types = profile.get("allowed_asset_types")
    if allowed_types is not None:
        if not isinstance(allowed_types, list):
            raise ValidationError("strategy_profile.allowed_asset_types musi być listą")
        unknown = [t for t in allowed_types if t not in ASSET_TYPES]
        if unknown:
            raise ValidationError(
                f"strategy_profile.allowed_asset_types zawiera nieznane typy: {unknown}. "
                f"Dostępne: {list(ASSET_TYPES)}"
            )
    allowed_markets = profile.get("allowed_markets")
    if allowed_markets is not None and not isinstance(allowed_markets, list):
        raise ValidationError("strategy_profile.allowed_markets musi być listą")
    horizon = profile.get("horizon")
    if horizon is not None and not isinstance(horizon, str):
        raise ValidationError("strategy_profile.horizon musi być stringiem")


def validate_exchange_config(
    kind: str,
    exchange: Optional[str],
    execution_mode: Optional[str],
) -> None:
    """Walidacja spójności OKX (#65): kind/exchange/execution_mode.

    CHECK w SQLite nie potrafi wyrazić cross-column constraint, więc reguła
    żyje tu, egzekwowana w Pythonie (analogicznie do validate_strategy_profile).

    - exchange=None → execution_mode musi być None (portfel bez integracji giełdowej).
    - exchange='okx' + kind='real' → execution_mode musi być 'read_only'.
    - exchange='okx' + kind='game' → execution_mode musi być 'trading'.
    """
    if exchange is not None and exchange not in SUPPORTED_EXCHANGES:
        raise ValidationError(
            f"exchange musi być jednym z {sorted(SUPPORTED_EXCHANGES)} lub NULL, otrzymano: {exchange}"
        )
    if execution_mode is not None and execution_mode not in EXECUTION_MODE_VALUES:
        raise ValidationError(
            f"execution_mode musi być jednym z {sorted(EXECUTION_MODE_VALUES)} lub NULL, otrzymano: {execution_mode}"
        )
    if exchange is None:
        if execution_mode is not None:
            raise ValidationError(
                "execution_mode musi być NULL gdy exchange nie jest ustawiony"
            )
        return
    if execution_mode is None:
        raise ValidationError(
            f"execution_mode jest wymagany gdy exchange='{exchange}'"
        )
    if exchange == "okx":
        if kind == "real" and execution_mode != "read_only":
            raise ValidationError(
                "portfel real z exchange=okx wymaga execution_mode='read_only'"
            )
        if kind == "game" and execution_mode != "trading":
            raise ValidationError(
                "portfel game z exchange=okx wymaga execution_mode='trading'"
            )


def create_portfolio(
    player_id: int,
    name: str,
    starting_capital: float,
    kind: str = "game",
    base_currency: str = "PLN",
    mandate_md: str = "",
    strategy_profile: Optional[dict] = None,
    preset: Optional[str] = None,
    managed_by: Optional[str] = None,
    accent_color: Optional[str] = None,
    exchange: Optional[str] = None,
    execution_mode: Optional[str] = None,
    exchange_credential_alias: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    if kind not in ("game", "real"):
        raise ValidationError(f"kind musi być game/real, otrzymano: {kind}")
    if isinstance(starting_capital, bool) or not isinstance(starting_capital, (int, float)):
        raise ValidationError("starting_capital musi być liczbą")
    if starting_capital < 0:
        raise ValidationError(f"starting_capital nie może być ujemny, otrzymano: {starting_capital}")
    if preset:
        if preset not in MANDATE_PRESETS:
            raise ValidationError(f"nieznany preset mandatu: {preset}. Dostępne: {list(MANDATE_PRESETS)}")
        preset_data = MANDATE_PRESETS[preset]
        mandate_md = mandate_md or preset_data["mandate_md"]
        strategy_profile = strategy_profile or preset_data["strategy_profile"]
    if strategy_profile is None:
        strategy_profile = DEFAULT_STRATEGY_PROFILE
    validate_strategy_profile(strategy_profile)
    validate_exchange_config(kind, exchange, execution_mode)

    own = conn is None
    conn = conn or get_conn()
    try:
        if managed_by is None:
            player_row = conn.execute("SELECT type FROM players WHERE id=?", (player_id,)).fetchone()
            managed_by = "user" if player_row and player_row["type"] == "human" else "ai"
        if managed_by not in MANAGED_BY_VALUES:
            raise ValidationError(f"managed_by musi być user/ai, otrzymano: {managed_by}")
        color = normalize_accent_color(accent_color)
        if color is None:
            color = default_accent_for_name(name)
        cur = conn.execute(
            "INSERT INTO portfolios (player_id, name, base_currency, starting_capital, kind, managed_by, "
            "mandate_md, strategy_profile, accent_color, exchange, execution_mode, exchange_credential_alias) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                player_id, name, base_currency, starting_capital, kind, managed_by,
                mandate_md, json.dumps(strategy_profile, ensure_ascii=False), color,
                exchange, execution_mode, exchange_credential_alias,
            ),
        )
        if own:
            conn.commit()
        return get_portfolio_row(cur.lastrowid, conn)
    finally:
        if own:
            conn.close()


def archive_portfolio(portfolio_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Deprecated soft-delete — preferuj delete_portfolio (hard delete)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        conn.execute("UPDATE portfolios SET archived=1 WHERE id=?", (portfolio_id,))
        conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def _table_has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    return column in cols


def delete_portfolio(portfolio_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Hard delete portfela + kaskada tx/cash/snapshots/(reco jeśli portfolio_id).

    Usuwa też auto-wpisy dziennika „Zmiana mandatu portfela #N …”.
    Nie dotyka historia.db.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            pf = get_portfolio_row(portfolio_id, conn)
            player_id = int(pf["player_id"])
            conn.execute("DELETE FROM transactions WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM cash_entries WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM snapshots WHERE portfolio_id=?", (portfolio_id,))
            conn.execute("DELETE FROM portfolio_positions WHERE portfolio_id=?", (portfolio_id,))
            if _table_has_column(conn, "recommendations", "portfolio_id"):
                conn.execute("DELETE FROM recommendations WHERE portfolio_id=?", (portfolio_id,))
            conn.execute(
                "DELETE FROM rounds WHERE summary_md LIKE ?",
                (f"Zmiana mandatu portfela #{portfolio_id} %",),
            )
            conn.execute("DELETE FROM portfolios WHERE id=?", (portfolio_id,))
            remaining = conn.execute(
                "SELECT COUNT(*) AS c FROM portfolios WHERE player_id=?",
                (player_id,),
            ).fetchone()["c"]
            player_deleted = int(remaining) == 0
            if player_deleted:
                conn.execute("DELETE FROM players WHERE id=?", (player_id,))
        except BaseException:
            conn.rollback()
            raise
        conn.commit()
        return {
            "deleted": True,
            "id": portfolio_id,
            "name": pf["name"],
            "playerId": player_id,
            "playerDeleted": player_deleted,
        }
    finally:
        if own:
            conn.close()


def purge_archived_portfolios(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Jednorazowe twarde usunięcie wszystkich portfeli z archived=1."""
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            "SELECT id FROM portfolios WHERE archived=1 ORDER BY id"
        ).fetchall()
        deleted = []
        for row in rows:
            deleted.append(delete_portfolio(int(row["id"]), conn))
        return deleted
    finally:
        if own:
            conn.close()


def set_managed_by(
    portfolio_id: int,
    managed_by: str,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Uwaga: gdy `conn` przekazane (np. z `patch_portfolio_fields`), NIE commituje —
    wołający odpowiada za commit/rollback całej operacji nadrzędnej."""
    if managed_by not in MANAGED_BY_VALUES:
        raise ValidationError(f"managed_by musi być user/ai, otrzymano: {managed_by}")
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        conn.execute("UPDATE portfolios SET managed_by=? WHERE id=?", (managed_by, portfolio_id))
        if own:
            conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def set_exchange_config(
    portfolio_id: int,
    exchange: Optional[str],
    execution_mode: Optional[str],
    exchange_credential_alias: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Ustawia konfigurację OKX portfela (#65). Nie dotyka sync pól
    (exchange_account_id/last_synced_at/sync_status) — te są domeną #66-68.
    Uwaga: gdy `conn` przekazane, NIE commituje — patrz `set_managed_by`."""
    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        validate_exchange_config(pf["kind"], exchange, execution_mode)
        conn.execute(
            "UPDATE portfolios SET exchange=?, execution_mode=?, exchange_credential_alias=? WHERE id=?",
            (exchange, execution_mode, exchange_credential_alias, portfolio_id),
        )
        if own:
            conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def set_accent_color(
    portfolio_id: int,
    accent_color: Optional[str],
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Uwaga: gdy `conn` przekazane, NIE commituje — patrz `set_managed_by`."""
    color = normalize_accent_color(accent_color)
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        conn.execute("UPDATE portfolios SET accent_color=? WHERE id=?", (color, portfolio_id))
        if own:
            conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def set_portfolio_name(
    portfolio_id: int,
    name: str,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Uwaga: gdy `conn` przekazane, NIE commituje — patrz `set_managed_by`."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise ValidationError("name nie może być puste")
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        conn.execute("UPDATE portfolios SET name=? WHERE id=?", (cleaned, portfolio_id))
        if own:
            conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def patch_portfolio_fields(
    portfolio_id: int,
    name: Optional[str] = None,
    managed_by: Optional[str] = None,
    accent_color: Optional[str] = None,
    accent_color_set: bool = False,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """PATCH /api/portfolios/{id}: aplikuje name/managed_by/accent_color w JEDNEJ
    transakcji (TASK_026-C) — błąd walidacji na drugim/trzecim polu (np. złe
    managed_by po poprawnej nazwie) nie może zostawić wcześniej zmienionej nazwy
    ani koloru. `accent_color_set` odróżnia "pole nieobecne w body" od
    "accent_color=None" (czyszczenie koloru), zgodnie z model_fields_set w main.py.
    Zawsze granica transakcji, patrz `create_player_with_portfolio`."""
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            if name is not None:
                set_portfolio_name(portfolio_id, name, conn=conn)
            if managed_by is not None:
                set_managed_by(portfolio_id, managed_by, conn=conn)
            if accent_color_set:
                set_accent_color(portfolio_id, accent_color, conn=conn)
        except Exception:
            conn.rollback()
            raise
        conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def get_portfolio_row(portfolio_id: int, conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM portfolios WHERE id=?", (portfolio_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"portfolio {portfolio_id} nie istnieje")
    d = dict(row)
    d["strategy_profile"] = json.loads(d["strategy_profile"])
    return d


def get_mandate(portfolio_id: int, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Sam mandat portfela — obowiązkowy pierwszy krok rundy agenta (SPEC-V2 §7)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        return {
            "portfolioId": pf["id"],
            "name": pf["name"],
            "mandate_md": pf["mandate_md"],
            "strategy_profile": pf["strategy_profile"],
            "mandate_updated_at": pf.get("mandate_updated_at"),
        }
    finally:
        if own:
            conn.close()


def update_mandate(
    portfolio_id: int,
    mandate_md: Optional[str] = None,
    strategy_profile: Optional[dict] = None,
    author: str = "system",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        new_mandate_md = mandate_md if mandate_md is not None else pf["mandate_md"]
        new_profile = strategy_profile if strategy_profile is not None else pf["strategy_profile"]
        validate_strategy_profile(new_profile)
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "UPDATE portfolios SET mandate_md=?, strategy_profile=?, mandate_updated_at=? WHERE id=?",
            (new_mandate_md, json.dumps(new_profile, ensure_ascii=False), now, portfolio_id),
        )
        conn.execute(
            "INSERT INTO rounds (date, summary_md, author) VALUES (?, ?, ?)",
            (
                now[:10],
                f"Zmiana mandatu portfela #{portfolio_id} ({pf['name']}).\n\n"
                f"Nowy mandate_md:\n{new_mandate_md}\n\nNowy strategy_profile: {json.dumps(new_profile, ensure_ascii=False)}",
                author,
            ),
        )
        conn.commit()
        return get_portfolio_row(portfolio_id, conn)
    finally:
        if own:
            conn.close()


def list_portfolios(conn: Optional[sqlite3.Connection] = None, include_archived: bool = False) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        q = "SELECT * FROM portfolios"
        if not include_archived:
            q += " WHERE archived=0"
        rows = conn.execute(q).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["strategy_profile"] = json.loads(d["strategy_profile"])
            out.append(d)
        return out
    finally:
        if own:
            conn.close()


# ---------- FIFO positions ----------

def compute_positions_fifo(
    portfolio_id: int,
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
    strict: bool = False,
) -> dict[str, list[list]]:
    """Zwraca {symbol: [[qty, cost_pln, price_native, currency], ...]} lotów FIFO.

    cost_pln z value_pln (już PLN) — bez ponownego FX. price_native/currency z tx.
    as_of=YYYY-MM-DD → tylko transakcje do tego dnia włącznie.

    strict=True (TASK_026-B): gdy SELL w replayowanej historii przekracza dostępne
    loty (dane w DB niespójne — np. ręczna edycja, uszkodzony import), zgłasza
    FifoIntegrityError z symbolem/datą/brakującą ilością zamiast po cichu ucinać
    SELL do dostępnej ilości (`remaining` po prostu zostawało niedopasowane).
    Domyślnie False — wycena/raportowanie (get_portfolio_valuation i in.) ma
    pozostać odporne na best-effort odczyt istniejących danych.
    """
    if as_of:
        rows = conn.execute(
            "SELECT symbol, side, qty, value_pln, price, currency, datetime FROM transactions "
            "WHERE portfolio_id=? AND substr(datetime,1,10)<=? ORDER BY datetime, id",
            (portfolio_id, as_of),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT symbol, side, qty, value_pln, price, currency, datetime FROM transactions "
            "WHERE portfolio_id=? ORDER BY datetime, id",
            (portfolio_id,),
        ).fetchall()
    positions: dict[str, list[list]] = {}
    for row in rows:
        lots = positions.setdefault(row["symbol"], [])
        unit_cost = row["value_pln"] / row["qty"] if row["qty"] else 0
        price_native = float(row["price"] or 0)
        currency = (row["currency"] or currency_for_symbol(row["symbol"]) or "PLN").upper()
        if row["side"] == "BUY":
            lots.append([row["qty"], unit_cost, price_native, currency])
        else:
            remaining = row["qty"]
            while remaining > 1e-9 and lots:
                matched = min(remaining, lots[0][0])
                lots[0][0] -= matched
                remaining -= matched
                if lots[0][0] <= 1e-9:
                    lots.pop(0)
            if strict and remaining > 1e-9:
                raise FifoIntegrityError(
                    f"integralność FIFO złamana: SELL {row['qty']} {row['symbol']} "
                    f"({row['datetime']}) nie ma pełnego pokrycia w lotach BUY — "
                    f"brakuje {remaining:.6f}"
                )
    return {
        sym: lots
        for sym, lots in positions.items()
        if sum(q for q, *_ in lots) > 1e-9
    }


def _fx_factor_to_pln(
    currency: str, fx_usdpln: float, as_of: Optional[str] = None
) -> Optional[float]:
    """Mnożnik native→PLN (#4). USD używa fx_usdpln już wczytanego przez wołającego
    (spójność z resztą wyceny); inne waluty (GBP, …) mają własny provider w
    pricing.fx_rate_for_currency. None gdy brak kursu — NIE zgadujemy 1:1."""
    c = (currency or "PLN").upper()
    if c == "PLN":
        return 1.0
    if c == "USD":
        if as_of:
            rate, _ = load_fx_as_of(as_of)
            return rate
        return fx_usdpln
    rate, _date = fx_rate_for_currency(c, as_of=as_of)
    return rate


def _fifo_cost_basis_pln(lots: list[list]) -> float:
    """Suma kosztu pozostałych lotów FIFO (value_pln z ledgera, bez ponownego FX)."""
    return sum(q * c for q, c, *_ in lots)


def _previous_trading_session_close(
    symbol: str, current_date: Optional[str]
) -> tuple[Optional[float], Optional[str]]:
    """Close i data sesji bezpośrednio poprzedzającej current_date w serii EOD."""
    if not current_date:
        return None, None
    dates, closes = load_price_series(symbol)
    if not dates:
        return None, None
    i = bisect_right(dates, current_date) - 1
    if i <= 0:
        return None, None
    return closes[i - 1], dates[i - 1]


def _round_money(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return float(_money(Decimal(str(value))))


def _round_pct(value: Optional[float]) -> Optional[float]:
    if value is None:
        return None
    return round(value, 2)


def _compute_position_performance_metrics(
    *,
    symbol: str,
    quantity: float,
    fifo_lots: list[list],
    current_close_native: Optional[float],
    current_date: Optional[str],
    market_value_pln: Optional[float],
    fx_usdpln: float,
    cached_change_pct_1d: Optional[float] = None,
) -> dict[str, Any]:
    """Metryki pozycji (#54 etap 2): unrealized + day change sesyjny.

    cached_change_pct_1d: gdy podane (z prices.change_pct_1d, patrz
    get_latest_cached_price), uzywane wprost jako dayChangePct zamiast
    przeliczania z pelnej serii EOD — czyta z tego samego wzoru, tylko
    juz policzonego przy ostatnim refresh_portfolio_prices. dayChangeCurrency/
    dayPnlPln/priceChangeCurrency/fxChangePln nadal licza sie na zywo (wymagaja
    per-share/FX rozbicia, ktorego cache nie przechowuje)."""
    quote_currency = currency_for_symbol(symbol)
    cost_basis_pln = _round_money(_fifo_cost_basis_pln(fifo_lots))

    unrealized_pnl = None
    unrealized_return = None
    if market_value_pln is not None and cost_basis_pln is not None:
        unrealized_pnl = _round_money(market_value_pln - cost_basis_pln)
        if cost_basis_pln:
            unrealized_return = _round_pct((market_value_pln / cost_basis_pln - 1) * 100)

    incomplete: list[str] = []
    day_change_currency = None
    day_change_pct = cached_change_pct_1d
    day_pnl_pln = None
    price_change_currency = None
    fx_change_pln = None

    if current_close_native is not None and current_date:
        prev_close, prev_date = _previous_trading_session_close(symbol, current_date)
        if prev_close is None or prev_date is None:
            incomplete.append("missing_previous_close")
        else:
            fx_current = _fx_factor_to_pln(quote_currency, fx_usdpln)
            fx_prev = _fx_factor_to_pln(quote_currency, fx_usdpln, as_of=prev_date)
            if quote_currency != "PLN" and fx_prev is None:
                incomplete.append("missing_previous_fx")
            if quote_currency != "PLN" and fx_current is None:
                incomplete.append("missing_fx")

            if not incomplete:
                per_share_change = current_close_native - prev_close
                day_change_currency = _round_money(quantity * per_share_change)
                if cached_change_pct_1d is None:
                    day_change_pct = _round_pct((current_close_native / prev_close - 1) * 100)

                if quote_currency == "PLN":
                    day_pnl_pln = day_change_currency
                    price_change_currency = day_change_currency
                    fx_change_pln = 0.0
                else:
                    price_change_currency = day_change_currency
                    price_impact_pln = _round_money(
                        quantity * per_share_change * fx_prev
                    )
                    day_pnl_pln = _round_money(
                        quantity
                        * (
                            current_close_native * fx_current
                            - prev_close * fx_prev
                        )
                    )
                    fx_change_pln = _round_money(
                        (day_pnl_pln or 0) - (price_impact_pln or 0)
                    )
    elif current_close_native is None:
        incomplete.append("missing_current_close")

    complete = len(incomplete) == 0

    return {
        "costBasisPln": cost_basis_pln,
        "marketValuePln": market_value_pln,
        "unrealizedPnlPln": unrealized_pnl,
        "unrealizedReturnPct": unrealized_return,
        "dayChangeCurrency": day_change_currency if complete else None,
        "dayChangePct": day_change_pct if complete else None,
        "dayPnlPln": day_pnl_pln if complete else None,
        "priceChangeCurrency": price_change_currency if complete else None,
        "fxChangePln": fx_change_pln if complete else None,
        "complete": complete,
        "incompleteReasons": incomplete,
    }


def cash_method_bossa(profile: Optional[dict]) -> bool:
    """BOSSA (IKZE_ZONA): cash = SUM(wpłaty) − Σ BUY + Σ SELL + SUM(dywidendy) (SCHEMA.md
    "Kontrakt liczenia cash"), rekonstrukcja bez polegania na blokada/odblokowanie/
    rozliczenie_transakcji z importu brokera."""
    if not profile:
        return False
    return profile.get("cash_method") == "bossa"


def cash_from_cashflow_only(profile: Optional[dict]) -> bool:
    """Real seeded (IKE/IKZE/…): gotówka = SUM(cash_entries) jak SUM(cashflow) brokera (#25).

    Jawna wartość `cash_from_cashflow_only: false` w strategy_profile NADPISUJE domyślne
    zachowanie wynikające z source='historia.db' (#41 pkt 3) — klucz musi być odczytany
    najpierw, zanim spadniemy na fallback po `source`.
    """
    if not profile:
        return False
    if cash_method_bossa(profile):
        return False
    if "cash_from_cashflow_only" in profile:
        return bool(profile.get("cash_from_cashflow_only"))
    return profile.get("source") == "historia.db"


def compute_cash(portfolio_id: int, conn: sqlite3.Connection, as_of: Optional[str] = None) -> float:
    """Gotówka portfela. as_of=YYYY-MM-DD → tylko tx/cash_entries do tego dnia włącznie.

    Trzy formuły (SCHEMA.md "Kontrakt liczenia cash"), wybierane przez strategy_profile:
    - cash_method=bossa (IKZE_ZONA): SUM(wpłaty) − Σ BUY + Σ SELL + SUM(dywidendy) —
      rekonstrukcja bez polegania na blokada/odblokowanie/rozliczenie_transakcji.
    - cash_from_cashflow_only (IKE, IKZE): wyłącznie SUM(cash_entries) — tx służą FIFO
      pozycji, nie ledgerowi cash.
    - domyślna (gra AI): SUM(cash_entries) − Σ BUY + Σ SELL (#51: starting_capital
      NIE jest już traktowany jako gotówka dostępna — środki powstają wyłącznie
      przez jawną wpłatę/cash_entry).
    """
    pf = get_portfolio_row(portfolio_id, conn)
    profile = pf.get("strategy_profile")

    if as_of:
        tx_rows = conn.execute(
            "SELECT side, value_pln, cash_effect_pln FROM transactions WHERE portfolio_id=? AND substr(datetime,1,10)<=?",
            (portfolio_id, as_of),
        ).fetchall()
    else:
        tx_rows = conn.execute(
            "SELECT side, value_pln, cash_effect_pln FROM transactions WHERE portfolio_id=?", (portfolio_id,)
        ).fetchall()
    cash_effect = sum(
        float(r["cash_effect_pln"])
        if r["cash_effect_pln"] is not None
        else (-float(r["value_pln"]) if r["side"] == "BUY" else float(r["value_pln"]))
        for r in tx_rows
    )

    if cash_method_bossa(profile):
        params: tuple = (portfolio_id,)
        date_filter = ""
        if as_of:
            date_filter = " AND date<=?"
            params = (portfolio_id, as_of)
        public_cashflow = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM cash_entries "
            "WHERE portfolio_id=? AND type IN "
            "('wplata','wyplata','dywidenda','odsetki','podatek')" + date_filter,
            params,
        ).fetchone()["s"]
        return float(public_cashflow) + cash_effect

    if as_of:
        cash_rows = conn.execute(
            "SELECT amount FROM cash_entries WHERE portfolio_id=? AND date<=?",
            (portfolio_id, as_of),
        ).fetchall()
    else:
        cash_rows = conn.execute(
            "SELECT amount FROM cash_entries WHERE portfolio_id=?", (portfolio_id,)
        ).fetchall()
    ce_sum = sum(float(row["amount"]) for row in cash_rows)

    if cash_from_cashflow_only(profile):
        return ce_sum

    # #51: starting_capital usunięty z formuły — gotówka pochodzi wyłącznie
    # z jawnych cash_entries (wpłat) i transakcji, nigdy z pola informacyjnego.
    return ce_sum + cash_effect


def compute_deposits(portfolio_id: int, conn: sqlite3.Connection, as_of: Optional[str] = None) -> Optional[float]:
    """Znany kapitał wpłacony z ledgera (#41 pkt 1): SUM(cash_entries.type='wplata').

    #51: jedyna podstawa dla deposits/returnPct — starting_capital nie jest już
    używany jako fallback (niezależnie od jego wartości w DB).
    Wypłaty NIE są odejmowane od historycznego kapitału wpłaconego (spec #41 pkt 1) —
    deposits to metryka "ile łącznie wpłacono", nie "ile zostało po wypłatach"
    (to drugie już wyraża cashPln / resultVsDeposits).
    """
    if as_of:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM cash_entries "
            "WHERE portfolio_id=? AND type='wplata' AND date<=?",
            (portfolio_id, as_of),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COALESCE(SUM(amount),0) AS s FROM cash_entries "
            "WHERE portfolio_id=? AND type='wplata'",
            (portfolio_id,),
        ).fetchone()
    return round(float(row["s"] or 0.0), 2)


def compute_cash_operation_metrics(
    portfolio_id: int,
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> dict:
    """Kontrolne fakty cashflow; bez końcowego TWR/portfolioReturnPct (#54)."""
    params: list[Any] = [portfolio_id]
    date_filter = ""
    if as_of:
        date_filter = " AND date<=?"
        params.append(as_of)
    rows = conn.execute(
        "SELECT type, COALESCE(SUM(amount), 0) AS amount "
        "FROM cash_entries WHERE portfolio_id=? "
        "AND type IN ('wplata','wyplata','dywidenda','odsetki','podatek')"
        + date_filter
        + " GROUP BY type",
        tuple(params),
    ).fetchall()
    totals = {row["type"]: float(row["amount"] or 0.0) for row in rows}
    gross_deposits = totals.get("wplata", 0.0)
    gross_withdrawals = abs(totals.get("wyplata", 0.0))
    dividend_income = totals.get("dywidenda", 0.0)
    interest_income = totals.get("odsetki", 0.0)
    taxes = abs(totals.get("podatek", 0.0))
    return {
        "grossDeposits": round(gross_deposits, 2),
        "grossWithdrawals": round(gross_withdrawals, 2),
        "netContributions": round(gross_deposits - gross_withdrawals, 2),
        "dividendIncome": round(dividend_income, 2),
        "interestIncome": round(interest_income, 2),
        "taxes": round(taxes, 2),
        "investmentIncome": round(dividend_income + interest_income - taxes, 2),
    }


def _compute_return_pct(
    total_value: float, deposits: Optional[float]
) -> Optional[float]:
    """Zwrot %: wyłącznie vs deposits (#51 — starting_capital NIE jest już bazą
    ani fallbackiem, niezależnie od jego wartości w DB). None gdy brak wpłat/bazy
    porównawczej (deposits=0/None) — nigdy 0% na zgadywanie."""
    if deposits:
        return round((total_value / deposits - 1) * 100, 2)
    return None


SETTLEMENT_WINDOW_DAYS = 5  # T+2 realny, +margines na weekendy/święta (#32)


def compute_settlement_receivable(
    portfolio_id: int, conn: sqlite3.Connection, as_of: Optional[str] = None
) -> float:
    """Wartość sprzedaży giełdowych oczekujących na rozliczenie T+2 (#32).

    Broker (eMakler) rozlicza gotówkę ze sprzedaży 2 dni po transakcji —
    FIFO usuwa pozycję z portfela w dniu transakcji, ale compute_cash nie
    widzi tej gotówki do dnia rozliczenia (cash_entries.type=rozliczenie_transakcji).
    Bez tego wykres wartości portfela pokazuje sztuczny dołek na te 1-2 dni.

    Heurystyka czasowa (nie dopasowanie 1:1 po ID zlecenia — transactions i
    cash_entries nie mają wspólnego klucza): SUM(SELL.value_pln) z ostatnich
    SETTLEMENT_WINDOW_DAYS dni minus SUM(rozliczenie_transakcji) z tego samego
    okna, nie mniej niż 0 (rozliczenia niepowiązane ze sprzedażą w oknie, np.
    korekty, nie powinny obniżać należności poniżej zera).

    Dodatkowo odejmuje SUM(BUY.value_pln) zawartych w oknie [pierwszy SELL w
    oknie, ref_date]: gdy środki ze sprzedaży są od razu reinwestowane (typowe
    u tego usera — sprzedaż i zakup tego samego dnia), broker jeszcze nie
    zaksięgował rozliczenia, ale gotówka nie jest już "w drodze" — jest w
    nowej pozycji, którą już liczy compute_positions_fifo. Bez tego odjęcia ta
    sama kwota wchodziła do NAV podwójnie: raz jako receivable, raz jako
    wartość nowego BUY (obserwowane jako sztuczny skok ~8000 zł na wykresie
    portfela 11, sprzedaż SYNEKTIK → zakup COLUMBUS tego samego dnia
    2026-07-13). Okno liczone OD pierwszego SELL w tym oknie (nie od
    window_start) — BUY sprzed sprzedaży to zwykłe otwarcie pozycji
    finansowane inną gotówką (np. wcześniejszą wpłatą), nie reinwestycja.
    """
    ref_date = date_cls.fromisoformat(as_of) if as_of else date_cls.today()
    window_start = (ref_date - timedelta(days=SETTLEMENT_WINDOW_DAYS)).isoformat()
    ref_str = ref_date.isoformat()

    sell_row = conn.execute(
        "SELECT COALESCE(SUM(value_pln), 0) AS s, MIN(substr(datetime,1,10)) AS first_sell "
        "FROM transactions WHERE portfolio_id=? AND side='SELL' "
        "AND substr(datetime,1,10) BETWEEN ? AND ?",
        (portfolio_id, window_start, ref_str),
    ).fetchone()
    sell_sum = sell_row["s"]
    first_sell_date = sell_row["first_sell"]
    settled_sum = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_entries "
        "WHERE portfolio_id=? AND type='rozliczenie_transakcji' AND date BETWEEN ? AND ?",
        (portfolio_id, window_start, ref_str),
    ).fetchone()["s"]
    reinvested_sum = 0.0
    if first_sell_date is not None:
        reinvested_sum = conn.execute(
            "SELECT COALESCE(SUM(value_pln), 0) AS s FROM transactions "
            "WHERE portfolio_id=? AND side='BUY' AND substr(datetime,1,10) BETWEEN ? AND ?",
            (portfolio_id, first_sell_date, ref_str),
        ).fetchone()["s"]
    settled_sum = float(settled_sum) + float(reinvested_sum)
    return max(0.0, float(sell_sum) - float(settled_sum))


PENDING_BUY_MATCH_DAYS = 2  # okno blokada → faktyczna realizacja BUY (#34)
PENDING_BUY_MATCH_TOLERANCE = 0.25  # 25% — blokada ≠ wartość finalna (prowizja, kurs)


def compute_pending_purchase_value(
    portfolio_id: int, conn: sqlite3.Connection, as_of: Optional[str] = None
) -> float:
    """Wartość zleceń kupna zablokowanych, ale jeszcze niezrealizowanych jako pozycja (#34).

    Broker czasem blokuje środki pod zlecenie kupna (cash_entries.type=blokada)
    dzień przed faktycznym wykonaniem — transactions.BUY pojawia się z opóźnieniem,
    co daje sztuczny dołek: gotówka już zniknęła z compute_cash, pozycja jeszcze nie
    istnieje.

    Bezpieczniejsze niż liczenie samej blokady (ta bywa anulowana i nigdy nie
    staje się pozycją — "unieważnienie przez Rynek", "anulowania decyzją klienta"
    widoczne w danych): dopasowanie 1:1 blokada↔BUY po zbliżonej kwocie
    (PENDING_BUY_MATCH_TOLERANCE — blokada ≠ finalna wartość z prowizją/kursem),
    zachłannie od najlepszego dopasowania, każdy BUY zużywany co najwyżej raz
    (inaczej kilka blokad tego samego dnia dopasowałoby się do jednej transakcji
    i zawyżyłoby wartość wielokrotnie). Dolicza się wartość transakcji BUY, nie
    blokady, i tylko dla dni od blokady (włącznie) do dnia przed realizacją —
    po realizacji pozycję liczy już compute_positions_fifo, nie dublować.
    """
    ref_date = date_cls.fromisoformat(as_of) if as_of else date_cls.today()
    ref_str = ref_date.isoformat()
    earliest_block = (ref_date - timedelta(days=PENDING_BUY_MATCH_DAYS)).isoformat()
    latest_buy = (ref_date + timedelta(days=PENDING_BUY_MATCH_DAYS)).isoformat()

    blocks = conn.execute(
        "SELECT id, date, -amount AS blocked FROM cash_entries "
        "WHERE portfolio_id=? AND type='blokada' AND date >= ? AND date <= ?",
        (portfolio_id, earliest_block, ref_str),
    ).fetchall()
    if not blocks:
        return 0.0

    candidate_buys = conn.execute(
        "SELECT id, value_pln, substr(datetime,1,10) AS d FROM transactions "
        "WHERE portfolio_id=? AND side='BUY' AND substr(datetime,1,10) >= ? "
        "AND substr(datetime,1,10) <= ?",
        (portfolio_id, earliest_block, latest_buy),
    ).fetchall()
    if not candidate_buys:
        return 0.0

    # Wszystkie pary (blokada, buy) w tolerancji, posortowane od najlepszego dopasowania.
    # Dopasowanie musi być 1:1 na oba końce (#41 pkt 8) — jedna blokada nie może pokryć
    # kilku BUY-ów i jeden BUY nie może być pokryty przez kilka blokad; inaczej dwa
    # podobne BUY po jednej blokadzie zdublowałyby wartość pending purchase.
    pairs = []
    for b in blocks:
        block_id = b["id"]
        block_date = b["date"]
        blocked_amount = float(b["blocked"])
        for buy in candidate_buys:
            if buy["d"] <= block_date:
                continue  # BUY musi być PO blokadzie
            diff = abs(float(buy["value_pln"]) - blocked_amount)
            if diff <= blocked_amount * PENDING_BUY_MATCH_TOLERANCE:
                pairs.append((diff, block_id, block_date, buy["id"], float(buy["value_pln"]), buy["d"]))
    pairs.sort(key=lambda p: p[0])

    used_buy_ids: set = set()
    used_block_ids: set = set()
    total = 0.0
    for _diff, block_id, block_date, buy_id, buy_value, buy_date in pairs:
        if buy_id in used_buy_ids or block_id in used_block_ids:
            continue
        used_buy_ids.add(buy_id)
        used_block_ids.add(block_id)
        if block_date <= ref_str < buy_date:
            total += buy_value
    return total


def list_cash_entries(portfolio_id: int, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        rows = conn.execute(
            "SELECT * FROM cash_entries WHERE portfolio_id=? ORDER BY date DESC, id DESC",
            (portfolio_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def list_cash_operations(
    portfolio_id: int,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    type_: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Publiczny ledger w deterministycznej kolejności date,id + podsumowanie."""
    if type_ is not None and type_ not in PUBLIC_CASH_OPERATION_TYPES:
        raise ValidationError(
            f"type musi być jednym z {sorted(PUBLIC_CASH_OPERATION_TYPES)}"
        )
    for field, value in (("date_from", date_from), ("date_to", date_to)):
        if value is not None:
            _validate_cash_operation_date(value, field)
    if date_from and date_to and date_from > date_to:
        raise ValidationError("date_from nie może być późniejsze niż date_to")

    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        if pf.get("archived"):
            raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")
        clauses = [
            "portfolio_id=?",
            "type IN ('wplata','wyplata','dywidenda','odsetki','podatek')",
        ]
        params: list[Any] = [portfolio_id]
        if date_from:
            clauses.append("date>=?")
            params.append(date_from)
        if date_to:
            clauses.append("date<=?")
            params.append(date_to)
        if type_:
            clauses.append("type=?")
            params.append(type_)
        rows = conn.execute(
            "SELECT * FROM cash_entries WHERE " + " AND ".join(clauses)
            + " ORDER BY date ASC, id ASC",
            tuple(params),
        ).fetchall()
        transaction_count = conn.execute(
            "SELECT COUNT(*) AS c FROM transactions WHERE portfolio_id=?",
            (portfolio_id,),
        ).fetchone()["c"]
        position_count = len(compute_positions_fifo(portfolio_id, conn))
        return {
            "portfolioId": portfolio_id,
            "currency": pf["base_currency"],
            "operations": [dict(row) for row in rows],
            "cash": round(compute_cash(portfolio_id, conn), 2),
            "transactionCount": int(transaction_count),
            "positionCount": int(position_count),
            **compute_cash_operation_metrics(portfolio_id, conn),
        }
    finally:
        if own:
            conn.close()


def _validate_cash_operation_date(value: Any, field: str = "date") -> str:
    raw = str(value or "").strip()
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValidationError(f"{field} musi mieć format YYYY-MM-DD") from exc
    if parsed.isoformat() != raw:
        raise ValidationError(f"{field} musi mieć format YYYY-MM-DD")
    return raw


def _cash_operation_amount(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError("amount musi być dodatnią kwotą")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError("amount musi być dodatnią kwotą") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValidationError("amount musi być większe od 0")
    if amount.as_tuple().exponent < -2:
        raise ValidationError("amount może mieć maksymalnie dwa miejsca po przecinku")
    return amount.quantize(Decimal("0.01"))


def _cash_operation_result(
    entry: dict,
    portfolio_id: int,
    currency: str,
    conn: sqlite3.Connection,
    replayed: bool,
) -> dict:
    return {
        "operation": entry,
        "idempotent_replay": replayed,
        "cash_before": round(float(entry["cash_before"]), 2),
        "cash_after": round(float(entry["cash_after"]), 2),
        "currency": currency,
        **compute_cash_operation_metrics(portfolio_id, conn),
    }


def add_cash_operation(
    portfolio_id: int,
    type_: str,
    amount: Any,
    date: str,
    idempotency_key: str,
    note: str = "",
    currency: Optional[str] = None,
    source: str = "api",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Atomowa, idempotentna mutacja publicznego ledgera gotówkowego (#52)."""
    if type_ not in PUBLIC_CASH_OPERATION_TYPES:
        raise ValidationError(
            f"type musi być jednym z {sorted(PUBLIC_CASH_OPERATION_TYPES)}"
        )
    operation_date = _validate_cash_operation_date(date)
    public_amount = _cash_operation_amount(amount)
    key = str(idempotency_key or "").strip()
    if not key:
        raise ValidationError("idempotency_key jest wymagany")
    if len(key) > 255:
        raise ValidationError("idempotency_key może mieć maksymalnie 255 znaków")
    normalized_note = str(note or "")
    if len(normalized_note) > 2000:
        raise ValidationError("note może mieć maksymalnie 2000 znaków")
    if source not in {"api", "mcp", "ui"}:
        raise ValidationError("nieprawidłowe source operacji")

    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            pf = get_portfolio_row(portfolio_id, conn)
            if pf.get("archived"):
                raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")
            # #76: wyjątek od #67 dla portfeli real+okx (read_only) — wplata/wyplata
            # to WYŁĄCZNIE lokalny, audytowy zapis służący do liczenia zysku względem
            # wpłat (resultVsDeposits w get_portfolio_valuation, patrz compute_deposits).
            # Nie zasila salda portfela — saldo dalej pochodzi tylko z syncu z giełdą.
            # Pozostałe typy (dywidenda/odsetki/podatek/blokada/odblokowanie/
            # rozliczenie_transakcji) sugerowałyby normalny ledger transakcyjny i
            # zostają twardo zablokowane przez ten sam guard co execute_trade (#67).
            is_readonly_exchange = bool(
                pf.get("exchange") and pf.get("execution_mode") in READ_ONLY_EXECUTION_MODES
            )
            allow_audit_cash_entry = is_readonly_exchange and type_ in {"wplata", "wyplata"}
            if not allow_audit_cash_entry:
                assert_not_readonly_exchange_portfolio(pf, "add_cash_operation")
            effective_currency = str(currency or pf["base_currency"]).strip().upper()
            if effective_currency != str(pf["base_currency"]).upper():
                raise ValidationError(
                    f"currency musi być zgodne z base_currency={pf['base_currency']}; FX nie jest obsługiwane"
                )
            signed = -public_amount if type_ in {"wyplata", "podatek"} else public_amount
            existing_row = conn.execute(
                "SELECT * FROM cash_entries WHERE portfolio_id=? AND idempotency_key=?",
                (portfolio_id, key),
            ).fetchone()
            if existing_row is not None:
                existing = dict(existing_row)
                same = (
                    existing["type"] == type_
                    and existing["date"] == operation_date
                    and Decimal(str(existing["amount"])).quantize(Decimal("0.01")) == signed
                    and (existing.get("note") or "") == normalized_note
                    and (existing.get("currency") or effective_currency) == effective_currency
                )
                if not same:
                    raise IdempotencyConflictError(
                        "idempotency_key został już użyty z innymi parametrami"
                    )
                conn.commit()
                return _cash_operation_result(
                    existing, portfolio_id, effective_currency, conn, replayed=True
                )

            # #76: dla real+okx compute_cash() sumuje wyłącznie lokalny ledger
            # cash_entries, który NIE odzwierciedla prawdziwego salda na giełdzie
            # (to jest wyłącznie w snapshots z syncu — patrz get_portfolio_valuation).
            # Walidacja "przekracza gotówkę" byłaby więc myląca: wypłata audytowa
            # ma prawo przekroczyć sumę wcześniejszych lokalnych wpłat, bo to tylko
            # zapis faktu ("wypłaciłem X"), nie operacja na realnym saldzie.
            cash_before = round(compute_cash(portfolio_id, conn), 2)
            cash_after = round(cash_before + float(signed), 2)
            if not allow_audit_cash_entry and signed < 0 and cash_after < -1e-6:
                raise ValidationError(
                    f"operacja przekracza gotówkę: dostępne {cash_before:.2f} "
                    f"{effective_currency}, żądane {abs(float(signed)):.2f} {effective_currency}"
                )
            cur = conn.execute(
                "INSERT INTO cash_entries "
                "(portfolio_id, date, type, amount, note, currency, idempotency_key, "
                "source, cash_before, cash_after) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    portfolio_id,
                    operation_date,
                    type_,
                    float(signed),
                    normalized_note,
                    effective_currency,
                    key,
                    source,
                    cash_before,
                    cash_after,
                ),
            )
            entry = dict(
                conn.execute(
                    "SELECT * FROM cash_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        upsert_today_snapshot(portfolio_id, conn)
        return _cash_operation_result(
            entry, portfolio_id, effective_currency, conn, replayed=False
        )
    finally:
        if own:
            conn.close()

def assert_cash_entry_allowed(type_: str, profile: dict) -> None:
    """wplata/wyplata/odsetki/podatek → wymaga lokaty; dywidenda → equity lub lokaty;
    blokada/odblokowanie/rozliczenie_transakcji → equity (broker cashflow)."""
    if type_ == "dywidenda":
        allowed = resolve_allowed_asset_types(profile)
        if allowed is None:
            return
        if not any(t in DIVIDEND_ALLOWED_ASSET_TYPES for t in allowed):
            raise ValidationError(
                f"strategy_profile.allowed_asset_types={allowed} złamane: operacja "
                f"cash-entry dywidenda wymaga jednego z {sorted(DIVIDEND_ALLOWED_ASSET_TYPES)}"
            )
        return
    if type_ in BROKER_CASHFLOW_ENTRY_TYPES:
        allowed = resolve_allowed_asset_types(profile)
        if allowed is None:
            return
        if not any(t in DIVIDEND_ALLOWED_ASSET_TYPES for t in allowed):
            raise ValidationError(
                f"strategy_profile.allowed_asset_types={allowed} złamane: operacja "
                f"cash-entry {type_} wymaga jednego z {sorted(DIVIDEND_ALLOWED_ASSET_TYPES)}"
            )
        return
    assert_asset_type_allowed("lokaty", profile, op=f"cash-entry {type_}")


def add_cash_entry(
    portfolio_id: int,
    date: str,
    type_: str,
    amount: float,
    note: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Dodaje ruch gotówkowy. wyplata/podatek zapisujemy jako kwoty ujemne.

    dywidenda: kwota dodatnia, cash += amount; nie zmienia qty pozycji (tylko cash).
    Typy broker (blokada/…) — znak jak w cashflow (bez normalizacji).

    Walidacja dostępnej gotówki (dla ujemnych entries: wyplata/podatek/blokada ujemna)
    i INSERT wykonują się w jednej transakcji blokującej zapis (BEGIN IMMEDIATE,
    TASK_026-B) — dwie równoległe wypłaty na tę samą ostatnią dostępną gotówkę nie
    mogą obie przejść i dać ujemnego salda; druga czeka na blokadę i widzi już
    zaktualizowany stan po pierwszej.
    """
    if type_ not in CASH_ENTRY_TYPES:
        raise ValidationError(f"type musi być jednym z {sorted(CASH_ENTRY_TYPES)}")
    if not date or not str(date).strip():
        raise ValidationError("date jest wymagane")
    signed = float(amount)
    if type_ not in BROKER_CASHFLOW_ENTRY_TYPES:
        if type_ in ("wyplata", "podatek") and signed > 0:
            signed = -signed
        if type_ in ("wplata", "odsetki", "dywidenda") and signed < 0:
            signed = abs(signed)

    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            pf = get_portfolio_row(portfolio_id, conn)
            assert_cash_entry_allowed(type_, pf["strategy_profile"])
            cash = compute_cash(portfolio_id, conn)
            if signed < 0 and cash + signed < -1e-6:
                raise ValidationError(
                    f"wypłata przekracza gotówkę: dostępne {cash:.2f} PLN, żądane {abs(signed):.2f} PLN"
                )
            cur = conn.execute(
                "INSERT INTO cash_entries (portfolio_id, date, type, amount, note) VALUES (?,?,?,?,?)",
                (portfolio_id, date, type_, round(signed, 2), note or ""),
            )
            entry = dict(conn.execute("SELECT * FROM cash_entries WHERE id=?", (cur.lastrowid,)).fetchone())
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        upsert_today_snapshot(portfolio_id, conn)
        return entry
    finally:
        if own:
            conn.close()


def value_positions(
    positions: dict[str, list[list]],
    fx: float,
    as_of: Optional[str] = None,
) -> tuple[list[dict], float, float, list[str]]:
    """Wycenia loty FIFO. marketValue zawsze PLN; pola *Native obok dla UI.

    FX na cenę bieżącą bierze walutę notowania (Stooq / currency_for_symbol),
    nie currency z transakcji (BOSSA bywa PLN przy spółkach US).
    Koszt FIFO (avgCostPln) zawsze z value_pln — bez ponownego FX.
    as_of → close_as_of zamiast last_close.

    Zwraca też `incomplete_symbols` (#41 pkt 5) — pozycje bez ceny lub bez kursu FX,
    które NIE mogły zostać wycenione i więc NIE wnoszą nic do total_value. Wywołujący
    (get_portfolio_valuation) ustawia na tej podstawie `valuationComplete=false` zamiast
    po cichu traktować taką pozycję jako wartą 0.
    """
    details = []
    total_value = 0.0
    us_value = 0.0
    incomplete_symbols: list[str] = []
    for symbol, lots in positions.items():
        net_qty = sum(q for q, *_ in lots)
        if net_qty <= 0:
            continue
        avg_cost_pln = sum(q * c for q, c, *_ in lots) / net_qty

        # Native cost tylko gdy tx.currency == waluta notowania (inaczej broker już dał PLN)
        quote_currency = currency_for_symbol(symbol)
        native_cost_sum = 0.0
        native_cost_ok = True
        for lot in lots:
            q = lot[0]
            tx_cur = str(lot[3] if len(lot) >= 4 else "PLN").upper()
            if len(lot) >= 4 and tx_cur == quote_currency and quote_currency != "PLN":
                native_cost_sum += q * float(lot[2])
            else:
                native_cost_ok = False
                break
        avg_cost_native = (native_cost_sum / net_qty) if native_cost_ok and quote_currency != "PLN" else None

        if as_of:
            price_result, _price_date = close_as_of(symbol, as_of)
        else:
            price_result, _price_date = price_for(symbol)
        market_value_pln = None
        market_value_native = None
        current_price_native = None
        current_price_pln = None
        display_currency = quote_currency
        fx_mult = _fx_factor_to_pln(quote_currency, fx, as_of=as_of)

        if price_result is not None:
            if isinstance(price_result, tuple) and price_result[0] == "PROXY_SMH":
                pct = price_result[1]
                current_price_pln = avg_cost_pln * (1 + pct)
                current_price_native = current_price_pln
                market_value_pln = net_qty * current_price_pln
                market_value_native = market_value_pln
                display_currency = "PLN"
                avg_cost_native = None
            else:
                current_price_native = float(price_result)
                market_value_native = net_qty * current_price_native
                if fx_mult is not None:
                    current_price_pln = current_price_native * fx_mult
                    market_value_pln = market_value_native * fx_mult

        if market_value_pln is not None:
            total_value += market_value_pln
            if is_usd_ticker(symbol) or display_currency == "USD":
                us_value += market_value_pln
        else:
            # Brak ceny (plik Stooq nie istnieje) LUB brak kursu FX dla waluty notowania
            # (#41 pkt 4+5) — pozycja NIE wnosi 0 po cichu, jest zgłoszona jako niekompletna.
            incomplete_symbols.append(symbol)

        return_pct = None
        if market_value_pln is not None and avg_cost_pln:
            return_pct = (market_value_pln / (net_qty * avg_cost_pln) - 1) * 100

        perf = _compute_position_performance_metrics(
            symbol=symbol,
            quantity=net_qty,
            fifo_lots=lots,
            current_close_native=current_price_native,
            current_date=_price_date,
            market_value_pln=round(market_value_pln, 2) if market_value_pln is not None else None,
            fx_usdpln=fx,
        )

        details.append({
            "symbol": symbol,
            "qty": net_qty,
            "currency": display_currency,
            "avgCostPln": round(avg_cost_pln, 4),
            "avgCostNative": round(avg_cost_native, 4) if avg_cost_native is not None else None,
            "currentPrice": round(current_price_native, 4) if current_price_native is not None else None,
            "currentPriceNative": round(current_price_native, 4) if current_price_native is not None else None,
            "currentPricePln": round(current_price_pln, 4) if current_price_pln is not None else None,
            "marketValue": round(market_value_pln, 2) if market_value_pln is not None else None,
            "marketValuePln": round(market_value_pln, 2) if market_value_pln is not None else None,
            "marketValueNative": round(market_value_native, 2) if market_value_native is not None else None,
            "returnPct": round(return_pct, 2) if return_pct is not None else None,
            "priceMissing": market_value_pln is None,
            **perf,
        })
    return details, total_value, us_value, incomplete_symbols


# ---------- #54 Etap 3: TWR + day PnL portfela + period metrics ----------

def _compute_twr(
    portfolio_id: int,
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> dict:
    """Time-Weighted Return (TWR) from snapshots and cash flows.

    TWR divides portfolio history into sub-periods at each external cash flow
    (deposit/withdrawal). Product of (1+r_i) - 1 gives return independent of flows.
    """
    incomplete_reasons: list[str] = []

    # Get snapshots ordered by date
    q = "SELECT date, total_value_pln FROM snapshots WHERE portfolio_id=? ORDER BY date"
    params: list[Any] = [portfolio_id]
    snapshots = conn.execute(q, tuple(params)).fetchall()

    if len(snapshots) < 2:
        incomplete_reasons.append("insufficient_snapshots")
        return {
            "portfolioReturnPct": None,
            "complete": False,
            "incompleteReasons": incomplete_reasons,
        }

    # Get all external flows (deposits/withdrawals) with dates
    flow_q = (
        "SELECT date, amount FROM cash_entries WHERE portfolio_id=? "
        "AND type IN ('wplata', 'wyplata')"
    )
    flow_params: list[Any] = [portfolio_id]
    if as_of:
        flow_q += " AND date<=?"
        flow_params.append(as_of)
    flow_q += " ORDER BY date"
    flow_rows = conn.execute(flow_q, tuple(flow_params)).fetchall()
    # Group flows by date
    flows_by_date: dict[str, float] = {}
    for row in flow_rows:
        flows_by_date[row["date"]] = flows_by_date.get(row["date"], 0) + float(row["amount"])

    # Filter snapshots up to as_of
    snap_list = []
    for s in snapshots:
        if as_of and s["date"] > as_of:
            break
        snap_list.append((s["date"], float(s["total_value_pln"])))

    if len(snap_list) < 2:
        incomplete_reasons.append("insufficient_snapshots")
        return {
            "portfolioReturnPct": None,
            "complete": False,
            "incompleteReasons": incomplete_reasons,
        }

    # Compute TWR: for each sub-period between snapshots,
    # r_i = (end_value - flow_during) / start_value - 1
    # where flow_during is net cash flow that occurred ON the end date.
    twr_product = 1.0
    for i in range(1, len(snap_list)):
        start_val = snap_list[i - 1][1]
        end_val = snap_list[i][1]
        end_date = snap_list[i][0]
        flow = flows_by_date.get(end_date, 0.0)
        if abs(start_val) < 1e-9:
            # Can't compute sub-period return with zero start value
            incomplete_reasons.append("zero_start_value")
            continue
        sub_return = (end_val - flow) / start_val
        # #72: niespójne snapshoty vs cash_entries (np. wpłata bez wzrostu NAV)
        # dają ekstremalne sub-okresy i TWR rzędu milionów %. Odrzucamy je.
        if sub_return <= 0 or sub_return > 5.0:
            incomplete_reasons.append("inconsistent_flow_snapshot")
            continue
        twr_product *= sub_return

    if "inconsistent_flow_snapshot" in incomplete_reasons and twr_product == 1.0:
        return {
            "portfolioReturnPct": None,
            "complete": False,
            "incompleteReasons": list(dict.fromkeys(incomplete_reasons)),
        }

    portfolio_return_pct = round((twr_product - 1) * 100, 2)
    return {
        "portfolioReturnPct": portfolio_return_pct,
        "complete": len(incomplete_reasons) == 0,
        "incompleteReasons": list(dict.fromkeys(incomplete_reasons)),
    }


def _compute_portfolio_day_pnl(
    portfolio_id: int,
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> dict:
    """Day PnL from last 2 complete snapshots, adjusting for deposits/withdrawals."""
    q = "SELECT date, total_value_pln FROM snapshots WHERE portfolio_id=?"
    params: list[Any] = [portfolio_id]
    if as_of:
        q += " AND date<=?"
        params.append(as_of)
    q += " ORDER BY date DESC LIMIT 2"
    rows = conn.execute(q, tuple(params)).fetchall()

    if len(rows) < 2:
        return {"dayPnlPln": None, "portfolioDayReturnPct": None}

    end_val = float(rows[0]["total_value_pln"])
    end_date = rows[0]["date"]
    start_val = float(rows[1]["total_value_pln"])

    # Net flows on the end date
    flow_row = conn.execute(
        "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_entries "
        "WHERE portfolio_id=? AND date=? AND type IN ('wplata', 'wyplata')",
        (portfolio_id, end_date),
    ).fetchone()
    day_flow = float(flow_row["s"])

    day_pnl = round(end_val - start_val - day_flow, 2)
    day_return_pct = None
    if abs(start_val) > 1e-9:
        day_return_pct = round((end_val - day_flow) / start_val * 100 - 100, 2)

    return {"dayPnlPln": day_pnl, "portfolioDayReturnPct": day_return_pct}


def compute_period_metrics(
    portfolio_id: int,
    period: str = "1D",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Period metrics for portfolio: priceReturnPct, positionValueChangePct, totalReturnPct.

    Periods: 1D, 7D, 30D, YTD, sincePurchase, custom (start_date/end_date).
    Dividends affect totalReturnPct not priceReturnPct.
    Returns complete=false + incompleteReasons when data missing.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        incomplete_reasons: list[str] = []

        # Resolve date range
        today = date_cls.today()
        if period == "custom":
            if not start_date or not end_date:
                return {
                    "period": period,
                    "complete": False,
                    "incompleteReasons": ["missing_date_range"],
                    "priceReturnPct": None,
                    "positionValueChangePct": None,
                    "totalReturnPct": None,
                    "returnMethod": "TWR",
                }
            resolved_start = start_date
            resolved_end = end_date
        elif period == "sincePurchase":
            # Earliest transaction date
            row = conn.execute(
                "SELECT MIN(substr(datetime,1,10)) AS d FROM transactions WHERE portfolio_id=?",
                (portfolio_id,),
            ).fetchone()
            if not row or not row["d"]:
                return {
                    "period": period,
                    "complete": False,
                    "incompleteReasons": ["no_transactions"],
                    "priceReturnPct": None,
                    "positionValueChangePct": None,
                    "totalReturnPct": None,
                    "returnMethod": "TWR",
                }
            resolved_start = row["d"]
            resolved_end = today.isoformat()
        else:
            days_map = {"1D": 1, "7D": 7, "30D": 30, "YTD": None}
            if period == "YTD":
                resolved_start = f"{today.year}-01-01"
            else:
                n = days_map.get(period, 1)
                resolved_start = (today - timedelta(days=n)).isoformat()
            resolved_end = today.isoformat()

        # Get snapshots for the range
        start_snap = conn.execute(
            "SELECT date, total_value_pln, positions_json FROM snapshots "
            "WHERE portfolio_id=? AND date<=? ORDER BY date DESC LIMIT 1",
            (portfolio_id, resolved_start),
        ).fetchone()
        end_snap = conn.execute(
            "SELECT date, total_value_pln, positions_json FROM snapshots "
            "WHERE portfolio_id=? AND date<=? ORDER BY date DESC LIMIT 1",
            (portfolio_id, resolved_end),
        ).fetchone()

        if not start_snap or not end_snap:
            incomplete_reasons.append("missing_snapshots")
            return {
                "period": period,
                "startDate": resolved_start,
                "endDate": resolved_end,
                "complete": False,
                "incompleteReasons": incomplete_reasons,
                "priceReturnPct": None,
                "positionValueChangePct": None,
                "totalReturnPct": None,
                "returnMethod": "TWR",
            }

        start_val = float(start_snap["total_value_pln"])
        end_val = float(end_snap["total_value_pln"])

        if abs(start_val) < 1e-9:
            incomplete_reasons.append("zero_start_value")
            return {
                "period": period,
                "startDate": start_snap["date"],
                "endDate": end_snap["date"],
                "complete": False,
                "incompleteReasons": incomplete_reasons,
                "priceReturnPct": None,
                "positionValueChangePct": None,
                "totalReturnPct": None,
                "returnMethod": "TWR",
            }

        # Net flows in the period (for positionValueChangePct / totalReturnPct via TWR)
        flows_in_period = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_entries "
            "WHERE portfolio_id=? AND type IN ('wplata', 'wyplata') "
            "AND date > ? AND date <= ?",
            (portfolio_id, start_snap["date"], end_snap["date"]),
        ).fetchone()["s"]
        net_flow = float(flows_in_period)

        # Dividends in the period
        div_in_period = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM cash_entries "
            "WHERE portfolio_id=? AND type='dywidenda' "
            "AND date > ? AND date <= ?",
            (portfolio_id, start_snap["date"], end_snap["date"]),
        ).fetchone()["s"]
        dividends = float(div_in_period)

        # positionValueChangePct: simple change adjusted for flows
        position_value_change_pct = round(
            (end_val - net_flow) / start_val * 100 - 100, 2
        )

        # priceReturnPct: exclude dividends from gain
        price_return_pct = round(
            (end_val - net_flow - dividends) / start_val * 100 - 100, 2
        )

        # totalReturnPct: includes dividends (same as positionValueChangePct
        # since dividends are already in cash → in totalValue)
        total_return_pct = position_value_change_pct

        return {
            "period": period,
            "startDate": start_snap["date"],
            "endDate": end_snap["date"],
            "startValue": round(start_val, 2),
            "endValue": round(end_val, 2),
            "netFlows": round(net_flow, 2),
            "dividends": round(dividends, 2),
            "priceReturnPct": price_return_pct,
            "positionValueChangePct": position_value_change_pct,
            "totalReturnPct": total_return_pct,
            "returnMethod": "TWR",
            "complete": len(incomplete_reasons) == 0,
            "incompleteReasons": incomplete_reasons,
        }
    finally:
        if own:
            conn.close()


def get_portfolio_valuation(
    portfolio_id: int,
    conn: Optional[sqlite3.Connection] = None,
    as_of: Optional[str] = None,
) -> dict:
    own = conn is None
    conn = conn or get_conn()
    try:
        pf = get_portfolio_row(portfolio_id, conn)
        if as_of:
            fx, fx_date = load_fx_as_of(as_of)
        else:
            fx, fx_date = load_fx()
        positions = compute_positions_fifo(portfolio_id, conn, as_of=as_of)
        details, positions_value, us_value, incomplete_symbols = value_positions(positions, fx, as_of=as_of)
        cash = compute_cash(portfolio_id, conn, as_of=as_of)
        settlement_receivable = 0.0
        pending_purchase = 0.0
        if cash_from_cashflow_only(pf.get("strategy_profile")):
            settlement_receivable = compute_settlement_receivable(portfolio_id, conn, as_of=as_of)
            pending_purchase = compute_pending_purchase_value(portfolio_id, conn, as_of=as_of)
        total_value = cash + positions_value + settlement_receivable + pending_purchase

        # #69: portfele read-only podpięte do giełdy (dziś: real+okx) nie mają
        # lokalnego ledgera transakcji/cash_entries — źródłem prawdy jest
        # WYŁĄCZNIE snapshot zapisany przez sync_real_okx_portfolio (#67).
        # compute_cash/compute_positions_fifo dla takiego portfela zwracają
        # 0/puste (brak transactions), więc total_value liczony z ledgera
        # byłby zawsze 0 — nadpisujemy najnowszym snapshotem.
        is_read_only_exchange = (
            bool(pf.get("exchange"))
            and pf.get("execution_mode") in READ_ONLY_EXECUTION_MODES
        )
        # #87/2026-07-22: portfele game+okx+trading (futures, np. Claude-krypto) NIE
        # MAJĄ już lokalnego ledgera — execute_okx_futures_order (services/okx_trade.py)
        # przestał zapisywać transactions (decyzja usera: portfel opiera się wyłącznie
        # na wartości konta zsynchronizowanej z OKX, bez śledzenia pozycji lokalnie).
        # Traktujemy ten portfel identycznie jak read-only real+okx: total_value/cash
        # WYŁĄCZNIE ze snapshotu (sync_trading_okx_portfolio), positions zawsze puste.
        is_trading_okx_exchange = (
            pf.get("kind") == "game"
            and pf.get("exchange") == "okx"
            and pf.get("execution_mode") == "trading"
        )
        exchange_positions: list[dict] = []
        if is_trading_okx_exchange:
            # OKX jest źródłem prawdy dla futures. Pozycje są projekcją
            # ostatniego snapshotu synchronizacji — nigdy nie trafiają do
            # lokalnego ledgera ani portfolio_positions.
            snapshot_query = (
                "SELECT date, positions_json FROM snapshots WHERE portfolio_id=?"
                + (" AND date<=?" if as_of else "")
                + " ORDER BY date DESC LIMIT 1"
            )
            snapshot_row = conn.execute(
                snapshot_query, (portfolio_id, as_of) if as_of else (portfolio_id,)
            ).fetchone()
            if snapshot_row is not None:
                try:
                    raw_positions = json.loads(snapshot_row["positions_json"] or "[]")
                except (TypeError, ValueError, json.JSONDecodeError):
                    raw_positions = []
                if isinstance(raw_positions, list):
                    for raw in raw_positions:
                        if not isinstance(raw, dict):
                            continue
                        try:
                            qty = float(raw.get("pos") or 0)
                        except (TypeError, ValueError):
                            continue
                        if abs(qty) <= 1e-12:
                            continue
                        try:
                            avg_px = float(raw["avgPx"]) if raw.get("avgPx") not in (None, "") else None
                        except (TypeError, ValueError):
                            avg_px = None
                        try:
                            mark_px = float(raw["markPx"]) if raw.get("markPx") not in (None, "") else None
                        except (TypeError, ValueError):
                            mark_px = None
                        try:
                            upl = float(raw["upl"]) if raw.get("upl") not in (None, "") else None
                        except (TypeError, ValueError):
                            upl = None
                        try:
                            notional_usd = float(raw["notionalUsd"]) if raw.get("notionalUsd") not in (None, "") else None
                        except (TypeError, ValueError):
                            notional_usd = None
                        inst_id = str(raw.get("instId") or "")
                        base_symbol = inst_id.split("-")[0] if inst_id else None
                        exchange_positions.append({
                            "symbol": base_symbol or inst_id,
                            "market": "OKX_FUTURES",
                            "side": str(raw.get("posSide") or ("long" if qty > 0 else "short")).lower(),
                            "qty": abs(qty),
                            "quantity": abs(qty),
                            "avgPrice": avg_px,
                            "lastPrice": mark_px,
                            "marketValuePln": round(abs(notional_usd) * fx, 2) if notional_usd is not None else None,
                            "uplPln": round(upl * fx, 2) if upl is not None else None,
                            "instId": inst_id,
                            "leverage": raw.get("lever"),
                            "marginMode": raw.get("mgnMode"),
                            "source": "OKX",
                            "snapshotDate": snapshot_row["date"],
                        })
        if is_read_only_exchange or is_trading_okx_exchange:
            latest_snap_row = conn.execute(
                "SELECT total_value_pln, cash_pln FROM snapshots WHERE portfolio_id=?"
                + (" AND date<=?" if as_of else "")
                + " ORDER BY date DESC LIMIT 1",
                (portfolio_id, as_of) if as_of else (portfolio_id,),
            ).fetchone()
            if latest_snap_row is not None:
                total_value = float(latest_snap_row["total_value_pln"])
                cash = float(latest_snap_row["cash_pln"])
                positions_value = 0.0
            else:
                total_value = 0.0
                cash = 0.0
            details = exchange_positions if is_trading_okx_exchange else []
        us_pct = (us_value / total_value * 100) if total_value else 0.0

        # #51: deposits liczony zawsze z ledgera cash_entries — starting_capital
        # nie warunkuje już tej ścieżki.
        deposits = compute_deposits(portfolio_id, conn, as_of=as_of)
        cashflow_metrics = compute_cash_operation_metrics(portfolio_id, conn, as_of=as_of)
        result_vs_deposits = None
        if deposits is not None:
            result_vs_deposits = round(total_value - deposits, 2)

        # --- #54 Etap 3: portfolio-level aggregates ---
        open_positions_cost_basis_pln = round(
            sum(p.get("costBasisPln") or 0 for p in details), 2
        )
        unrealized_pnl_pln = round(positions_value - open_positions_cost_basis_pln, 2)
        net_contributions = cashflow_metrics["netContributions"]
        total_pnl_pln = round(total_value - net_contributions, 2) if net_contributions else None

        # TWR via snapshots
        twr_result = _compute_twr(portfolio_id, conn, as_of=as_of)
        portfolio_return_pct = twr_result["portfolioReturnPct"]
        twr_complete = twr_result["complete"]
        twr_incomplete_reasons = list(twr_result["incompleteReasons"])
        return_method = "TWR"
        # #72: gdy TWR niemożliwy / niestabilny (złe historyczne snapshoty vs cash),
        # pokaż zwrot vs wpłaty — UI nie zostaje z „—” ani z absurdalnym %.
        deposits_return = _compute_return_pct(total_value, deposits)
        use_simple_fallback = False
        if deposits_return is not None:
            if portfolio_return_pct is None:
                use_simple_fallback = True
            elif "inconsistent_flow_snapshot" in twr_incomplete_reasons:
                use_simple_fallback = True
            elif abs(portfolio_return_pct) > 1000:
                use_simple_fallback = True
        if use_simple_fallback:
            portfolio_return_pct = deposits_return
            return_method = "simple"
            if "insufficient_snapshots" in twr_incomplete_reasons:
                twr_complete = False
            else:
                twr_complete = True
                twr_incomplete_reasons = []

        # Day PnL (portfolio level) — last 2 complete snapshots
        day_result = _compute_portfolio_day_pnl(portfolio_id, conn, as_of=as_of)

        valuation_complete = len(incomplete_symbols) == 0

        # #87: dla trading+okx (Claude-krypto) total_value pochodzi z salda USDC
        # całego konta demo (sync_trading_okx_portfolio) — NIE ma związku z
        # baseline 20000 PLN wpłaconym przez create_player (konto demo ma własny
        # "starter pack" niezależny od naszej gry). Porównywanie total_value do
        # deposits dałoby fałszywe, ogromne "zyski" — zerujemy te pola zamiast
        # pokazywać matematykę bez sensu (decyzja usera, 2026-07-21).
        if is_trading_okx_exchange:
            result_vs_deposits = None
            unrealized_pnl_pln = None
            total_pnl_pln = None
            portfolio_return_pct = None
            return_method = "n/a"
            twr_complete = False
            twr_incomplete_reasons = ["trading_okx_balance_not_comparable_to_deposits"]

        # #67 AC: status świeżości danych dla portfeli podpiętych do giełdy
        # (dziś: real+okx). Dla portfeli bez exchange pole jest zawsze None —
        # brak sensu "stale" bo nie ma synchronizacji zewnętrznej.
        exchange_sync = None
        if pf.get("exchange"):
            from services.okx_sync import is_stale  # lazy: unika cyklu importów
            exchange_sync = {
                "lastSyncedAt": pf.get("last_synced_at"),
                "syncStatus": pf.get("sync_status"),
                "stale": is_stale(pf.get("last_synced_at")),
            }

        return {
            "portfolio": pf,
            "exchangeSync": exchange_sync,
            "cashPln": round(cash, 2),
            "settlementReceivablePln": round(settlement_receivable, 2),
            "pendingPurchasePln": round(pending_purchase, 2),
            "positions": details,
            "exchangePositions": exchange_positions,
            "valuationComplete": valuation_complete,
            "incompleteSymbols": incomplete_symbols,
            "deposits": deposits,
            **cashflow_metrics,
            "resultVsDeposits": result_vs_deposits,
            "openPositionsValue": round(positions_value, 2),
            "positionsValuePln": round(positions_value, 2),
            "totalValue": round(total_value, 2),
            "totalValuePln": round(total_value, 2),
            "openPositionsCostBasisPln": open_positions_cost_basis_pln,
            "unrealizedPnlPln": unrealized_pnl_pln,
            "totalPnlPln": total_pnl_pln,
            "portfolioReturnPct": portfolio_return_pct,
            "returnMethod": return_method,
            "portfolioReturnComplete": twr_complete,
            "portfolioReturnIncompleteReasons": twr_incomplete_reasons,
            "dayPnlPln": day_result["dayPnlPln"],
            "portfolioDayReturnPct": day_result["portfolioDayReturnPct"],
            "usExposurePct": round(us_pct, 2),
            # #69: portfele read-only podpięte do giełdy nie mają deposits
            # (brak cash_entries) — zwrot liczony wyłącznie z historii snapshots
            # (TWR degeneruje się do prostego zwrotu najstarszy/najnowszy snapshot
            # przy braku cash_entries), zamiast zawsze None z formuły deposits-based.
            # #87: trading+okx — total_value niezwiązany z deposits, zawsze None.
            "returnPct": (
                None if is_trading_okx_exchange
                else portfolio_return_pct if is_read_only_exchange
                else _compute_return_pct(total_value, deposits)
            ),
            "fx": {"usdpln": fx, "date": fx_date},
        }
    finally:
        if own:
            conn.close()


# ---------- validation + execute_trade ----------

def market_bucket(symbol: str) -> str:
    """Klasyfikacja rynku z symbolu Stooq: PL | US | L | DE | …"""
    s = symbol.upper().strip()
    if "." not in s:
        return "PL"
    suffix = s.rsplit(".", 1)[-1]
    if suffix == "WA":
        return "PL"
    return suffix


# Gołe symbole bazowe krypto (bez sufiksu pary, np. z okx_trade.py::execute_okx_futures_order
# gdzie symbol to 'BTC'/'ETH'/'DOGE', nie pełny instId futures) — spójne z
# services.okx_trade.ALLOWED_OKX_FUTURES_BASES (nie importowane wprost: game.py
# nie może zależeć od okx_trade.py, odwrotny kierunek do istniejących importów lazy).
_BARE_CRYPTO_SYMBOLS = frozenset({"BTC", "ETH", "DOGE"})

# Sufiks pozycji krótkiej futures (#72) — 'BTC-SHORT' to osobny "symbol" w
# ledgerze/FIFO od 'BTC' (long), patrz services.okx_trade::_map_futures_side_to_ledger.
_FUTURES_SHORT_SUFFIX = "-SHORT"


def asset_type_for_symbol(symbol: str) -> str:
    """Mapuje symbol na typ aktywów z allowed_asset_types."""
    s = symbol.upper().strip()
    bucket = market_bucket(s)
    bare = s[: -len(_FUTURES_SHORT_SUFFIX)] if s.endswith(_FUTURES_SHORT_SUFFIX) else s
    if (
        bucket in ("CRYPTO", "CC")
        or s.endswith(("-USDT", "-USDC", "-USD"))
        or bare in _BARE_CRYPTO_SYMBOLS
        or ".CRYPTO" in s
    ):
        return "krypto"
    if bucket == "PL":
        return "akcje_pln"
    return "akcje_zagraniczne"


def resolve_allowed_asset_types(profile: dict) -> Optional[list[str]]:
    """Zwraca listę dozwolonych typów albo None (= brak twardego limitu, legacy)."""
    raw = profile.get("allowed_asset_types")
    if raw is not None:
        if not isinstance(raw, list):
            raise ValidationError("strategy_profile.allowed_asset_types musi być listą")
        return [str(x) for x in raw]
    if profile.get("cash_only"):
        return ["lokaty"]
    markets = profile.get("allowed_markets") or []
    if isinstance(markets, list) and any(str(m).lower() == "foreign" for m in markets):
        return ["akcje_zagraniczne"]
    return None


def assert_asset_type_allowed(asset_type: str, profile: dict, *, op: str) -> None:
    allowed = resolve_allowed_asset_types(profile)
    if allowed is None:
        return
    if asset_type not in allowed:
        raise ValidationError(
            f"strategy_profile.allowed_asset_types={allowed} złamane: operacja {op} "
            f"wymaga typu '{asset_type}'"
        )


def assert_allowed_markets(symbol: str, profile: dict) -> None:
    """Egzekwuje strategy_profile.allowed_markets (opcjonalne).

    Wartości:
    - brak / null / [] → bez ograniczeń
    - zawiera ``foreign`` → zakaz rynku PL (.WA lub ticker bez sufiksu)
    - lista kodów (``US``, ``L``, ``PL``, …) → tylko te buckety
    """
    allowed = profile.get("allowed_markets")
    if not allowed:
        return
    if not isinstance(allowed, list):
        raise ValidationError("strategy_profile.allowed_markets musi być listą")
    bucket = market_bucket(symbol)
    normalized = [str(a).upper() for a in allowed]
    if "FOREIGN" in normalized:
        if bucket == "PL":
            raise ValidationError(
                f"strategy_profile.allowed_markets=foreign złamane: {symbol} to rynek PL "
                f"(dozwolone tylko zagraniczne, np. .US / .L)"
            )
        return
    if bucket not in normalized:
        raise ValidationError(
            f"strategy_profile.allowed_markets={allowed} złamane: {symbol} jest rynkiem "
            f"{bucket}, dozwolone: {allowed}"
        )


def assert_round_exists(round_id: Optional[int], conn: sqlite3.Connection) -> None:
    """round_id musi istnieć w rounds przed zapisem transakcji (TASK_026-B) — inaczej
    FK jest tylko deklaratywny (PRAGMA foreign_keys włączone, ale lepiej dać czytelny
    ValidationError niż surowy sqlite3.IntegrityError na etapie INSERT)."""
    if round_id is None:
        return
    row = conn.execute("SELECT 1 FROM rounds WHERE id=?", (round_id,)).fetchone()
    if row is None:
        raise ValidationError(f"round_id={round_id} nie istnieje w rounds")


def assert_sell_covered_by_fifo(
    portfolio_id: int,
    symbol: str,
    qty: float,
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
) -> None:
    """Waliduje SELL względem FIFO na moment `as_of` (nie względem stanu bieżącego,
    TASK_026-B) — kluczowe dla importu historycznego: SELL starszy niż dostępny BUY
    (np. BUY 2025-02-01 + import SELL 2025-01-01) musi zostać odrzucony, mimo że
    *dzisiejszy* stan portfela mógłby wyglądać na wystarczający.

    Zamiast po cichu ignorować niedopasowaną część (poprzednie zachowanie
    compute_positions_fifo dla SELL > dostępne loty), zgłasza FifoIntegrityError
    z symbolem, datą i brakującą ilością.
    """
    positions = compute_positions_fifo(portfolio_id, conn, as_of=as_of, strict=True)
    held_qty = sum(q for q, *_ in positions.get(symbol, []))
    if qty > held_qty + 1e-9:
        missing = qty - held_qty
        when = as_of or "teraz"
        raise FifoIntegrityError(
            f"nie można sprzedać {qty} {symbol}: portfel posiada tylko {held_qty} "
            f"(integralność FIFO na dzień {when}, brakuje {missing:.6f}) — transakcja "
            f"odrzucona, brak zmiany DB"
        )


def validate_trade(
    portfolio_id: int,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    currency: str,
    fx_rate: float,
    reason: str,
    exit_level: Optional[float],
    round_id: Optional[int],
    conn: sqlite3.Connection,
    as_of: Optional[str] = None,
    commission_pln: float = 0.0,
) -> float:
    """Sprawdza reguły strategy_profile. Zwraca value_pln obliczoną, albo rzuca ValidationError.

    as_of (YYYY-MM-DD): moment, względem którego walidowany jest SELL FIFO — None
    (domyślnie execute_trade, transakcja "teraz") = stan bieżący; dla importu
    historycznego (add_transaction) przekazywana jest data transakcji, żeby SELL
    starszy niż dostępny BUY został odrzucony (TASK_026-B).
    """
    if not reason or not reason.strip():
        raise ValidationError("reason jest wymagany dla każdej transakcji")
    if side not in ("BUY", "SELL"):
        raise ValidationError(f"side musi być BUY/SELL, otrzymano: {side}")
    if qty <= 0:
        raise ValidationError("qty musi być dodatnie")
    if price <= 0:
        raise ValidationError("price musi być większe od 0")
    if fx_rate <= 0:
        raise ValidationError("fx_rate musi być większe od 0")
    if commission_pln < 0:
        raise ValidationError("commission_pln nie może być ujemne")

    pf = get_portfolio_row(portfolio_id, conn)
    if pf.get("archived"):
        raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")
    profile = pf["strategy_profile"]
    assert_asset_type_allowed(asset_type_for_symbol(symbol), profile, op=f"{side} {symbol}")
    allowed_types = resolve_allowed_asset_types(profile)
    if allowed_types is not None and not any(
        t in allowed_types for t in ("akcje_pln", "akcje_zagraniczne", "krypto")
    ):
        raise ValidationError(
            "strategy_profile.allowed_asset_types — ten portfel nie pozwala na BUY/SELL "
            "(brak typów equity/krypto); użyj cash-entries jeśli 'lokaty' jest dozwolone"
        )
    assert_allowed_markets(symbol, profile)
    fx_mult = _fx_factor_to_pln(currency, fx_rate)
    if fx_mult is None:
        raise ValidationError(
            f"brak kursu {currency}→PLN dla {symbol} — nie można ustalić value_pln "
            f"(#41 pkt 4: waluty bez kursu nie są przeliczane 1:1 na PLN)"
        )
    value_pln = qty * price * fx_mult

    valuation = get_portfolio_valuation(portfolio_id, conn)
    total_value = valuation["totalValue"] or 0.0
    cash = valuation["cashPln"]

    if profile.get("stop_loss_required") and side == "BUY" and exit_level is None:
        raise ValidationError(
            "strategy_profile.stop_loss_required=true — transakcja BUY wymaga podania exit_level"
        )

    if side == "BUY":
        required_cash = value_pln + commission_pln
        if required_cash > cash + 1e-6:
            raise ValidationError(
                f"brak gotówki: transakcja wymaga {required_cash:.2f} PLN, dostępne {cash:.2f} PLN"
            )

        cash_after = cash - required_cash
        min_cash_pct = profile.get("min_cash_pct")
        if min_cash_pct is not None and total_value:
            min_cash_required = total_value * min_cash_pct / 100
            if cash_after < min_cash_required - 1e-6:
                raise ValidationError(
                    f"strategy_profile.min_cash_pct={min_cash_pct}% złamane: po transakcji gotówka "
                    f"{cash_after:.2f} PLN < wymagane minimum {min_cash_required:.2f} PLN"
                )

        max_position_pct = profile.get("max_position_pct")
        if max_position_pct is not None and total_value:
            existing = next((p for p in valuation["positions"] if p["symbol"] == symbol), None)
            existing_value = existing["marketValue"] or 0 if existing else 0
            new_position_value = existing_value + value_pln
            position_pct = new_position_value / total_value * 100
            if position_pct > max_position_pct + 1e-6:
                raise ValidationError(
                    f"strategy_profile.max_position_pct={max_position_pct}% złamane: pozycja {symbol} "
                    f"po transakcji stanowiłaby {position_pct:.1f}% portfela"
                )

        max_us_pct = profile.get("max_us_pct")
        if max_us_pct is not None and total_value:
            us_value_after = valuation["totalValue"] * valuation["usExposurePct"] / 100
            if is_usd_ticker(symbol):
                us_value_after += value_pln
            us_pct_after = us_value_after / total_value * 100
            if us_pct_after > max_us_pct + 1e-6:
                raise ValidationError(
                    f"strategy_profile.max_us_pct={max_us_pct}% złamane: ekspozycja USA po transakcji "
                    f"wyniosłaby {us_pct_after:.1f}%"
                )
    else:  # SELL
        assert_sell_covered_by_fifo(portfolio_id, symbol, qty, conn, as_of=as_of)

    assert_round_exists(round_id, conn)

    max_trades_per_round = profile.get("max_trades_per_round")
    if max_trades_per_round is not None and round_id is not None:
        count = conn.execute(
            "SELECT COUNT(*) as c FROM transactions WHERE portfolio_id=? AND round_id=?",
            (portfolio_id, round_id),
        ).fetchone()["c"]
        if count >= max_trades_per_round:
            raise ValidationError(
                f"strategy_profile.max_trades_per_round={max_trades_per_round} złamane: "
                f"runda {round_id} ma już {count} transakcji dla tego portfela"
            )

    return value_pln


MONEY_QUANT = Decimal("0.01")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{field} musi być liczbą")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{field} musi być liczbą") from exc
    if not result.is_finite() or (positive and result <= 0):
        raise ValidationError(f"{field} musi być większe od 0")
    return result


def _money(value: Decimal) -> Decimal:
    """Jedyna reguła finansowa #53: dwa miejsca, ROUND_HALF_UP."""
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def _trade_fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _position_quantity(portfolio_id: int, symbol: str, conn: sqlite3.Connection) -> float:
    row = conn.execute(
        "SELECT quantity FROM portfolio_positions WHERE portfolio_id=? AND symbol=?",
        (portfolio_id, symbol),
    ).fetchone()
    return float(row["quantity"]) if row else 0.0


def _refresh_positions_projection(portfolio_id: int, conn: sqlite3.Connection) -> list[dict]:
    """Odbudowuje projekcję z immutable ledgeru; wołający zarządza transakcją DB."""
    rows = conn.execute(
        "SELECT symbol, market, side, qty, currency, unit_price_currency, unit_price_pln, "
        "price, fx_rate, value_pln, executed_at, datetime FROM transactions "
        "WHERE portfolio_id=? ORDER BY COALESCE(executed_at, datetime), id",
        (portfolio_id,),
    ).fetchall()
    positions: dict[str, dict[str, Any]] = {}
    for row in rows:
        symbol = row["symbol"]
        quantity = _decimal(row["qty"], "quantity", positive=True)
        if row["side"] == "BUY":
            native = _decimal(
                row["unit_price_currency"] if row["unit_price_currency"] is not None else row["price"],
                "unit_price_currency",
                positive=True,
            )
            pln = _decimal(
                row["unit_price_pln"]
                if row["unit_price_pln"] is not None
                else float(row["value_pln"]) / float(row["qty"]),
                "unit_price_pln",
                positive=True,
            )
            current = positions.get(symbol)
            if current is None:
                positions[symbol] = {
                    "quantity": quantity,
                    "native_cost": quantity * native,
                    "pln_cost": quantity * pln,
                    "currency": row["currency"],
                    "market": row["market"],
                }
            else:
                current["quantity"] += quantity
                current["native_cost"] += quantity * native
                current["pln_cost"] += quantity * pln
        else:
            current = positions.get(symbol)
            held = current["quantity"] if current else Decimal("0")
            if quantity > held:
                raise FifoIntegrityError(
                    f"rebuild pozycji: SELL {quantity} {symbol} przekracza pozycję {held}"
                )
            remaining = held - quantity
            if remaining == 0:
                del positions[symbol]
            else:
                ratio = remaining / held
                current["quantity"] = remaining
                current["native_cost"] *= ratio
                current["pln_cost"] *= ratio

    conn.execute("DELETE FROM portfolio_positions WHERE portfolio_id=?", (portfolio_id,))
    for symbol, item in positions.items():
        quantity = item["quantity"]
        conn.execute(
            "INSERT INTO portfolio_positions "
            "(portfolio_id,symbol,market,quantity,currency,average_unit_cost_currency,average_unit_cost_pln) "
            "VALUES (?,?,?,?,?,?,?)",
            (
                portfolio_id,
                symbol,
                item["market"],
                float(quantity),
                item["currency"],
                float(_money(item["native_cost"] / quantity)),
                float(_money(item["pln_cost"] / quantity)),
            ),
        )
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM portfolio_positions WHERE portfolio_id=? ORDER BY symbol",
            (portfolio_id,),
        ).fetchall()
    ]


def rebuild_portfolio_positions(
    portfolio_id: int, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            get_portfolio_row(portfolio_id, conn)
            result = _refresh_positions_projection(portfolio_id, conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise
    finally:
        if own:
            conn.close()


# ---------- tracked symbols + EOD price cache (#54 etap 1) ----------

_VALID_QUOTE_CURRENCIES = frozenset({"PLN", "USD", "GBP"})
_FRESHNESS_STALE_DAYS = 4
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _normalize_symbol(symbol: str) -> str:
    return (symbol or "").upper().strip()


def _validate_eod_quote(
    symbol: str, close: Any, price_date: str, currency: str
) -> Optional[str]:
    """None = OK; otherwise komunikat błędu walidacji (AC #5)."""
    try:
        value = float(close)
    except (TypeError, ValueError):
        return f"nieprawidłowa cena dla {symbol}"
    if not value > 0:
        return f"cena ≤ 0 dla {symbol}"
    if not _ISO_DATE_RE.match(price_date or ""):
        return f"nieprawidłowa data ceny dla {symbol}"
    try:
        year = int(price_date[:4])
    except ValueError:
        return f"nieprawidłowa data ceny dla {symbol}"
    if year < 1990 or year > 2100:
        return f"nieprawidłowa data ceny dla {symbol}"
    cur = (currency or "").upper()
    if cur not in _VALID_QUOTE_CURRENCIES:
        return f"nieobsługiwana waluta notowania dla {symbol}: {currency}"
    expected = currency_for_symbol(symbol)
    if cur != expected:
        return f"waluta {cur} nie zgadza się z oczekiwaną {expected} dla {symbol}"
    return None


def _freshness_for_price_date(price_date: str, *, today: Optional[date_cls] = None) -> str:
    if not price_date or not _ISO_DATE_RE.match(price_date):
        return "missing"
    today = today or date_cls.today()
    try:
        px = date_cls.fromisoformat(price_date)
    except ValueError:
        return "missing"
    age = (today - px).days
    return "fresh" if age <= _FRESHNESS_STALE_DAYS else "stale"


def get_latest_cached_price(
    symbol: str, conn: sqlite3.Connection
) -> Optional[dict]:
    row = conn.execute(
        "SELECT symbol, date, close, market, currency, source, change_pct_1d, updated_at "
        "FROM prices WHERE symbol=? ORDER BY date DESC LIMIT 1",
        (_normalize_symbol(symbol),),
    ).fetchone()
    return dict(row) if row else None


def list_tracked_symbols(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Unikalne symbole z portfolio_positions (qty>0, niearchiwizowane portfele)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            """
            SELECT pp.symbol, pp.market, pp.currency, pp.portfolio_id
            FROM portfolio_positions pp
            JOIN portfolios p ON p.id = pp.portfolio_id
            WHERE pp.quantity > 0 AND p.archived = 0
            ORDER BY pp.symbol, pp.portfolio_id
            """
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            sym = row["symbol"]
            item = grouped.get(sym)
            if item is None:
                item = {
                    "symbol": sym,
                    "market": row["market"] or market_bucket(sym),
                    "currency": row["currency"],
                    "portfolioIds": [],
                }
                grouped[sym] = item
            item["portfolioIds"].append(row["portfolio_id"])
        return [grouped[sym] for sym in sorted(grouped)]
    finally:
        if own:
            conn.close()


def _extract_eod_close(symbol: str, price_result: Any) -> tuple[Optional[float], Optional[str]]:
    if price_result is None:
        return None, None
    if isinstance(price_result, tuple) and price_result and price_result[0] == "PROXY_SMH":
        return None, "proxy_not_supported"
    try:
        return float(price_result), None
    except (TypeError, ValueError):
        return None, "invalid_close"


def refresh_portfolio_prices(
    conn: Optional[sqlite3.Connection] = None,
    *,
    symbols: Optional[list[str]] = None,
) -> dict:
    """Idempotentny batch EOD: tracked symbols → tabela prices (AC #1–#5)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        if symbols is None:
            target_symbols = [row["symbol"] for row in list_tracked_symbols(conn)]
        else:
            target_symbols = [_normalize_symbol(s) for s in symbols if s]
        target_symbols = sorted(set(target_symbols))

        if not target_symbols:
            return {"symbolCount": 0, "results": []}

        begin_immediate(conn)
        results: list[dict[str, Any]] = []
        now = datetime.utcnow().replace(microsecond=0).isoformat(sep=" ")
        try:
            for symbol in target_symbols:
                cached = get_latest_cached_price(symbol, conn)
                entry: dict[str, Any] = {"symbol": symbol}
                try:
                    price_result, price_date = price_for(symbol)
                    close, close_err = _extract_eod_close(symbol, price_result)
                    currency = currency_for_symbol(symbol)
                    market = market_bucket(symbol)

                    if close_err == "proxy_not_supported":
                        if cached:
                            entry.update({
                                "status": "stale",
                                "close": float(cached["close"]),
                                "priceDate": cached["date"],
                                "freshness": _freshness_for_price_date(cached["date"]),
                            })
                        else:
                            entry.update({
                                "status": "error",
                                "error": f"symbol proxy {symbol} wymaga osobnej obsługi",
                            })
                        results.append(entry)
                        continue

                    if close is None or not price_date:
                        if cached:
                            entry.update({
                                "status": "stale",
                                "close": float(cached["close"]),
                                "priceDate": cached["date"],
                                "freshness": _freshness_for_price_date(cached["date"]),
                            })
                        else:
                            entry.update({
                                "status": "error",
                                "error": f"brak pliku cenowego dla {symbol}",
                            })
                        results.append(entry)
                        continue

                    validation_error = _validate_eod_quote(symbol, close, price_date, currency)
                    if validation_error:
                        if cached:
                            entry.update({
                                "status": "stale",
                                "close": float(cached["close"]),
                                "priceDate": cached["date"],
                                "freshness": _freshness_for_price_date(cached["date"]),
                                "warning": validation_error,
                            })
                        else:
                            entry.update({"status": "error", "error": validation_error})
                        results.append(entry)
                        continue

                    existing = conn.execute(
                        "SELECT close FROM prices WHERE symbol=? AND date=?",
                        (symbol, price_date),
                    ).fetchone()
                    if existing is not None and float(existing["close"]) == close:
                        entry.update({
                            "status": "unchanged",
                            "close": close,
                            "priceDate": price_date,
                            "freshness": _freshness_for_price_date(price_date),
                        })
                    else:
                        conn.execute(
                            """
                            INSERT INTO prices
                                (symbol, date, close, market, currency, source, source_path, change_pct_1d, updated_at)
                            VALUES (?, ?, ?, ?, ?, 'stooq', ?, ?, ?)
                            ON CONFLICT(symbol, date) DO UPDATE SET
                                close=excluded.close,
                                market=excluded.market,
                                currency=excluded.currency,
                                source=excluded.source,
                                source_path=excluded.source_path,
                                change_pct_1d=excluded.change_pct_1d,
                                updated_at=excluded.updated_at
                            """,
                            (
                                symbol, price_date, close, market, currency,
                                price_source_path(symbol), change_pct_1d(symbol, price_date, close),
                                now,
                            ),
                        )
                        entry.update({
                            "status": "updated",
                            "close": close,
                            "priceDate": price_date,
                            "freshness": _freshness_for_price_date(price_date),
                        })
                except Exception as exc:  # noqa: BLE001 — AC #4: jeden symbol nie blokuje reszty
                    logger.exception("refresh_portfolio_prices failed for %s", symbol)
                    if cached:
                        entry.update({
                            "status": "stale",
                            "close": float(cached["close"]),
                            "priceDate": cached["date"],
                            "freshness": _freshness_for_price_date(cached["date"]),
                            "warning": str(exc),
                        })
                    else:
                        entry.update({"status": "error", "error": str(exc)})
                results.append(entry)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return {"symbolCount": len(target_symbols), "results": results}
    finally:
        if own:
            conn.close()


def list_portfolio_positions(
    portfolio_id: int, conn: Optional[sqlite3.Connection] = None
) -> list[dict]:
    """Skład portfela z projekcji portfolio_positions + cache prices + metryki (#54 etap 2)."""
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        fx, _fx_date = load_fx()
        fifo_positions = compute_positions_fifo(portfolio_id, conn)
        rows = conn.execute(
            "SELECT * FROM portfolio_positions WHERE portfolio_id=? ORDER BY symbol",
            (portfolio_id,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            symbol = row["symbol"]
            quote_currency = currency_for_symbol(symbol)
            cached = get_latest_cached_price(symbol, conn)
            last_price = None
            price_date = None
            freshness = "missing"
            if cached:
                last_price = float(cached["close"])
                price_date = cached["date"]
                freshness = _freshness_for_price_date(price_date)

            qty = float(row["quantity"])
            avg_cost_currency = float(row["average_unit_cost_currency"])
            avg_cost_pln = float(row["average_unit_cost_pln"])
            market = row["market"] or market_bucket(symbol)
            currency = row["currency"]

            value_pln = None
            if last_price is not None:
                fx_mult = _fx_factor_to_pln(quote_currency, fx)
                if fx_mult is not None:
                    value_pln = round(qty * last_price * fx_mult, 2)

            lots = fifo_positions.get(symbol, [])
            cached_pct = cached.get("change_pct_1d") if cached else None
            perf = _compute_position_performance_metrics(
                symbol=symbol,
                quantity=qty,
                fifo_lots=lots,
                current_close_native=last_price,
                current_date=price_date,
                market_value_pln=value_pln,
                fx_usdpln=fx,
                cached_change_pct_1d=cached_pct,
            )

            out.append({
                "symbol": symbol,
                "market": market,
                "currency": currency,
                "quantity": qty,
                "avgCostCurrency": avg_cost_currency,
                "avgCostPln": avg_cost_pln,
                "lastPrice": last_price,
                "priceDate": price_date,
                "valuePln": value_pln,
                "freshness": freshness,
                **perf,
            })
        return out
    finally:
        if own:
            conn.close()


def assert_not_readonly_exchange_portfolio(pf: dict, action: str) -> None:
    """#67: twarda blokada mutacji ledgera dla portfeli read_only (real+okx).

    Saldo/pozycje takiego portfela mają pochodzić wyłącznie z synchronizacji
    z giełdą (services/okx_sync.py). Wołane jawnie z _record_trade (czyli
    execute_trade i add_transaction) oraz add_cash_operation — nie polega na
    tym, że UI/wywołujący nie zaproponuje takiej operacji.
    """
    if pf.get("exchange") and pf.get("execution_mode") in READ_ONLY_EXECUTION_MODES:
        raise ValidationError(
            f"portfel {pf.get('id')} ({pf.get('name')}) jest read-only "
            f"(exchange={pf.get('exchange')}, execution_mode={pf.get('execution_mode')}) — "
            f"{action} zablokowane; saldo pochodzi wyłącznie z synchronizacji z giełdą"
        )


def _decorate_trade_result(tx: dict, replayed: bool) -> dict:
    tx["idempotent_replay"] = replayed
    tx["portfolio_before"] = {
        "cashPln": tx.get("cash_before_pln"),
        "symbol": tx.get("symbol"),
        "quantity": tx.get("position_quantity_before"),
    }
    tx["portfolio_after"] = {
        "cashPln": tx.get("cash_after_pln"),
        "symbol": tx.get("symbol"),
        "quantity": tx.get("position_quantity_after"),
    }
    return tx


def _record_trade(
    *,
    portfolio_id: int,
    symbol: str,
    side: str,
    qty: Any,
    unit_price_currency: Any,
    currency: str,
    fx_rate_pln: Any,
    commission_pln: Any,
    executed_at: str,
    reason: str,
    idempotency_key: Optional[str],
    source: str,
    market: Optional[str],
    exit_level: Optional[float],
    round_id: Optional[int],
    fingerprint: str,
    enforce_mandate: bool,
    conn: sqlite3.Connection,
    exchange_order_id: Optional[str] = None,
) -> dict:
    symbol = (symbol or "").upper().strip()
    side = (side or "").upper().strip()
    currency = (currency or "").upper().strip()
    reason = (reason or "").strip()
    key = (idempotency_key or "").strip() or None
    if idempotency_key is not None and key is None:
        raise ValidationError("idempotency_key nie może być pusty")
    quantity = _decimal(qty, "quantity", positive=True)
    price = _decimal(unit_price_currency, "unit_price_currency", positive=True)
    rate = _decimal(fx_rate_pln, "fx_rate_pln", positive=True)
    commission = _money(_decimal(commission_pln or 0, "commission_pln"))
    if commission < 0:
        raise ValidationError("commission_pln nie może być ujemne")

    pf_guard = get_portfolio_row(portfolio_id, conn)
    assert_not_readonly_exchange_portfolio(pf_guard, "execute_trade")

    if key:
        existing = conn.execute(
            "SELECT * FROM transactions WHERE portfolio_id=? AND idempotency_key=?",
            (portfolio_id, key),
        ).fetchone()
        if existing:
            tx = dict(existing)
            if tx.get("request_fingerprint") != fingerprint:
                raise IdempotencyConflictError(
                    f"idempotency_key '{key}' został już użyty z innymi parametrami"
                )
            return _decorate_trade_result(tx, True)

    unit_pln = _money(price * rate)
    gross_currency = _money(quantity * price)
    # Kwota całkowita korzysta z niezaokrąglonego iloczynu ceny i FX; w innym
    # przypadku błąd zaokrąglenia ceny jednostkowej mnożyłby się przez quantity.
    gross_pln = _money(quantity * price * rate)
    if enforce_mandate:
        validate_trade(
            portfolio_id, symbol, side, float(quantity), float(price), currency,
            float(rate), reason, exit_level, round_id, conn,
            as_of=executed_at[:10], commission_pln=float(commission),
        )
    else:
        pf = get_portfolio_row(portfolio_id, conn)
        if pf.get("archived"):
            raise NotFoundError(f"portfolio {portfolio_id} nie jest aktywne")
        if not reason:
            raise ValidationError("reason jest wymagany dla każdej transakcji")
        if side not in ("BUY", "SELL"):
            raise ValidationError(f"side musi być BUY/SELL, otrzymano: {side}")
        if side == "SELL":
            assert_sell_covered_by_fifo(
                portfolio_id, symbol, float(quantity), conn, as_of=executed_at[:10]
            )
        elif not cash_from_cashflow_only(pf.get("strategy_profile")):
            required = gross_pln + commission
            available = _money(Decimal(str(compute_cash(portfolio_id, conn))))
            # Wyłącznie disaster recovery: backup brokera może różnić się o 1 gr
            # wskutek historycznej kolejności zaokrągleń. Bieżący execute_trade
            # nadal przechodzi przez validate_trade bez tej tolerancji.
            if required - available > Decimal("0.01"):
                raise ValidationError(
                    f"brak gotówki: transakcja wymaga {required:.2f} PLN, dostępne {available:.2f} PLN"
                )
    cash_before = _money(Decimal(str(compute_cash(portfolio_id, conn))))
    position_before = _position_quantity(portfolio_id, symbol, conn)
    cash_effect = (
        -(gross_pln + commission) if side == "BUY" else gross_pln - commission
    )
    cash_after = _money(cash_before + cash_effect)
    cur = conn.execute(
        "INSERT INTO transactions "
        "(portfolio_id,datetime,executed_at,operation_id,idempotency_key,symbol,market,side,qty,price,"
        "unit_price_currency,currency,fx_rate,fx_rate_pln,unit_price_pln,value_pln,"
        "gross_value_currency,gross_value_pln,commission_pln,cash_effect_pln,reason,source,"
        "request_fingerprint,cash_before_pln,cash_after_pln,position_quantity_before,"
        "position_quantity_after,exit_level,round_id,exchange_order_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            portfolio_id, executed_at, executed_at, str(uuid4()), key, symbol, market, side,
            float(quantity), float(price), float(price), currency, float(rate), float(rate),
            float(unit_pln), float(gross_pln), float(gross_currency), float(gross_pln),
            float(commission), float(cash_effect), reason, source, fingerprint,
            float(cash_before), float(cash_after), position_before, 0.0, exit_level, round_id,
            exchange_order_id,
        ),
    )
    _refresh_positions_projection(portfolio_id, conn)
    position_after = _position_quantity(portfolio_id, symbol, conn)
    conn.execute(
        "UPDATE transactions SET position_quantity_after=? WHERE id=?",
        (position_after, cur.lastrowid),
    )
    tx = dict(conn.execute("SELECT * FROM transactions WHERE id=?", (cur.lastrowid,)).fetchone())
    return _decorate_trade_result(tx, False)


def execute_trade(
    portfolio_id: int,
    symbol: str,
    side: str,
    qty: Optional[float],
    reason: str,
    market: Optional[str] = None,
    exit_level: Optional[float] = None,
    round_id: Optional[int] = None,
    dt: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    commission_pln: float = 0.0,
    source: str = "legacy",
    conn: Optional[sqlite3.Connection] = None,
    take_profit_price: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
) -> dict:
    """Atomowy BUY/SELL po ostatnim close: ledger, cash effect i projekcja.

    #68/#72: dla portfela kind='game' + exchange='okx' + execution_mode='trading'
    (demo trading) deleguje do services.okx_trade.execute_okx_futures_order —
    futures long/short z dźwignią x10 na X-Perps OKX, zamiast ceny EOD Stooq.
    Decyzja usera (2026-07-21): TYLKO futures dla tej kombinacji kind/exchange/
    execution_mode — execute_okx_market_order (spot) zostaje dostępna funkcja
    w services.okx_trade, ale nie jest wołana z tego dispatch.
    Import lazy — unika cyklu importów (okx_trade importuje get_portfolio_row
    z game), analogicznie do take_snapshot_all/okx_sync.
    """
    symbol = (symbol or "").upper().strip()
    side = (side or "").upper().strip()

    own_probe = conn is None
    probe_conn = conn or get_conn()
    try:
        pf_probe = get_portfolio_row(portfolio_id, probe_conn)
    finally:
        if own_probe:
            probe_conn.close()
    if (
        pf_probe.get("kind") == "game"
        and pf_probe.get("exchange") == "okx"
        and pf_probe.get("execution_mode") == "trading"
    ):
        from services.okx_trade import execute_okx_futures_order  # lazy: unika cyklu importów

        return execute_okx_futures_order(
            portfolio_id=portfolio_id,
            symbol=symbol,
            side=side,
            qty=qty,
            reason=reason,
            take_profit_price=take_profit_price,
            stop_loss_price=stop_loss_price,
            market=market,
            exit_level=exit_level,
            round_id=round_id,
            dt=dt,
            idempotency_key=idempotency_key,
            commission_pln=commission_pln,
            source=source,
            conn=conn,
        )

    payload = {
        "kind": "execute_trade", "portfolio_id": portfolio_id, "symbol": symbol,
        "side": side, "quantity": str(qty), "reason": (reason or "").strip(),
        "market": market, "exit_level": exit_level, "round_id": round_id,
        "commission_pln": str(commission_pln),
    }
    fingerprint = _trade_fingerprint(payload)
    own = conn is None
    conn = conn or get_conn()
    try:
        if idempotency_key:
            existing = conn.execute(
                "SELECT * FROM transactions WHERE portfolio_id=? AND idempotency_key=?",
                (portfolio_id, idempotency_key.strip()),
            ).fetchone()
            if existing:
                tx = dict(existing)
                if tx.get("request_fingerprint") != fingerprint:
                    raise IdempotencyConflictError(
                        f"idempotency_key '{idempotency_key}' został już użyty z innymi parametrami"
                    )
                return _decorate_trade_result(tx, True)
        price_result, _price_date = price_for(symbol)
        if price_result is None:
            raise ValidationError(f"brak pliku cenowego dla symbolu {symbol} — nie można ustalić ceny close")
        if isinstance(price_result, tuple) and price_result[0] == "PROXY_SMH":
            raise ValidationError(f"{symbol} ma tylko wycenę proxy — execute_trade wymaga bezpośredniego close")
        currency = currency_for_symbol(symbol)
        if currency == "USD":
            fx_rate, _ = load_fx()
        elif currency == "PLN":
            fx_rate = 1.0
        else:
            fx_rate, _ = fx_rate_for_currency(currency)
            if fx_rate is None:
                raise ValidationError(f"brak kursu {currency}→PLN dla {symbol}")
        begin_immediate(conn)
        try:
            tx = _record_trade(
                portfolio_id=portfolio_id, symbol=symbol, side=side, qty=qty,
                unit_price_currency=price_result, currency=currency, fx_rate_pln=fx_rate,
                commission_pln=commission_pln,
                executed_at=dt or datetime.now().isoformat(timespec="seconds"), reason=reason,
                idempotency_key=idempotency_key, source=source, market=market,
                exit_level=exit_level, round_id=round_id, fingerprint=fingerprint, conn=conn,
                enforce_mandate=True,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not tx["idempotent_replay"]:
            upsert_today_snapshot(portfolio_id, conn)
        return tx
    finally:
        if own:
            conn.close()


def add_transaction(
    portfolio_id: int,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    dt: str,
    currency: str = "PLN",
    commission_pln: float = 0.0,
    note: str = "",
    value_pln: Optional[float] = None,
    fx_rate: Optional[float] = None,
    market: Optional[str] = None,
    idempotency_key: Optional[str] = None,
    source: str = "legacy",
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    """Atomowy import historyczny z jawną ceną, FX, datą i prowizją."""
    if not dt or not str(dt).strip():
        raise ValidationError("executed_at jest wymagane")
    executed_at = str(dt).strip()
    currency = (currency or "PLN").upper().strip()
    as_of = executed_at[:10]
    if fx_rate is None:
        if currency == "PLN":
            fx_rate = 1.0
        elif currency == "USD":
            fx_rate, _ = load_fx_as_of(as_of)
        else:
            fx_rate, _ = fx_rate_for_currency(currency, as_of=as_of)
            if fx_rate is None:
                raise ValidationError(f"brak kursu {currency}→PLN na {as_of}")
    expected = _money(_decimal(qty, "quantity", positive=True) * _money(
        _decimal(price, "unit_price_currency", positive=True) * _decimal(fx_rate, "fx_rate_pln", positive=True)
    ))
    if value_pln is not None and _money(_decimal(value_pln, "value_pln")) != expected:
        raise ValidationError(f"value_pln niezgodne z ceną i FX; backend wyliczył {expected}")
    reason = (note or "").strip() or f"historyczny {side} {symbol}"
    payload = {
        "kind": "add_historical_trade", "portfolio_id": portfolio_id,
        "executed_at": executed_at, "symbol": (symbol or "").upper().strip(),
        "market": market, "side": (side or "").upper().strip(), "quantity": str(qty),
        "currency": currency, "unit_price_currency": str(price), "fx_rate_pln": str(fx_rate),
        "commission_pln": str(commission_pln), "reason": reason,
    }
    fingerprint = _trade_fingerprint(payload)
    own = conn is None
    conn = conn or get_conn()
    try:
        begin_immediate(conn)
        try:
            tx = _record_trade(
                portfolio_id=portfolio_id, symbol=symbol, side=side, qty=qty,
                unit_price_currency=price, currency=currency, fx_rate_pln=fx_rate,
                commission_pln=commission_pln, executed_at=executed_at, reason=reason,
                idempotency_key=idempotency_key, source=source, market=market,
                exit_level=None, round_id=None, fingerprint=fingerprint, conn=conn,
                enforce_mandate=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not tx["idempotent_replay"]:
            upsert_today_snapshot(portfolio_id, conn)
        return tx
    finally:
        if own:
            conn.close()


# ---------- snapshots ----------

def take_snapshot(portfolio_id: int, snap_date: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> dict:
    """Upsert wyceny dziennej. Odrzuca zapis gdy wycena wygląda na uszkodzoną (#30) —
    np. wyliczoną w trakcie migracji ledgera (DELETE cash_entries przed re-INSERT) —
    zamiast zapisać śmieciowy wiersz z ujemną wartością.
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        snap_date = snap_date or date_cls.today().isoformat()
        valuation = get_portfolio_valuation(portfolio_id, conn, as_of=snap_date)
        existing = conn.execute(
            "SELECT * FROM snapshots WHERE portfolio_id=? AND date=?", (portfolio_id, snap_date)
        ).fetchone()
        if valuation["totalValue"] < 0 or valuation["cashPln"] < 0:
            logger.warning(
                "take_snapshot: odrzucono podejrzaną wycenę portfolio_id=%s date=%s "
                "totalValue=%.2f cashPln=%.2f (prawdopodobny stan pośredni ledgera) — zapis pominięty",
                portfolio_id, snap_date, valuation["totalValue"], valuation["cashPln"],
            )
            if existing:
                return dict(existing)
            return {
                "id": None,
                "portfolio_id": portfolio_id,
                "date": snap_date,
                "total_value_pln": valuation["totalValue"],
                "cash_pln": valuation["cashPln"],
                "positions_json": json.dumps(valuation["positions"], ensure_ascii=False),
                "rejected": True,
            }
        if not valuation["valuationComplete"] and existing is not None:
            # #41 pkt 5: wycena niekompletna (brak ceny/kursu dla którejś pozycji) NIE
            # może nadpisać już zapisanego snapshotu — zostawiamy poprzedni "dobry" wiersz,
            # zamiast po cichu zaniżyć NAV o wartość pozycji bez ceny.
            logger.warning(
                "take_snapshot: wycena niekompletna portfolio_id=%s date=%s incompleteSymbols=%s "
                "— istniejący snapshot NIE nadpisany",
                portfolio_id, snap_date, valuation["incompleteSymbols"],
            )
            return dict(existing)
        conn.execute(
            "INSERT INTO snapshots (portfolio_id, date, total_value_pln, cash_pln, positions_json) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(portfolio_id, date) DO UPDATE SET total_value_pln=excluded.total_value_pln, "
            "cash_pln=excluded.cash_pln, positions_json=excluded.positions_json",
            (portfolio_id, snap_date, valuation["totalValue"], valuation["cashPln"], json.dumps(valuation["positions"], ensure_ascii=False)),
        )
        conn.commit()
        return dict(conn.execute(
            "SELECT * FROM snapshots WHERE portfolio_id=? AND date=?", (portfolio_id, snap_date)
        ).fetchone())
    finally:
        if own:
            conn.close()


def upsert_today_snapshot(portfolio_id: int, conn: Optional[sqlite3.Connection] = None) -> Optional[dict]:
    """Wymusza przeliczenie snapshotu dnia bieżącego (#30) — wołane po każdej zmianie
    ledgera (transakcja, cash entry) żeby wykres wartości portfela nie polegał na
    ręcznym backfill_snapshots.py.

    Best-effort: błąd przeliczenia (np. brak pliku cenowego dla jednej z pozycji) jest
    logowany, ale nie przerywa operacji nadrzędnej (transakcja/cash entry jest już
    trwale zapisana przed tym wywołaniem — snapshot to tylko cache do wykresu).
    """
    try:
        return take_snapshot(portfolio_id, date_cls.today().isoformat(), conn)
    except Exception:
        logger.exception(
            "upsert_today_snapshot: przeliczenie snapshotu nie powiodło się dla portfolio_id=%s "
            "— operacja nadrzędna była już zapisana, snapshot pozostaje nieaktualny do następnej próby",
            portfolio_id,
        )
        return None


def take_snapshot_all(snap_date: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Snapshot batch dla wszystkich portfeli aktywnych.

    kind='game': jak dotąd, take_snapshot lokalny (Stooq/ledger).
    kind='real' + exchange='okx': #67 — osobna ścieżka przez
    services.okx_sync.sync_real_okx_portfolio (import lazy, żeby uniknąć
    cyklu game.py <-> okx_sync.py, który importuje get_portfolio_row z game).
    Błąd syncu jednego portfela real+okx (wyjątek domenowy OKX, złapany już
    wewnątrz sync_real_okx_portfolio) NIE przerywa pętli — inne portfele
    (game i pozostałe real+okx) są przetwarzane dalej, a ostatni dobry
    snapshot tego portfela nie jest nadpisywany błędną wyceną.
    """
    from services.okx_sync import sync_real_okx_portfolio  # lazy: unika cyklu importów

    own = conn is None
    conn = conn or get_conn()
    try:
        portfolios = list_portfolios(conn, include_archived=False)
        results = []
        for pf in portfolios:
            if pf["kind"] == "game":
                results.append(take_snapshot(pf["id"], snap_date, conn))
            elif pf["kind"] == "real" and pf.get("exchange") == "okx":
                try:
                    results.append(sync_real_okx_portfolio(pf["id"], conn, snap_date))
                except Exception:
                    # sync_real_okx_portfolio już łapie OkxError wewnętrznie i zwraca
                    # ok=False; ten except to dodatkowa siatka bezpieczeństwa (np. błąd
                    # bazy) żeby jeden portfel nigdy nie wywrócił całego batcha (#67 AC).
                    logger.exception(
                        "take_snapshot_all: nieoczekiwany błąd sync real+okx portfolio_id=%s — "
                        "pominięto, inne portfele przetwarzane dalej",
                        pf["id"],
                    )
                    results.append({"ok": False, "portfolio_id": pf["id"], "sync_status": "error"})
        return results
    finally:
        if own:
            conn.close()


def get_history(portfolio_id: int, period: str = "MAX", conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute(
            "SELECT date, total_value_pln, cash_pln, positions_json "
            "FROM snapshots WHERE portfolio_id=? ORDER BY date",
            (portfolio_id,),
        ).fetchall()
        pf = get_portfolio_row(portfolio_id, conn)
        rows = [dict(r) for r in rows]
        profile = pf.get("strategy_profile") or {}
        rebuild_transaction_cash = cash_method_bossa(profile) or (
            profile.get("source") == "historia.db"
            and profile.get("cash_from_cashflow_only") is False
        )
        if rebuild_transaction_cash:
            # Snapshot jest cache'em, ale kontrakt cash IKZE_ZONA zmienił się z
            # SUM(cash_entries) na wpłaty - BUY + SELL + dywidendy. Stare wiersze
            # zachowały przez to zawyżony cash i po dopisaniu poprawnego punktu
            # tworzyły na wykresie sztuczny klif. Starsze bazy nie mają jeszcze
            # cash_method=bossa, dlatego rozpoznajemy też równoważny kontrakt
            # source=historia.db + cash_from_cashflow_only=false. Pozycje mają historyczną
            # wycenę EOD w positions_json, więc odtwarzamy NAV z aktualnego ledgera
            # cash bez kosztownego ponownego pobierania cen dla każdego dnia.
            normalized = []
            for row in rows:
                positions = json.loads(row.get("positions_json") or "[]")
                values = [p.get("marketValuePln", p.get("marketValue")) for p in positions]
                if any(value is None for value in values):
                    # Nie zastępuj kompletnego legacy NAV sumą niekompletnej wyceny.
                    total_value = float(row["total_value_pln"])
                    cash = float(row["cash_pln"])
                else:
                    cash = compute_cash(portfolio_id, conn, as_of=row["date"])
                    total_value = cash + sum(float(value) for value in values)
                normalized.append({
                    "date": row["date"],
                    "total_value_pln": round(total_value, 2),
                    "cash_pln": round(cash, 2),
                })
            rows = normalized
        else:
            rows = [
                {
                    "date": row["date"],
                    "total_value_pln": row["total_value_pln"],
                    "cash_pln": row["cash_pln"],
                }
                for row in rows
            ]
        if period == "MAX" or not rows:
            return rows
        days_map = {"1D": 1, "1W": 7, "1M": 30, "3M": 90, "YTD": None}
        n = days_map.get(period)
        if period == "YTD":
            year = date_cls.today().year
            return [r for r in rows if r["date"] >= f"{year}-01-01"]
        if n is not None:
            return rows[-n:]
        return rows
    finally:
        if own:
            conn.close()


# ---------- leaderboard ----------

def list_portfolios_board(
    conn: Optional[sqlite3.Connection] = None,
    *,
    kinds: Optional[tuple[str, ...]] = None,
) -> list[dict]:
    """Lista aktywnych portfeli z wyceną (MCP list_portfolios / delete verification).

    kinds=None → wszystkie niezaarchiwizowane (game + real, np. OKX Real).
    kinds=("game",) → tylko portfele gry (jak historyczny ranking).
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        portfolios = list_portfolios(conn, include_archived=False)
        board = []
        for pf in portfolios:
            kind = pf["kind"]
            if kinds is not None and kind not in kinds:
                continue
            val = get_portfolio_valuation(pf["id"], conn)
            # #51: deposits/resultVsDeposits pochodzą wyłącznie z get_portfolio_valuation
            # (ledger cash_entries) — starting_capital nie jest już fallbackiem.
            deposits = val.get("deposits")
            result_vs = val.get("resultVsDeposits")
            read_only = bool(
                pf.get("exchange") and pf.get("execution_mode") in READ_ONLY_EXECUTION_MODES
            )
            board.append({
                "portfolioId": pf["id"],
                "key": pf["name"],
                "name": pf["name"],
                "kind": kind,
                "managedBy": pf.get("managed_by", "ai"),
                "readOnly": read_only,
                "totalValue": val["totalValue"],
                "returnPct": val["returnPct"],
                # #72: zysk dzienny % na leaderboardzie (obok łącznego returnPct)
                "dayReturnPct": val.get("portfolioDayReturnPct"),
                "usExposurePct": val["usExposurePct"],
                "deposits": deposits,
                "resultVsDeposits": result_vs,
                "valuationComplete": val.get("valuationComplete", True),
            })
        board.sort(key=lambda x: -(x["returnPct"] if x["returnPct"] is not None else -1e9))
        return board
    finally:
        if own:
            conn.close()


def get_leaderboard(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Ranking gry z tracker.db (user + AI) — kind=game oraz real+okx (#69).

    real+okx (kind='real', exchange='okx') to jedyny portfel real dołączony do
    rankingu gry. list_portfolios_board(kinds=...) filtruje wyłącznie po kind,
    więc real+okx dociągamy osobno po kind='real' i zawężamy tu do exchange='okx'
    (inne ewentualne portfele kind='real' bez giełdy mają pozostać poza rankingiem).
    """
    own = conn is None
    conn = conn or get_conn()
    try:
        board = list_portfolios_board(conn, kinds=("game",))
        real_portfolios = [
            pf for pf in list_portfolios(conn, include_archived=False)
            if pf["kind"] == "real" and pf.get("exchange") == "okx"
        ]
        real_ids = {pf["id"] for pf in real_portfolios}
        if real_ids:
            real_board = list_portfolios_board(conn, kinds=("real",))
            board.extend(row for row in real_board if row["portfolioId"] in real_ids)
        board.sort(key=lambda x: -(x["returnPct"] if x["returnPct"] is not None else -1e9))
        return board
    finally:
        if own:
            conn.close()

# ---------- recommendations ----------

def create_recommendation(
    author: str, symbol: str, action: str, thesis: str, confidence: int,
    market: Optional[str] = None, target_price: Optional[float] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict:
    if action not in ("BUY", "SELL", "HOLD", "WATCH"):
        raise ValidationError(f"action musi być BUY/SELL/HOLD/WATCH, otrzymano: {action}")
    if not (1 <= confidence <= 5):
        raise ValidationError("confidence musi być w zakresie 1-5")
    own = conn is None
    conn = conn or get_conn()
    try:
        price_result, _ = price_for(symbol)
        price_at_reco = price_result if isinstance(price_result, (int, float)) else None
        cur = conn.execute(
            "INSERT INTO recommendations (author, symbol, market, action, confidence, price_at_reco, target_price, thesis) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (author, symbol, market, action, confidence, price_at_reco, target_price, thesis),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM recommendations WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        if own:
            conn.close()


def list_recommendations(status: Optional[str] = None, conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM recommendations WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM recommendations ORDER BY created_at DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def resolve_recommendation(reco_id: int, status: str, outcome_note: str = "", conn: Optional[sqlite3.Connection] = None) -> dict:
    if status not in ("active", "executed", "expired", "invalidated"):
        raise ValidationError(f"status nieprawidłowy: {status}")
    own = conn is None
    conn = conn or get_conn()
    try:
        conn.execute(
            "UPDATE recommendations SET status=?, outcome_note=?, resolved_at=? WHERE id=?",
            (status, outcome_note, datetime.now().isoformat(timespec="seconds"), reco_id),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM recommendations WHERE id=?", (reco_id,)).fetchone()
        if row is None:
            raise NotFoundError(f"recommendation {reco_id} nie istnieje")
        return dict(row)
    finally:
        if own:
            conn.close()


# ---------- rounds ----------

def create_round(date_str: str, summary_md: str, author: str, conn: Optional[sqlite3.Connection] = None) -> dict:
    own = conn is None
    conn = conn or get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO rounds (date, summary_md, author) VALUES (?, ?, ?)", (date_str, summary_md, author)
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM rounds WHERE id=?", (cur.lastrowid,)).fetchone())
    finally:
        if own:
            conn.close()


def list_rounds(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    own = conn is None
    conn = conn or get_conn()
    try:
        rows = conn.execute("SELECT * FROM rounds ORDER BY date DESC, id DESC").fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()


def list_transactions(
    portfolio_id: int,
    conn: Optional[sqlite3.Connection] = None,
    *,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    symbol: Optional[str] = None,
    side: Optional[str] = None,
) -> list[dict]:
    if side is not None and side.upper() not in ("BUY", "SELL"):
        raise ValidationError("side musi być BUY lub SELL")
    own = conn is None
    conn = conn or get_conn()
    try:
        get_portfolio_row(portfolio_id, conn)
        clauses = ["portfolio_id=?"]
        params: list[Any] = [portfolio_id]
        if date_from:
            clauses.append("substr(COALESCE(executed_at,datetime),1,10)>=?")
            params.append(date_from)
        if date_to:
            clauses.append("substr(COALESCE(executed_at,datetime),1,10)<=?")
            params.append(date_to)
        if symbol:
            clauses.append("symbol=?")
            params.append(symbol.upper().strip())
        if side:
            clauses.append("side=?")
            params.append(side.upper())
        rows = conn.execute(
            "SELECT * FROM transactions WHERE " + " AND ".join(clauses)
            + " ORDER BY COALESCE(executed_at,datetime) ASC, id ASC",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        if own:
            conn.close()
