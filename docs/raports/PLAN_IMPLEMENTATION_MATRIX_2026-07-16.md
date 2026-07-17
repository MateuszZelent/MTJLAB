# Macierz realizacji `PLAN_WDROZENIA.md`

Data audytu: 2026-07-16  
Zakres: aktualny worktree, testy automatyczne i artefakty lokalne.  
Legenda: **DONE** — istnieje bezpośredni kod i test; **PARTIAL** — część wymogu ma dowód,
ale pełny zakres nie jest osiągnięty; **HIL** — wymaga zatwierdzonego stanowiska; **OPEN** — brak.

## 1. Fundament, ustawienia i bezpieczeństwo

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| PySide6/Qt, logika poza GUI | DONE | `app/ui`, `app/devices`, `app/engine`, `app/storage` |
| Lokalny, generowany `settings.yml` | DONE | `SettingsRepository.ensure_exists`, testy settings |
| Atomowy zapis, backup, unieważnienie approval | DONE | `SettingsRepository.save_raw`, testy safety/settings |
| Jednostki SI, notacja naukowa, NaN/Inf | DONE | `app/domain/quantities.py`, testy granic |
| Trwały pełny audyt | DONE | append-only JSONL, redakcja, sekwencja, correlation ID, fail-closed ARM/run |
| Role operator/inżynier/serwis | DONE | tożsamość konta OS, deny-by-default RBAC, service-only role management, blokady backend/UI, audit i HDF5 operator context; `docs/ACCESS_CONTROL.md` |
| Fizyczny interlock/E-STOP | HIL | software E-STOP działa; brak informacji o wejściu sprzętowym |

## 2. Adaptery i bezpieczeństwo urządzeń

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| Rigol: transakcje, readback, current/power preflight | DONE | adapter i `rigol_current.py`, testy adaptera/runnera |
| Keithley: whitelist TSP, compliance przed ON, atomiczne I/V, trip | DONE | adapter i `safety/keithley.py`, testy fail-safe |
| Keithley: sprzętowy rejestr compliance | HIL | działa konserwatywny fallback pomiarowy; rejestr zależy od firmware |
| Anritsu: fresh single sweep + OPC deadline + trace | DONE | adapter, testy protokołu i symulator |
| Anritsu: RBW/VBW/detector/attenuation/preamp/sweep time z readback | DONE/HIL | adapter, symulator, GUI i receptury obsługują pełny readback oraz konserwatywny fallback; zapis jest fail-closed i wymaga `standard_scpi` oraz dokładnego firmware na liście kwalifikowanej po HIL |
| Anritsu SG z osobnym ARM | DONE/HIL | adapter wykrywa opcje 020/120/021/121, jawnie przełącza tryb, wymusza i weryfikuje RF OFF przed konfiguracją, wymaga jednorazowego ARM, readbacku, profilu, limitów stacji i DUT; GUI, receptury, symulator i E-STOP są przetestowane. Domyślny protokół pozostaje `unverified`, więc RF ON jest zablokowane do kwalifikacji HIL |
| Deterministyczne symulatory awarii | DONE | normal/timeout/malformed/error/disconnect/error queue/compliance/noise |

## 3. GUI

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| Responsywna powłoka, ribbon, splitter, dock logu, motywy | DONE | `MainWindow`, QSettings workspace, testy UI |
| Dashboard kart VISA i przypisań | DONE | discovery + test komunikacji + atomowe assignment |
| Dynamiczna checklista gotowości | DONE | czysty model `domain/readiness.py`: profil, audyt, VISA/IDN, stany wyjść, błędy, DUT, zapis katalogu oraz plan/czas/dysk; blokery są egzekwowane przed runem |
| Rigol CH1/CH2, dynamiczne pola, preview, advanced | DONE | manual UI + capability gating + testy |
| Keithley A/B, live readout, historia 30 s, output workflow | DONE | manual UI + wykresy + testy |
| Keithley konfigurator rampy i preview punktów | DONE | rampa aktywnego źródła od rzeczywistego poziomu do celu, ograniczenie kroku/dwell/deadline, preview, pomiar I/V w każdym punkcie i fail-safe OFF |
| Anritsu Live/Single/reference/averaging/processing | DONE | centralny stan strony, 1×/N×/Use current, metadane i ochrona nadpisania, nieblokujący błąd Live, HDF5 save/load przez PyThat oraz walidacja siatki, Reference Level i pełnej konfiguracji zaawansowanej |
| Anritsu center/span oraz zaawansowane parametry | DONE/HIL | Start/Stop i Center/Span zachowują te same granice fizyczne; okno Advanced odczytuje RBW/VBW/detector/attenuation/preamp/sweep time, a zapis pozostaje zablokowany do kwalifikacji dokładnego firmware |
| Recipe Builder: YAML, drzewo, inspector, versioning, autosave | DONE | `RecipePage`, `RecipeRepository`, testy |
| Recipe Builder: DnD i pełne typy węzłów | DONE | bezpieczny, zachowujący komentarze round-trip YAML; parser blokuje root/cykle/granicę `finally`; działają `Repeat`, `If/else`, `Checkpoint`, `Connect`, `Comment` i `Finally` |
| Recipe Builder: czas, dysk, ostrzeżenia | DONE | `PlanEstimator` pokazuje czas nominalny/retry, checkpointy, widma, wartości, model rozmiaru i ostrzeżenia |
| Run Monitor: pause/resume/stop/E-STOP/heartbeat | DONE | `RunMonitorPage`, `RunController`, watchdog |
| Run Monitor: ścieżka drzewa, setpoint/readback, widmo, ETA, write rate | DONE | bieżący node/index, skalarne SI, write latency/rate, kolejka ostrzeżeń, ETA i próbkowany preview ostatniego checkpointu |
| Settings: per-device edycja i approval | DONE | zakładki ogólne/per-device, walidacja, save/autosave, approval phrase |
| Settings: profesjonalne formularze, diff, role, diagnostyka/export | DONE | tabela Safety limits, diff/discard, SHA/backup, redacted export oraz osobna karta ról OS z service-only zarządzaniem |

