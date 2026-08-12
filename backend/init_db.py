#!/usr/bin/env python3
"""Tworzy/aktualizuje schema SQLite `tracker.db` (Portfolio Tracker v2).

Bez Alembica (celowo, patrz SPEC-V2.md sekcja 3) — schema jest tworzona
idempotentnie przez `CREATE TABLE IF NOT EXISTS`. Dla przyszłych zmian
kolumn: dopisz proste `ALTER TABLE ... ADD COLUMN` chronione sprawdzeniem
`PRAGMA table_info` (patrz `_ensure_column` niżej) — wystarczające dla
zakresu tego projektu (1 plik, 1 developer, RPi4).

Użycie: python init_db.py [--db sciezka/do/tracker.db]
"""
import argparse
import sqlite3
from pathlib import Path
from typing import Optional

DEFAULT_DB_PATH = Path(__file__).parent / "tracker.db"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL CHECK (type IN ('ai', 'human', 'benchmark')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS portfolios (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    player_id INTEGER NOT NULL REFERENCES players(id),
    name TEXT NOT NULL,
    base_currency TEXT NOT NULL DEFAULT 'PLN',
    starting_capital REAL NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('game', 'real')),
    managed_by TEXT NOT NULL DEFAULT 'ai' CHECK (managed_by IN ('user', 'ai')),
    mandate_md TEXT NOT NULL DEFAULT '',
    strategy_profile TEXT NOT NULL DEFAULT '{}',
    mandate_updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    archived INTEGER NOT NULL DEFAULT 0,
    accent_color TEXT
);

CREATE TABLE IF NOT EXISTS cash_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    date TEXT NOT NULL,
    type TEXT NOT NULL CHECK (type IN (
        'wplata', 'wyplata', 'odsetki', 'podatek', 'dywidenda',
        'blokada', 'odblokowanie', 'rozliczenie_transakcji'
    )),
    amount REAL NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    currency TEXT,
    idempotency_key TEXT,
    source TEXT NOT NULL DEFAULT 'legacy',
    cash_before REAL,
    cash_after REAL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    summary_md TEXT NOT NULL DEFAULT '',
    author TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    datetime TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT,
    side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty REAL NOT NULL,
    price REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT 'PLN',
    fx_rate REAL NOT NULL DEFAULT 1.0,
    value_pln REAL NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    exit_level REAL,
    round_id INTEGER REFERENCES rounds(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS okx_execution_requests (
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    idempotency_key TEXT NOT NULL,
    request_fingerprint TEXT NOT NULL,
    cl_ord_id TEXT NOT NULL,
    state TEXT NOT NULL,
    exchange_order_id TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (portfolio_id, idempotency_key),
    UNIQUE (cl_ord_id)
);

CREATE TABLE IF NOT EXISTS portfolio_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id) ON DELETE CASCADE,
    symbol TEXT NOT NULL,
    market TEXT,
    quantity REAL NOT NULL CHECK (quantity > 0),
    currency TEXT NOT NULL,
    average_unit_cost_currency REAL NOT NULL,
    average_unit_cost_pln REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (portfolio_id, symbol)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
    date TEXT NOT NULL,
    total_value_pln REAL NOT NULL,
    cash_pln REAL NOT NULL,
    positions_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (portfolio_id, date)
);

CREATE TABLE IF NOT EXISTS recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    author TEXT NOT NULL,
    symbol TEXT NOT NULL,
    market TEXT,
    action TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD', 'WATCH')),
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 1 AND 5),
    price_at_reco REAL,
    target_price REAL,
    thesis TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'executed', 'expired', 'invalidated')),
    resolved_at TEXT,
    outcome_note TEXT
);

