# Audyt wymagań `PLAN_WDROZENIA.md`

Data: 2026-07-17  
Zakres: aktualny worktree, kod aplikacji, testy automatyczne i dostępne artefakty lokalne.  
Zasada oceny: samo istnienie kodu lub zielony test pośredni nie jest dowodem ukończenia.

## 1. Wynik audytu

Warstwa programowa etapów 1–5 ma bezpośrednie dowody wykonawcze. W trakcie niniejszego audytu
domknięto trzy wcześniej zbyt słabo udowodnione wymagania:

1. pełny run symulacyjny 100 × 20 zapisuje i ponownie otwiera dokładnie 2000 widm;
2. każde zdarzenie silnika ma unikalny correlation ID runu, identyfikator tokenu anulowania,
   deadline akcji i snapshot stanu;
3. E-STOP zawsze uruchamia niezależne, krótkotrwałe sesje VISA, również gdy nie działa receptura
   i zwykły worker ręcznego sterowania może być zablokowany.

Wersja v1 nie ma jeszcze dowodu kwalifikacji produkcyjnej całego stanowiska. Brakujące dowody są
zewnętrzne: fizyczny HIL, zatwierdzone limity DUT/toru RF, fizyczny interlock oraz round-trip w
laboratoryjnym systemie inwentaryzacji. Symulator ani test jednostkowy nie może ich zastąpić.

## 2. Kryteria ukończenia z rozdziału 18

| Kryterium | Ocena | Bezpośredni dowód / brak |
|---|---|---|
| Każde urządzenie można niezależnie połączyć, zidentyfikować, skonfigurować i bezpiecznie wyłączyć | SOFTWARE PROVEN / HIL REQUIRED | adaptery i `test_all_simulated_instruments_support_safe_core_operations`; fizyczny protokół sprawdza `app/qualification` |
| Żadna ścieżka GUI nie włącza wyjścia bez walidacji i kontrolowanego uzbrojenia | SOFTWARE PROVEN | backendowe ARM w adapterach, RBAC i `test_rigol_requires_one_shot_arm_before_enabling_output`, `test_keithley_output_switch_runs_configure_unlock_and_enable_sequence`, testy SG |
| Keithley ustawia compliance przed OUTPUT ON | SOFTWARE PROVEN / HIL REQUIRED | transakcja adaptera, testy kolejności i tripów w `test_adapters_and_runner.py`; rzeczywisty readback wymaga HIL |
| Rigol blokuje przekroczenie szacowanego prądu | PROVEN | `test_rigol_current_estimate_is_limited`, `test_recipe_dut_rigol_current_limit_is_applied_to_every_expanded_value`, walidacja także bezpośrednio w adapterze |
| Anritsu pobiera zsynchronizowane, opisane widmo | SOFTWARE PROVEN / HIL REQUIRED | `test_anritsu_opc_query_uses_and_restores_hard_visa_deadline`, single sweep + `*OPC?`, kompletna oś i trace; firmware/transport wymaga HIL |
| Receptura 100 × 20 daje dokładnie 2000 rekordów albo czytelny plik częściowy | PROVEN IN SIMULATION / HIL REQUIRED | `test_full_100_by_20_run_writes_exactly_2000_complete_spectra` wykonuje runner, HDF5 i PyThat dla 2000 × 101 wartości; fault/rollback pokrywają testy writera |
| Timeout, compliance, rozłączenie i E-STOP kończą się zdefiniowanym stanem | SOFTWARE PROVEN / PHYSICAL FAULT INJECTION REQUIRED | `test_watchdog_deadline_faults_run_and_emits_out_of_band_event`, trip/compliance, fault simulators, niezależny E-STOP; odłączenie fizycznego kabla pozostaje HIL |
| Wynik zawiera recepturę, settings, IDN, capabilities, wersję i log | PROVEN | `test_writer_flushes_a_point_and_trace`, kontrakt `Hdf5RunWriter`, validator i Data Browser |
| Każdy wynik ma strukturę thaTEC:OS i otwiera się przez przypięty PyThat | PROVEN FOR LOCAL CONTRACT | manifest golden file, PyThat 0.2.14 oraz `test_completed_aborted_and_faulted_runs_pass_manifest_and_pythat` |
| Plik przechodzi round-trip w laboratoryjnym systemie inwentaryzacji | EXTERNAL EVIDENCE MISSING | brak dostępu/API/raportu z systemu laboratorium; lokalny PyThat nie jest równoważnym dowodem |
| Testy symulacyjne i kwalifikacja na sztucznym obciążeniu są udokumentowane | PARTIAL | testy i `docs/HIL_QUALIFICATION.md` istnieją; podpisany wynik fizycznej kwalifikacji nie istnieje w repozytorium |

## 3. Wymagania przekrojowe

