# Ponowny audyt sweepów i aktualnej konfiguracji Anritsu — 5.10.2026

**Werdykt: nie potwierdzam pełnej gotowości produkcyjnej z aktualnym panelem i konfiguracją analizatora.** Pięć nowych prób odtworzyło luki niewykryte przez poprzednie testy. Poprzedni wniosek o gotowości był zbyt szeroki; naprawy SW-01–SW-20 nie zamykają tych dodatkowych problemów.

Audyt obejmuje aktualny working tree na bazie `acce560`, razem z poprzednimi naprawami. Kod wykonawczy i rzeczywiste ustawienia stanowiska nie zostały zmienione w tym ponownym przeglądzie. Połączenia w próbach były wyłącznie symulowane. Firmware `7.03.00` odczytano z listy kwalifikacji w konfiguracji, nie z fizycznego urządzenia.

## Ustalenia

### SA-R1 — P1: dwa niezależne źródła RBW/VBW w edytorze sweepa

Aktualny `AnritsuSpectrumConfigurationPanel` pokazuje RBW oraz VBW, także wybór Video/Power. `AnritsuNodeEditorDialog` równocześnie zawiera osobny panel Advanced. Generowanie `parameter_actions` pobiera RBW/VBW z Advanced, a nie z widocznych pól podstawowego panelu.

Reprodukcja: podstawowy panel ustawiony na **Manual / 10 kHz**, oba selektory RBW ustawione na **Set**. Wygenerowane akcje zawierają **auto / 1 kHz**. Przy tej parze walidacja może następnie odrzucić zapis zamiast utworzyć oczekiwany plan. Zapis podstawowego snapshotu również nie zawiera pól RBW/VBW. Interfejs nie ma jednego źródła prawdy dla tych ustawień.

Źródła: [pola panelu](app/devices/anritsu_ms2830a/ui/page.py:222), [snapshot](app/devices/anritsu_ms2830a/ui/page.py:342), [odczyt wartości do receptury](app/devices/anritsu_ms2830a/ui/recipe_dialog.py:299), [zapis węzła](app/ui/recipes/page.py:5346).

Wymagana naprawa: wspólny model i jedno powiązanie kontrolek z wybranymi parametrami; round-trip edytor → YAML → kompilator → potwierdzony stan karty. Nowe pola nie mogą być automatycznie wysyłane, jeśli operator pozostawił je jako Unchanged.

### SA-R2 — P2: brak jawnego sterowania Video/Power w recepturze

Natywny `SpectrumConfig` i adapter obsługują `vbw_mode=VID/POW`. Schemat `configure_anritsu` odrzuca to pole. Zaawansowane `vbw_mode` oznacza natomiast **auto/manual/off**, czyli inną właściwość urządzenia. Nie istnieje kompletna ścieżka zapisania jawnego wyboru Video/Power w drzewie.

Reprodukcja: `configure_anritsu` z `vbw_mode: POW` kończy się `unknown configure_anritsu fields: vbw_mode`. To bezpieczna odmowa, ale nie zgodność z aktualnym zakresem konfiguracji panelu.

Źródła: [schemat](app/recipes/models.py:74), [model](app/devices/anritsu_ms2830a/configuration.py:7), [kompilator](app/engine/compiler.py:3093), [adapter](app/devices/anritsu_ms2830a/adapter.py:1048).

Wymagana naprawa: osobne, jednoznaczne nazwy dla wyboru Video/Power i AUTO/manual/off, z obsługą masek zmian, odczytu, metadanych i recovery.

### SA-R3 — P1: jawna wartość pasma może zostać po cichu pominięta

Samodzielny węzeł YAML `configure_anritsu_advanced` z `rbw: '10 kHz'`, bez `rbw_mode`, przechodzi kompilację. Kompilator tworzy `rbw_hz=None`, więc jawna wartość znika. Analogiczny wzorzec dotyczy VBW, tłumienia i czasu przemiatania. Edytor wybranych parametrów sprawdza pary, ale bezpośredni węzeł zaawansowany omija tę kontrolę.

Źródło: [kompilacja konfiguracji zaawansowanej](app/engine/compiler.py:2948).

Wymagana naprawa: odrzucać niekompletne/sprzeczne pary lub obsługiwać jawnie potwierdzony tryb bazowy. Nie zamieniać zadanej wartości na brak operacji. Testować także wartość podaną razem z AUTO/OFF.

### SA-R4 — P1: brak odczytu Video/Power staje się zmyślonym potwierdzeniem VID

`read_full_configuration()` przechwytuje każdy wyjątek zapytania `BAND:VID:MODE?` i wpisuje `VID`. Wymuszony błąd odczytu nie powoduje błędu ani stanu unknown. Taki snapshot jest używany przez fingerprint konfiguracji rejestrowany przy widmach i porównania z referencją.

Źródła: [fallback odczytu](app/devices/anritsu_ms2830a/adapter.py:750), [fingerprint](app/devices/anritsu_ms2830a/acquisition_context.py:9), [odczyt tożsamości akwizycyjnej](app/engine/runner.py:1885).

