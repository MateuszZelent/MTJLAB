# Sweep Production Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Doprowadzić mechanizm sweepów, zapis danych i symulację bez sprzętu do stanu, w którym każdy zakończony run ma dowiedzioną kompletność, recovery wznawia wyłącznie z potwierdzonego stanu bezpiecznego, a przebieg symulacyjny jest odtwarzalny i obejmuje wszystkie wspierane urządzenia.

**Architecture:** Zachować jedną ścieżkę `Recipe -> RecipeCompiler -> RunWorker -> RecipeRunner -> adapters -> Hdf5RunWriter -> PyThat/Results` dla sprzętu i symulacji. Dodać typowany kontrakt danych runu oraz typowany ledger zastosowanego stanu urządzeń; symulacja ma różnić się wyłącznie fabryką transportu i jawnym kontekstem symulacji. Aktywne bezpieczeństwo wyjść pozostaje domeną limitów sprzętowych i profilu stacji; legacy `dut_limits` są wyłącznie nieegzekwowanym provenance. Kompletność checkpointu, bezpieczny resume i końcowa walidacja mają być niezależnymi, fail-closed bramkami.

**Tech Stack:** Python 3.12, PySide6, PySide6-Fluent-Widgets, Pydantic, ruamel.yaml, h5py, NumPy, PyThat, pytest/unittest, Ruff.

**Spec:** `docs/superpowers/specs/2026-07-18-sweep-simulation-design.md`; ustalenia audytu w sekcjach „Werdykt” i „Rejestr ustaleń” tego dokumentu są wiążącym uzupełnieniem specyfikacji.

## Global Constraints

- Zachować Fluent-native shell opisany w `AGENTS.md`; testy UI muszą wykonać `show()` i przetworzyć event loop.
- Nie tworzyć osobnego runnera ani osobnego formatu danych dla symulacji.
- Wszystkie wartości fizyczne z YAML/UI mają mieć jednostkę, być normalizowane do SI dokładnie raz i mieć jawne jednostki w zapisie.
- Limity sprzętowe i konfigurowalne limity profilu stacji są autorytatywne. Legacy `recipe.dut_limits` nie mogą blokować wykonania i muszą być zapisane jednoznacznie jako `enforced: false`; ewentualny przyszły opt-in envelope wymaga osobnego projektu i akceptacji operatora.
- Konfiguracja odbywa się przy potwierdzonym OUTPUT OFF; OUTPUT ON wymaga osobnej, jawnej i odczytanej transakcji.
- `SAFE` wolno zapisać wyłącznie po potwierdzeniu stanu; stan niepewny pozostaje `UNKNOWN` albo `FAULT`.
- HDF5 jest naukowym rekordem i źródłem prawdy; CSV jest odbudowywalnym indeksem pomocniczym.
- Run nie może uzyskać statusu `completed`, jeżeli liczba punktów, widm, wymagane pola, tożsamości, capabilities, seed symulacji albo PyThat round-trip nie spełniają kontraktu planu.
- Resume wymaga zgodności planu, receptury, settings, trybu, seedu, tożsamości, capabilities oraz potwierdzonego OUTPUT OFF wszystkich urządzeń wyjściowych.
- Symulacja nie może otworzyć fizycznego VISA, TCP, USB ani portu szeregowego.

---

## Werdykt audytu z 2026-08-31

**Decyzja: NO-GO dla deklaracji „produkcyjnie kompletne” oraz NO-GO dla deklaracji „100% testów bez sprzętu”.**

Nominalny pionowy przebieg działa i ma mocną bazę testową: receptura jest kompilowana, adaptery wykonują readback, Anritsu zapisuje RAW/processed/reference, HDF5 jest transakcyjny, Results otwiera dane, a `--simulate` nie korzysta z fizycznych transportów dla Rigola, Keithleya, Anritsu i MOKE. Nie można jednak uznać całości za produkcyjną, ponieważ obecny writer może zamknąć niekompletny plik jako `completed`, recovery może oznaczyć granicę jako bezpieczną przy aktywnym Anritsu SG, a seed zapisany w pliku nie jest wiarygodnie związany z danymi generowanymi przez sesje dzierżawione z GUI ani przez resume.

### Stan podsystemów

| Podsystem | Ocena | Dowód / ograniczenie |
| --- | --- | --- |
| Parser i authoring receptur | warunkowo gotowy | Ścisły YAML, unikalne ID, skończone pętle, limity ekspansji; współistnieją legacy `sweep` i modułowe `parameter_actions`. |
| Jednostki i osie | mocny | Jawne jednostki, SI, weryfikacja wymiaru, dB/dBm rozdzielone, readback zastępuje żądany setpoint; prywatny JSON nadal nie ma osobnego manifestu jednostek. |
| Compiler/preflight | warunkowo gotowy | Waliduje kolejność konfiguracji/OUTPUT i limity profilu; celowo nie egzekwuje legacy `dut_limits`. Audyt wykrył fałszywy komunikat readiness, poprawiony w bieżącej korekcie. |
| Adaptery i bezpieczeństwo komend | mocny z luką systemową | Configure-off, readback, ograniczone retry, watchdog, E-STOP i wieloetapowy shutdown są zaimplementowane; pozostała luka dotyczy recovery. |
| Akwizycja widma | mocny | Pojedynczy sweep, deadline, kontrola liczby punktów, `-999`, finite, rosnąca oś, averaging w mocy liniowej, RAW/reference/processed. |
| HDF5/CSV/PyThat | niegotowy produkcyjnie | Atomowy checkpoint i rollback istnieją, ale końcowa kompletność względem planu nie jest sprawdzana. |
| Recovery/resume | niegotowy produkcyjnie | Niepełne liczenie checkpointów, brak SG w granicy bezpiecznej i brak porównania tożsamości/capabilities. |
| Symulacja end-to-end | częściowa | Jest prawdziwa ścieżka compiler-runner-writer i losowe widmo/MOKE; seed, leasing, resume, Lake Shore, fault scenarios i pełny kontrakt stanu są niekompletne. |
| Run Monitor/Results | częściowy | Pokazuje stan i dane, ale ledger jest nietypowany, a błąd PyThat jest połykany i zamieniany na brak danych. |
| Kwalifikacja | częściowa | Zielone testy nominalne; brak kompletnej receptury wszystkich urządzeń, golden HDF5 jest pomijany, pełna suite jest zbyt wolna jako szybka bramka. |

