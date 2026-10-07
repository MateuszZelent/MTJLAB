# Results: osobne odejmowanie zapisanej referencji i tła

Data: 2026-10-06. Przegląd kodu, poprawka UI oraz wykonanie receptury na
symulowanych adapterach. Nie wykonywano komunikacji z fizycznymi urządzeniami.

## Plik wskazany przez użytkownika

Na zrzucie wybrano wynik zaczynający się od
`20261005T220405.096456Z_INL_MTJ_02.2026_R19C4_P4_300_nm_Anritsu_only_…`,
z katalogu `F:/Abbas/p26-03 Non-stationary vortex dynamics INL Portugal/PyLab`.
W tym środowisku `Test-Path F:/` zwraca False. Nie znaleziono tego pliku
w repozytorium. Nie potwierdzono jego zapisanych referencji, metadanych ani
receptury; zadano pytanie o dostępną ścieżkę H5. Poniższe ustalenia dotyczą
obecnego kodu i obecnej receptury testowej, a nie odczytu niedostępnego archiwum.

Sam widok „raw, 1 checkpoint x 10001” jest zgodny z testem analizatora.
Nie dowodzi braku referencji: są one archiwizowane osobno od checkpointów.

## Przeczytana ścieżka wykonania i interpretacji

- `recipes/anritsu_background_reference_smoke_test.yml`: cała receptura.
- `app/engine/compiler.py`: kompilacja acquire_reference/acquire_spectrum,
  walidacja celu, czasu, liczby sweepów, opóźnienia i zapisu RAW.
- `app/engine/runner.py`: obie akcje, pełna procedura akwizycji/uśredniania,
  kwalifikacja ustawień i dobór shutdownu analizatora.
- `app/storage/hdf5_writer.py`: zapis referencji, źródeł i linku przetworzonego
  widma, metadane oraz granica zatwierdzenia checkpointu.
- `app/ui/results/processing.py` i `processing_controls.py`: całe pliki;
  wybór baseline, zgodność siatki/ustawień, historia RAW, UI i jednostki.
- `app/spectrum/processing.py`: uśrednianie mocy i operacje na referencji.
- Testy Results i receptury; testy nie zastąpiły powyższego przeglądu źródeł.

## Zawartość obecnej receptury

| Kolejność | Akcja | Znaczenie |
| --- | --- | --- |
| 1 | Acquire reference, purpose=background | Co najmniej 4 kompletne sweepy i co najmniej 30 s zbierania; 3 s pomiędzy sweepami |
| 2 | Wait | 3 s |
| 3 | Acquire reference, purpose=reference | 4 kompletne sweepy; 3 s pomiędzy sweepami |
| 4 | Wait | 3 s |
| 5 | Acquire spectrum | 1 RAW; reference_operation=none; bez zapisu przetworzonego widma |

Plan wymaga tylko Anritsu. Nie zawiera konfiguracji źródeł, MOKE, Keithley
ani Rigola, ustawiania parametrów analizatora ani OUTPUT ON. Akwizycja korzysta
z jego bieżącej konfiguracji; odczytuje ją przed i po bloku. Zmiana konfiguracji
lub siatki w trakcie bloku kończy akwizycję błędem, a źródła RAW pozostają zapisane.
Zakończenie analizatora bez własności generatora wykonuje abort akwizycji,
bez komendy RF OUTPUT OFF.

Uśrednianie zachodzi w liniowej mocy, następnie wynik wraca do dBm.
30 sekund obejmuje też odstępy między sweepami: nie jest to 30 sekund ciągłej
integracji w każdym binie. Referencja i tło są niezależnymi rekordami
`/references/0` i `/references/1`, z celem, czasem UTC, liczbą sweepów,
siatką Hz, mocą dBm, dowodami konfiguracji i indeksami źródłowych RAW.
Legacy alias `/reference` wskazuje pierwszy rekord, czyli tutaj tło;
Results korzysta z katalogu wszystkich referencji, a nie wyłącznie tego aliasu.

Końcowy RAW celowo nie ma reference_index ani processed_values.
Przy reference_operation=none oznacza to brak korekcji podczas rejestracji,
a nie brak zapisanej referencji. Referencja nie tworzy dodatkowego publicznego
checkpointu, dlatego jeden checkpoint widoczny na zrzucie jest oczekiwany.