| Obszar | Ocena | Dowód |
|---|---|---|
| Qt 6/PySide6, domena niezależna od GUI | PROVEN | podział `app/domain`, `app/devices`, `app/engine`, `app/storage`, `app/ui` |
| Jedna sesja VISA na worker i brak blokowania GUI | PROVEN | `DeviceController`/`InstrumentWorker`, `RunWorker`, test QThread runu |
| Deadline, anulowanie, correlation ID i snapshot stanu | PROVEN | `ExecutionPolicy`, watchdog, tokeny `Event`, unikalny UUID oraz snapshot w każdym evencie; `test_every_run_event_carries_unique_correlation_cancellation_and_state_snapshot` |
| E-STOP nie zależy od kolejki zablokowanego workera | PROVEN | `EmergencyStopWorker` uruchamiany dla każdego E-STOP; `test_estop_uses_out_of_band_sessions_without_an_active_recipe` |
| Limity: hardware ∩ profil ∩ DUT ∩ receptura | PROVEN PROGRAMOWO | modele settings, compiler preflight, ponowna walidacja adapterów i testy przecięć limitów |
| Brak raw SCPI/Pythona w recepturze | PROVEN | dyskryminowana lista typów Pydantic, ścisłe modele i testy nieznanych/niebezpiecznych węzłów |
| Niezmienny plan, hash i jawny safe shutdown | PROVEN | `ExecutionPlan`, manifest shutdownu i `test_runner_executes_hashed_shutdown_manifest_in_declared_order` |
| Checkpoint atomowy i odzyskiwalny po awarii | PROVEN | rollback HDF5/CSV, durable events, recovery boundary i testy resume |
| Pełny audyt odporny na manipulację | PROVEN LOKALNIE | append-only JSONL, sekwencja, redakcja, fsync alarmów, correlation ID i testy korupcji |
| Zaawansowany Anritsu Spectrum | SOFTWARE PROVEN / FIRMWARE HIL REQUIRED | RBW/VBW/detector/attenuation/preamp/sweep time: GUI, receptury, simulator, readback, fallback i bramka dokładnego firmware |
| Generator RF Anritsu | SOFTWARE PROVEN / ENERGIZED HIL REQUIRED | wykrycie opcji, RF OFF na connect/disconnect/E-STOP, osobny ARM, DUT/profile limits i readback |

## 4. Otwarte decyzje z rozdziału 19

Poniższych danych aplikacja celowo nie zgaduje. Muszą zostać zapisane w lokalnym profilu i
zatwierdzone przez odpowiedzialną osobę:

- rzeczywiste IDN/opcje/firmware Keithley i Anritsu;
- maksymalne I/V/P obu kanałów DUT;
- minimalna impedancja widziana przez kanały Rigola;
- maksymalny poziom wejściowy i tłumienie toru Anritsu;
- zakres/opcja generatora RF Anritsu;
- obecność i sposób odczytu fizycznego interlocku;
- docelowa bezpieczna wartość rampy Keithleya;
- dokładny kontrakt nazw i przyjęcia pliku przez system inwentaryzacji;
- polityka wielu trace/kanałów w publicznym drzewie PyThat.

Do czasu dostarczenia tych dowodów domyślny szablon pozostaje fail-closed. Nie należy wpisywać
fikcyjnych wartości ani odblokowywać protokołów tylko po to, aby uzyskać status `DONE`.

## 5. Polecenia weryfikacyjne

Wynik ostatniej pełnej weryfikacji (2026-07-17): **221 passed, 34 subtests passed**; Ruff,
`compileall` i `git diff --check` zakończone bez błędów. `git submodule status` potwierdza brak
modyfikacji w obu submodułach. Ostrzeżenia testów dotyczą zależności NumPy/xarray/netCDF4 i nie
zmieniają wyniku testów, ale powinny zostać ponownie ocenione przy aktualizacji środowiska.
Próba CLI na lokalnym profilu została wykonana w izolowanym trybie `--simulate`: 9/9 przypadków,
status `simulation_passed` i poprawny digest raportu. Próba fizycznego trybu `passive` została
poprawnie zatrzymana przed otwarciem VISA, ponieważ bieżące konto ma rolę operatora zamiast
wymaganej roli serwisowej. CLI zwraca kod `0` wyłącznie dla raportu zaliczonego, `1` dla kompletnego
raportu `failed`/`blocked`/`incomplete` oraz `2` dla błędu przed utworzeniem raportu.

```powershell
python -m pytest -q
python -m ruff check app tests gui.py test.py
python -m compileall -q app tests gui.py test.py
git diff --check
git submodule status
```

Kwalifikację sprzętową wykonać wyłącznie procedurą z `docs/HIL_QUALIFICATION.md`; raport
symulacyjny ma status `simulation_passed` i nie zatwierdza profilu fizycznego.