## Dowody wykonane podczas audytu

- `python -m pytest -q tests/test_sweep_points.py tests/test_recipe_compiler.py tests/test_adapters_and_runner.py tests/test_simulators.py tests/test_simulation_context.py tests/test_simulated_run.py tests/test_hdf5_writer.py tests/test_thatec_schema_mapper.py tests/test_thatec_validator.py tests/test_station_readiness.py tests/test_device_run_lease.py tests/test_results_page.py`
  - wynik: `249 passed, 3 skipped, 35 subtests passed in 195.34s`;
  - pominięcia: brak laboratoryjnego/licencjonowanego golden HDF5.
- `python -m pytest -q tests/test_run_recovery.py tests/test_run_controller.py tests/test_fluent_recipe_execution_pages.py ...`
  - wynik: `21 passed in 348.67s`.
- `python -m ruff check app tests`
  - wynik: `All checks passed!`.
- Pełne `python -m pytest -q` zostało przerwane po długim okresie w grupie 16%; testy nadal emitowały sukcesy, lecz zestaw nie daje praktycznej szybkiej bramki i nie ma końcowego wyniku z tego audytu.
- Sonda negatywna utworzyła writer z `expected_points=2`, nie zapisała żadnego punktu i wywołała `close("completed")`.
  - wynik: `{'status': 'completed', 'points': 0, 'measurement_running': 0}`;
  - dowodzi to braku końcowej walidacji kardynalności.

## Rejestr ustaleń

### C-01 — `completed` nie oznacza kompletnego runu

**Priorytet:** krytyczny.

`Hdf5RunWriter.close()` uruchamia walidator struktury i PyThat, lecz nie porównuje liczby zapisanych punktów i widm z planem. `ThatecHdf5Writer._expected_points` jest przypisywane, ale nie uczestniczy w zamknięciu. Nie istnieje `RunDataContract`, chociaż zatwierdzona specyfikacja go wymaga. W efekcie poprawny strukturalnie, pusty plik może zostać oznaczony jako `completed`.

**Dowód:** `app/storage/hdf5_writer.py:643`, `app/storage/thatec_writer.py:44`, sonda `0/2` powyżej.

### C-02 — recovery może wybrać niebezpieczną albo semantycznie błędną granicę

**Priorytet:** krytyczny.

- `RecipeRunner._record_safe_boundary_if_advanced()` sprawdza Rigol i Keithley, ale pomija `_anritsu_sg_output_active`.
- `RunRecoveryManager._latest_boundary()` liczy wyłącznie `acquire_spectrum` i `checkpoint`, chociaż runner tworzy punkty również dla `measure_moke_hall` i `measure_lakeshore_field` z `checkpoint=true`.
- Prelude odtwarza pełne konfiguracje Rigol/Keithley/Anritsu Spectrum, ale nie Anritsu SG i nie dowodzi odtworzenia ostatniego zastosowanego stanu.
- Resume nie porównuje aktualnej tożsamości i capabilities ze snapshotem runu.

**Dowód:** `app/engine/runner.py:1752`, `app/engine/recovery.py:105`, `app/engine/recovery.py:116`, `app/ui/run_worker.py:255`.

### C-03 — legacy DUT metadata były przedstawiane jako egzekwowane

**Priorytet:** średni — spójność provenance i komunikacji z operatorem; nie jest to brak aktywnej blokady bezpieczeństwa.

Typed DUT envelope i jego restrykcyjne blokady zostały celowo usunięte w commicie `003c3b1`. Testy kompilatora prawidłowo utrwalają, że legacy `recipe.dut_limits` nie zmieniają wykonania. Błąd polegał na tym, że readiness deklarował ich walidację, runner domieszał je do aktywnego `safety_context`, a HDF5 nie informował, że są nieegzekwowanym metadata-only provenance.

**Korekta wdrożona:** readiness rozróżnia legacy metadata i autorytatywny profil stacji, `/run/dut_limits_policy_json` oraz labbook zapisują `enforced: false`, a checkpoint `safety_context` zawiera tylko zastosowane/readback parametry. Aktywne limity sprzętowe, `lab_limits`, permissions, compliance i shutdown pozostają bez zmian.

**Dowód:** `app/domain/readiness.py`, `app/recipes/models.py`, `app/storage/hdf5_writer.py`, `app/storage/thatec_writer.py`, `app/engine/runner.py`, `tests/test_station_readiness.py`, `tests/test_hdf5_writer.py`, `tests/test_simulated_run.py`.

### H-01 — seed symulacji nie jest end-to-end provenance

**Priorytet:** wysoki.

`RunWorker` tworzy `SimulationContext`, ale adapter z aktywnego `DeviceController` jest zwracany przed fabryką korzystającą z tego kontekstu. Modal readiness wymaga połączenia urządzeń i przekazuje aktywne kontrolery, więc typowy run GUI używa wcześniej utworzonych symulatorów bez run-scoped seedu. `RunController.start()` nie udostępnia `simulation_seed`; resume tworzy nowy losowy kontekst, a istniejące `/run/simulation_json` pozostaje z poprzednim seedem.

**Dowód:** `app/ui/run_worker.py:100`, `app/ui/run_worker.py:128`, `app/ui/run_worker.py:354`, `app/ui/run_worker.py:492`, `app/ui/shell/main_window.py:1525`, `app/ui/shell/main_window.py:1671`.

### H-02 — zatwierdzone kontrakty stanu urządzeń nie zostały wdrożone

**Priorytet:** wysoki.

