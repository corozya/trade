# Ranking par OKX po obrocie i zmianie

`backend/scripts/rank_okx_pairs.py` pobiera bulk tickery
OKX (`GET /api/v5/market/tickers`) i tylko je odczytuje. Domyślnie ogranicza
wynik do perpetual swaps rozliczanych w USDT, sortując po przybliżonym obrocie
24h (`volCcy24h * last`). Nie korzysta z allowlisty execution i nie wysyła
zleceń. Ten publiczny endpoint działa bez kluczy OKX; alias jest zachowany
wyłącznie dla zgodności z innymi skryptami. Pozostałe metody klienta (saldo,
pozycje i zlecenia) nadal wymagają uwierzytelnienia.

```sh
cd backend
.venv/bin/python scripts/rank_okx_pairs.py --quote USDT --limit 30
.venv/bin/python scripts/rank_okx_pairs.py --sort change --limit 30
.venv/bin/python scripts/rank_okx_pairs.py --quote USDT --json
```

`--sort change` sortuje po wartości bezwzględnej zmiany procentowej, więc
pokazuje zarówno największe wzrosty, jak i spadki. `--quote ''` wyłącza filtr
kwotowania. Alias OKX można podać jako pierwszy argument.