Ponieważ receptura nie zmienia układu pomiędzy tłem, referencją i RAW,
jest testem akwizycji/zapisu. Nie tworzy pomiaru przy innym punkcie pracy.
Pojedynczy końcowy sweep nadal zawiera losowy szum; odejmowanie średniego
tła/referencji nie usuwa losowej realizacji szumu tego sweepa.

## Naprawa Results

Dodano osobne pozycje `Raw − background — signed W` oraz
`Raw − reference — signed W`, wspólne dla Spectrum i Heatmaps.
Obie korzystają z istniejącej matematyki odejmowania liniowej mocy:
`P_RAW[W] − P_baseline[W]`. Ujemne reszty pozostają widoczne.
`Difference in dB` pozostaje osobną operacją o innym znaczeniu fizycznym.

Automatyczny wybór sprawdza purpose także dla referencji powiązanej
z checkpointem. Link do tła nie zastępuje referencji. Przy wielu kandydatach
bez zgodnego linku wymagany jest jawny wybór. Wybranie tła dla nowej operacji
Raw − reference zwraca czytelny błąd. Przełączenie rodzaju odejmowania
usuwa poprzedni jawny wybór baseline o innym celu i pokazuje odpowiednio
Automatic reference / Automatic background.

Pełna siatka oraz fingerprint ustawień nadal są weryfikowane przed DSP.
Brak referencji nie powoduje cichego użycia tła. Operacje są podglądem:
H5 nie jest modyfikowany, co sprawdza test hasha przed/po obliczeniu.
Format HDF5 i składnia operacji w recepturach pozostały bez zmian.

## Weryfikacja

- `tests/test_results_postprocessing.py`: **24 passed**, w tym poprawna
  referencja przy linku brak/reference/background, zachowanie wartości ujemnych,
  brak referencji, jawny zły baseline, przełączanie i zgodność obu widoków.
- `tests/test_anritsu_smoke_recipe.py`, `tests/test_spectrum_benchmark_resources.py`,
  `tests/test_shutdown_tree_projection.py`: **7 passed**.
- Ruff dla dwóch plików produkcyjnych i dwóch zmienionych testów: zaliczony.
- Widoki pokazano i przetworzono zdarzenia Qt przy 1440 px (light/dark)
  i 760 px; sprawdzono geometrię, synchronizację Spectrum/Heatmaps i reset.
  Zachowano i obejrzano `reference-subtraction-light.png` oraz
  `reference-subtraction-heatmap-dark.png`.

Pierwsza próba: 23 testy Results zaliczone; wykonanie receptury odrzucone
przez kontrolę wolnego miejsca (potrzeba ok. 5.15 GB, dostępne ok. 4.56 GB).
Test receptury odizolowano od pojemności dysku przez mock disk_usage.
Produkcja nadal wykonuje kontrolę pojemności; jej testy pozostały zaliczone.

## Odczyt wygenerowanego przykładu H5

`anritsu-reference-smoke-simulation.h5` jest kopią wyniku wykonania obecnej
receptury, z identyfikacją symulowanego analizatora. Nie jest plikiem użytkownika.
Bezpośredni odczyt HDF5 potwierdził:

- status completed; 1 checkpoint, 1 widmo RAW, 1001 binów;
- background 0: 11 sweepów, 30.177 s, 11 indeksów źródeł;
- reference 1: 4 sweepy, 9.031 s, 4 indeksy źródeł;
- 16 źródłowych RAW: 11 + 4 + 1;
- zgodne, niepuste fingerprinty konfiguracji; odstępy 3 s;
- końcowy RAW: brak processed_values i reference_index;
- checkpoint: brak setpointów, RAW index 15, tryb dry_run,
  outputs_forced_off=true, processing=none i dowód konfiguracji;
- automatyczne odejmowanie tła i referencji zgodne z obliczeniem z pełnych
  zapisanych tablic; walidator thaTEC z require_pythat=True zaliczony.

Zakresy i liczby binów tego przykładu wynikają z symulacji; nie przypisujemy
ich fizycznemu sweepowi 10001-binowemu ze zrzutu.