W kodzie nie ma `AppliedParameter`, `AppliedDeviceState`, `RunDataContract`, `DeviceDataRequirement` ani zdarzenia `device_state_applied`. Obecny `dict` przechowuje `requested/actual`, ale nie ma rewizji, czasu, jednostki i rodzaju potwierdzenia (`readback`, `simulated_ack`, `command_only`). Test czterech urządzeń sprawdza tylko zestaw kluczy urządzeń, nie komplet parametrów.

**Dowód:** brak wyników `rg` w `app`/`tests`; wymagania istnieją w `docs/superpowers/specs/2026-07-18-sweep-simulation-design.md:152` i `:263`; obecny zapis jest w `app/engine/runner.py:1465`.

### H-03 — brak kompletnej kwalifikacji symulacyjnej wszystkich urządzeń

**Priorytet:** wysoki.

- Nie ma śledzonej receptury czterech/pięciu urządzeń z MOKE w każdym punkcie.
- Test 1000 widm obejmuje Rigol, Keithley i Anritsu; test czterech urządzeń jest ręcznie zbudowanym planem jednopunktowym.
- `simulated_station_settings()` włącza MOKE, ale nie przygotowuje Lake Shore 475, więc pełna symulacja zależy od realnego profilu.
- Keithley domyślnie ma `noise_fraction=0`, Lake Shore zwraca stałą wartość, a `time_scale` jest tylko metadanym.

**Dowód:** `tests/test_simulated_run.py:531`, `tests/test_simulated_run.py:574`, `app/devices/simulators.py:829`, `app/devices/simulators.py:883`, `app/devices/lakeshore_475/simulator.py:8`.

### H-04 — fault injection nie jest scenariuszem end-to-end

**Priorytet:** wysoki.

`SimulatorFault` pozwala testom jednostkowym wstrzyknąć błąd odpowiedzi/komendy, lecz operator ani qualification runner nie może wybrać odtwarzalnego scenariusza: timeout akwizycji, utrata transportu, readback mismatch, compliance, błąd storage, anulowanie w punkcie i nieudane shutdown confirmation. Nie można więc w 100% przećwiczyć zachowania aplikacji bez ręcznego konstruowania adapterów w teście.

### M-01 — PyThat i mapowanie osi degradują się zbyt cicho

**Priorytet:** średni.

`ThatecSchemaMapper` przechodzi do `checkpoint_fallback` przy błędzie mapowania lub niejednoznacznej topologii, a Results połyka wyjątek `read_pythat_run_data()` i pokazuje brak danych bez zachowania treści błędu. Dla runu aplikacji utrata fizycznych osi nie może wyglądać jak pełny sukces.

**Dowód:** `app/storage/thatec_schema_mapper.py:52`, `app/storage/thatec_schema_mapper.py:75`, `app/ui/results/page.py:73`, `app/ui/results/metadata_panel.py:187`.

### M-02 — prywatne setpointy nie mają własnego manifestu jednostek

**Priorytet:** średni.

Publiczny thaTEC zapisuje jednostki osi i wskaźników, lecz `/points/<n>/setpoints_json` opiera znaczenie na nazwie targetu i aktualnym registry. Długoterminowa interpretacja prywatnego checkpointu wymaga zamrożonego manifestu `target -> dimension -> unit` w `/run`.

### M-03 — testy nie są ułożone jako praktyczne bramki

**Priorytet:** średni.

Nominalne testy są szerokie, ale pełny zestaw jest zbyt wolny do szybkiej iteracji, brakuje obowiązkowego golden HDF5 w środowisku audytu, a krytyczne negatywne kontrakty C-01/C-02/H-01 nie mają testów. Potrzebne są rozdzielone lane: szybki PR, symulacja end-to-end i kwalifikacja release/HIL.

## Mocne strony, które należy zachować

- `parse_quantity` i registry zachowują wymiar, SI, prefiksy oraz osobne znaczenie dB/dBm.
- Compiler rozwija plan deterministycznie, ogranicza liczbę akcji, waliduje kolejność konfiguracji/OUTPUT i zapisuje hash.
- Adaptery ponownie walidują limity, konfigurują przy OUTPUT OFF, czytają stan i odrzucają mismatch.
- Retry nie obejmuje ślepo energizujących przejść; watchdog wyzwala kooperatywny stop i niezależny E-STOP.
- Anritsu wykonuje kwalifikowaną sekwencję single sweep, sprawdza deadline, sentinel `-999`, liczność, finite i oś.
- Reference i processed spectrum zachowują RAW, sprawdzają grid i zapisują operację/jednostkę.
- HDF5 używa `/_pending`, flush, commit przez przeniesienie linku, rollback public/private i natychmiastowy flush zdarzeń.
- Readiness modal i lease zapobiegają dublowaniu sesji oraz blokują manualne I/O podczas runu.

---

### Task 1: Wprowadzić typowany kontrakt danych runu

**Files:**

- Create: `app/domain/run_contract.py`
- Modify: `app/engine/compiler.py`
- Modify: `app/engine/__init__.py`
- Test: `tests/test_run_data_contract.py`
- Modify: `tests/test_recipe_compiler.py`

**Interfaces:**

- Produces: `VerificationKind`, `AppliedParameter`, `AppliedDeviceState`, `DeviceDataRequirement`, `RunDataContract`.
- `ExecutionPlan.data_contract: RunDataContract` jest częścią canonical hash planu.
- `RunDataContract.validate_checkpoint(...) -> None` odrzuca brakujące urządzenia, parametry, pomiary, widmo i wartości niefinite.

- [ ] **Step 1: Napisać testy RED dla kontraktu**

```python
def test_contract_rejects_missing_required_device_state() -> None:
    contract = RunDataContract(
        devices=(DeviceDataRequirement("anritsu", frozenset({"spectrum.points"}), frozenset()),),
        sweep_axes=("rigol.1.frequency",),
        expected_points=1,
        expected_spectra=1,
    )
    with pytest.raises(ExecutionError, match="anritsu"):
        contract.validate_checkpoint(point, trace, device_states={})

def test_compiler_hash_includes_data_contract() -> None:
    plan = RecipeCompiler(settings).compile(recipe)
    assert plan.data_contract.expected_points == plan.total_points
    assert plan.data_contract.expected_spectra == plan.total_spectra
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_run_data_contract.py tests/test_recipe_compiler.py -k data_contract`

