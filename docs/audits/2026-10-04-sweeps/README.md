# Materiały audytu sweepów — 2026-10-04

Główny dokument: [AUDYT_SWEEPS_2026-10-04.md](../../../AUDYT_SWEEPS_2026-10-04.md).

Dowody dotyczą lokalnego drzewa roboczego bazującego na HEAD `c6697e2cee78277cd9125e8cc2a9e63c64ccc4a4` z wcześniejszymi zmianami użytkownika. Nie są kwalifikacją rzeczywistej aparatury. Komunikacja instrumentalna w testach używała wyłącznie symulatorów, a kwalifikacja i compliance w recepturze są syntetyczne.

## Zawartość

| Plik | Znaczenie |
|---|---|
| [scenario-simulation-only.yml](scenario-simulation-only.yml) | dokładne źródło receptury 2×3×3; nie jest recepturą gotową do laboratorium |
| [cartesian-simulation.h5](cartesian-simulation.h5) | 18 checkpointów, 18 widm, 18 surowych ramek, 523 eventy; PyThat validation przechodzi |
| [cartesian-simulation.csv](cartesian-simulation.csv) | 18 wierszy skróconego eksportu tego samego run; to nie jest eksport wszystkich metadanych HDF5 |
| [evidence.json](evidence.json) | wyciąg punktów 0/1/9/17, nastawy/pomiary/metadane/stany, kolejność pierwszych operacji, osie publiczne i estymacja |
| [contracts.log](contracts.log), [contracts.xml](contracts.xml) | wykonanie kontraktów z pominięciem dodanego później SW-20: 2 passed, 23 xfailed |
| [shutdown-contract.log](shutdown-contract.log) | osobne wykonanie SW-20: 1 xfailed, 25 deselected |
| [rendering-tests.log](rendering-tests.log), [rendering-tests.xml](rendering-tests.xml) | pokazane strony i potwierdzona projekcja prądu: 4 passed, 1 xfailed |
| [broader-tests.log](broader-tests.log), [broader-tests.xml](broader-tests.xml) | 17 istniejących plików testów: 193 passed, 16 failed, 4 skipped |
| [baseline-summary.json](baseline-summary.json) | obserwowany wynik wcześniejszej grupy 14 plików: 266 passed, 29 failed; pełny log nie był zachowany |
| [ruff.log](ruff.log) | 1 511 zgłoszeń Ruff w zastanym `app tests`; bez automatycznego poprawiania kodu produkcyjnego |
| [source-and-evidence-sha256.json](source-and-evidence-sha256.json) | sumy kontrolne źródeł objętych przeglądem i końcowych artefaktów; bez treści lokalnych prywatnych ustawień |

Zrzuty z rzeczywiście pokazanych okien offscreen po obsłużeniu zdarzeń:

- [Sweeps 1360×880 light](sweeps-1360-light.png), [dark](sweeps-1360-dark.png), [1000×760 light](sweeps-1000-light.png).
- [Execution 1360×880 light](execution-1360-light.png), [dark](execution-1360-dark.png), [1000×760 light](execution-1000-light.png).
- [Keithley — projekcja zdarzenia 1,4 mA](keithley-live-projection.png). To kontrolowana projekcja syntetycznego eventu, nie zdjęcie fizycznego pomiaru.

**Interpretacja xfail:** test żąda poprawnego zachowania i obecnie nie przechodzi. `strict=True` powoduje błąd przy nieoczekiwanym przejściu, aby po naprawie zaktualizować oznaczenie. To materiał dowodowy nieusuniętych usterek, nie zielona kwalifikacja produktu.

## Odtwarzanie

Polecenia PowerShell z katalogu głównego repozytorium. Testy UI używają fixture'ów izolujących QSettings, inventory DB, profile i logi. Pozostawić `simulation=True`; nie zastępować syntetycznych profili rzeczywistymi połączeniami.

```powershell
.venv/Scripts/python.exe -m pytest tests/test_sweep_audit_contracts.py -q -rx --basetemp=.audit-sweeps-reproduction-tmp -p no:cacheprovider
.venv/Scripts/python.exe -m pytest tests/test_sweep_audit_rendering.py -q -rx --basetemp=.audit-sweeps-ui-reproduction-tmp -p no:cacheprovider
.venv/Scripts/python.exe -m tools.summarize_sweep_audit docs/audits/2026-10-04-sweeps/cartesian-simulation.h5
.venv/Scripts/python.exe -m ruff check tests/test_sweep_audit_contracts.py tests/test_sweep_audit_rendering.py tools/summarize_sweep_audit.py
```

Pierwszy zestaw ma obecnie 26 przypadków: 2 przejścia i 24 oczekiwane niepowodzenia. W audycie ostatni przypadek SW-20 wykonano osobno. Drugi zestaw ma 5 przypadków. Symulacyjny test kartezjański rejestruje wywołania Wait zamiast wykonywać 18 pauz; osobny test rzeczywiście mierzy przerwę 3 s i anulowanie. Nowe wykonanie stworzy nowy HDF5 z innymi timestampami; nie należy nadpisywać historycznego artefaktu udając identyczny run. Narzędzie ekstrakcji odmawia zastąpienia istniejącego archiwum innym plikiem.

Testy renderowania zapisują PNG pod tymi samymi nazwami, więc odtwarzanie ich odświeży zrzuty. Sumy kontrolne w manifestach odnoszą się do końcowego stanu oryginalnego audytu.

Środowisko użyte w audycie: Python 3.14.6; pytest 9.1.1; PySide6 6.11.2; PySide6-Fluent-Widgets 1.11.2; NumPy 2.5.2; h5py 3.16.0; PyThat 0.2.14; Ruff 0.16.6. Cztery istniejące testy golden thaTEC pominięto z powodu braku licencjonowanego fixture'u.
