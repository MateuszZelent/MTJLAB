# Pierwszy test tylko analizatora

Plik: `recipes/anritsu_background_reference_smoke_test.yml`.

Kolejność:

1. Tło: co najmniej 30 s i co najmniej 4 pełne przebiegi; 3 s między przebiegami.
2. Przerwa 3 s.
3. Referencja: 4 pełne przebiegi, średnia w mocy liniowej; 3 s między przebiegami.
4. Przerwa 3 s.
5. Jedno widmo RAW, bez odejmowania referencji i bez filtrów.

Plan ma 5 akcji, 1 punkt pomiarowy i wymaga wyłącznie Anritsu. Przewidywany czas to około 45 s plus czas akwizycji i zapisu. Tło, referencja, wszystkie surowe przebiegi i końcowe widmo są zapisywane w HDF5. To test działania akwizycji i archiwizacji, nie kwalifikacja statystyczna jakości tła.

## Uruchomienie na aparaturze

- Uruchomić aplikację bez `--simulate`; na ekranie nie może być oznaczenia SIMULATION.
- Wybrać na analizatorze docelowy zakres, RBW, VBW i detektor. Przepis nie nadpisuje tych parametrów.
- W Sweeps otworzyć plik YAML i wybrać **Dry run — outputs forced OFF**, następnie zweryfikować plan i rozpocząć.
- Tło i referencja zostaną zebrane w tym samym, ręcznie przygotowanym stanie układu. W tym teście nie ma sterowania MOKE, Keithleyem ani Rigolem.

Po poprawce zakresu wykonania zarówno dry run, jak i zwykły pomiar łączą i porządkują tylko urządzenia planu — tutaj Anritsu. Ten plan nie steruje generatorem RF: po akwizycji wysyłane jest tylko `ABORT`, bez `INST SG`, `OUTP 0` ani `OUTP?`. Odbiornik rejestruje rzeczywiste widma, jeśli aplikacja pracuje poza symulacją. Tę samą zasadę stosują dry run i automatyczny watchdog tego planu.

## Regresja

`tests/test_anritsu_smoke_recipe.py` wykonuje dokładnie ten plik w symulacji, z rzeczywistymi czasami przerw i zbierania tła. Sprawdza kompletność surowych przebiegów, dwie odrębne referencje oznaczone background/reference, końcowe widmo i zgodność HDF5 z PyThat. Wywołania connect/emergency_off/disconnect dla innych urządzeń są zabronione w teście, również gdy inne sesje są oznaczone jako już połączone.

W ramach przygotowania poprawiono porządkowanie RunWorker: dry run nie rozłącza już nieużywanych sesji urządzeń przekazanych z kart głównego okna. Normalna polityka pomiarowa pozostaje bez zmian. Fizycznych urządzeń nie uruchamiano podczas przygotowania.