Expected: FAIL, ponieważ typy i `ExecutionPlan.data_contract` jeszcze nie istnieją.

- [ ] **Step 3: Zaimplementować typy i deterministyczną serializację**

```python
class VerificationKind(StrEnum):
    READBACK = "readback"
    SIMULATED_ACK = "simulated_ack"
    COMMAND_ONLY = "command_only"

@dataclass(frozen=True, slots=True)
class AppliedParameter:
    parameter_id: str
    requested_value: bool | int | float | str | None
    actual_value: bool | int | float | str | None
    unit: str | None
    verification: VerificationKind

@dataclass(frozen=True, slots=True)
class AppliedDeviceState:
    device_key: str
    revision: int
    applied_at_utc: str
    parameters: tuple[AppliedParameter, ...]
```

Do kontraktu kompilatora wpisać wymagania wynikające z rodzaju każdej akcji; nie używać globalnej listy urządzeń jako substytutu planu.

- [ ] **Step 4: Uruchomić GREEN i sprawdzić stabilność hasha**

Run: `python -m pytest -q tests/test_run_data_contract.py tests/test_recipe_compiler.py`

Expected: PASS; dwie kompilacje identycznego YAML mają identyczny hash i kontrakt.

- [ ] **Step 5: Commit**

```bash
git add app/domain/run_contract.py app/engine/compiler.py app/engine/__init__.py tests/test_run_data_contract.py tests/test_recipe_compiler.py
git commit -m "feat: define sweep run data contract"
```

### Task 2: Uporządkować legacy DUT metadata bez przywracania restrykcyjnych blokad

**Files:**

- Modify: `app/recipes/models.py`
- Modify: `app/domain/readiness.py`
- Modify: `app/engine/runner.py`
- Modify: `app/storage/hdf5_writer.py`
- Modify: `app/storage/thatec_writer.py`
- Modify: `tests/test_station_readiness.py`
- Modify: `tests/test_hdf5_writer.py`
- Modify: `tests/test_simulated_run.py`

**Interfaces:**

- `legacy_dut_limits_policy() -> dict[str, object]` zwraca wersjonowane, addytywne provenance z `enforced: false`.
- Istniejący `/run/dut_limits_json` zachowuje surowe wartości dla kompatybilności; `/run/dut_limits_policy_json` opisuje ich semantykę.
- `safety_context` checkpointu zawiera wyłącznie zastosowane i odczytane parametry, nigdy legacy deklaracje; autorytatywny profil pozostaje zachowany w `settings_yaml`.

- [x] **Step 1: Napisać RED dla fałszywego PASS, niejawnej polityki i zanieczyszczonego safety context**

```python
def test_energized_plan_reports_legacy_dut_limits_as_not_enforced() -> None:
    readiness = evaluate_station_readiness(settings, plan=energized_plan_with_legacy_limits, ...)
    item = next(item for item in readiness.items if item.key == "dut")
    assert item.level is ReadinessLevel.WARNING
    assert "not enforced" in item.detail.lower()

def test_energized_point_keeps_legacy_dut_limits_out_of_active_safety_context() -> None:
    assert "current_max_a" not in stored_safety_context["keithley.B"]
```

