"""Tymczasowy lokalny store zgodności dla kodu agenta podczas migracji."""
import os
import sqlite3
from pathlib import Path

BACKEND_DIR = Path(__file__).parent.parent
BOT_ROOT = Path(os.environ.get("CRYPTO_RUNTIME_ROOT", BACKEND_DIR.parent / "data" / "runtime")).resolve()
TRACKER_DB = BOT_ROOT / "compat" / "tracker.db"
HISTORIA_DB = BOT_ROOT / "stocks" / "historia_transakcji" / "historia.db"
GRA_DIR = BOT_ROOT / "stocks" / "gra"
PRICE_FOLDERS = [
    BOT_ROOT / "stocks" / "portfele",
    BOT_ROOT / "stocks" / "kandydaci",
    BOT_ROOT / "stocks" / "watchlist_extra",
]


def get_conn(db_path: Path = TRACKER_DB) -> sqlite3.Connection:
    """Połączenie do tracker.db w trybie WAL, read-write.

    isolation_level=None → pełna kontrola nad BEGIN/COMMIT/ROLLBACK (sqlite3 nie
    wstawia własnego niejawnego BEGIN przed DML). Wymagane, żeby `begin_immediate`
    mógł otworzyć transakcję piszącą (BEGIN IMMEDIATE) *przed* pierwszym SELECT-em
    walidacji — inaczej dwa równoległe wątki mogłyby oba przeczytać ten sam stan
    (np. dostępną gotówkę) przed zapisem i oba przejść walidację (TASK_026-B).
    timeout=30s → przy BEGIN IMMEDIATE na zajętej bazie czekaj na zwolnienie
    blokady zamiast natychmiastowego `database is locked`.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def begin_immediate(conn: sqlite3.Connection) -> None:
    """Otwiera transakcję piszącą natychmiast (BEGIN IMMEDIATE).

    Musi być pierwsza instrukcja operacji nadrzędnej (przed jakimkolwiek SELECT-em
    użytym do walidacji) — blokuje bazę do zapisu od razu, więc drugi równoległy
    wątek czeka (do busy_timeout) zamiast czytać przedwczesny stan i przechodzić
    tę samą walidację równolegle (np. dwa BUY na ostatnią dostępną gotówkę).
    Wołający odpowiada za `conn.commit()` na sukces i `conn.rollback()` na błąd —
    funkcje pomocnicze, które dostały `conn` operacji nadrzędnej, NIE commitują.
    """
    conn.execute("BEGIN IMMEDIATE")


def get_historia_conn_readonly() -> sqlite3.Connection:
    """Połączenie do historia.db w trybie ściśle read-only (uri mode=ro).

    historia.db jest source of truth dla realnych kont (IKE/IKZE/IKZE_ZONA),
    nigdy nie zapisujemy do niego z tej aplikacji.
    """
    uri = f"file:{HISTORIA_DB}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn
