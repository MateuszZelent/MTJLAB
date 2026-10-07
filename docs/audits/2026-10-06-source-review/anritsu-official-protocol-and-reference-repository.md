# Anritsu MS2830A: porównanie kodu, dokumentacji i logów stanowiska

Data: 2026-10-06. Urządzenie z logów: MS2830A, firmware 7.03.00,
GPIB0::23::INSTR. Nie wysyłano poleceń do fizycznego urządzenia podczas audytu.

## Źródła

- [Repozytorium wskazane przez użytkownika](https://github.com/salvadorJMA/Python-Libraries-to-manage-Anritsu-MS2830A-Agilent-N9020A-machines/tree/d0b5492635ff3eafe45fc7940885d0959695d9c7), commit `d0b5492635ff3eafe45fc7940885d0959695d9c7`.
  Przeczytano pliki AnritsuMS2830A.py, AgilentN9020A.py, README, zależności i licencję.
  Kod pobrano do `.tmp-anritsu-reference-repo`; nie instalowano ani nie uruchamiano biblioteki.
- [Anritsu Mainframe Remote Control, wydanie 42](https://dl.cdn-anritsu.com/en-au/test-measurement/files/Manuals/Operation-Manual/MS2830A/MS269xA_2830A_40A_50A_Mainframe_Remote_Manual_e_42_0.pdf).
  Zakres wprost obejmuje MS2830A. Istotne sekcje: 1.6.1–1.6.2, 4-47, 4-116, 6-75.
- [Anritsu Spectrum Analyzer Remote Control, wydanie 43](https://dl.cdn-anritsu.com/en-au/test-measurement/files/Manuals/Operation-Manual/MS269xA/MS2830A_40A_SpectrumAnalyzer_Remote_Manual_e_43_0.pdf).
  Zakres wprost obejmuje MS2830A. Istotne strony drukowane: 2-63–2-65,
  2-184–2-185, 2-206, 2-216, 2-241, 3-15.

## Repozytorium nie jest wzorcem produkcyjnym

Biblioteka Anritsu używa PyVISA przez Ethernet i odczytu `FORM ASC` →
`TRAC? TRAC1`, podobnie jak nasza ścieżka ASCII. Nie synchronizuje tego
odczytu z nowym pomiarem: pobiera bieżący bufor. Nie obsługuje naszych
sweepów, limitów stanowiska, anulowania, kolejki błędów ani trwałego zapisu HDF5.

Błędy znalezione w źródle:

- Oś częstotliwości dzieli span przez liczbę punktów, zamiast przez `points - 1`.
  Nasz adapter stosuje poprawną oś domkniętą.
- `getInitialParamsAnritsu()` przełącza aplikację na SG, następnie SPECT;
  metody nadpisują zmienną służącą do przywracania aplikacji.
- `setParamsGenerator()` zawiera `UNIT.POW DBM` i automatycznie włącza RF.
- `setParamsSpectrumSpan()` wywołuje metodę o nieistniejącej nazwie.

Nie przeniesiono tych zachowań do naszej aplikacji.

## Fakty potwierdzone logami sprzętowymi

1. `*IDN?` poprawnie identyfikuje model i firmware.
2. `*OPT?` kończy się timeoutem i błędem `-113`.
3. `TRAC1:TYPE WRIT` znajduje się w kolejce jako odrzucony nagłówek.
4. Kolejny log potwierdza `SYST:LANG?` → `NAT`.
5. `OPTINFO? HARD` również kończy się timeoutem i `-113` na tym stanowisku.
6. `-350` oznacza przepełnienie kolejki błędów; wcześniejszych wpisów może brakować.

Nie wolno utożsamiać `INST? = SPECT` z potwierdzeniem języka SCPI.

## Regresja względem HEAD, a nie zmiana urządzenia

Porównano `git diff` oraz źródło adaptera w `HEAD`, commit `44b4a6e`
(`Naprawa Analziatora Spekturm`).

| Ścieżka | HEAD | Aktualny diff przed poprawką | Skutek |
| --- | --- | --- | --- |
| Start pojedynczego pomiaru | Po wejściu w SPECT: `INIT:MODE:SING`, `*WAI` | Dodane `TRAC1:TYPE WRIT` przed startem | Native odrzuca dodatkowy nagłówek; to bezpośrednia regresja startu sweepa |
| Opcje podczas połączenia | Już zawierał `*OPT?`; wyjątek był pomijany | Dodano obowiązkową kontrolę `SYST:ERR?` przed pomiarem | Wcześniej ignorowany błąd z kolejki zaczyna blokować pomiar |
| Pierwsza poprawka katalogu | Brak | `OPTINFO? HARD` w Native | Kolejny log sprzętu potwierdził odrzucenie; odczyt usunięto z Native |
| Wejście do SPECT przed pomiarem | `_enter_spectrum_mode_with_rf_off()` wysyłał wybór aplikacji | Potwierdzenie przez `INST?` | Ogranicza przełączanie aplikacji i operacje RF w pomiarach odbiorczych |
| Transfer ASCII | `FORM ASC` przed każdym odczytem | Odczyt `FORM?`, zmiana tylko gdy potrzebna | Nie zmienia zasady odczytu bufora, ogranicza powtarzane zapisy |

Sprostowanie: `*OPT?` nie zostało dodane w najnowszym niezacommitowanym
diffie. Dodano natomiast wykrywanie skutków jego odrzucenia. Testy wcześniejszej
wersji nie modelowały rzeczywistego języka Native ani kolejki tych błędów.
Nie cofnięto całego adaptera, ponieważ przywróciłoby to także przełączanie SG/RF
oraz pomijanie błędów urządzenia.

## Dokumentacja a kwalifikacja stanowiska

Mainframe opisuje SCPI i Native oraz konwersję indeksów nagłówka do
pierwszego argumentu Native. Opisuje również odczyty katalogu opcji:
`SYST:HARD:OPT:CAT?` i `OPTINFO? HARD`. Sam opis w nowszej instrukcji
nie jest dowodem obsługi komendy przez firmware 7.03.00 w aktualnej aplikacji.
Dlatego log odrzucenia `OPTINFO? HARD` ma pierwszeństwo przed symulatorem.

Spectrum Remote dokumentuje `TRAC1:TYPE WRIT` dla SCPI, pojedynczy pomiar
`INIT:MODE:SING`, synchronizację `*WAI` i odczyt `TRAC? TRAC1`.
Sterowanie typem śladu ma ograniczenia dla SEM/Spurious.

## Zmiany w kodzie

- Usunięto wysyłanie `*OPT?`.
- Przy połączeniu odczytywany jest język przez `SYST:LANG?`.
- W Native nie jest wysyłany opcjonalny odczyt opcji ani przełączana aplikacja.
  Opcje pozostają niepotwierdzone; wymagane opcje blokują połączenie,
  a dostęp do generatora nie jest przyznawany bez ich potwierdzenia.
- W SCPI katalog jest parsowany z uwzględnieniem flag ON/OFF, liczby wpisów
  i nazw CSV. Opcja wymieniona jako OFF nie przyznaje możliwości sterowania.
- Przygotowanie Trace A dobiera nagłówek do aktualnego języka:
  SCPI: `TRAC1:TYPE WRIT`; Native: `TRAC:TYPE 1,WRIT`.
- Język nie jest zmieniany automatycznie. Błąd przygotowania śladu zatrzymuje
  pomiar przed uruchomieniem sweepa i odczytem danych.
- Aktualizowane są również Live i jawne Apply, korzystające ze wspólnej ścieżki.

Nie zmieniono granic stanowiska, setpointów MOKE/Keithley/Rigola ani polityki RF.
Nie dodano resetu urządzenia, czyszczenia błędów przez `*CLS` ani konfiguracji
częstotliwości/pasm w każdym punkcie. Odczyt języka jest kontrolą poprawności
protokołu, a nie zmianą parametru pomiarowego.

## Istotne otwarte ustalenie: jednostki danych

`_read_ascii_trace()` i `_read_binary_trace()` przypisują dane do `powers_dbm`.
Nie potwierdzają skali amplitudy i jednostki wybranej na fizycznym analizatorze.
Instrukcja opisuje inny format poziomów dla skali liniowej. Warunkiem obecnej
ścieżki jest LOG/dBm na analizatorze; wybór jednostek w naszym wykresie
nie potwierdza jednostek bufora sprzętowego. To pozostaje luka walidacji,
wymagająca odrębnej poprawki przy odczycie `DISP:WIND:TRAC:Y:SPAC?`
i `UNIT:POW?`, bez narzucania zmian w ustawieniach urządzenia.

## Weryfikacja i jej granice

Testy regresyjne sprawdzają rzeczywiste nagłówki wysyłane przez adapter,
brak odczytu opcji w Native, zmianę języka po połączeniu, odrzucenie komendy,
brak dalszego startu/odczytu po błędzie oraz blokowanie niepotwierdzonych opcji.
Symulator odrzuca `*OPT?` i nagłówek SCPI `TRAC1:TYPE` w Native.

Końcowy zestaw: **221 testów przeszło, 5 podtestów przeszło**.
Uruchomiono `test_anritsu_documented_protocol`, `test_anritsu_acquisition_traffic`,
`test_adapters_and_runner`, `test_analyzer_only_rf_scope`,
`test_sweep_release_contracts`, `test_anritsu_fast_acquisition`,
`test_anritsu_hardware`, `test_source_review_anritsu_simulator_protocol`.
`ruff check app tests` również przechodzi.

Zaktualizowano dwa nieaktualne założenia istniejących testów: fixture wznowienia
otrzymała prawidłowe hashe, aby rzeczywiście dojść do walidacji pamięci;
test akwizycji modułu oczekuje Single pomiędzy punktami zamiast wznowienia
Continuous. Nie zmieniano produkcyjnej walidacji wznowienia.

Native `TRAC:TYPE 1,WRIT` jest wyprowadzony z udokumentowanej reguły konwersji
i sprawdzony w testach. Nie ma jeszcze logu potwierdzającego jego przyjęcie
przez fizyczny MS2830A 7.03.00. Tak samo nie potwierdzono sprzętowo odczytu
katalogu w SCPI. Nie deklarujemy pełnej kwalifikacji produkcyjnej na podstawie
samego repozytorium ani testów symulacyjnych.