- [x] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_station_readiness.py tests/test_hdf5_writer.py tests/test_simulated_run.py -k "dut_limits or writer_flushes"`

Expected: trzy FAIL na starym zachowaniu: fałszywy PASS, brak policy dataset i legacy pola w safety context.

- [x] **Step 3: Rozdzielić metadata-only provenance od aktywnego bezpieczeństwa**

Zachować akceptację starych YAML i ich surowy zapis. Dodać wspólną wersjonowaną politykę `legacy_metadata_only`, poprawić readiness i usunąć domieszanie do `safety_context`. Nie zmieniać aktywnych limitów sprzętowych, profilu stacji, output permissions, compliance ani shutdown.

- [x] **Step 4: Sprawdzić regresję compiler–runner–storage–readiness**

Run: `python -m pytest -q tests/test_recipe_compiler.py tests/test_station_readiness.py tests/test_adapters_and_runner.py tests/test_simulated_run.py tests/test_hdf5_writer.py tests/test_thatec_reader.py tests/test_thatec_schema_mapper.py tests/test_thatec_validator.py tests/test_run_recovery.py`

Expected: `198 passed, 4 skipped, 14 subtests passed`; skips wyłącznie z powodu nieobecnych laboratoryjnych/licencjonowanych golden HDF5.

- [ ] **Step 5: Commit**

```bash
git add app/recipes/models.py app/domain/readiness.py app/engine/runner.py app/storage/hdf5_writer.py app/storage/thatec_writer.py tests/test_station_readiness.py tests/test_hdf5_writer.py tests/test_simulated_run.py
git commit -m "fix: mark legacy DUT limits as non-enforcing provenance"
```

### Task 3: Zastąpić nieformalny `device_states` typowanym ledgerem

**Files:**

- Create: `app/engine/applied_state.py`
- Modify: `app/engine/runner.py`
- Modify: `app/devices/rigol_dg1000z/adapter.py`
- Modify: `app/devices/keithley_2600/adapter.py`
- Modify: `app/devices/anritsu_ms2830a/adapter.py`
- Modify: `app/devices/moke_box/adapter.py`
- Modify: `app/devices/lakeshore_475/adapter.py`
- Test: `tests/test_applied_device_state.py`
- Modify: `tests/test_adapters_and_runner.py`

**Interfaces:**

- `AppliedStateLedger.apply(device_key, parameters) -> AppliedDeviceState` zwiększa rewizję i zachowuje pełny snapshot.
- `AppliedStateLedger.snapshot(required_devices) -> dict[str, AppliedDeviceState]` odrzuca brak urządzenia.
- Runner emituje `device_state_applied` z rewizją i skrótem, a pełny snapshot przekazuje bezpośrednio do writera.

- [ ] **Step 1: Napisać RED dla kompletności i rodzaju potwierdzenia**

```python
def test_frequency_update_preserves_complete_rigol_snapshot() -> None:
    runner.execute(configure_rigol)
    before = ledger.snapshot(("rigol",))["rigol"]
    runner.execute(update_frequency)
    after = ledger.snapshot(("rigol",))["rigol"]
    assert after.revision == before.revision + 1
    assert parameter(after, "carrier.high_level").actual_value == 0.001
    assert parameter(after, "carrier.frequency").verification is VerificationKind.READBACK
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_applied_device_state.py`

Expected: FAIL, ponieważ obecny snapshot nie ma rewizji/jednostek/verification.

- [ ] **Step 3: Zaimplementować ledger na granicy runnera**

Adapter ma zwracać typed readback/snapshot, a runner ma mapować tylko oficjalne pola. Symulator zwraca `SIMULATED_ACK` wyłącznie wtedy, gdy fizyczny kontrakt nie oferuje readbacku; nie wolno oznaczać go jako `READBACK`.

- [ ] **Step 4: Uruchomić regresje komend i stanu**

Run: `python -m pytest -q tests/test_applied_device_state.py tests/test_adapters_and_runner.py tests/test_simulators.py`

Expected: PASS, w tym readback mismatch i utrata OUTPUT continuity.

- [ ] **Step 5: Commit**

```bash
git add app/engine/applied_state.py app/engine/runner.py app/devices tests/test_applied_device_state.py tests/test_adapters_and_runner.py
git commit -m "feat: record typed applied device state"
```

### Task 4: Wymusić kompletność HDF5 przed statusem `completed`

**Files:**

- Modify: `app/storage/hdf5_writer.py`
- Modify: `app/storage/thatec_writer.py`
- Modify: `app/storage/thatec_validator.py`
- Modify: `app/storage/hdf5_reader.py`
- Modify: `app/resources/thatec_manifest_v1.json` only if the public contract changes
- Test: `tests/test_hdf5_writer.py`
- Modify: `tests/test_thatec_validator.py`
- Create: `tests/test_run_completion_contract.py`

**Interfaces:**

- `Hdf5RunWriter(..., data_contract: RunDataContract, quantity_manifest: Mapping[str, ...])`.
- `close("completed")` najpierw wykonuje `validate_completion()`, a dopiero potem nadaje terminalny status.
- `/run/data_contract_json` i `/run/quantity_manifest_json` są deterministyczne i hashowane.

- [ ] **Step 1: Zachować sondę 0/2 jako test RED**

```python
def test_completed_close_rejects_missing_points() -> None:
    writer = writer_for_contract(expected_points=2, expected_spectra=2)
    with pytest.raises(ExecutionError, match="expected 2.*stored 0"):
        writer.close("completed")
    assert Hdf5RunReader.detail(writer.path).summary.status == "faulted"
```

Dodać przypadki: brak widma, brak identity, brak capability, brak required measurement, niepełny device state, brak seedu w symulacji i prawidłowy pusty plan tylko wtedy, gdy kontrakt oczekuje zera.

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_run_completion_contract.py`

Expected: FAIL; obecny writer akceptuje `0/2`.

- [ ] **Step 3: Zaimplementować dwufazowe zamknięcie**

Walidować prywatne checkpointy, publiczne wiersze, kontrakt oraz PyThat przed terminalnym sukcesem. Przy błędzie ustawić `faulted`, `measurement running=0`, zapisać `storage_validation_error`, flush i zgłosić `ExecutionError`.

- [ ] **Step 4: Uruchomić storage/PyThat round-trip**

Run: `python -m pytest -q tests/test_run_completion_contract.py tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_thatec_reader.py`

Expected: PASS; walidacja z `require_pythat=True` dla pliku nominalnego.

- [ ] **Step 5: Commit**

```bash
git add app/storage app/resources/thatec_manifest_v1.json tests/test_run_completion_contract.py tests/test_hdf5_writer.py tests/test_thatec_validator.py tests/test_thatec_reader.py
git commit -m "fix: reject incomplete completed sweep files"
```

### Task 5: Naprawić bezpieczne granice i recovery wszystkich akcji

**Files:**

- Modify: `app/engine/runner.py`
- Modify: `app/engine/recovery.py`
- Modify: `app/ui/run_worker.py`
- Modify: `app/ui/shell/main_window.py`
- Modify: `app/storage/hdf5_writer.py`
- Modify: `tests/test_run_recovery.py`
- Create: `tests/test_run_recovery_all_devices.py`

**Interfaces:**

- `SafeResumeBoundary` zawiera point/action index, wszystkie output states, ledger revision/hash, plan/settings/recipe hash, simulation identity i device identity/capability hashes.
- `RunRecoveryManager.inspect(..., connected_evidence) -> RecoveryCheckpoint` odrzuca każdą niezgodność.
- Prelude odtwarza pełny stan zastosowany na granicy przy OUTPUT OFF albo cofa resume do wcześniejszej bezpiecznej akcji konfigurującej.

- [ ] **Step 1: Napisać RED dla znanych luk**

```python
def test_no_safe_boundary_while_anritsu_sg_is_on() -> None:
    result = run_until_checkpoint(plan_with_sg_on=True)
    assert "safe_resume_boundary" not in result.event_names

@pytest.mark.parametrize("kind", ["measure_moke_hall", "measure_lakeshore_field"])
def test_recovery_counts_device_measurement_checkpoint(kind: str) -> None:
    checkpoint = recovery_after_one_checkpoint(kind)
    assert checkpoint.stored_points == 1

def test_resume_rejects_device_identity_change() -> None:
    with pytest.raises(ExecutionError, match="identity"):
        inspect_with_identity("different-serial")
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_run_recovery.py tests/test_run_recovery_all_devices.py`

