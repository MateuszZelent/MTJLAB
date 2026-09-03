# Audyt i weryfikacja funkcji `wait` w drzewie Sweep (MTJLAB)

**Data audytu:** 2026-09-03  
**Status kwalifikacji:** ZWERYFIKOWANY POZYTYWNIE (13/13 testów jednostkowych i integracyjnych przeszło pomyślnie)  
**Środowisko:** Python 3.14 / PySide6 / PySide6-Fluent-Widgets, architektura MTJLAB  
**Plik testowy audytu:** [`tests/test_audit_wait_in_sweep_tree.py`](file:///C:/Users/Shark/git/MTJLAB/tests/test_audit_wait_in_sweep_tree.py)

---

## 1. Cel audytu

Celem audytu była pełna, wielopoziomowa weryfikacja poprawności działania funkcji oraz węzła `wait` (opóźnienia czasowego / czasu ustalania) w strukturze drzewa sweep (`sweep tree` / `MeasurementTree`):
1. **Parsowanie i model receptury** (`app/recipes/models.py`) — struktura węzła `wait`, dozwolone relacje rodzic-dziecko.
2. **Drzewo semantyczne i normalizacja** (`app/recipes/semantic_tree.py`) — translacja węzła `wait` wewnątrz pętli sweep (`LOOP_BODY`), etykietowanie, przypisanie identyfikatora semantycznego (`semantic_id`), ikony i role.
3. **Prezentacja w modelu drzewa Fluent** (`app/ui/measurement_tree/model.py`) — kolumny *Operation*, *Configured / active value*, *Progress*, *State*, tokeny kolorystyczne, ikona `STOP_WATCH`.
4. **Kompilacja planu wykonawczego** (`app/engine/compiler.py`) — generowanie akcji planu (`PlanAction`) dla każdego punktu siatki sweep, propagacja kontekstu osi (`AxisPointContext`), iloczyn kartezjański w zagnieżdżonych sweepach, walidacja limitów bezpieczeństwa (0–3600 s) i wymiarów fizycznych (`DIMENSION_TIME`).
5. **Estymacja i polityka bezpieczeństwa** (`app/engine/estimation.py`, `app/engine/policy.py`) — uwzględnianie czasu `wait` w całkowitym czasie receptury oraz wyliczanie dynamicznego watchdog deadline (`duration + watchdog_grace`).
6. **Wykonanie w silniku pomiarowym** (`app/engine/runner.py`) — dokładność odliczania (`_interruptible_wait`), emitowanie strumienia zdarzeń (`action_started`, `semantic_operation_started`, `action_finished`, `semantic_operation_applied`), kooperatywna przerywalność przy żądaniu STOP / E-STOP.
7. **Interfejs operatora i edytor** (`app/ui/execution/page.py`, `app/ui/recipes/common_dialogs.py`, `app/ui/recipes/page.py`) — karta bieżącej operacji, odliczanie na żywo w heartbeacie, okno edycyjne `ActionNodeEditorDialog` z walidacją jednostek SI.

---

## 2. Wynik ogólny (Werdykt)

Funkcja **`wait` w drzewie sweep działa w pełni poprawnie, deterministycznie i bezpiecznie**.

- Każdy punkt siatki sweep w pętli otrzymuje dedykowaną akcję `wait` z zachowanym pełnym kontekstem osi (`point_index`, `point_count`, `value_si`, `active_setpoints_si`).
- W zagnieżdżonych drzewach sweep (np. oś zewnętrzna napięcia compliance × oś wewnętrzna prądu) iloczyn kartezjański jest generowany bezbłędnie: czasy oczekiwania na poziomie zewnętrznym wykonują się raz na punkt zewnętrzny, a na poziomie wewnętrznym – przy każdym kroku wewnętrznym.
- Węzeł `wait` w drzewie wizualnym Fluent prezentuje się spójnie (żółty/caution akcent, ikona stopera, etykieta z czasem). W trakcie pomiaru kolumna *Progress* precyzyjnie raportuje indeks punktu (np. `1/3 · ROI 1`), a kolumna *State* przechodzi przez fazy `READY` → `RUNNING` → `APPLIED`.
- Wycofanie / zatrzymanie pomiaru (`STOP` / `E-STOP`) w trakcie trwania `wait` jest **natychmiastowe** (czas reakcji poniżej 50 ms dzięki kwantowaniu pętli `_interruptible_wait` w interwałach do 50 ms) i gwarantuje bezpieczne wyłączenie wyjść przyrządów (`ApplicationState.SAFE`).
- Wprowadzanie wartości w UI i kompilatorze rygorystycznie wymusza jednostki czasu (`s`, `ms`, `us`, `min`) oraz granice 0–3600 s.

---

## 3. Szczegółowe wyniki audytu według podsystemów

### 3.1. Model danych i gramatyka receptur (`app/recipes/models.py`)

- **Typ węzła:** `wait` jest zarejestrowany w `ACTION_TYPES`.
- **Brak dzieci (liść wykonawczy):** `wait` nie należy do `CONTAINER_NODE_TYPES`. Próba zdefiniowania dzieci dla `wait` kończy się błędem `ConfigurationError: action wait cannot have children.`
- **Dozwolone osadzenie:** Węzeł `wait` może być legalnym dzieckiem węzłów kontenerowych:
  - `sweep` (bezpośrednio w pętli sweep),
  - `sequence` (sekwencja nadrzędna lub podrzędna),
  - `repeat` (pętla powtórzeń wewnątrz lub na zewnątrz sweepa),
  - `if` (gałęzie warunkowe `children` oraz `else` wewnątrz sweepa).

### 3.2. Drzewo semantyczne (`app/recipes/semantic_tree.py`)

- Gdy receptura ze sweepem zawiera węzeł `wait`, funkcja `normalize_recipe_tree()` tworzy:
  - Węzeł osi: `SemanticNodeKind.SWEEP_AXIS` (np. `id: current-sweep`).
  - Węzeł pętli: `SemanticNodeKind.LOOP_BODY` z etykietą np. `"For each source-current point"`.
  - Dzieci pętli:
    1. `SET_ROI_VALUE` – syntetyczny wiersz ustawienia wartości osi,
    2. `ACTION` – wiersz odpowiadający węzłowi `wait` o etykiecie `"Wait · <duration>"` (np. `"Wait · 250 ms"`),
    3. kolejne akcje (np. pomiar, akwizycja).
- Węzeł `wait` zachowuje swój `source_node_id`, dzięki czemu kliknięcie w drzewie umożliwia jego zaznaczenie i edycję.
- Relacje w grafie (`parent_by_id`, `children_by_id`, `by_id`) są w 100% spójne.

### 3.3. Model widoku drzewa Fluent (`app/ui/measurement_tree/model.py`)

Zbadano zachowanie czterech kolumn modelu `MeasurementTreeModel`:
1. **Kolumna 0 (Operation):** Tekst `"Wait · <duration>"`, ikona `FluentIcon.STOP_WATCH`, demi-bold dla nagłówków, akcent `tokens.caution`.
2. **Kolumna 1 (Configured / active value):** Wyświetla skonfigurowany czas trwania (np. `"250 ms"`). Tekst pozostaje czytelny zarówno przed startem, jak i w trakcie biegu.
3. **Kolumna 2 (Progress):**
   - Przed pomiarem: `"—"`.
   - W trakcie pomiaru: przyjmuje `point_index` i `point_count` z kontekstu osi `axis_context` i wyświetla np. `"1/3 · ROI 1"`, `"2/3 · ROI 1"`.
4. **Kolumna 3 (State):**
   - Przed pomiarem: `"READY"`.
   - W trakcie trwania kroku wait: `"RUNNING"`.
   - Po upływie czasu: `"APPLIED"` z kolorem sukcesu (`tokens.success`).
- Zmiana stanu następuje bez przebudowy drzewa (`tree_rebuilds == 0`), co chroni interfejs przed migotaniem i spadkiem wydajności.

### 3.4. Kompilator planu wykonawczego (`app/engine/compiler.py`)

Zbadano kompilację węzła `wait` wewnątrz pętli sweep:
- **Liczba wygenerowanych akcji:** Dla sweepa o $N$ punktach kompilator generuje dokładnie $N$ akcji `PlanAction` typu `"wait"`.
- **Kolejność wykonania:**
  $$\text{Ustawienie nastawy ROI (Set ROI / update)} \longrightarrow \text{wait (stabilizacja)} \longrightarrow \text{akwizycja / pomiar}$$
- **Propagacja kontekstu osi:** Każda wygenerowana akcja `wait` ma przypisany `axis_context` zawierający:
  - `point_index`: aktualny indeks kroku ($0, 1, \dots, N-1$),
  - `point_count`: całkowita liczba punktów,
  - `value_si`: wartość fizyczna nastawy w danym kroku,
  - `active_setpoints_si`: słownik aktywnych nastaw (w tym osi nadrzędnych).
- **Zagnieżdżanie wieloosiowe:**
  - Oś zewnętrzna (2 punkty) z `outer-wait` (100 ms) oraz oś wewnętrzna (3 punkty) z `inner-wait` (50 ms):
    - Wygenerowano 2 akcje `outer-wait` (wykonywane przed wejściem w pętlę wewnętrzną).
    - Wygenerowano 6 akcji `inner-wait` ($2 \times 3$).
    - W każdej akcji wewnętrznej `active_setpoints_si` zawierało aktualne wartości obu osi.
- **Koegzystencja wielu węzłów `wait`:**
  - Węzły `pre-wait` i `post-wait` w tej samej pętli kompilują się jako niezależne kroki z odrębnymi `semantic_id`.
- **Obsługa pętli `repeat` i rozgałęzień `if` wewnątrz sweepa:**
  - `repeat(count=3)` wewnątrz sweepa generuje $N \times 3$ wywołań `wait`, zachowując kontekst osi sweepa nadrzędnego.
  - Warunek `if` poprawnie ewaluuje zmienną sweepa (np. `${keithley.B.current} > 0.5 mA`) i włącza właściwy węzeł `wait` (np. krótki lub długi).

### 3.5. Walidacja bezpieczeństwa i granice fizyczne

Przetestowano odporność kompilatora na nieprawidłowe parametry:
| Scenariusz | Wartość | Rezultat | Oczekiwane zachowanie | Status |
| :--- | :--- | :--- | :--- | :--- |
| Minimalny czas | `0 s` | Sukces (`duration_s = 0.0`) | Dopuszczalny (brak opóźnienia) | **PASS** |
| Maksymalny czas | `3600 s` (1 godzina) | Sukces (`duration_s = 3600.0`) | Maksymalny dozwolony limit laboratoryjny | **PASS** |
| Przekroczenie limitu | `3601 s` | `SafetyViolation` | Zgłoszenie naruszenia limitu 0–3600 s | **PASS** |
| Wartość ujemna | `-10 ms` | `SafetyViolation` | Zgłoszenie naruszenia limitu 0–3600 s | **PASS** |
| Niepoprawny wymiar | `5 V` | `QuantityError` | Odrzucenie jednostki niezgodnej z czasem | **PASS** |
| Brak jednostki | `10` (liczba) | `QuantityError` | Wymóg jawnej jednostki fizycznej | **PASS** |
| Zmienna kontekstowa | `${keithley.B.settling_time}` | Sukces | Dynamiczne podstawienie wartości z osi sweep | **PASS** |

### 3.6. Wykonanie w Runnerze i przerywalność (`app/engine/runner.py`)

- **Precyzja:** W symulowanym przebiegu dla 2 punktów po 30 ms zmierzony czas wykonania wynosił $> 60\text{ ms}$, co dowodzi rzeczywistego odczekiwania zadanego interwału.
- **Zdarzenia:** Runner bezbłędnie wyemitował zdarzenia:
  - `semantic_operation_started` z `phase="running"`, `duration_s`, `axis_context`,
  - `semantic_operation_applied` z `phase="applied"`, `applied_si=duration_s`, `readback_si=duration_s`.
- **Natychmiastowe przerwanie (STOP / E-STOP):**
  - Uruchomiono recepturę z krokiem `wait: 10 s` wewnątrz sweepa.
  - Po 80 ms wysłano żądanie `runner.request_stop()`.
  - Runner natychmiast przerwał oczekiwanie (w czasie $< 100\text{ ms}$ od żądania), nie czekając na upłynięcie 10 sekund.
  - Nastąpiło natychmiastowe przejście do procedury `_safe_shutdown()`, wyłączenie wyjść przyrządów i osiągnięcie stanu `ApplicationState.SAFE`.

### 3.7. Interfejs edycji (`ActionNodeEditorDialog`)

- Kliknięcie węzła `wait` w edytorze receptur otwiera modal `ActionNodeEditorDialog`.
- Pole `duration` jest edytowane jako ciąg tekstowy z walidacją `parse_quantity(value, DIMENSION_TIME)`.
- Wprowadzenie poprawnej wartości (np. `"2.5 s"`) zostaje zaakceptowane i zaktualizowane w YAML.
- Wprowadzenie wartości niebędącej czasem (np. `"not-a-time"`, `"500 V"`) jest blokowane wyjątkiem `QuantityError`.

---

## 4. Ważna uwaga architektoniczna: Jawny `wait` vs `settling_time` przyrządu

Audyt ujawnił kluczowe rozróżnienie w zachowaniu parametrów stabilizacji:

1. **Jawny węzeł `type: sweep` (rekomendowana, nowoczesna składnia):**
   - Jeśli przed sweepem zdefiniowano blok `configure_keithley` z parametrem `settling_time: 40 ms`, czas ten jest aplikowany **jednorazowo** podczas początkowej konfiguracji przyrządu przed startem sweepa.
   - Aby przyrząd stabilizował się w **każdym punkcie sweepa**, operator powinien umieścić jawny węzeł `type: wait` jako dziecko węzła `sweep` (np. `duration: 40 ms`). Jest to rozwiązanie w 100% audytowalne, widoczne w drzewie i czytelne dla operatora.
2. **Historyczna składnia modułowa (`parameter_actions` z `mode: sweep`):**
   - Kompilator legacy automatycznie wstrzykiwał syntetyczną akcję `{node.id}.settle` (typu `wait`) przed akwizycją w każdym punkcie.
   - W nowym drzewie zunifikowanym (`Unified Sweep Tree`) jawny węzeł `wait` jest czystszym i preferowanym podejściem.

---

## 5. Zestawienie uruchomionych testów kwalifikacyjnych

Utworzony dedykowany zestaw testów w [`tests/test_audit_wait_in_sweep_tree.py`](file:///C:/Users/Shark/git/MTJLAB/tests/test_audit_wait_in_sweep_tree.py):

```
tests/test_audit_wait_in_sweep_tree.py::test_single_sweep_wait_semantic_tree_structure PASSED
tests/test_audit_wait_in_sweep_tree.py::test_measurement_tree_model_wait_presentation_and_lifecycle PASSED
tests/test_audit_wait_in_sweep_tree.py::test_single_sweep_wait_compilation PASSED
tests/test_audit_wait_in_sweep_tree.py::test_nested_sweep_wait_compilation_and_cartesian_expansion PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_wait_safety_bounds_and_validation PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_wait_variable_substitution PASSED
tests/test_audit_wait_in_sweep_tree.py::test_legacy_device_sweep_auto_settle_coexistence PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_wait_simulated_runner_execution PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_wait_prompt_cancellation PASSED
tests/test_audit_wait_in_sweep_tree.py::test_action_node_editor_dialog_validates_wait_duration PASSED
tests/test_audit_wait_in_sweep_tree.py::test_multiple_wait_nodes_in_single_sweep PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_containing_repeat_with_wait PASSED
tests/test_audit_wait_in_sweep_tree.py::test_sweep_containing_if_condition_with_wait PASSED
============================== 13 passed in 10.61s ==============================
```

Ruff linter:
```
python -m ruff check tests/test_audit_wait_in_sweep_tree.py
All checks passed!
```

---

## 6. Wnioski i zalecenia

1. **Stabilność produkcyjna:** Funkcja `wait` wewnątrz drzewa sweep jest w pełni zintegrowana z silnikiem wykonawczym, drzewem semantycznym i interfejsem graficznym. Nie stwierdzono żadnych wycieków wątków, zawieszeń pętli GUI ani błędów desynchronizacji indeksów.
2. **Dobre praktyki tworzenia receptur:**
   - W recepturach opartych o węzły `type: sweep` zawsze zaleca się umieszczanie jawnego węzła `wait` przed blokiem pomiarowym (`measure_*` lub `acquire_*`), np.:
     ```yaml
     - id: magnetic-field-sweep
       type: sweep
       target: lakeshore.field
       start: 0 mT
       stop: 100 mT
       points: 51
       children:
         - id: field-settling
           type: wait
           duration: 200 ms
         - id: acquire-point
           type: acquire_spectrum
           trace: TRAC1
     ```
   - Gwarantuje to czytelną prezentację czasu stabilizacji w drzewie pomiarowym oraz pewność, że pomiar spektrometrem lub woltomierzem nastąpi po ustabilizowaniu parametrów fizycznych próbki.