## 4. Receptury i wykonanie

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| Ścisły YAML bez Python/raw SCPI | DONE | parser whitelist + `extra`/unknown rejection |
| Zagnieżdżone linear/log sweepy i limit ekspansji | DONE | compiler + testy 2×2/PyThat |
| DUT limits + preflight skrajnych wartości | DONE | `dut.py`, compiler, adapter revalidation |
| Immutable `ExecutionPlan`, hash, required devices | DONE | compiler i testy |
| Jawny safe shutdown w planie | DONE | hashowany manifest kolejności OFF/abort/flush jest częścią `ExecutionPlan`; runner raportuje każdy krok i ma konserwatywny fallback |
| Deadline, cancellation, retry, heartbeat, watchdog | DONE | `ExecutionPolicy`, VISA timeout cap, bezpieczne retry, out-of-band E-STOP |
| Retry nigdy nie powtarza OUTPUT ON | DONE | jawna klasyfikacja i test ambiguous failure |
| Wznowienie od bezpiecznej granicy | DONE | recovery manager, hash checks, tail truncation, UI confirmation |

## 5. Dane i odtwarzalność

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| HDF5 thaTEC/PyThat, wielopunktowe widma | DONE | writer/mapper, PyThat/xarray testy |
| Golden manifest + SHA-256 + walidator | DONE | `thatec_manifest_v1.json`, validator |
| completed/aborted/faulted i transakcyjne checkpointy | DONE | writer/recovery tests |
| Snapshot recipe/settings/IDN/capabilities/log/DUT | DONE | HDF5 metadane i testy |
| Bez nadpisania, CSV summary | DONE | writer + repository tests |
| Round-trip przez rzeczywisty system inwentaryzacji | HIL/EXTERNAL | PyThat przechodzi; brak dostępu do zewnętrznego systemu laboratorium |

## 6. Testy i kwalifikacja

| Wymóg | Stan | Dowód / brak |
|---|---|---|
| Testy bez sprzętu | DONE | 221 testów, 34 podtesty; pełny symulowany run 100 × 20 zapisuje 2000 kompletnych widm i przechodzi round-trip przez PyThat; tryb kwalifikacji symulacyjnej izoluje fizyczne VISA i serial binding, daje 9/9 przypadków oraz zweryfikowany raport `simulation_passed`; Ruff dla `app`, `tests`, entrypointów i compileall green; stare błędy lint submodułów są poza zakresem i nie są modyfikowane |
| Wykonywalny harness HIL i integralność dowodów | DONE | `app/qualification`: service-only, passive/OFF, opcjonalny read-only trace, wielokrotna bramka energized, wykonanie przez kompilator/runner, atomowy JSON+SHA-256, audit i HDF5; `docs/HIL_QUALIFICATION.md` |
| HIL read-only/OFF/minimum/1 point/2×2/faults/100×20/soak | HIL | harness, macierz i procedura istnieją; fizyczne wykonanie i podpis odpowiedzialnej osoby nadal wymagają zatwierdzonego stanowiska |

## 7. Kolejność dalszej implementacji

1. wykonać kontrolowany provisioning pierwszego konta serwisowego według `docs/ACCESS_CONTROL.md`;
2. uruchomić `lab-control-qualify passive` i zarchiwizować raport według `docs/HIL_QUALIFICATION.md`;
3. zakwalifikować ścieżkę SG na wykrytej opcji i firmware, a następnie jawnie ustawić `basic_scpi` oraz zatwierdzone zakresy częstotliwości/mocy;
4. zakwalifikować zapis RBW/VBW/detector/attenuation/preamp/sweep time na dokładnym firmware i dopiero wtedy dodać firmware do `qualified_firmware`;
5. wykonać pełną macierz HIL i soak test.

Macierz nie jest deklaracją gotowości sprzętowej. Po każdej większej zmianie liczby testów i
statusy muszą zostać ponownie wyliczone z aktualnego worktree.