Expected: FAIL dla SG, MOKE/Lake i identity mismatch.

- [ ] **Step 3: Zaimplementować pełny snapshot granicy**

Uwzględnić Anritsu SG w warunku OFF, liczyć wszystkie semantyki checkpointu z jednej funkcji używanej przez compiler/runner/recovery i zapisać hash ledgeru. Nie inferować bezpieczeństwa z samej liczby wierszy.

- [ ] **Step 4: Sprawdzić nominalne i negatywne resume**

Run: `python -m pytest -q tests/test_run_recovery.py tests/test_run_recovery_all_devices.py tests/test_hdf5_writer.py`

Expected: PASS dla przerwania po bezpiecznym punkcie; FAIL dla aktywnego output, zmiany settings/identity/capabilities/seedu i uszkodzonego checkpointu.

- [ ] **Step 5: Commit**

```bash
git add app/engine/runner.py app/engine/recovery.py app/ui/run_worker.py app/ui/shell/main_window.py app/storage/hdf5_writer.py tests/test_run_recovery.py tests/test_run_recovery_all_devices.py
git commit -m "fix: resume sweeps only from verified safe state"
```

### Task 6: Przenieść jeden `SimulationContext` przez GUI, leasing i resume

**Files:**

- Modify: `app/devices/simulation.py`
- Modify: `app/bootstrap.py`
- Modify: `app/ui/workers.py`
- Modify: `app/ui/run_worker.py`
- Modify: `app/ui/shell/main_window.py`
- Modify: `app/ui/recipes/page.py`
- Modify: `tests/test_simulation_context.py`
- Modify: `tests/test_device_run_lease.py`
- Modify: `tests/test_run_controller.py`
- Modify: `tests/test_main_window.py`

**Interfaces:**

- `SimulationRunConfig(seed: int, model_version: str, time_scale: float, scenario: str)`.
- `RunController.start(..., simulation_config: SimulationRunConfig | None)` zastępuje ukryty losowy seed.
- `DeviceController.rebind_simulation(context) -> None` jest dozwolone wyłącznie przy rozłączonym adapterze; readiness tworzy/łączy adapter należący do danego runu.
- Resume odczytuje dokładny config z HDF5 i nie generuje nowego seedu.

- [ ] **Step 1: Napisać RED dla lease i resume**

```python
def test_leased_anritsu_trace_is_controlled_by_run_seed() -> None:
    first = run_through_main_window(seed=17)
    second = run_through_main_window(seed=17)
    third = run_through_main_window(seed=18)
    assert first.power_dbm == second.power_dbm
    assert first.power_dbm != third.power_dbm

def test_simulation_resume_reuses_original_seed() -> None:
    original, resumed = interrupt_and_resume(seed=23)
    assert resumed.simulation_metadata["seed"] == 23
    assert resumed.expected_next_trace == uninterrupted(seed=23).next_trace
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_device_run_lease.py tests/test_run_controller.py -k seed`

Expected: FAIL, ponieważ `RunController.start` nie przyjmuje configu, a lease omija context.

- [ ] **Step 3: Zaimplementować run-scoped composition**

Seed generować raz przed readiness, pokazać go w UI i przekazać do wszystkich fabryk. Nie reużywać wcześniejszego seedless symulatora jako urządzenia runu. W hardware mode zachować obecny lease fizycznej sesji.

- [ ] **Step 4: Uruchomić GREEN dla GUI i workerów**

Run: `python -m pytest -q tests/test_simulation_context.py tests/test_device_run_lease.py tests/test_run_controller.py tests/test_main_window.py -k "simulation or seed or lease or resume"`

Expected: PASS; HDF5 seed odpowiada rzeczywistym danym.

- [ ] **Step 5: Commit**

```bash
git add app/devices/simulation.py app/bootstrap.py app/ui/workers.py app/ui/run_worker.py app/ui/shell/main_window.py app/ui/recipes/page.py tests/test_simulation_context.py tests/test_device_run_lease.py tests/test_run_controller.py tests/test_main_window.py
git commit -m "fix: bind simulated sessions to run seed"
```

### Task 7: Dokończyć modele dummy-data i deterministyczne fault scenarios

**Files:**

- Create: `app/devices/simulation_scenarios.py`
- Modify: `app/devices/simulators.py`
- Modify: `app/devices/moke_box/simulator.py`
- Modify: `app/devices/lakeshore_475/simulator.py`
- Modify: `app/devices/lakeshore_475/module.py`
- Modify: `app/devices/moke_box/module.py`
- Modify: `app/devices/registry.py`
- Modify: `app/devices/simulators.py`
- Test: `tests/test_simulation_scenarios.py`
- Modify: `tests/test_simulators.py`

**Interfaces:**

- `SimulationScenario` opisuje deterministyczne zdarzenia po `device`, `operation`, `occurrence` i `fault`.
- Wspierane fault kinds: `timeout`, `transport_loss`, `device_error`, `malformed_response`, `readback_mismatch`, `compliance`, `shutdown_unconfirmed`, `storage_failure`.
- Każdy symulator używa niezależnego streamu z jednego contextu; `time_scale` skaluje tylko jawnie modelowane opóźnienia.

- [ ] **Step 1: Napisać RED dla wszystkich pięciu urządzeń**

```python
@pytest.mark.parametrize("device", ["rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"])
def test_simulated_device_never_opens_physical_transport(device: str) -> None:
    adapter = simulated_registry(seed=7).create_adapter(device)
    identity = adapter.connect()
    assert identity.resource.startswith("SIM::")

def test_fault_occurs_at_exact_operation_and_occurrence() -> None:
    scenario = SimulationScenario.parse("anritsu.acquire_spectrum#3=timeout")
    assert acquire(1, scenario).ok and acquire(2, scenario).ok
    with pytest.raises(DeviceError, match="simulated timeout"):
        acquire(3, scenario)
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_simulation_scenarios.py`

