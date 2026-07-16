# Lab Control

Lokalna aplikacja PySide6 do bezpiecznego sterowania stanowiskiem z:

- Rigol DG1032Z (CH1/CH2);
- Keithley 2600/2602A (SMU A/B);
- Anritsu MS2830A (widmo oraz odświeżanie Live).

Architektura, limity oraz procedury kwalifikacji są opisane w [masterplanie](docs/PLAN_WDROZENIA.md). Bieżąca [procedura operatora](docs/PROCEDURA_OPERATORA.md) opisuje bezpieczne uruchomienie i awarię. Wynik pierwszej kwalifikacji komunikacji z podłączonym Rigolem jest w [raporcie sprzętowym](docs/HARDWARE_TEST_RIGOL_DG1032Z.md), a wymagane bramki dla trzech urządzeń w [macierzy kwalifikacji](docs/MACIERZ_KWALIFIKACJI_SPRZETOWEJ.md).

## Uruchomienie

```powershell
python -m pip install -e ".[dev]"
lab-control
```

Alternatywnie:

```powershell
python -m app.main --settings .config/settings.yml
```

Bez sprzętu można uruchomić komplet trzech symulowanych urządzeń:

```powershell
lab-control --simulate
```

Tryb symulacji nie dotyka USB, TCP/IP ani pliku profilu. Zawiera deterministyczne widmo Anritsu, model rezystancyjnego DUT Keithley i pełny model podstawowych stanów Rigola.

## Bezpieczny start

1. Otwórz zakładkę **Ustawienia**.
2. Wpisz zasoby VISA i limity stanowiska/DUT dla każdego urządzenia.
3. Podłącz urządzenia przez **Dashboard** i sprawdź ich `*IDN?`.
4. Uzupełnij limit wejścia RF Anritsu przed włączeniem akwizycji.
5. Wykonaj kwalifikację `standard_scpi_opc` Anritsu na konkretnym firmware, zanim użyjesz receptury z `AcquireSpectrum`.
6. Zapisz konfigurację; zapis celowo ustawia profil na `unverified`.
7. Odpowiedzialna osoba zatwierdza profil przez przycisk **Zatwierdź profil** i frazę potwierdzającą.

Do tego momentu aplikacja może odczytywać urządzenia i konfigurować je wyłącznie przy `OUTPUT OFF`, ale nie pozwala na `OUTPUT ON`.

## Rigol — ważne ograniczenie

DG1032Z jest źródłem napięciowym, nie SMU. Aplikacja oblicza konserwatywny **szacowany prąd obciążenia** z modelu 50 Ω i minimalnej impedancji DUT. To nie jest pomiar prądu ani hardware compliance.

Po każdej konfiguracji aplikacja odczytuje funkcję, częstotliwość, HighL, LowL oraz stan output. Jeśli urządzenie skoryguje wartość (np. przez minimalne Vpp), transakcja kończy się błędem przy nadal wyłączonym wyjściu.

## Receptury

Przykład [recipes/example_nested_sweep.yml](recipes/example_nested_sweep.yml) opisuje:

```text
Keithley B: 1 mA → 10 mA, 100 punktów
Rigol CH1 HighL: 1 mV → 3 mV, 20 punktów
Anritsu: jedno widmo dla każdego punktu
```

Kompilator rozwija ją do 2000 widm i sprawdza wszystkie limity przed otwarciem sesji pomiarowej. Run Engine zapisuje metadane, snapshot receptury i ustawień oraz checkpoint każdego widma w HDF5.

Przykład jest celowo nieenergetyczny: nie ma w nim `ARM` ani `OUTPUT ON`. Wzorzec jawnej sekwencji `ARM → OUTPUT ON → finally: Ramp to zero/OFF` znajduje się w [recipes/example_energized_template.yml](recipes/example_energized_template.yml), a pełne 100 × 20 w [recipes/example_energized_nested_sweep_template.yml](recipes/example_energized_nested_sweep_template.yml). Oba są zablokowane, dopóki profil nie zostanie zatwierdzony oraz oba `allow_output_enable` nie będą ustawione przez osobę odpowiedzialną.

Każdy run zapisuje HDF5 z recepturą, snapshotem ustawień, IDN, capabilities i dziennikiem zdarzeń. Gdy `storage.write_csv_summary` jest włączone, obok niego powstaje także checkpointowany indeks CSV — nie zawiera on pełnych danych widma.

## Testy

```powershell
python -m pytest -q
```

Testy domyślnie używają fake VISA i nie dotykają urządzeń. Testy sprzętowe wykonuj dopiero zgodnie z rozdziałem kwalifikacyjnym masterplanu i zawsze zaczynaj od `OUTPUT OFF` oraz sztucznego obciążenia.

## Narzędzia historyczne

`gui.py` i zapisujący tryb `test.py --apply` są zachowane wyłącznie do serwisu starego prototypu. Oba wymagają teraz jawnego przełącznika `--unsafe-legacy`, mogą omijać limity z `settings.yml` i nie są częścią produkcyjnej ścieżki sterowania.
