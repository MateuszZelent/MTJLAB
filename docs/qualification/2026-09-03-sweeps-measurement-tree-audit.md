# Sweeps — audyt drzewa pomiarowego i korekty

Data: 2026-09-03  
Zakres: autorowanie receptury w `Sweeps`, projekcja `Execution`, DnD,
edycja ROI/device, akcje strukturalne, wygląd i responsywność.

## Werdykt

W zakresie drzewa Sweeps korekta jest zakończona. Produkcyjna ścieżka używa
jednego, niemutowalnego snapshotu `SemanticMeasurementTree` oraz wspólnego
Fluent `MeasurementTreeModel`/`MeasurementTreeView`. DnD i akcje przycisków
przechodzą przez transakcję na źródle YAML, a nie przez zmianę samych elementów
Qt. Oprogramowanie przechodzi kwalifikację symulacyjną; przed użyciem z
fizycznym DUT nadal wymagany jest zatwierdzony HIL.

## Diagnoza stanu początkowego

Zidentyfikowane przyczyny niepełnego drzewa:

1. Widok elementowy (legacy `QTreeWidget`) był traktowany jako indeks
   receptury. Generowane wiersze pętli/ROI nie miały jednoznacznego odpowiednika
   w YAML, więc upuszczenie obok takiego wiersza mogło pozornie zadziałać, ale
   nie zapisać poprawnej pozycji.
2. Drop nie rozróżniał stabilnie pozycji przed, za i wewnątrz kontenera;
   pusty kontener oraz środkowy obszar liścia nie dawały operatorowi czytelnej
   informacji zwrotnej.
3. `Edit ROI` był dostępny głównie dla bezpośredniego typu `sweep`; węzły
   device z `parameter_actions` oraz wariant `anritsu_sg` nie miały spójnego
   wejścia do edytora.
4. Projekcja używała technicznych wierszy `update_*`, mieszała żądanie,
   zastosowanie i readback oraz nie utrzymywała jednolitego stanu aktywnego
   punktu.
5. Rozwinięcie, kolory, ikony, stan pusty, szerokości kolumn i zachowanie na
   małym ekranie nie były częścią kontraktu widoku.
6. Przy szybkim sweepie seria zdarzeń i odświeżeń Qt mogła zagłodzić pętlę
   GUI na Windows, mimo że pojedyncze operacje sprzętowe były krótkie.

## Wprowadzone korekty

### Model i architektura

- `normalize_recipe_tree()` buduje jedną, niemutowalną projekcję z identyfikacją
  `semantic_id`, `source_node_id`, rodzajem węzła, kontekstem osi i ścieżką
  rodziców.
- `MeasurementTreeModel` jest jedynym mutowalnym miejscem prezentacji stanu.
  `replace_tree()` podmienia snapshot na granicy dokumentu, a `apply_states()`
  zmienia tylko dotknięte wiersze.
- W `Sweeps` i `Execution` usunięto zależność produkcyjną od
  `RecipeTreeWidget`, `QTreeWidgetItem` i `execution_tree_snapshot`. Drzewo
  Execution jest jawnie tylko do odczytu; generowane wiersze są nieedytowalne i
  nieprzeciągalne.

### Drag and drop

- Widok używa własnych MIME dla semantycznego bloku i biblioteki bloków.
- Miejsce upuszczenia jest jawne: `BEFORE`, `AFTER`, `INSIDE` albo
  `ROOT_END`. Kontenery puste są drop-targetami; liść ma deterministyczny
  podział przed/za.
- Ruch jest zatwierdzany przez `move_recipe_node()` i ponowny parse całej
  receptury. Błąd walidacji pozostawia źródło i widok bez zmian.
- Odrzucane są ruchy do siebie/potomka, ruchy przez granicę `Finally` oraz
  próby przemieszczania wierszy generowanych. Biblioteka korzysta z tej samej
  ścieżki transakcyjnej.
- Podczas przeciągania widoczny jest kolorowy cue Fluent i komunikat z
  kierunkiem operacji; po dropie sygnał jest czyszczony niezależnie od wyniku.

### ROI i edycja device

- `Edit ROI` działa dla natywnego `sweep` (wybrana oś/segment), wiersza
  `Set ROI value` oraz modułu device zawierającego sweep w
  `parameter_actions`.
- Obsłużone warianty: Keithley, Rigol, Anritsu Spectrum i Anritsu SG.
  `anritsu_sg` zachowuje nazwę w YAML i dedykowany edytor, ale korzysta z
  zatwierdzonego resolvera/provider'a Anritsu.
- Osobno działa `Device settings`; wybór modułu nie jest mylony z wyborem
  osi ROI. Brakujące lub uszkodzone `parameter_actions` są traktowane jako
  pusta kolekcja i nie powodują wyjątku w UI.

### Akcje drzewa

- `Wrap in Repeat…` wymaga niepustego zaznaczenia, pyta o liczbę powtórzeń i
  zapisuje jedną transakcję. Nie pozwala owijać `Finally`, korzenia ani pustego
  kontenera.
- `Up`, `Down`, `Duplicate` i `Delete` działają na źródłowym grafie receptury;
  duplikowanie nadaje nowe ID również potomkom. Obowiązują granice gałęzi,
  `else` i `Finally`.