Expected: FAIL; obecne fault injection jest tylko parametrem lokalnej fabryki.

- [ ] **Step 3: Zaimplementować realistyczne, ograniczone modele**

Anritsu: szum + piki zależne od konfiguracji. Keithley: seedowany szum I/V/P i compliance. MOKE: napięcie, pole, stddev i ADC. Lake Shore: seedowany DC/RMS/peak/frequency zamiast stałego zera. Rigol: pełny stan i potwierdzenia, bez fikcyjnego pomiaru. `simulated_station_settings` ma tworzyć kompletny in-memory profil SIM także dla Lake Shore, bez zapisu do `.config/settings.yml`.

- [ ] **Step 4: Sprawdzić odtwarzalność i fault matrix**

Run: `python -m pytest -q tests/test_simulation_scenarios.py tests/test_simulators.py tests/test_adapters_and_runner.py -k "simulat or fault or watchdog or compliance or shutdown"`

Expected: PASS dla tego samego seedu; różne finite dane dla różnych seedów; identyczna sekwencja shutdown jak w hardware path.

- [ ] **Step 5: Commit**

```bash
git add app/devices/simulation_scenarios.py app/devices/simulators.py app/devices/moke_box app/devices/lakeshore_475 app/devices/registry.py tests/test_simulation_scenarios.py tests/test_simulators.py
git commit -m "feat: add complete deterministic station simulation"
```

### Task 8: Pokazać provenance i błędy kompatybilności w Fluent UI

**Files:**

- Modify: `app/ui/execution/page.py`
- Modify: `app/ui/results/page.py`
- Modify: `app/ui/results/metadata_panel.py`
- Modify: `app/ui/results/spectrum_tab.py`
- Modify: `tests/test_fluent_recipe_execution_pages.py`
- Modify: `tests/test_results_page.py`
- Modify: `tests/test_results_browser.py`

**Interfaces:**

- Run Monitor pokazuje seed/scenario/model version oraz `requested -> actual`, jednostkę, verification, rewizję i czas.
- `_ResultPayload.pythat_error: str | None` zachowuje dokładny błąd.
- Results nie przedstawia native readera jako pełnego źródła danych, jeżeli PyThat nie otworzył publicznego kontraktu.

- [ ] **Step 1: Napisać renderowane testy RED**

```python
def test_results_exposes_pythat_failure_without_silent_fallback(qtbot) -> None:
    page = ResultsPage()
    page.show(); QApplication.processEvents()
    page.apply_payload(payload_with_pythat_error="dimension mismatch")
    assert "dimension mismatch" in page.metadata_panel.pythat_data.toPlainText()
    assert page.geometry().width() > 0

def test_run_monitor_shows_sim_seed_and_verification(qtbot) -> None:
    page = RunMonitorPage(); page.show(); QApplication.processEvents()
    page.apply_execution_event("device_state_applied", event)
    assert "Seed 17" in page.simulation_badge.text()
    assert "readback" in page.device_state_card.text()
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_fluent_recipe_execution_pages.py tests/test_results_page.py -k "seed or verification or pythat_failure"`

Expected: FAIL, ponieważ wyjątek PyThat jest obecnie redukowany do `None`.

- [ ] **Step 3: Zaimplementować czytelną prezentację**

Użyć istniejących tokenów i Fluent controls; nie dodawać legacy tabs/shell. Na normalnym i wąskim oknie zapewnić niezerową geometrię, keyboard focus, dark/light contrast i stan błędu.

- [ ] **Step 4: Uruchomić testy renderowania i Results**

Run: `python -m pytest -q tests/test_fluent_recipe_execution_pages.py tests/test_results_page.py tests/test_results_browser.py`

Expected: PASS po `show()` i event processing; błąd kompatybilności jest widoczny.

- [ ] **Step 5: Commit**

```bash
git add app/ui/execution/page.py app/ui/results tests/test_fluent_recipe_execution_pages.py tests/test_results_page.py tests/test_results_browser.py
git commit -m "feat: expose simulation and device provenance"
```

### Task 9: Dodać śledzone receptury i kwalifikację symulacyjną end-to-end

**Files:**

- Create: `recipes/simulation_all_devices_2x3.yml`
- Create: `recipes/simulation_all_devices_10x100.yml`
- Create: `app/qualification/simulation.py`
- Modify: `app/qualification/__main__.py`
- Modify: `app/qualification/runner.py`
- Create: `tests/test_simulation_acceptance.py`
- Modify: `tests/test_simulated_run.py`

**Interfaces:**

- `python -m app.qualification simulation --recipe ... --seed 17 --scenario nominal --output ...`.
- Szybka receptura: 6 checkpointów, 6 widm, Keithley + MOKE + Lake w każdym punkcie, pełny stan pięciu urządzeń.
- Wolna receptura: 1000 checkpointów/widm i przetwarzanie reference; oznaczenie `@pytest.mark.qualification`.

- [ ] **Step 1: Napisać test akceptacyjny RED przez `RunController`**

```python
def test_all_device_2x3_simulation_is_complete_and_reproducible(tmp_path) -> None:
    first = run_qualification(seed=17, output=tmp_path / "first")
    second = run_qualification(seed=17, output=tmp_path / "second")
    assert first.status == second.status == "completed"
    assert first.point_count == first.spectrum_count == 6
    assert first.scientific_digest == second.scientific_digest
    assert first.devices == {"rigol", "keithley", "anritsu", "moke_box", "lakeshore_gaussmeter"}
```

- [ ] **Step 2: Uruchomić RED**

Run: `python -m pytest -q tests/test_simulation_acceptance.py`

Expected: FAIL, ponieważ receptury i qualification entry point nie istnieją.

- [ ] **Step 3: Zaimplementować nominalną i faultową kwalifikację**

Każdy run ma przejść przez prawdziwy compiler, readiness contract, RunController/worker, adapters, runner, writer, walidator i Results reader. `measure_moke_hall` oraz `measure_lakeshore_field` mają `checkpoint: false`, jeśli ich dane należą do tego samego punktu co następujące widmo.

