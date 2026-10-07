# Zamykanie urządzeń ograniczone do planu sweepa

## Problem i zmiana

Tryb pomiarowy dodawał do końcowego shutdownu Keithley, Rigol i Anritsu
niezależnie od zależności planu. Worker również mógł łączyć i zamykać sesje
nieużywanej aparatury. Samo usunięcie węzłów z drzewa nie wystarczało.

Kompilator, runner i worker korzystają teraz z zakresu urządzeń planu.
Manifest zależności jest łączony z zależnościami rzeczywistych akcji, aby
niepełny manifest nie pomijał urządzenia obsługiwanego przez sweep.
Końcowe wyłączenie, przerwanie po błędzie, bezpieczny stop, zamykanie sesji
i automatyczna interwencja watchdoga nie obejmują obcych urządzeń.
Runner filtruje również nadmiarowe shutdowny ze starszych manifestów.
Pusty zakres nie oznacza całego stanowiska.

Urządzenia użyte przez sweep nadal otrzymują końcowe bezpieczne wyłączenie.
Jawne akcje w sekcji `finally` również stanowią część planu i jego zależności.
Zakres jest określany na poziomie urządzenia, nie pojedynczego kanału.
MOKE jest zatrzymywany przez runner tylko wtedy, gdy run steruje jego wyjściem;
watchdog nie dodaje zapisu DAC dla samego odczytu Hall ani dry run.
Zapis checkpointu pozostaje częścią zamknięcia.

Ręczny przycisk **E-STOP — ALL OUTPUTS OFF** zachowuje globalne działanie
z dotychczasowymi warunkami dopuszczenia sterowania MOKE.

## Weryfikacja

- Testy workerów dla measurement i dry run: sweep samego Anritsu pozostawia
  podłączone Rigol i Keithley w stanie OUTPUT_ON, bez connect, OFF i disconnect.
- Testy runnera: stary szeroki manifest, błąd akwizycji, finally i pauza
  nie powodują wyłączenia urządzeń poza planem.
- Testy zakresu automatycznego emergency stop, pustego zakresu oraz globalnego
  ręcznego E-STOP z włączonym symulowanym MOKE.
- Regresje kompilatora, recovery, dry run i kontrolera uruchomienia.

Testy wykorzystują symulatory i atrapy transportu; fizyczna aparatura nie była
uruchamiana. Po aktualizacji należy zrestartować aplikację i ponownie
skompilować recepturę, aby podgląd planu zawierał nową listę shutdownów.

## Korekta podglądu drzewa

Po pierwszej poprawce wykonania pozostała niezależna, zakodowana na sztywno
lista trzech urządzeń w `normalize_recipe_tree`. Dodatkowo
`RecipePage.semantic_tree_snapshot` ignorował argument `plan`. Usunięto
obie przyczyny oraz analogiczną domyślną listę w starszym helperze projekcji.

Przed kompilacją drzewo pokazuje jawne akcje `finally` i komunikat o konieczności
kompilacji do ustalenia automatycznych działań. Po kompilacji Builder oraz
snapshot przekazywany do Execution pokazują dokładnie `safe_shutdown_actions`,
również obok jawnego cleanupu. Stan gałęzi to PENDING lub PLANNED, a nie
przedwczesne zapewnienie SAFE. Ponowna edycja usuwa nieaktualny manifest.

Weryfikacja: 99 testów i 6 podtestów dla drzewa, modelu, kompilatora oraz
audytu sweepów; dodatkowe 3 testy projekcji na natywnym Qt Windows (szerokości
900 i 1440 px). Testy potwierdzają dwa końcowe wpisy dla receptury samego
Anritsu: zatrzymanie analizatora i flush checkpointu. Bez komunikacji ze sprzętem.
Ruff dla zmienionych plików przechodzi. Pełne `ruff check app tests` nadal
zgłasza 10 wcześniejszych nieużywanych importów w testach
`test_spectrum_correction_layout.py` i `test_sweep_release_contracts.py`.
