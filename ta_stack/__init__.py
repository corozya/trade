"""TA snapshot stack — Faza A (#9). Jednolity JSON stocks+crypto."""

from .snapshot import build_snapshot, REQUIRED_TOP_LEVEL
from .loaders import load_stooq_csv, load_bitget_feather

__all__ = [
    "build_snapshot",
    "REQUIRED_TOP_LEVEL",
    "load_stooq_csv",
    "load_bitget_feather",
]
