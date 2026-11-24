# Audyt regresji komunikacji po timeout TRAC:TYPE? — 5.10.2026

Przegląd objął zmiany adapterów względem HEAD `acce560`, zapytania Anritsu we wspólnych ścieżkach ręcznych/sweepów, zachowanie Rigola zależne od przebiegu, nowe odczyty Keithleya oraz zachowanie po braku odpowiedzi. Nie wykonywano połączeń z fizyczną aparaturą.

## Znalezione i poprawione

| Miejsce | Problem | Poprawka |
|---|---|---|
| Rigol `configure_channel`, wybrane pola | Nowa ścieżka zachowania pominiętej fazy zawsze odpytuje `PHAS?`, również dla DC i NOIS. Weryfikacja końcowa już pomija ten odczyt dla tych przebiegów. Dostępność niepotrzebnego parametru mogła więc blokować konfigurację. | Pominięcie odczytu fazy dla DC/NOIS również w przygotowaniu konfiguracji. Testy obu kanałów wymuszają błąd na tym zapytaniu i potwierdzają, że nie jest wysyłane podczas konfiguracji. |
| Anritsu `read_full_configuration` | Dodatkowe `FREQ:CENT?` i `FREQ:SPAN?`, z kontynuowaniem odczytu po dowolnym wyjątku, w tym timeout. Wartości można wyznaczyć z już odczytanych Start/Stop. | Usunięcie obu zapytań; obliczenie Center/Span z potwierdzonego, zwalidowanego zakresu. Bez oczekiwania na zbędną odpowiedź i bez kontynuowania po jej timeout. |
| Anritsu odczyt konfiguracji | Odczyty właściwe dla Spectrum mogły być wysyłane mimo innej odpowiedzi `INST?`; pełny odczyt nie korzystał z istniejącej walidacji granic i liczby punktów. | Sprawdzenie aplikacji przed odczytem parametrów Spectrum; pełny odczyt korzysta ze zwalidowanego odczytu podstawowego. Test innej aplikacji potwierdza, że wysłano tylko `INST?`. |

To dodatkowe ryzyka odtworzone kontrolowanymi odpowiedziami testowymi, a nie dowody, że fizyczny Rigol lub Anritsu już zgłosił te konkretne timeouty. Zgłoszony sprzętowo `TRAC:TYPE?` został naprawiony wcześniej: [opis poprawki](ANRITSU_TRACE_TIMEOUT_FIX_2026-10-05.md).

## Pozostałe odczyty

- W produkcyjnym adapterze Anritsu nie pozostało wykonywane zapytanie `TRAC:TYPE?`; występuje wyłącznie w komentarzach.
- `INIT:CONT?`, `INST?`, zakończenie pojedynczego przemiatania i odczyt danych pozostają wymagane. Przygotowanie Trace A do świeżej akwizycji nie zostało cofnięte.
- Odczyty VID/POW, RBW/VBW, tłumienia, detektora i liczby uśrednień nadal są częścią metadanych. Osiem prób wymusza timeout na wymaganych zapytaniach; odczyt zatrzymuje się dokładnie tam i nie produkuje zastępczych metadanych.
- Stan preamp jest odpytywany tylko przy wykrytej opcji sprzętowej. Nie usuwano kontroli OUTPUT ani odczytów bezpieczeństwa źródeł.
- W przejrzanych nowych zmianach Keithleya nie znaleziono analogicznego nowego literalnego zapytania VISA; ścieżka wybranych pól korzysta z istniejącego odczytu konfiguracji. Testy adaptera i runnera pozostają częścią bramki regresji.

## Ograniczenia i otwarte sprawdzenia sprzętowe

Nie ma podstaw, by uznać wszystkie pozostałe zapytania za potwierdzone na aktualnym firmware tylko dlatego, że odpowiada na nie symulator. W szczególności rygorystyczny odczyt `BAND:VID:MODE?` i `AVER:COUN?` oraz odczyty toru wyjściowego Rigola wymagają rzeczywistej odpowiedzi przy użyciu tych funkcji. Nie stwierdzono ich niezgodności na podstawie dostępnego zgłoszenia, dlatego nie usuwano ich ani nie maskowano błędów.

Osobnym, starszym mechanizmem jest opcjonalne rozpoznawanie możliwości Rigola podczas connect. Może wysyłać zapytania nieobsługiwane przez firmware, także o fazę, i oznaczać funkcję jako niedostępną. Poprawka DC/NOIS dotyczy wymaganej konfiguracji po connect, nie tego opcjonalnego rozpoznawania. Nie kwalifikowano tutaj synchronizacji transportu po spóźnionej odpowiedzi ani wszystkich opcjonalnych funkcji aparatury.

Próba odczytu dokumentacji online nie dostarczyła instrukcji protokołu dla właściwego modelu; nie używano instrukcji innych analizatorów jako dowodu zgodności MS2830A. Ocena opiera się na lokalnym kodzie, historii, zgłoszeniu sprzętowym i testach błędów.

## Weryfikacja

**Końcowy wynik: 224 passed, 5 subtests passed, 133,35 s.**

Nowy plik: `tests/test_protocol_regression_audit.py` — 14 przypadków, w tym oba kanały Rigola, DC/NOIS, zbędne zapytania Anritsu, niewłaściwa aplikacja i osiem wymaganych odczytów. Końcowy log i manifest: `docs/audits/2026-10-05-production-followup/protocol-audit-*`.

Pierwszy szeroki przebieg: 224 testy przeszły, ale wystąpił błąd sprzątania testu niewłaściwej aplikacji — jego atrapę odpowiedzi pozostawiono aktywną podczas rozłączania. Ograniczono atrapę do badanego odczytu; ponowny test celowany: 14 passed. Końcowy szeroki przebieg wykonano ponownie po tej korekcie.

Odtworzenie:

```powershell
python -m pytest -q -p tests.shell_test_isolation tests/test_protocol_regression_audit.py tests/test_adapters_and_runner.py tests/test_anritsu_fast_acquisition.py tests/test_sweep_release_contracts.py tests/test_sweep_production_followup.py tests/test_sweep_selected_mutations.py
```