Wymagana naprawa: zachować brak wiedzy albo odmówić kwalifikowanej akwizycyji wymagającej tego pola. Ewentualny fallback dla konkretnego firmware wymaga jawnej podstawy; nie może wyglądać jak pomiar parametru.

### SA-R5 — P1: recovery nie przywraca potwierdzonego Video/Power

Prelude odtwarza podstawowe nastawy i starszy `AdvancedSpectrumConfig`. Ten model nie ma pola VID/POW. Reprodukcja ustawia POW, zapisuje potwierdzone snapshoty, zmienia stan symulatora na VID i wykonuje prelude. Po odtworzeniu pozostaje **VID**, zamiast wcześniejszego **POW**.

Próba podała recovery nawet pełny snapshot w `confirmed_states`; mimo to pole nie zostało wykorzystane. Przy aktywnej referencji kontrola fingerprintu może zatrzymać dalszą pracę. Bez referencji nie należy zakładać identycznych warunków pomiaru po wznowieniu.

Źródło: [odtwarzanie konfiguracji](app/engine/recovery.py:181), szczególnie budowa konfiguracji zaawansowanej przy linii 283.

Wymagana naprawa: utrwalać i odtwarzać pełną wymaganą tożsamość analizatora, potwierdzić odczytem przed wznowieniem i przetestować zmianę stanu urządzenia między przerwaniem a recovery.

## Aktualna konfiguracja i rozmiar Twojej serii

Odczyt lokalnego profilu: **100 MHz–6 GHz, −10 dBm, 10001 punktów**, RBW AUTO z nominalnym polem 3 MHz, VBW manual 30 kHz, detektor NORM, tłumienie manual 10 dB, preamp OFF, czas przemiatania AUTO. `application_average_count=198` jest ustawieniem aplikacji analizatora; receptura musi określić swój `average_count` jawnie. Próby poniżej używają 1, nie 198.

Dla **1309 checkpointów po 10001 punktów** aktualny estymator wyliczył **922 378 048 B** pamięci walidacji/importu, przy skonfigurowanym budżecie **536 870 912 B (512 MiB)**. Kontrola używana przez RunWorker odrzuca ten plan **przed połączeniami z aparaturą**. Próba izoluje analizator i używa Repeat zamiast trzech fizycznych osi; pełne osie dodają dalsze metadane i nie usuwają tego przekroczenia.

To poprawna ochrona zasobów, nie błąd poleceń urządzenia. Jest jednak dodatkowym warunkiem uruchomienia docelowej serii. Potrzebna jest kwalifikacja odpowiedniego budżetu/ścieżki odczytu albo świadomy podział danych. Nie zwiększano limitu i nie zmniejszano rozdzielczości automatycznie. Poprzednia próba 1309 kombinacji miała **1001**, a nie 10001 punktów na widmo.

[Dokładne ustawienia i wynik kontroli](docs/audits/2026-10-05-sweep-analyzer-recheck/current-config-capacity.json).

## Co nadal działa i co sprawdzono

- **68 passed, 27,09 s**: istniejące testy szybkiej akwizycyji, sprzętowego modelu Anritsu, kontraktów sweepów, przetwarzania widm i recovery referencji.
- **1 passed, 6,54 s**: natywny pipeline SIM wykonał trzy checkpointy 10001 punktów przy 100 MHz–6 GHz i jawnie zapisanym zaawansowanym bloku z VBW 30 kHz. Archiwum przeszło PyThat. Oczekiwania 3 s zarejestrowano bez rzeczywistego czekania w tej próbie. Video/Power nie było zadawane, więc ten wynik nie zamyka SA-R2/SA-R5.
- **5 failed, 5,69 s**: nowe próby poprawnego zachowania odtworzyły SA-R1–SA-R5. Są celowo zachowane jako diagnostyka poza zwykłą kolekcją testów. Ich niepowodzenie jest dowodem otwartych problemów, nie zaakceptowanym wynikiem kwalifikacji.

[Skrypt reprodukcji](docs/audits/2026-10-05-sweep-analyzer-recheck/reproduce_gaps.py), [log pięciu błędów](docs/audits/2026-10-05-sweep-analyzer-recheck/probes.log), [próba bieżących wartości](docs/audits/2026-10-05-sweep-analyzer-recheck/verify_current_values.py), [wynik](docs/audits/2026-10-05-sweep-analyzer-recheck/current-values-result.json), [log regresji](docs/audits/2026-10-05-sweep-analyzer-recheck/regressions.log).

Polecenia odtworzenia: `python -m pytest docs/audits/2026-10-05-sweep-analyzer-recheck/reproduce_gaps.py -q` oraz analogicznie `verify_current_values.py`. Pierwsze polecenie obecnie powinno wykazać opisane błędy. Nie używać tego raportu jako dopuszczenia fizycznego pomiaru: pełna integracja aktualnych pól analizatora pozostaje otwarta.
