#!/usr/bin/env python3
"""Manage the shared list of symbols used by crypto backfills."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx


BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9_-]*(?:-[A-Z0-9_]+)+$")
_MAX_SYMBOL_LENGTH = 64


def _print_add_result(status: str, symbol: str, exit_code: int) -> int:
    print(json.dumps({"status": status, "symbol": symbol, "exit_code": exit_code}))
    return exit_code


def _instrument_type(symbol: str) -> str | None:
    if symbol.endswith("-SWAP"):
        return "SWAP"
    if "_XPERP-" in symbol:
        return "FUTURES"
    return None


def _is_supported_live_instrument(
    payload: Any, *, symbol: str, instrument_type: str
) -> bool:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise ValueError("OKX returned an invalid instruments response")

    for instrument in payload["data"]:
        if not isinstance(instrument, dict) or instrument.get("instId") != symbol:
            continue
        if instrument.get("state") != "live":
            return False
        if instrument_type == "SWAP":
            return instrument.get("instType", "SWAP") == "SWAP"

        inst_family = instrument.get("instFamily")
        return (
            instrument.get("instType", "FUTURES") == "FUTURES"
            and (
                instrument.get("ruleType") == "xperp"
                or (isinstance(inst_family, str) and inst_family.endswith("_XPERP"))
            )
        )
    return False


def list_symbols() -> None:
    """Print the validated backfill symbols, one per line."""
    from services.crypto_data_lake import _load_backfill_symbols

    symbols = _load_backfill_symbols()
    print("\n".join(symbols))


def add_symbol(
    raw_symbol: str,
    *,
    config_path: Path | None = None,
    client: Any | None = None,
) -> int:
    """Validate an OKX derivative and append it to the shared config safely."""
    from services.crypto_data_lake import (
        _BACKFILL_SYMBOLS_CONFIG,
        _load_backfill_symbols,
    )
    from services.okx_client import OkxClient, OkxError

    symbol = (raw_symbol or "").strip().upper()
    instrument_type = _instrument_type(symbol)
    if (
        not 3 <= len(symbol) <= _MAX_SYMBOL_LENGTH
        or not _SYMBOL_PATTERN.fullmatch(symbol)
        or instrument_type is None
    ):
        return _print_add_result("invalid", symbol, 2)

    owns_client = client is None
    okx = client or OkxClient(alias="backfill_symbols")
    try:
        payload = okx.get_instruments(instrument_type)
        if not _is_supported_live_instrument(
            payload, symbol=symbol, instrument_type=instrument_type
        ):
            return _print_add_result("invalid", symbol, 2)
    except (OkxError, httpx.TransportError, OSError, ValueError) as exc:
        print(f"ERROR: OKX validation failed: {exc}", file=sys.stderr)
        return _print_add_result("api_error", symbol, 3)
    finally:
        if owns_client:
            okx.close()

    path = Path(config_path) if config_path is not None else _BACKFILL_SYMBOLS_CONFIG
    lock_path = path.with_name(f"{path.name}.lock")
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            symbols = list(_load_backfill_symbols(path))
            if symbol in symbols:
                return _print_add_result("duplicate", symbol, 4)

            symbols.append(symbol)
            file_descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
            )
            temporary_path = Path(temporary_name)
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                os.chmod(temporary_path, path.stat().st_mode & 0o777)
                json.dump(
                    {"schema_version": 1, "symbols": symbols},
                    handle,
                    ensure_ascii=False,
                    indent=2,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, path)
            temporary_path = None
    except (OSError, ValueError) as exc:
        print(f"ERROR: could not update symbol configuration: {exc}", file=sys.stderr)
        return _print_add_result("storage_error", symbol, 5)
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass

    return _print_add_result("added", symbol, 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("list", "add"))
    parser.add_argument("symbol", nargs="?")
    args = parser.parse_args(argv)

    if args.command == "add":
        if args.symbol is None:
            parser.error("add requires SYMBOL")
        return add_symbol(args.symbol)
    if args.symbol is not None:
        parser.error("list does not accept SYMBOL")

    try:
        list_symbols()
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 5
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