- [ ] **Step 4: Uruchomić smoke i ciężką kwalifikację osobno**

Run: `python -m pytest -q tests/test_simulation_acceptance.py -m "not qualification"`

Expected: PASS w szybkim lane.

Run: `python -m pytest -q tests/test_simulation_acceptance.py -m qualification`

Expected: PASS; dokładnie 1000 kompletnych punktów i widm, poprawny PyThat round-trip.

- [ ] **Step 5: Commit**

```bash
git add recipes/simulation_all_devices_2x3.yml recipes/simulation_all_devices_10x100.yml app/qualification tests/test_simulation_acceptance.py tests/test_simulated_run.py
git commit -m "test: qualify all-device simulated sweeps"
```

### Task 10: Ustawić produkcyjne bramki CI, golden data i kwalifikację HIL

**Files:**

- Modify: `pyproject.toml`
- Modify: `.github/workflows/ci.yml` if present; otherwise create the repository's established CI workflow file
- Modify: `app/resources/thatec_manifest_v1.json` only after independent golden verification
- Create: `docs/qualification/sweep-release-gate.md`
- Modify: `tests/test_thatec_validator.py`
- Modify: `tests/test_results_page.py`

**Interfaces:**

- Markery: `unit`, `integration`, `simulation`, `qualification`, `hil`.
- PR gate: Ruff + szybkie unit/integration/simulation smoke.
- Release gate: 1000/2000 punktów, fault matrix, golden PyThat, rendering desktop/narrow.
- HIL gate: kwalifikowane dokumenty urządzeń, dummy load, interlock, rzeczywiste command order/readback/shutdown.

- [ ] **Step 1: Dodać test, że wymagany golden nie jest cicho pomijany w release lane**

```python
def test_release_lane_requires_golden_fixture() -> None:
    if os.environ.get("LAB_CONTROL_RELEASE_QUALIFICATION") == "YES":
        assert GOLDEN_PATH.is_file(), "release qualification requires golden HDF5"
```

- [ ] **Step 2: Skonfigurować markery i limity czasu**

Szybki lane ma raportować czas każdego testu powyżej 5 s; qualification lane ma osobny limit i artefakty HDF5/JSON. Nie ukrywać testu bezpieczeństwa pod automatycznym `skip` w release lane.

- [ ] **Step 3: Opisać bramkę HIL**

Dokument ma wymagać: wersji firmware/opcji, numeru seryjnego, dummy load ID, interlock, wartości tuż poniżej/równo/powyżej limitu, readback mismatch, timeout, transport loss, compliance, E-STOP i nieudane potwierdzenie shutdown. Simulator nie może być dowodem zachowania fizycznego protokołu.

- [ ] **Step 4: Uruchomić pełną weryfikację release**

Run: `python -m ruff check app tests`

Expected: `All checks passed!`

Run: `python -m pytest -q -m "not hil"`

Expected: PASS bez pominiętych safety/compatibility testów w profilu release.

Run: `python -m app.qualification simulation --recipe recipes/simulation_all_devices_10x100.yml --seed 17 --scenario nominal --output qualification-output`

Expected: podpisany raport `simulation_passed`, poprawny HDF5 i PyThat.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .github/workflows docs/qualification/sweep-release-gate.md app/resources/thatec_manifest_v1.json tests/test_thatec_validator.py tests/test_results_page.py
git commit -m "ci: enforce sweep production qualification gates"
```

## Kryteria końcowego GO

Wydanie może otrzymać status produkcyjny dopiero, gdy wszystkie poniższe warunki są spełnione jednocześnie:

1. Sonda `expected_points=2, stored=0` kończy się `faulted`, nigdy `completed`.
2. Każdy checkpoint spełnia `RunDataContract` i ma pełny typed state wymaganych urządzeń.
3. Legacy `dut_limits` nie blokują wykonania, są zapisane z `enforced: false`, nie trafiają do aktywnego `safety_context`, a readiness wskazuje autorytatywne limity sprzętu i profilu stacji.
4. Nie powstaje `safe_resume_boundary`, gdy jakikolwiek Rigol, Keithley lub Anritsu SG ma aktywne wyjście albo stan nieznany.
5. Recovery działa dla spectrum/checkpoint/MOKE/Lake, weryfikuje identity/capabilities i odtwarza dokładny bezpieczny stan.
6. Ten sam seed i scenariusz dają identyczny naukowy payload przez GUI lease i resume; inny seed daje różne, finite dane.
7. Szybka receptura pięciu urządzeń daje dokładnie 6 kompletnych punktów i 6 widm; wolna daje dokładnie 1000.
8. Nominalny i każdy fault scenario kończy się właściwym terminalnym statusem oraz potwierdzonym shutdown albo jawnym `FAULT/UNKNOWN`.
9. Results pokazuje błąd PyThat wprost i nie udaje pełnej kompatybilności przez native fallback.
10. Ruff, szybki CI, release simulation, golden PyThat i osobna HIL qualification przechodzą bez nierozliczonych pominięć.

## Self-review

- Pokrycie specyfikacji: zadania 1–9 pokrywają kontrakt danych, applied state, wszystkie urządzenia, seed, fault injection, HDF5/PyThat, Run Monitor, Results i receptury akceptacyjne; zadanie 10 rozdziela symulację od fizycznej kwalifikacji HIL.
- Placeholder scan: dokument nie zawiera niewypełnionych kroków ani nieokreślonych interfejsów implementacyjnych.
- Spójność typów: `ExecutionPlan.data_contract` jest źródłem wymagań dla runnera, writera, recovery i kwalifikacji; `SimulationRunConfig` jest jedynym źródłem seedu/modelu/scenariusza; `AppliedDeviceState` jest jedynym snapshotem konfiguracji punktu.
- Zakres: plan jest podzielony na niezależne bramki review; żadna z nich nie wymaga hybrydowego UI ani równoległego runnera symulacyjnego.
