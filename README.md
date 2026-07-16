# Lab Control

Lokalna aplikacja PySide6 do bezpiecznego sterowania stanowiskiem z:

- Rigol DG1032Z (CH1/CH2);
- Keithley 2600/2602A (SMU A/B);
- Anritsu MS2830A (widmo oraz odświeżanie Live).

Architektura, limity oraz procedury kwalifikacji są opisane w [masterplanie](docs/PLAN_WDROZENIA.md). Wynik pierwszej kwalifikacji komunikacji z podłączonym Rigolem jest w [raporcie sprzętowym](docs/HARDWARE_TEST_RIGOL_DG1032Z.md).

## Uruchomienie

```powershell
python -m pip install -e ".[dev]"
lab-control
```

Alternatywnie:

```powershell
python -m app.main --settings .config/settings.yml
```

## Bezpieczny start

1. Otwórz zakładkę **Ustawienia**.
2. Wpisz zasoby VISA i limity stanowiska/DUT dla każdego urządzenia.
3. Podłącz urządzenia przez **Dashboard** i sprawdź ich `*IDN?`.
4. Uzupełnij limit wejścia RF Anritsu przed włączeniem akwizycji.
5. Zapisz konfigurację; zapis celowo ustawia profil na `unverified`.
6. Odpowiedzialna osoba zatwierdza profil przez przycisk **Zatwierdź profil** i frazę potwierdzającą.

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

## Testy

```powershell
python -m pytest -q
```

Testy domyślnie używają fake VISA i nie dotykają urządzeń. Testy sprzętowe wykonuj dopiero zgodnie z rozdziałem kwalifikacyjnym masterplanu i zawsze zaczynaj od `OUTPUT OFF` oraz sztucznego obciążenia.