CREATE TABLE IF NOT EXISTS prices (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS fx (
    pair TEXT NOT NULL,
    date TEXT NOT NULL,
    rate REAL NOT NULL,
    PRIMARY KEY (pair, date)
);

-- Seria EOD equity/ETF importowana z plikow Stooq do bazy (zamiast pricing.py
-- czytajacego .txt na zywo przy kazdym request). Importowana od (data
-- pierwszego BUY symbolu we wszystkich portfelach - 1 sesja, bufor na
-- change_pct_1d pierwszego dnia) — nie cala historia Stooq. Symbol to ticker
-- PO przejsciu przez NAME_TO_TICKER/resolve_ticker (np. "CLC.WA", nie
-- "COLUMBUS"). Krypto futures (BTC/ETH) i FX (USDPLN/GBPPLN) NIE sa tu —
-- inne zrodlo (OKX JSON / Stooq currencies), pozostaja poza tabela prices.
CREATE TABLE IF NOT EXISTS price_history (
    symbol TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL NOT NULL,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, date)
);

CREATE INDEX IF NOT EXISTS idx_transactions_portfolio ON transactions(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_transactions_symbol_side ON transactions(symbol, side, datetime);
CREATE INDEX IF NOT EXISTS idx_portfolio_positions_portfolio ON portfolio_positions(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_cash_entries_portfolio ON cash_entries(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_snapshots_portfolio ON snapshots(portfolio_id);
CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations(status);
"""


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    """Dodaje kolumnę jeśli brak (SQLite nie ma IF NOT EXISTS dla ADD COLUMN)."""
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


_CASH_ENTRIES_TYPE_CHECK = (
    "'wplata', 'wyplata', 'odsetki', 'podatek', 'dywidenda', "
    "'blokada', 'odblokowanie', 'rozliczenie_transakcji'"
)


def _cash_entries_sql(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='cash_entries'"
    ).fetchone()
    if row is None or not row[0]:
        return None
    return row[0]


def _cash_entries_allows_dywidenda(conn: sqlite3.Connection) -> bool:
    """True gdy CHECK na cash_entries.type zawiera 'dywidenda' (lub tabela nie istnieje)."""
    sql = _cash_entries_sql(conn)
    if sql is None:
        return True
    return "dywidenda" in sql


def _cash_entries_allows_broker_cashflow_types(conn: sqlite3.Connection) -> bool:
    """True gdy CHECK zawiera typy cashflow brokera (#25)."""
    sql = _cash_entries_sql(conn)
    if sql is None:
        return True
    return "blokada" in sql and "rozliczenie_transakcji" in sql


def _migrate_cash_entries_types(conn: sqlite3.Connection) -> None:
    """SQLite nie pozwala ALTER CHECK — recreate z pełnym zbiorem typów (#20/#25)."""
    if _cash_entries_allows_broker_cashflow_types(conn):
        return
    old_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(cash_entries)").fetchall()
    }
    optional_selects = {
        "currency": "currency" if "currency" in old_columns else "NULL",
        "idempotency_key": (
            "idempotency_key" if "idempotency_key" in old_columns else "NULL"
        ),
        "source": "source" if "source" in old_columns else "'legacy'",
        "cash_before": "cash_before" if "cash_before" in old_columns else "NULL",
        "cash_after": "cash_after" if "cash_after" in old_columns else "NULL",
    }
    conn.execute(
        f"""
        CREATE TABLE cash_entries_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            portfolio_id INTEGER NOT NULL REFERENCES portfolios(id),
            date TEXT NOT NULL,
            type TEXT NOT NULL CHECK (type IN ({_CASH_ENTRIES_TYPE_CHECK})),
            amount REAL NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            currency TEXT,
            idempotency_key TEXT,
            source TEXT NOT NULL DEFAULT 'legacy',
            cash_before REAL,
            cash_after REAL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    conn.execute(
        "INSERT INTO cash_entries_new "
        "(id, portfolio_id, date, type, amount, note, currency, idempotency_key, "
        "source, cash_before, cash_after, created_at) "
        "SELECT id, portfolio_id, date, type, amount, note, "
        f"{optional_selects['currency']}, {optional_selects['idempotency_key']}, "
        f"{optional_selects['source']}, {optional_selects['cash_before']}, "
        f"{optional_selects['cash_after']}, created_at FROM cash_entries"
    )
    conn.execute("DROP TABLE cash_entries")
    conn.execute("ALTER TABLE cash_entries_new RENAME TO cash_entries")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cash_entries_portfolio "
        "ON cash_entries(portfolio_id)"
    )


def _migrate_cash_entries_dywidenda(conn: sqlite3.Connection) -> None:
    """Kompat: stary hook #20 → pełna migracja typów (#25)."""
    _migrate_cash_entries_types(conn)


def _seed_default_accent_colors(conn: sqlite3.Connection) -> None:
    """Uzupełnia accent_color tylko gdy NULL — nie nadpisuje ręcznych wyborów."""
    from services.game import DEFAULT_ACCENT_BY_NAME

    for name, color in DEFAULT_ACCENT_BY_NAME.items():
        conn.execute(
            "UPDATE portfolios SET accent_color=? WHERE name=? AND (accent_color IS NULL OR accent_color='')",
            (color, name),
        )


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA_SQL)
        _ensure_column(
            conn,
            "portfolios",
            "managed_by",
            "managed_by TEXT NOT NULL DEFAULT 'ai'",
        )
        _ensure_column(
            conn,
            "portfolios",
            "accent_color",
            "accent_color TEXT",
        )
        # OKX integracja (#65): exchange/execution_mode/credential alias/sync pola.
        # Walidacja spójności kind+exchange+execution_mode żyje w Pythonie
        # (services/game.py: validate_exchange_config) — CHECK w SQLite nie
        # potrafi wyrazić cross-column constraint.
        _ensure_column(conn, "portfolios", "exchange", "exchange TEXT")
        _ensure_column(
            conn,
            "portfolios",
            "execution_mode",
            "execution_mode TEXT CHECK (execution_mode IN ('read_only', 'trading'))",
        )
        _ensure_column(
            conn,
            "portfolios",
            "exchange_credential_alias",
            "exchange_credential_alias TEXT",
        )
        _ensure_column(
            conn, "portfolios", "exchange_account_id", "exchange_account_id TEXT"
        )
        _ensure_column(conn, "portfolios", "last_synced_at", "last_synced_at TEXT")
        _ensure_column(conn, "portfolios", "sync_status", "sync_status TEXT")
        _migrate_cash_entries_dywidenda(conn)
        _ensure_column(conn, "cash_entries", "currency", "currency TEXT")
        _ensure_column(
            conn, "cash_entries", "idempotency_key", "idempotency_key TEXT"
        )
        _ensure_column(
            conn,
            "cash_entries",
            "source",
            "source TEXT NOT NULL DEFAULT 'legacy'",
        )
        _ensure_column(conn, "cash_entries", "cash_before", "cash_before REAL")
        _ensure_column(conn, "cash_entries", "cash_after", "cash_after REAL")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_cash_entries_portfolio_idempotency "
            "ON cash_entries(portfolio_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
        transaction_columns = (
            ("operation_id", "operation_id TEXT"),
            ("idempotency_key", "idempotency_key TEXT"),
            ("executed_at", "executed_at TEXT"),
            ("unit_price_currency", "unit_price_currency REAL"),
            ("fx_rate_pln", "fx_rate_pln REAL"),
            ("unit_price_pln", "unit_price_pln REAL"),
            ("gross_value_currency", "gross_value_currency REAL"),
            ("gross_value_pln", "gross_value_pln REAL"),
            ("commission_pln", "commission_pln REAL NOT NULL DEFAULT 0"),
            ("cash_effect_pln", "cash_effect_pln REAL"),
            ("source", "source TEXT NOT NULL DEFAULT 'legacy'"),
            ("request_fingerprint", "request_fingerprint TEXT"),
            ("cash_before_pln", "cash_before_pln REAL"),
            ("cash_after_pln", "cash_after_pln REAL"),
            ("position_quantity_before", "position_quantity_before REAL"),
            ("position_quantity_after", "position_quantity_after REAL"),
            # #68: łączy wiersz lokalny (fill, ew. częściowy) z realnym zleceniem
            # OKX (ordId). Osobny od idempotency_key — idempotency_key dedupikuje
            # lokalne requesty; exchange_order_id wiąże fill<->zlecenie giełdowe.
            ("exchange_order_id", "exchange_order_id TEXT"),
        )
        for column, ddl in transaction_columns:
            _ensure_column(conn, "transactions", column, ddl)
        price_columns = (
            ("market", "market TEXT"),
            ("currency", "currency TEXT"),
            ("source", "source TEXT NOT NULL DEFAULT 'stooq'"),
            ("updated_at", "updated_at TEXT"),
            # Sciezka pliku Stooq, z ktorego faktycznie odczytano close (np.
            # stocks/stooq/us/data/daily/us/nasdaq etfs/spcx.us.txt) — pozwala
            # bez przeszukiwania dysku zobaczyc ktory region/plik stoi za cena,
            # gdy pobrano tylko czesc regionow i ceny sa niespojne miedzy symbolami.
            ("source_path", "source_path TEXT"),
            # Zmiana % dzien-do-dnia (close vs poprzednia sesja EOD), ten sam
            # wzor co day_change_pct w _compute_position_performance_metrics —
            # cachowana przy zapisie, zeby nie liczyc jej od nowa z pelnej
            # serii cenowej przy kazdym odczycie.
            ("change_pct_1d", "change_pct_1d REAL"),
        )
        for column, ddl in price_columns:
            _ensure_column(conn, "prices", column, ddl)
        # Zachowaj stare rekordy: nowe nazwy są jednoznacznymi aliasami legacy.
        conn.execute(
            "UPDATE transactions SET "
            "operation_id=COALESCE(operation_id, 'legacy-' || id), "
            "executed_at=COALESCE(executed_at, datetime), "
            "unit_price_currency=COALESCE(unit_price_currency, price), "
            "fx_rate_pln=COALESCE(fx_rate_pln, fx_rate), "
            "unit_price_pln=COALESCE(unit_price_pln, price * fx_rate), "
            "gross_value_currency=COALESCE(gross_value_currency, qty * price), "
            "gross_value_pln=COALESCE(gross_value_pln, value_pln), "
            "cash_effect_pln=COALESCE(cash_effect_pln, "
            "CASE side WHEN 'BUY' THEN -value_pln ELSE value_pln END)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_operation_id "
            "ON transactions(operation_id) WHERE operation_id IS NOT NULL"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_transactions_portfolio_idempotency "
            "ON transactions(portfolio_id, idempotency_key) "
            "WHERE idempotency_key IS NOT NULL"
        )
        # #68: index (NIE unique) na (portfolio_id, exchange_order_id) — lookup
        # szybki dla powiązania fill<->zlecenie OKX. Świadomie nie-unique: jedno
        # zlecenie market może wypełnić się w N fill'ach (partial fill), każdy
        # zapisywany jako osobny wiersz transactions z tym samym exchange_order_id
        # (Scope #68 pkt 3) — unique constraint na samej parze uniemożliwiłby to.
        # Idempotencja lokalna (brak podwójnego zlecenia na OKX) jest już
        # zapewniona przez uq_transactions_portfolio_idempotency powyżej.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_transactions_portfolio_exchange_order "
            "ON transactions(portfolio_id, exchange_order_id) "
            "WHERE exchange_order_id IS NOT NULL"
        )
        _seed_default_accent_colors(conn)
        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    init_db(args.db)
    print(f"OK: schema utworzona/zaktualizowana w {args.db} (WAL mode)")


if __name__ == "__main__":
    main()
