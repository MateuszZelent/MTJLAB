# Audyt drzewa pomiarowego i zakresu Anritsu

Zakres: normalizacja receptury, Builder, model/widok Qt, projekcja Execution,
Results, edycja i unieważnianie planu, shutdown oraz transport analizatora.
Nie wykonywano poleceń na fizycznym sprzęcie.

## Znalezione i poprawione błędy

| Problem | Poprawka i dowód |
| --- | --- |
| Reset modelu i opóźniony callback rozwijały wszystkie gałęzie, cofając decyzję operatora. | Zapamiętanie zwiniętych gałęzi po ID, usunięcie opóźnionego expandAll; test resetu oraz obsługi kolejki Qt. |
| Po zmianie modelu widok nadal nasłuchiwał resetów poprzedniego modelu. | Odłączenie sygnałów i anulowanie oczekującego śledzenia; test resetu starego modelu. |
| Klikanie gałęzi ignorowało przesunięcie poziome. | Hit testing według widocznej geometrii; test głęboko zagnieżdżonego drzewa po przesunięciu o 96 px. |
| Splitter wymuszał 430 px w karcie dopuszczającej tylko 220 px, obcinając dolne wiersze. | Usunięcie sprzecznych minimów, zachowanie przewijania wewnątrz drzewa; test rzeczywistej geometrii viewportu w Windows. |
| Edycja YAML i zmiana trybu pozostawiały nieaktualny manifest shutdownu. | Unieważnienie również projekcji drzewa; dwa osobne testy. |
| Limit odświeżania śledzenia odrzucał ostatni krok. | Oczekująca aktualizacja zawsze zachowuje najnowszy krok, nowszy force anuluje starą; testy żądań w odstępie poniżej 100 ms. |
| Qt model zwracał dziecko dla rodzica z kolumny innej niż zero, mimo rowCount=0. | Spójny kontrakt index/rowCount; test regresyjny. |
| Results wywoływał normalizator bez wymaganego rejestru, a catch maskował błąd strukturą zastępczą. | Przekazanie rejestru; test odczytu HDF5 wymaga oryginalnych ID i źródła receptury. |
| Then i Else wyglądały jak kolejne instrukcje jednej sekwencji; repeat nie pokazywał liczby powtórzeń. | Jawne etykiety warunku, obu gałęzi i liczby powtórzeń, również w wyłączonych węzłach; ID i semantyka kompilatora zachowane. |
| Finally po awarii pozostawało RUNNING lub gubiło wcześniejszy błąd shutdownu. | Agregacja wyników całego manifestu, zachowanie FAILED; operacje suppressed oznaczane SKIPPED. |
| Rejestracja widma była traktowana jako sterowanie RF. | Odrębny `anritsu.abort_acquisition`, bez deklarowania potwierdzonego RF OFF; poprawiony zakres odzyskiwania i dry run. |
| Obecność opcji sprzętowej SG uruchamiała ukryte komendy RF podczas connect/disconnect i konfiguracji widma. | Connect identyfikuje urządzenie bez mutacji. Zwykła ścieżka analizatora nie wybiera SG ani nie pisze/odpytuje OUTP. Explicit SG nadal ustala OFF przed konfiguracją; globalny ręczny E-STOP zachowuje dotychczasową funkcję. |

## Weryfikacja

- Testy semantyczne i kompilatora: 60 passed, 6 subtests.
- Testy modelu i nowych kontraktów interakcji: 26 passed.
- Renderowanie i nowe kontrakty na natywnym Qt Windows: 10 passed przed
  dodatkową korektą zakresu RF; ponowna weryfikacja projekcji to część testów RF.
- Dostarczona receptura tło + referencja + widmo przeszła symulację wraz z
  kontrolą HDF5 oraz PyThat. Test trwa rzeczywiste minimum 30 s zbierania tła.
- Testy RF korzystają z rzeczywistego adaptera z wstrzykniętym transportem,
  który deklaruje opcję SG i odrzuca każdą komendę `OUTP*` / `INST SG`.
  Obejmują measurement, dry run, stary manifest shutdownu i błąd `ABORT`.
- Końcowe testy adapterów, shutdownu, dry run i watchdogów: 60 passed
  w ukierunkowanej regresji. Szerszy zestaw wcześniej: 181 passed i 11 podtestów;
  dwa oczekiwania starego kontraktu poprawiono i ponownie zaliczono.
- Końcowe testy izolacji RF i szybkiej akwizycji Anritsu: 19 passed.
  Zachowana jest blokada disconnect po nieudanym OFF jawnie konfigurowanego SG.
- Końcowe natywne testy projekcji i interakcji drzewa: 11 passed.
- Shell/Execution z izolacją: 27 passed; dwa nieaktualne oczekiwania
  (dawny tekst dry run i wymuszane 420 px splittera) zastąpiono bieżącym
  kontraktem oraz kontrolą geometrii i zaliczono osobno: 2 passed.
- Ruff zmienionych plików przechodzi. W repozytorium pozostaje 10 wcześniej
  znanych nieużywanych importów w dwóch niezwiązanych plikach testowych.

Podgląd po zmianach: [natywny Builder, 1440 px](tree-abort-only-1440.png).

## Ograniczenia i osobne ustalenia

Symulowany stress test zapisał wszystkie 1000 widm po 10001 wartości, a HDF5
ma status completed. Maksymalna aktualizacja drzewa wyniosła 29,5 ms, podglądu
widma 27,8 ms. Całe GUI miało jednak przy zerowej liczbie zapisanych punktów
przerwę 381,8 ms, przekraczającą próg testu 350 ms. Nie uznajemy tego testu
wydajności całej aplikacji za zaliczony ani nie przypisujemy przerwy samemu
drzewu bez profilowania startu. Dowód: `artifacts/sweeps-spectrum/stress-runtime.json`.

Testy całego shellu wymagają `-p tests.shell_test_isolation`, aby nie korzystać
z katalogu stanowiska. Pierwsze uruchomienie bez izolacji zwróciło błędy bazy
SQLite tylko do odczytu; nie są to dowody usterek drzewa.

Po zmianach trzeba ponownie skompilować recepturę. Plan samego analizatora
powinien kończyć się wyłącznie zatrzymaniem akwizycji i zapisem checkpointu.
