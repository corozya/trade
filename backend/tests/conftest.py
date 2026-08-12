"""Fixtures pytest: izolowana baza tracker.db (tmp) + izolowane pliki cenowe/FX (tmp).

Testy NIE dotykają prawdziwego tracker.db ani stocks/gra/* — wszystko w tmp_path,
zgodnie z zasadą projektu (stocks/gra/* i historia.db są read-only, source of truth).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import pytest

import services.db as db_module
import services.pricing as pricing_module
from init_db import init_db


PRICE_CSV_HEADER = "<TICKER>,<PER>,<DATE>,<TIME>,<OPEN>,<HIGH>,<LOW>,<CLOSE>,<VOL>,<OPENINT>\n"


def _write_price_file(folder: Path, filename: str, ticker: str, close: float, date: str = "20260718"):
    folder.mkdir(parents=True, exist_ok=True)
    row = f"{ticker},D,{date},000000,{close},{close},{close},{close},1000,0\n"
    (folder / filename).write_text(PRICE_CSV_HEADER + row)


@pytest.fixture
def price_env(tmp_path, monkeypatch):
    """Podmienia PRICE_FOLDERS / GRA_DIR na katalog tymczasowy z syntetycznymi cenami."""
    price_folder = tmp_path / "prices"
    gra_dir = tmp_path / "gra"
    gra_dir.mkdir()

    # Kilka symboli testowych z ustalonymi cenami close.
    _write_price_file(price_folder, "abc.txt", "ABC", close=100.0)
    _write_price_file(price_folder, "xyz.us.txt", "XYZ.US", close=50.0)
    _write_price_file(price_folder, "def.txt", "DEF", close=20.0)
    # .L (London/GBP) — do testów #41 pkt 4 (waluty instrumentów / kurs GBPPLN).
    _write_price_file(price_folder, "ghi.l.txt", "GHI.L", close=10.0)

    (gra_dir / "fx.json").write_text(json.dumps({
        "USDPLN": {"rate": 4.0, "date": "2026-07-18", "source": "test"}
    }))

    gbppln_path = tmp_path / "gbppln.txt"
    gbppln_path.write_text(
        PRICE_CSV_HEADER + "GBPPLN,D,20260718,000000,5.0,5.0,5.0,5.0,1000,0\n"
    )

    monkeypatch.setattr(pricing_module, "PRICE_FOLDERS", [price_folder])
    monkeypatch.setattr(pricing_module, "GRA_DIR", gra_dir)
    monkeypatch.setattr(pricing_module, "GBPPLN_PATH", gbppln_path)
    monkeypatch.setattr(db_module, "GRA_DIR", gra_dir)
    # Puste archiwum Stooq w testach — tylko PRICE_FOLDERS
    monkeypatch.setattr(pricing_module, "_STOOQ_INDEX", {})
    if hasattr(pricing_module, "clear_pricing_caches"):
        pricing_module.clear_pricing_caches()

    return {"price_folder": price_folder, "gra_dir": gra_dir, "gbppln_path": gbppln_path}


@pytest.fixture
def db_path(tmp_path, price_env):
    """Ścieżka do izolowanej tracker.db (schema utworzona)."""
    path = tmp_path / "test_tracker.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db_path):
    """Świeże połączenie do izolowanego tracker.db (schema utworzona)."""
    c = db_module.get_conn(db_path)
    yield c
    c.close()
