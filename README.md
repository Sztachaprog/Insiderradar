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
- **Strategie** (`/krypto/strategie`): symulacja transakcji dziennych na wykrytych ruchach krypto. Każda strategia ma warunki wejścia (typ ruchu, ocena, kapitalizacja, zmiana 24h, wolumen), take-profit, stop-loss, maksymalny czas trzymania i koszt transakcji. Wynik pokazuje trafność, średni wynik netto, profit factor, wynik w $, obsunięcie, krzywą kapitału i listę transakcji. Jest 6 gotowych strategii, edytor do tworzenia i zapisywania własnych oraz lista „Okazje teraz” ze świeżymi ruchami pasującymi do strategii.
- **Wieloryby** (`/wieloryby`): baza dużych portfeli na Ethereum (co najmniej $1 mln w jednym z ~140 największych tokenów; dane z Blockscout, bez klucza). Giełdy, pule DEX i mosty są odfiltrowane, a portfele zespołów i skarbców (≥ 5% podaży albo grupy identycznych sald) oznaczone osobno. Za zakup lub sprzedaż liczy się tylko przepływ z giełdą albo DEX-em. Widać tokeny akumulowane przez wieloryby z oceną, duże transakcje (≥ $1 mln) z interpretacją (wypłata z giełdy, wpłata na giełdę, DEX) i strony portfeli i tokenów. Od oceny 60 token dostaje sygnał spot z zapisaną ceną i wynikiem po 7, 30 i 90 dniach. Możesz też dodać własny portfel do śledzenia.
- **XTB**: przy tickerach i tokenach dostępnych na XTB jest znaczek **XTB ↗**, który prowadzi prosto do strony instrumentu. Na stronie spółki są przyciski „Kup akcje na XTB” i „CFD na XTB”, a w filtrach opcja „Tylko dostępne na XTB”. Lista instrumentów pochodzi z mapy strony XTB (odświeżana raz dziennie), a dopasowanie tickera jest weryfikowane po symbolu na stronie XTB. Spółki, których XTB nie ma, są sprawdzane ponownie co 14 dni.
- **Newsy krypto**: przy każdym wykrytym ruchu aplikacja szuka nagłówków z ostatnich 3 dni o tym projekcie (Google News RSS, bez klucza), żeby było widać, *dlaczego* się rusza. CryptoPanic wyłączył darmowe API w 2026.
- **Powiadomienia Telegram** (sekrety `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`): po każdym skanie w GitHub Actions bot wysyła nowe silne sygnały, czyli zakupy insiderów z oceną 60+, ruchy krypto z oceną 70+ albo pasujące do strategii (z newsem i linkiem do XTB), sygnały spot wielorybów i transfery wielorybów od $5 mln. Każdy sygnał przychodzi raz. Przy pierwszym uruchomieniu bot nie wysyła historii, tylko potwierdzenie podłączenia. Progi ustawiasz zmiennymi `NOTIFY_STOCK_MIN`, `NOTIFY_CRYPTO_MIN`, `NOTIFY_WHALE_TX_USD`.
- **Czas**: godziny krypto i wielorybów są pokazywane w czasie polskim (CEST latem, CET zimą; zmienna `APP_TZ`).
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
| `WHALE_INTERVAL_MIN` | `30` | co ile minut skanować portfele wielorybów |
| `WHALE_TOKENS` / `WHALE_MIN_USD` | `150` / `1e6` | ile tokenów przeglądać i od jakiej kwoty portfel jest wielorybem |
| `BLOCKSCOUT_API_KEY` | brak | darmowy klucz z dev.blockscout.com: wieloryby także na Arbitrum i Optimism (Base i Polygon tylko w płatnym planie, `WHALE_CHAINS`) |
| `HELIUS_API_KEY` | brak | darmowy klucz z dashboard.helius.dev: wieloryby na Solanie (lista tokenów z Jupiter, posiadacze i transakcje z Helius) |
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

## Działanie 24/7 na GitHubie

Workflow [.github/workflows/scan.yml](.github/workflows/scan.yml) co ~10 min uruchamia jeden cykl skanów (`python ci.py`): insiderzy z cenami, krypto, a co ~30 min wieloryby. Baza trafia na gałąź `data` (jeden nadpisywany commit, więc historia nie rośnie), a podgląd aplikacji na GitHub Pages.

Jednorazowa konfiguracja na GitHubie:

1. **Settings → General → Danger Zone → Change visibility → Public.** Publiczne repo ma darmowe, nielimitowane minuty Actions. Prywatne ma 2000 min/mies., a ten workflow zużywa ok. 10 tys. GitHub Pages na darmowym koncie też działa tylko dla publicznych repo.
2. **Settings → Secrets and variables → Actions → New repository secret:**
   - `SEC_USER_AGENT`: np. `Imie Nazwisko twoj@mail.com` (wymóg SEC; jako sekret nie jest widoczny publicznie),
   - `BLOCKSCOUT_API_KEY` (opcjonalny): darmowy klucz z dev.blockscout.com, który rozszerza wieloryby o Arbitrum i Optimism (Base i Polygon od 1.10.2026 tylko w płatnym planie),
   - `COINGECKO_API_KEY`: darmowy klucz „Demo” z coingecko.com/en/api. Opcjonalny, ale bez niego CoinGecko często blokuje serwery GitHuba.
3. **Settings → Pages → Build and deployment → Source: GitHub Actions.**
4. **Actions → Skan danych → Run workflow**, żeby uruchomić pierwszy skan od razu.

Strona: `https://<użytkownik>.github.io/<repo>/`. To podgląd tylko do odczytu, bez filtrów, edytora strategii i dodawania portfeli, bo te wymagają serwera. Pełną wersję na najświeższych danych uruchomisz lokalnie:

```powershell
python sync_db.py                              # pobiera form4.db z gałęzi data
$env:SCAN_DISABLED = "1"; python app.py        # tylko przeglądanie, bez własnych skanów
```

Uwagi:

- Zaplanowane uruchomienia GitHub potrafi opóźnić o kilka–kilkanaście minut przy dużym obciążeniu.
- Nie uruchamiaj jednocześnie skanów lokalnie (`python app.py` bez `SCAN_DISABLED`), bo powstaną dwie rozbieżne bazy. Zmiany zrobione lokalnie (zapisane strategie, dodane portfele, wagi kalibracji) nie trafiają na GitHuba.
- GitHub wyłącza harmonogram w repo bez aktywności przez 60 dni. Wtedy wystarczy go włączyć jednym kliknięciem w zakładce Actions.

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
- Na Solanie nie ma darmowych etykiet giełd: portfel, który jest wśród największych posiadaczy wielu tokenów naraz, jest traktowany jako giełda / custody i ukrywany. Pule DEX (adresy programów) są odfiltrowane.
- Wieloryby działają na Ethereum (bez klucza) oraz na Arbitrum i Optimism (z darmowym kluczem Blockscout). Base i Polygon od 1.10.2026 są tylko w płatnym planie Blockscout, a Solana i BSC wymagają innych API. Pierwszy skan trwa ~7 min, a pełne pokrycie listy tokenów ~3 h.
- Ocena sygnału to heurystyka. Przed decyzją przeczytaj powody i przypisy.
