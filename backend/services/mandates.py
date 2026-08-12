"""Presety mandatów portfela gry (SPEC-V2.md sekcja 4a) — stałe w kodzie serwisu."""

# Typy aktywów w strategy_profile.allowed_asset_types
ASSET_TYPES = (
    "akcje_pln",
    "akcje_zagraniczne",
    "lokaty",
    "krypto",
)

ASSET_TYPE_LABELS = {
    "akcje_pln": "Akcje PL",
    "akcje_zagraniczne": "Akcje zagraniczne",
    "lokaty": "Lokaty (cash)",
    "krypto": "Krypto",
}

MANDATE_PRESETS = {
    "Długoterminowy": {
        "mandate_md": (
            "Portfel długoterminowy: spółki dywidendowe i ETF, rotacja rzadka, "
            "sprzedaż tylko przy pogorszeniu fundamentów. Horyzont wielomiesięczny/wieloletni, "
            "unikaj impulsywnych decyzji na krótkoterminowym szumie cenowym."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 20,
            "min_cash_pct": 5,
            "max_us_pct": 50,
            "max_trades_per_round": 2,
            "requires_fundamental_check": True,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
        },
    },
    "Spekulacyjny": {
        "mandate_md": (
            "Portfel spekulacyjny: momentum, wybicia, krótki horyzont, szybkie cięcie strat. "
            "Akceptowana wyższa zmienność i rotacja kapitału, każda pozycja z jasnym poziomem wyjścia."
        ),
        "strategy_profile": {
            "horizon": "spec",
            "max_position_pct": 15,
            "min_cash_pct": 10,
            "max_us_pct": 50,
            "max_trades_per_round": 5,
            "requires_fundamental_check": False,
            "stop_loss_required": True,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
        },
    },
    "Dywidendowy": {
        "mandate_md": (
            "Portfel dywidendowy: stabilne spółki z historią wypłat dywidend, nacisk na "
            "jakość bilansu i powtarzalność zysków, rotacja bardzo rzadka."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 25,
            "min_cash_pct": 5,
            "max_us_pct": 50,
            "max_trades_per_round": 2,
            "requires_fundamental_check": True,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
        },
    },
}

DEFAULT_STRATEGY_PROFILE = {
    "horizon": "swing",
    "max_position_pct": 20,
    "min_cash_pct": 5,
    "max_us_pct": 50,
    "max_trades_per_round": 3,
    "requires_fundamental_check": True,
    "stop_loss_required": False,
    "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
}

# Mandaty startowe Agent Gry (#8) + Lokata (#10)
GAME_PLAYER_MANDATES = {
    "Claude-clone": {
        "mandate_md": (
            "Portfel Claude-clone: aktywne zarządzanie odwzorowanym portfelem (start = "
            "przeskalowany realny portfel IKE+IKZE+IKZE_ZONA z 16.07.2026). Może sprzedawać "
            "słabe pozycje i rotować kapitał do lepszych sygnałów — w odróżnieniu od biernego "
            "trzymania. Zasady gry: limit 50% ekspozycji USA, każda nowa pozycja wymaga "
            "weryfikacji fundamentalnej, nie tylko sygnału technicznego."
        ),
        "strategy_profile": {
            "horizon": "swing",
            "max_position_pct": 30,
            "min_cash_pct": 0,
            "max_us_pct": 50,
            "max_trades_per_round": 5,
            "requires_fundamental_check": True,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
        },
    },
    "Claude-free": {
        "mandate_md": (
            "Portfel Claude-free: budowa od zera z czystej gotówki, dowolne instrumenty i rynki. "
            "Swobodny dobór pozycji wg sygnałów technicznych + fundamentalnych, bez ograniczenia "
            "do odwzorowania istniejącego portfela. Zasady gry: limit 50% ekspozycji USA, każda "
            "nowa pozycja wymaga weryfikacji fundamentalnej."
        ),
        "strategy_profile": {
            "horizon": "swing",
            "max_position_pct": 30,
            "min_cash_pct": 0,
            "max_us_pct": 50,
            "max_trades_per_round": 5,
            "requires_fundamental_check": True,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
        },
    },
    "Zagranica": {
        "mandate_md": (
            "Portfel Zagranica: wyłącznie rynki zagraniczne (USA, LSE i inne poza GPW). "
            "Zakaz kupna instrumentów PL (.WA / ticker bez sufiksu zagranicznego). "
            "Budowa od gotówki, limity koncentracji i USA jak w zasadach gry; "
            "nowe pozycje z weryfikacją fundamentalną."
        ),
        "strategy_profile": {
            "horizon": "swing",
            "max_position_pct": 25,
            "min_cash_pct": 5,
            "max_us_pct": 80,
            "max_trades_per_round": 4,
            "requires_fundamental_check": True,
            "stop_loss_required": False,
            "allowed_markets": ["foreign"],
            "allowed_asset_types": ["akcje_zagraniczne"],
        },
    },
    "VeloBank": {
        "mandate_md": (
            "Lokaty VeloBank — portfel gry cash-only (managed_by=user). "
            "Seed z stocks/lokaty/lokaty.json; operacje cash w panelu, bez zapisu do JSON/historia.db."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 0,
            "min_cash_pct": 0,
            "max_us_pct": 0,
            "max_trades_per_round": 0,
            "requires_fundamental_check": False,
            "stop_loss_required": False,
            "cash_only": True,
            "allowed_asset_types": ["lokaty"],
            "bank": "VeloBank",
        },
        "managed_by": "user",
        "starting_capital": 175000.0,
    },
    "IKE": {
        "mandate_md": (
            "Konto realne IKE (eMakler) — portfel gry managed_by=user. "
            "Seed z historia.db (RO); dalsze operacje tylko w tracker.db."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 40,
            "min_cash_pct": 0,
            "max_us_pct": 100,
            "max_trades_per_round": 20,
            "requires_fundamental_check": False,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
            "source": "historia.db",
            "cash_from_cashflow_only": True,
        },
        "managed_by": "user",
        "starting_capital": 0.0,
    },
    "IKZE": {
        "mandate_md": (
            "Konto realne IKZE (eMakler) — portfel gry managed_by=user. "
            "Seed z historia.db (RO); dalsze operacje tylko w tracker.db."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 40,
            "min_cash_pct": 0,
            "max_us_pct": 100,
            "max_trades_per_round": 20,
            "requires_fundamental_check": False,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
            "source": "historia.db",
            "cash_from_cashflow_only": True,
        },
        "managed_by": "user",
        "starting_capital": 0.0,
    },
    "IKZE_ZONA": {
        "mandate_md": (
            "Konto realne IKZE_ZONA (BOSSA, żona) — portfel gry managed_by=user. "
            "Seed z historia.db (RO); dalsze operacje tylko w tracker.db."
        ),
        "strategy_profile": {
            "horizon": "long",
            "max_position_pct": 40,
            "min_cash_pct": 0,
            "max_us_pct": 100,
            "max_trades_per_round": 20,
            "requires_fundamental_check": False,
            "stop_loss_required": False,
            "allowed_asset_types": ["akcje_pln", "akcje_zagraniczne"],
            "source": "historia.db",
            "cash_from_cashflow_only": True,
        },
        "managed_by": "user",
        "starting_capital": 0.0,
    },
}

GAME_PLAYER_MANAGED_BY = {
    "Claude-clone": "ai",
    "Claude-free": "ai",
    "Zagranica": "ai",
    "VeloBank": "user",
    "IKE": "user",
    "IKZE": "user",
    "IKZE_ZONA": "user",
}
