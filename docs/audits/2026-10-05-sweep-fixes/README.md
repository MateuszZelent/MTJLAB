# Dowody napraw sweepów

[Raport i rozliczenie SW-01–SW-20](../../../SWEEPS_PRODUCTION_READINESS.md).

Wszystkie urządzenia w próbach są symulatorami. Nie zmieniono rzeczywistych profili stanowiska i nie wysłano pomiarów do zewnętrznego eLab.

## Zakończone pakiety

- `core-release.log`: 466 passed, 4 skipped, 18 subtests — kompilacja, wykonanie, zapis, recovery, estymacja i Wait.
- `builder-release.log`: 95 passed, 4 subtests — edytor i przewijanie.
- `native-complete.log`: 135 passed, 5 subtests — natywne polecenia oraz dodatkowe kontrakty.
- `contracts-release.log`: 93 passed, 6 subtests — konfiguracja, parser, budżety i monitorowanie SMU.
- `integrations-release.log`: 14 passed — eLab i odczyt drzewa wyników; wszystkie wysyłki eLab są zastąpione testowymi mockami.
- `fluent-complete.log`: 56 passed — Fluent, interakcje drzewa, responsywność oraz 1000 × 10001 próbek widmowych.
- `render-release.log`: 9 passed — pokazane okno, drzewa i kompletne karty Keithleya.
- `render-desktop-last.log`: 4 passed — końcowe zrzuty stron po zmianie prezentacji bezpieczeństwa.
- `render-caption-final.log`: 9 passed — renderowanie po poprawce szerokości napisów bezpieczeństwa.
- `render-ready-final.log`: 9 passed — końcowy przebieg na aktualnym kodzie; geometria, pełne napisy i zrzuty po faktycznym zakończeniu natywnej animacji nawigacji.
- `safety-strip-final.log`: 1 passed — pasek bezpieczeństwa i unieważnienie starego stanu sesji.
- `caption-geometry-final.log`: 11 passed — pełne napisy stanu bezpieczeństwa, geometria i działanie E-STOP/Save przy 240–1500 px, oba motywy. Bardzo wąski pasek przenosi napisy do osobnych wierszy.
- `stress-release.log`: 1 passed — ponowny pomiar 1000 × 10001 na aktualnym kodzie; liczby w `gui-responsiveness.json`.
- `scale-release.log` i `scale-1309.json`: 1309 kombinacji, 1001 bins, poprawne zamknięcie, górna estymacja pokrywająca plik i odczyt PyThat 0.2.14.
- `lint-complete.log`: Ruff F w zmienionych plikach.
- `collection-final.log`: 649 zebranych przypadków, weryfikowanych pakietami. Pakiety częściowo się nakładają; ich liczebności nie należy sumować.

Cztery pominięcia wynikają z braku licencjonowanych golden files thaTEC. Odczyt nowych plików przez PyThat został wykonany. Diagnostyczne zrzuty stosów oznaczone „Timeout (0:00:40)” w części logów są okresowym działaniem faulthandlera, a nie przekroczeniem limitu testu; wynik próby znajduje się na końcu logu.

`render-fixed-delay-diagnostic.log` zachowuje pośrednią próbę: 3 asercje zakończenia animacji nie przeszły przy stałej pauzie, 6 pozostałych przypadków przeszło. Diagnostyka potwierdziła nadal działającą natywną animację. Końcowy test czeka na rzeczywiste zakończenie przejścia z limitem pięciu sekund; nie zatrzymuje ani nie ukrywa wskaźnika na potrzeby zrzutu.

## Artefakty do inspekcji

Zrzuty `sweeps-*`, `execution-*`, `baseline-authoring-*` oraz `keithley-*` pokazują natywne okno Fluent po `show()` i obsłużeniu zdarzeń, przy 1360 × 880 i 1000 × 760. Drzewo sweepa zostało odsłonięte realnym przewinięciem strony. Karty ukryte w chwili zdarzenia pokazują potwierdzone pola i OUTPUT UNKNOWN po wejściu na trasę urządzenia.

Zrzuty `safety-strip-*` pokazują dodatkowo bardzo wąski i kompaktowy pasek bezpieczeństwa. Napisy stanu pozostają kompletne; tożsamość operatora ma pełny tooltip przy ograniczonej szerokości.

[HDF5 z 18 kombinacjami](cartesian-simulation.h5) i odpowiadający mu [CSV](cartesian-simulation.csv) umożliwiają inspekcję aktualnej struktury i metadanych. Duże HDF5 prób skali są generowane w katalogach testowych; JSON utrwala rozmiar i SHA-256. Wyników symulacji nie należy traktować jako kwalifikacji rzeczywistego DUT.

`verification-manifest.json` zawiera hashe końcowych zmienionych źródeł i zachowanych artefaktów.