- Dostępne są także skróty `Delete`, `Ctrl+D`, `Alt+Up`, `Alt+Down`, `Return`,
  `Enter` oraz menu kontekstowe. Każda udana edycja odświeża snapshot i historię
  undo/redo; błędna transakcja nie zmienia drzewa.

### Czytelność i stany

- Drzewo jest domyślnie w pełni rozwinięte po `setModel()` i po każdym resecie
  modelu.
- Ikony i akcenty pochodzą z tokenów Fluent: device/axis/loop, action, wait,
  acquire, output, `Finally` i automatyczne safety. Aktywny wiersz ma spine i
  wyróżnienie delegata.
- Cztery kolumny rozdzielają `Operation`, `Configured / active value`,
  `Progress` i `State`. Wartości pokazują żądanie, potwierdzone zastosowanie i
  readback bez przedstawiania żądania jako potwierdzenia; nieznany semantic ID
  daje pojedynczy sygnał diagnostyczny.
- Kolumny wartości/progress/state mają stabilne szerokości, operacja dostaje
  resztę miejsca, a widok zachowuje przewijanie również przy szerokości
  roboczej 820 px.

### Wykonanie i responsywność

- `RunTelemetryCoalescer` i bufor `RunMonitorPage` scalają tylko wysokoczęste
  projekcje prezentacyjne (najnowszy stan semantyczny, checkpoint, preview).
  Zdarzenia bezpieczeństwa, terminalne i trwałe nie są gubione.
- Aktualizacje modelu są grupowane jednym timerem; ukryte strony device nie są
  przebudowywane przy każdym punkcie. Zapis HDF5/checkpointów pozostaje
  punktowy i trwały w workerze.
- W ścieżce symulacji dodano wyłącznie zeroczasowe ustąpienie wątku po
  zdarzeniu, aby Windows oddawał czas pętli GUI. Nie dotyczy to ścieżki
  sprzętowej ani kolejności/zawartości zapisu.
- Zapis ustawień Keithley z readbacku jest poza GUI; zwykłe poziom/compliance
  pozostają wartościami roboczymi, natomiast jawnie trwałe ustawienia (np.
  NPLC/settling) są zapisywane w globalnej transakcji.

### Jednostki i bezpieczeństwo

Normalizacja SI, wymiary i formatowanie pozostają w domenie/compilerze; UI nie
interpretuje prefiksów ani dBm jako zwykłego napięcia. Resolver/provider,
compiler, Runner i istniejące kontrole OUTPUT/safety zachowują swoje granice.
Zmiana drzewa nie omija preflightu, `Finally`, watchdogów, E-STOP ani
potwierdzonego shutdownu.

## Dowody testowe

| Zakres | Wynik |
| --- | --- |
| `tests/test_measurement_tree_model.py` | 11 passed |
| Design system | 12 passed |
| Builder, ROI Keithley/Rigol/Anritsu/Anritsu SG, akcje i layout | 84 passed, 4 subtests |
| Semantic tree/provider/compiler/run controller | 82 passed, 6 subtests |
| Adaptery, Runner, HDF5, recovery, symulacja | 138 passed, 5 subtests |
| Responsywność GUI bez qualification | 7 passed, 1 deselected |
| Scenariusze Sweeps/Execution (focused) | 3 passed, 16 deselected |
| Log zdarzeń (sampling poza aktywnym runem) | 2 passed |
| Qualification 1 000 punktów | 1 passed, 130.82 s; 1 000 widm raw/processed po 10 001 wartości, zero rebuildów drzewa, maksymalna luka GUI poniżej limitu 250 ms |
| Keithley global-save/background-save subset | 4 passed |
| `ruff check app tests` | All checks passed |
| `python -m compileall -q app tests` | passed |
| `git diff --check` | passed (jedynie standardowe ostrzeżenia CRLF Git) |

Polecenia kwalifikacyjne i warunki sprzętowe są opisane w
[`unified-sweep-tree-release-gate.md`](unified-sweep-tree-release-gate.md).

## Pozostałe uwagi i granice dowodu

- Jeden istniejący test `test_anritsu_manual_read_retries_unmeasured_trace_without_new_sweep`
  pozostaje niespójny z sąsiednim kontraktem `read_once`: produkcyjny przycisk
  wykonuje świeży `single_sweep`, a test oczekuje wyłącznie pasywnego
  `fetch_current_trace`. Nie dotyczy to drzewa Sweeps ani DnD; nie zmieniono
  zachowania sprzętowego tylko po to, aby ukryć tę sprzeczność.
- Testy geometrii wykonują `show()` i `processEvents()`. Natywne screenshoty
  Windows dla jasnego/ciemnego motywu oraz HIL z każdym instrumentem pozostają
  obowiązkowym krokiem release gate; backend offscreen nie jest dowodem
  typografii ani kontrastu.
- Kwalifikacja symulacyjna nie jest zgodą na podłączenie DUT. Przed release
  fizycznym trzeba zarejestrować firmware, seriale, kalibrację, trace TX/RX,
  kolejność OUTPUT-OFF i wynikowy plik HDF5 zgodnie z gate.

## Rekomendacja

Software gate dla drzewa pomiarowego: **GO**.  HIL/physical release: **PENDING**
do wykonania procedury bezpieczeństwa na zatwierdzonych urządzeniach.

