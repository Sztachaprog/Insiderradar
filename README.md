# form4_scanner

Skanuje najnowsze zgłoszenia Form 4 w SEC EDGAR i pokazuje zakupy akcji przez insiderów (kod `P`) na otwartym rynku. Nie wymaga klucza API. Działa jako aplikacja webowa albo z linii komend.

## Start (PowerShell)

```powershell
pip install -r requirements.txt
$env:SEC_USER_AGENT = "Imie Nazwisko twoj@mail.com"
python app.py
```

Otwórz http://127.0.0.1:5000. Po starcie aplikacja od razu robi pierwszy skan (około pół minuty), a potem powtarza go co 15 minut, dopóki działa. Zgłoszenia trafiają do `form4.db` (SQLite) i nie znikają po restarcie.

### Aplikacja webowa

- **Filtry**: zakupy lub sprzedaże, minimalna i maksymalna kwota, wyszukiwanie po tickerze, spółce albo osobie.
- **Maks. kwota** pozwala odsiać duże transakcje, które zwykle są emisjami prywatnymi albo ruchami dużych udziałowców.
- **Klaster** oznacza, że w wynikach tę samą spółkę kupiło kilku różnych insiderów.
- **Skanuj teraz** uruchamia skan od razu, bez czekania na kolejny cykl.
- **Sygnał 0–100** ocenia, czy zakup to mocny sygnał. Liczy się rola kupującego (CEO/CFO > zarząd > rada > 10% udziałowiec), kwota i jej udział w kapitalizacji, klaster i powtarzające się zakupy. Odejmowane są plany 10b5-1, emisje i zakupy przez fundusze. Na stronie spółki widać wszystkie powody oceny.
- **Rodzaj zakupu** jest rozpoznawany z przypisów Form 4: *insider na rynku*, *fundusz / podmiot* albo *emisja / umowa ze spółką* (prywatna emisja, PIPE). Starsze zgłoszenia dostają przypisy przy następnym skanie.
- **Strona spółki** (kliknij wiersz): sektor, branża, kapitalizacja, opis działalności, wykres kursu z ceną z chwili dodania i ceną insidera, przypisy.
- **Śledzenie cen**: gdy zgłoszenie trafia do bazy, zapisywana jest aktualna cena. Po każdym skanie odświeżane są notowania dzienne, więc widać wynik *od dodania* oraz po 1 i 3 sesjach, po tygodniu i po miesiącu, także względem SPY (S&P 500).
- **Kontekst kursu**: o ile kurs był pod szczytem 52-tyg., jak blisko dołka i jak się ruszał miesiąc przed zakupem. Zakup po dużym spadku podnosi ocenę, a zakup po rajdzie ją obniża.
- **Historia insidera**: wcześniejsze zakupy tej osoby z ostatnich 3 lat (z SEC) i wynik kursu po 1, 3 i 6 miesiącach od każdego z nich. Insider z dobrą historią dostaje plus. Pobiera się do 5 osób na skan i odświeża co tydzień.
- **Kalibracja** (na dole stron z wynikami): korelacja oceny z wynikiem, wynik w przedziałach oceny i wpływ każdego czynnika (z nim kontra bez niego). Od 30 śledzonych transakcji przycisk *Zastosuj propozycję* przesuwa wagi w stronę tego, co faktycznie działało. *Przywróć domyślne wagi* cofa zmiany.
- **Krypto** (`/krypto`): radar dużych ruchów na mniejszych tokenach z CoinGecko (kapitalizacja $3 mln–$1,5 mld). Ruch trafia na listę przy ±25% w 24h, ±10% w 1h, wolumenie powyżej 60% kapitalizacji albo gdy token jest trending. Każdy ruch dostaje ocenę potencjału z powodami (wolumen, parabola, odblokowania FDV, odległość od ATH, GitHub, społeczność, memecoin, narracja), opis projektu i linki. Cena z chwili wykrycia jest punktem startu, a **Krypto wyniki** pokazują wynik po 1h, 24h, 3 i 7 dniach, wynik względem BTC, trafność kierunku (czy ocena 50+ faktycznie rosła) i kalibrację.
- **Wyniki** (`/wyniki`): średni wynik, trafność, średni zysk i strata, mediana i wynik vs SPY dla każdego horyzontu, a do tego wykres średniej ścieżki i rozkładu wyników. Liczone dla tych samych filtrów co w Skanerze, więc da się np. sprawdzić, czy *silne* sygnały faktycznie zarabiają więcej niż *słabe*.
- **API**: `/api/filings` zwraca wyniki w JSON i przyjmuje te same parametry co strona. `/api/status` zwraca stan skanera.

Ustawienia przez zmienne środowiskowe:

| Zmienna | Domyślnie | Działanie |
|---|---|---|
| `SCAN_INTERVAL_MIN` | `15` | co ile minut skanować |
| `SCAN_PAGES` | `3` | ile stron feedu, po 100 zgłoszeń na stronę |
| `FORM4_DB` | `form4.db` | ścieżka do bazy |
| `TRACK_DAYS` | `120` | przez ile dni po dodaniu odświeżać ceny |
| `CRYPTO_INTERVAL_MIN` | `15` | co ile minut skanować krypto |
| `CRYPTO_PAGES` | `4` | ile stron rankingu CoinGecko (po 250 tokenów) |
| `CRYPTO_MIN_MCAP` / `CRYPTO_MAX_MCAP` | `3e6` / `1.5e9` | zakres kapitalizacji radaru |
| `COINGECKO_API_KEY` | brak | darmowy klucz „Demo” z CoinGecko przyspiesza skan (bez klucza około 3–4 min) |

### Linia komend

```powershell
python form4_scanner.py --pages 3
```

| Flaga | Działanie |
|---|---|
| `--min-value 250000` | minimalna łączna kwota na zgłoszenie (domyślnie 100k $) |
| `--pages 3` | ile stron feedu przejrzeć |
| `--code S` | sprzedaże zamiast zakupów |
| `--all-owners` | uwzględnia też posiadaczy 10% akcji |
| `--include-10b5-1` | uwzględnia transakcje z planów 10b5-1 (domyślnie pomijane, bo były zaplanowane z góry) |
| `--state seen.json` | zapamiętuje już zgłoszone filingi |
| `--json` | wynik w JSON |

## Testy

```powershell
pip install pytest; pytest -q
```

## Ograniczenia

- Feed EDGAR obejmuje tylko ostatnie zgłoszenia. Jeśli aplikacja była dłużej wyłączona, zwiększ `SCAN_PAGES`.
- Przy wspólnych zgłoszeniach (kilka właścicieli na jednym Form 4) brany jest pierwszy właściciel.
- Transakcje pochodne (opcje, kod `M`) są pomijane.
- SEC limituje ruch do 10 zapytań na sekundę, skaner robi przerwy między zapytaniami.
- Aplikacja skanuje tylko wtedy, gdy działa `python app.py` (zamknięta karta przeglądarki nie przeszkadza). Przerwy w cenach krypto uzupełnia historia godzinowa z CoinGecko, a w cenach akcji notowania dzienne z Yahoo.
- Ceny i profile pochodzą z nieoficjalnego API Yahoo Finance. Jeśli Yahoo coś zmieni, skanowanie Form 4 działa dalej, tylko bez cen.
- Ocena sygnału to heurystyka. Przed decyzją przeczytaj powody i przypisy.
