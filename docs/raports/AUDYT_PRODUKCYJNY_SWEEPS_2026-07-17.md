# Audyt produkcyjny sekcji Sweeps

## Aktualizacja po wdrożeniu rekomendacji — 2026-07-17

Po wykonaniu audytu wdrożono pierwszą serię zmian produkcyjnych. Poniższa lista ma
pierwszeństwo przed historycznymi ocenami i blockerami opisanymi dalej w raporcie:

- rozdzielono dwie niezależne czynności w drzewie: **Device settings** otwiera pełną
  konfigurację urządzenia, a **Edit ROI** wyłącznie edytor punktów i przedziałów;
- dodano pełne okna konfiguracji dla bloków Keithley, Rigol i Anritsu oraz obsługę
  starszych węzłów `configure_*` znajdujących się wewnątrz osi `sweep`;
- dodano komendę **New** (`Ctrl+N`) i akcję **New empty sweep** w menu kontekstowym;
- dozwolono poprawny pusty korzeń `sequence`, dzięki czemu można usunąć ostatni obiekt
  i rozpocząć budowę planu od zera; kompilator nadal nie pozwala uruchomić pustego planu;
- dodano wspólny rejestr parametrów i wykonywalne providery DeviceNode dla Keithley
  oraz Rigol wraz z aktualizacją parametrów pomiędzy punktami;
- dodano provider Anritsu Spectrum dla pełnego snapshotu base + advanced, jednej osi
  lokalnej i zachowanych osobnych dzieci `acquire_spectrum`;
- dodano czwarty, osobny blok biblioteki **Anritsu signal generator** z niezależnym
  oknem frequency/power, trybami `Bez zmian / Ustaw / Sweep`, własnym ROI i pełnym
  providerem wykonawczym; konfiguracja każdego punktu pozostawia RF OFF, a energizacja
  nadal wymaga jawnej, walidowanej sekwencji ARM/ON;
- formularz Sweeps dla Anritsu SG dziedziczy frequency/power z ręcznej zakładki SG;
  biblioteka udostępnia jawne bloki **Anritsu SG ARM** i **Anritsu SG RF ON**, a test
  wielopunktowy potwierdza świeży ARM przed każdym RF ON oraz wymuszenie limitu DUT;
- dodano statyczny preflight kolejności `configure → arm → output on → update`, który
  odrzuca aktualizacje bez konfiguracji oraz energizację bez jednorazowego ARM;
- rozszerzono walidowany readback konfiguracji Rigol oraz zgodność mappera thaTEC;
- mapper thaTEC obsługuje teraz osie wszystkich czterech typów DeviceNode:
  Keithley, Rigol, Anritsu Spectrum i Anritsu SG;
- drzewo zachowuje zaznaczenie, rozwinięcie i pozycję przewijania po przebudowie;
- ROI liczy duże osie bez alokowania wektora, blokuje ponad 100 000 punktów przed
  generowaniem oraz decymuje wykres dla zachowania responsywności;
- kompilacja i estymacja uruchamiane z UI pracują w osobnym `QThread`, mają anulowanie
  oraz odrzucają spóźniony wynik, jeżeli YAML zmienił się w czasie preflightu;
- dodano standardową edycję klawiszami Enter/Return, odwracalne Enable/Disable poddrzewa
  (z niewyłączalnym `finally`) oraz duplikowanie kompletnych poddrzew z nowymi ID;
- `KeithleyAdapter.configure_source()` potwierdza readback funkcji, poziomu, compliance,
  NPLC, sense, autorange i jawnych zakresów, zanim zapisze konfigurację jako aktywną;
- Rigol potwierdza readback każdego pola modulation, internal frequency sweep i burst,
  a mismatch pozostawia OUTPUT OFF i kończy transakcję błędem;
- Rigol DeviceNode zapisuje i wykonuje teraz także kompletny output path: load,
  polarity, normal/gated mode, gate polarity, SYNC enable/polarity/delay; konfiguracja
  jest wykonywana i odczytywana zwrotnie przy OUTPUT OFF przed ewentualnym ARM;
- Anritsu Advanced wymusza atomowe pary `mode=manual` + wartość w UI i kompilatorze;
- wąski Sweeps automatycznie zwija Inspector, na bardzo małej szerokości także Library,
  a oba panele mają jawne przełączniki; dialog Anritsu układa panele pionowo;
- zablokowane przyciski edycji MIN/MAX wyjaśniają teraz dokładną przyczynę:
  tryb symulacji, brak `EDIT_SETTINGS` wraz z aktualną rolą albo stały zakres NPLC;
  opis dostępności podaje też wymaganą rolę engineer/service;
- blok `Acquire spectrum/reference` ma osobny edytor trace, operacji referencyjnej
  oraz polityki zapisu raw/processed;
- ten sam edytor obsługuje teraz `average_count=1..9999` dla sygnału i referencji;
  runner pobiera kompletne, zgodne częstotliwościowo widma, uśrednia moc w liniowych
  mW, reaguje na Stop między ramkami, zachowuje Pause na bezpiecznej granicy
  checkpointu i zapisuje provenance uśredniania;
- timeout watchdoga i estymacja czasu skalują się z liczbą fizycznych ramek
  `average_count`, zamiast traktować wielokrotne uśrednianie jak pojedynczy sweep;
- uzupełniono testy resetu planu, niezależnych ścieżek edycji, providerów i wykonania.

Stan kontroli po zmianach:

| Kontrola | Wynik |
|---|---|
| `python -m pytest -q` | **332 passed**, **34 subtests passed** |
| `python -m ruff check app tests` | **PASS** |
| `python -m compileall -q app tests` | **PASS** |

Zmiany usuwają zgłoszoną regresję edycji i istotną część blockerów P0/P1, ale nie są
podstawą do uznania całej stacji za zakwalifikowaną produkcyjnie. Nadal wymagane są:
kwalifikacja HIL na fizycznych urządzeniach, zatwierdzony profil bezpieczeństwa,
domknięcie pełnego pokrycia parametrów Anritsu oraz testy wydajności bardzo dużych ROI.

**Data audytu:** 2026-07-17  
**Zakres:** bieżący working tree na bazie `6db854bd1cf4`, w tym niezacommitowane zmiany  
**Obszary:** UI, UI/UX, ergonomia, responsywność, budowanie drzewa, model receptury,
kompilacja, wykonanie, bezpieczeństwo, pokrycie parametrów urządzeń, thaTEC/PyThat/HDF5  
**Urządzenia:** Keithley 2600/2602A, Rigol DG1032Z, Anritsu MS2830A Spectrum/SG  
**Zasada oceny:** zielony test symulacyjny jest dowodem działania ścieżki programowej,
ale nie zastępuje kwalifikacji fizycznego urządzenia, firmware, toru DUT ani zewnętrznego
systemu inwentaryzacji.

## 1. Werdykt

Sekcja **Sweeps nie jest obecnie gotowa produkcyjnie** i nie powinna być używana do
projektowania oraz uruchamiania wartościowych, bezobsługowych pomiarów z poziomu nowego
drzewa urządzeń.

Najważniejszy powód jest architektoniczny: aplikacja ma równolegle dwie ścieżki:

1. starsze, natywne węzły `sweep` i akcje YAML, które kompilują się, wykonują i mają
   mocne testy symulacyjne;
2. nowe bloki urządzeń z `parameter_actions`, wspólnymi panelami i czytelnym drzewem,
   których kompilator **celowo nie obsługuje**.

Nowy interfejs jest wizualnie i ergonomicznie wyraźnie lepszy od starego edytora YAML,
ale jest w dużej mierze edytorem dokumentu demonstracyjnego. Po skonfigurowaniu urządzenia
kompilator zatrzymuje plan komunikatem:

> `DeviceNode provider compilation is not implemented yet`

Dowód: `app/engine/compiler.py:240-246`.

### Odpowiedzi na pytania audytowe

| Pytanie | Odpowiedź |
|---|---|
| Czy nowe drzewo Sweeps wykonuje skonfigurowane bloki urządzeń? | **Nie.** Skonfigurowane `DeviceNode` są blokowane przed wykonaniem. |
| Czy każdy parametr urządzeń można ustawić w drzewie? | **Nie.** Pokrycie jest częściowe; szczególnie duży brak dotyczy Rigola. |
| Czy każdy parametr można zautomatyzować jako oś? | **Nie.** Wykonywalna lista osi jest krótka i zdublowana w trzech modułach. |
| Czy oś zbudowana wizualnie zachowuje OUTPUT między punktami? | **Nie zawsze.** Stary generator tworzy pełne `configure_*`, które wymuszają OUTPUT OFF. |
| Czy wszystkie punkty są sprawdzane względem limitów przed Run? | **Tak dla starej ścieżki kompilowanej; nie w nowym edytorze przed kompilacją.** Nowa ścieżka i tak jest globalnie zablokowana. |
| Czy urządzenia są poprawnie zaprogramowane? | **Programowo w znacznym stopniu, fizycznie nieudowodnione.** Brak podpisanej kwalifikacji HIL dla całej stacji. |
| Czy HDF5 jest zgodny z thaTEC/PyThat? | **Tak dla lokalnego, przetestowanego kontraktu starych receptur i PyThat 0.2.14; nieudowodnione dla nowych DeviceNode i systemu zewnętrznego.** |
| Czy UI jest profesjonalny i ergonomiczny? | **Częściowo.** Układ, ROI, inspector i cofanie są dobre; spójność, responsywność dużych planów, język, prawdziwość statusów oraz kompletność akcji wymagają pracy. |
| Czy aktualny profil pozwala na produkcyjny run? | **Nie.** Profil jest `unverified`; Anritsu acquisition i SG są zablokowane. |

### Ocena syntetyczna

| Obszar | Ocena | Komentarz |
|---|---:|---|
| Bezpieczeństwo backendu | 7.5/10 | Fail-closed, ARM, limity i shutdown są mocne; brakuje pełnego HIL i statycznej analizy kolejności stanów. |
| Stara ścieżka YAML/sweep | 8/10 | Działa w symulacji, ma round-trip PyThat i 2000 widm; UI generuje jednak niewłaściwe akcje dla sweepów energetycznych. |
| Nowe drzewo urządzeń | 2/10 | Dobre UI, brak providera i brak wykonywalności. |
| Kompletność parametrów | 3/10 | Keithley częściowo, Anritsu częściowo, Rigol w nowym bloku praktycznie tylko OUTPUT. |
| UI/UX i ergonomia | 6/10 | Dobra baza, lecz kilka zachowań narusza przewidywalność i zachowanie kontekstu. |
| Responsywność | 5/10 | Układ reaguje na szerokość; generowanie/rysowanie i kompilacja dużych planów blokują GUI. |
| thaTEC/PyThat lokalnie | 8/10 | Mocne testy starych osi; runtime nie wykonuje obowiązkowego PyThat round-trip. |
| Gotowość produkcyjna całości | 3/10 | Blockery funkcjonalne, profil niezatwierdzony, brak fizycznej kwalifikacji. |

## 2. Metoda i dowody

Audyt objął:

- mapowanie przepływu `UI → YAML/RecipeNode → RecipeCompiler → ExecutionPlan →
  RecipeRunner → adaptery → HDF5/thaTEC/PyThat`;
- porównanie formularzy ręcznych z polami udostępnionymi w Sweeps;
- analizę wszystkich aktywnych typów węzłów, map osi i mappera thaTEC;
- odtworzenie zachowania drag-and-drop;
- analizę bieżącego profilu `.config/settings.yml`;
- przegląd renderów szerokiego i wąskiego workspace;
- uruchomienie pełnych testów, lint, kompilacji modułów i kontroli diff.

Wyniki narzędzi:

| Kontrola | Wynik |
|---|---|
| `python -m pytest -q` | **293 passed**, **34 subtests passed**, 32 warnings, 96.78 s |
| PyThat | 0.2.14 |
| PySide6 | 6.11.1 |
| pyqtgraph | 0.13.7 |
| NumPy / xarray | 2.5.1 / 2026.7.0 |
| `python -m compileall -q app tests` | PASS |
| `git diff --check` | PASS, tylko ostrzeżenia LF→CRLF |
| `python -m ruff check app tests` | **FAIL: 2 × F821** |

Ostrzeżenia testów nie zmieniają wyniku logicznego, ale wymagają kwalifikacji środowiska:

- `numpy.ndarray size changed` wskazuje na potencjalną niezgodność binarną;
- backend xarray/netCDF4 emituje deprecacje związane z NumPy 2.5.

## 3. Architektura aktualnej sekcji Sweeps

```text
RecipePage / QTreeWidget / dialogi ROI
                 │
                 ├── stary model: type=sweep + configure/update actions
                 │                       │
                 │                       └── RecipeCompiler → Runner → urządzenia
                 │
                 └── nowy model: type=sequence + device_module
                                      + parameter_actions
                                         │
                                         └── BLOKADA kompilatora

stary RecipeNode ──→ ThatecSchemaMapper ──→ ThatecHdf5Writer ──→ PyThat 0.2.14
nowy DeviceNode ───→ brak providera i brak kontraktu osi
```

Kod Sweeps nie jest wydzielonym modułem. Znajduje się razem ze stronami ręcznymi w
`app/ui/main_window.py`, który zawiera ponad 10 tysięcy linii. Nie istnieje deklarowany
w masterplanie `DeviceSweepProvider`, centralny katalog descriptorów ani niezależny
`app/sweeps`.

Istnieją trzy odrębne źródła prawdy o osiach:

- `_SWEEPABLE_PARAMETERS` w `app/ui/main_window.py:5335`;
- `_SWEEP_DIMENSIONS` w `app/engine/compiler.py:50`;
- `_TARGETS` w `app/storage/thatec_schema_mapper.py:45`.

To już spowodowało drift: UI odwołuje się do niezdefiniowanego `_SWEEP_DIMENSIONS`
(`app/ui/main_window.py:7910` i `:7999`), co wykrywa Ruff.

## 4. Blockery P0

### P0-1. Nowe bloki urządzeń nie mają providera wykonawczego

Każdy skonfigurowany Keithley, Rigol lub Anritsu otrzymuje:

```yaml
type: sequence
device_module: ...
operation: configure_selected_parameters
parameter_actions: ...
```

`RecipeCompiler._visit()` odrzuca taki węzeł bez próby kompilacji. Jest to zachowanie
testowane w `tests/test_recipe_compiler.py:126`, więc nie jest przypadkową regresją,
lecz świadomie niedokończonym kontraktem.

Skutki:

- kliknięcie `Validate & preview` kończy się błędem;
- `Run plan` nie zostaje aktywowany;
- `output_policy`, stałe parametry, ROI i zagnieżdżone urządzenia nie mają semantyki
  wykonawczej;
- nie istnieje dowód `manual ↔ sweep ↔ adapter`.

### P0-2. Nieskonfigurowany placeholder Keithley/Rigol może przepuścić dzieci

Nowy placeholder Keithley i Rigol ma `configuration_required: true`, ale przed otwarciem
edytora nie ma pola `operation`. Kompilator nie sprawdza `configuration_required`; traktuje
placeholder jak zwykłą sekwencję i kompiluje jego dzieci.

Odtworzony przypadek:

```text
placeholder Keithley (configuration_required=true)
└── acquire_spectrum
```

Kompiluje się do dokładnie jednej akcji `acquire_spectrum`.

To jest niebezpieczne semantycznie: użytkownik widzi blok urządzenia i zagnieżdżenie,
ale urządzenie nie jest konfigurowane ani nie tworzy pętli. Powstaje poprawny technicznie,
lecz błędny znaczeniowo wynik. Status `SETUP` w UI nie może być jedyną ochroną.

Wymagana poprawka: każdy `device_module` bez zakończonej konfiguracji i każdy
`configuration_required: true` musi blokować kompilację przed odwiedzeniem dzieci.

### P0-3. Wizualny generator starego typu wyłącza źródło w każdym punkcie

`RecipePage._sweep_node_from_generator()` buduje dla kolejnych punktów:

- `configure_keithley`;
- `configure_rigol`;
- `configure_anritsu_sg`;
- `configure_anritsu`.

Dowód: `app/ui/main_window.py:9614-9680`.

Adaptery mają poprawną konserwatywną politykę:

- `KeithleyAdapter.configure_source()` wymusza OUTPUT OFF
  (`app/devices/keithley/adapter.py:235-245`);
- `RigolAdapter.configure_channel()` wymusza OUTPUT OFF
  (`app/devices/rigol/adapter.py:301-316`);
- `AnritsuAdapter.configure_signal_generator()` wymusza RF OFF
  (`app/devices/anritsu/adapter.py:418-430`).

W rezultacie wizualnie zbudowany sweep źródła nie zachowuje energizacji pomiędzy punktami.
Działający przykład 10×100 omija ten problem ręcznie napisanym YAML:

- najpierw wykonuje pełne `configure_*`;
- następnie używa `update_keithley_level` i `update_rigol_frequency`, które zachowują
  OUTPUT;
- OUTPUT jest uzbrajany i włączany jeden raz.

Nowy provider miał tę semantykę uogólnić, ale nie istnieje. Dla amplitudy Rigola i SG
nie ma nawet odpowiednika bezpiecznego `update_*`.

### P0-4. Domyślna receptura otwierana przez Sweeps nie działa

`RecipePage` domyślnie ładuje `recipes/example_nested_sweep.yml`. Aktualny plik zawiera
nowy, niekompletny blok Keithley:

- `device_module: keithley`;
- `configuration_required: true`;
- `operation: configure_selected_parameters`;
- sweep bez zdefiniowanego ROI.

Kompilacja kończy się błędem providera. Jednocześnie README opisuje ten sam plik jako
działający 100×20, czyli 2000 widm. Dokumentacja, przykład startowy i implementacja
rozjechały się.

Skutek operatorski: pierwsza interakcja z zakładką prowadzi do planu, którego nie można
zatwierdzić ani uruchomić.

### P0-5. Brak produkcyjnej kwalifikacji fizycznej i profil fail-closed

Bieżący profil ma:

- `profile.state: unverified`;
- Keithley `allow_output_enable: false`;
- Anritsu `single_sweep_mode: unverified`;
- Anritsu `acquisition_allowed: false`;
- Anritsu `signal_generator_output_allowed: false`.

Lokalne raporty kwalifikacji są oznaczone `simulation: true`; dwa mają wynik
`simulation_passed`, jeden `failed`. Nie ma artefaktu potwierdzającego pełne fizyczne HIL.

To jest prawidłowe zachowanie fail-closed, ale oznacza brak gotowości produkcyjnej.

## 5. Defekty P1

### P1-1. Dwa aktywne modele edycji

Nowa biblioteka tworzy `device_module`, a menu kontekstowe nadal otwiera stary
`DeviceParameterDialog` / `SweepGeneratorDialog`. Obie ścieżki są aktywne i tworzą
inne dokumenty oraz inną semantykę wykonania.

Użytkownik nie ma czytelnej informacji, która ścieżka:

- jest nowa;
- jest wykonywalna;
- zachowuje OUTPUT;
- zapisuje właściwą oś thaTEC.

Należy pozostawić jedną ścieżkę produkcyjną.

### P1-2. UI oferuje osie, których backend i storage nie znają

Keithley oznacza jako sweepowalne:

- `source.level`;
- `source.compliance`;
- `measurement.settling_time`.

Jednak generowane cele dla dwóch ostatnich to:

- `keithley.<channel>.compliance`;
- `keithley.<channel>.settling_time`.

Nie występują one ani w `_SWEEP_DIMENSIONS`, ani w `_TARGETS` mappera thaTEC.
Samo dodanie providera nie wystarczy — kompilator, runner, adapter i storage wymagają
nowego, wspólnego descriptor contract.

### P1-3. Drag-and-drop Flow ignoruje miejsce upuszczenia

Dla `device:*` przekazywane są `parent_id`, `branch` i `index`. Dla `flow:*` kod:

1. zaznacza tylko właściciela;
2. wywołuje `_library_add_basic(kind)`;
3. ignoruje `branch` i `index`.

Dowód: `app/ui/main_window.py:7360-7382`.

Odtworzono:

- drop `Wait` na indeks `0` dopisał go na końcu;
- drop `Wait` do `if.else` dodał go do gałęzi `children`.

Jest to naruszenie podstawowej przewidywalności drzewa i może zmienić kolejność
sterowania aparaturą.

### P1-4. Edycja Anritsu usuwa istniejące akwizycje

`_configured_anritsu_node()` buduje dzieci z warunkiem:

```python
if child.type != "acquire_spectrum"
```

Następnie odtwarza akwizycję tylko, gdy `acquire_single` jest zaznaczone. Ten checkbox
jest obecnie ukryty i domyślnie wyłączony. Otworzenie i zapisanie konfiguracji Anritsu
może więc usunąć wcześniej przeciągnięty blok `Acquire spectrum once`.

Dowód: `app/ui/main_window.py:9139-9161`.

### P1-5. Preview ROI nie waliduje bezpieczeństwa urządzenia

`SweepGeneratorDialog` sprawdza:

- jednostkę i wymiar;
- points/step;
- linear/log;
- limit 100 000 punktów przy akceptacji.

Nie sprawdza:

- limitów stacji;
- limitów DUT;
- relacji start/stop Anritsu dla każdego punktu;
- mocy/compliance Keithley;
- zależności amplitudy, impedancji i prądu Rigola.

Stary kompilator waliduje wszystkie rozwinięte akcje i zatrzyma Run. Nowe drzewo może
jednak pokazać status `SWEEP` i `configuration_required: false` dla ROI przekraczającego
limity, po czym zatrzymać się dopiero na ogólnym braku providera. UI nie jest więc
prawdziwym preflightem.

### P1-6. Brak statycznej walidacji kolejności stanów

Kompilator potrafi skompilować:

- `update_keithley_level` bez wcześniejszego `configure_keithley`;
- `update_rigol_frequency` bez wcześniejszego `configure_rigol`;
- `set_*_output(enabled=true)` bez wcześniejszego `arm_*`.

Adaptery zatrzymają takie akcje w runtime, co jest bezpieczne, ale przeczy obietnicy
pełnego preflightu. Potrzebna jest analiza stanowa po wszystkich ścieżkach:

```text
configure → arm → output on → update/acquire → cleanup
```

### P1-7. Responsywność dużego ROI i planu

`_refresh_preview()` działa synchronicznie w wątku GUI, generuje pełny wektor i rysuje
każdy punkt z symbolem. Generator pozwala dojść do około miliona punktów, a limit
100 000 jest sprawdzany dopiero w `accept()`.

Kompilacja i estymacja również wykonują się synchronicznie w `RecipePage.compile_recipe()`.
Plan może rozwinąć do około miliona akcji. Skutkiem może być wielosekundowe zamrożenie
interfejsu, duże zużycie pamięci i brak możliwości anulowania.

Wymagane:

- limit przed generowaniem pełnego wektora;
- decymacja wykresu;
- worker dla compile/estimate;
- progres i anulowanie;
- wczesna estymacja iloczynu osi bez materializacji akcji.

### P1-8. Niepełna walidacja aplikowanego stanu urządzeń

Mocne strony:

- Rigol odczytuje waveform/frequency/high/low/output po podstawowej konfiguracji;
- update częstotliwości Rigola i poziomu Keithley ma readback i sprawdza stan OUTPUT;
- Anritsu base, advanced oraz SG wykonują readback;
- Anritsu single sweep używa `INIT:CONT OFF`, `INIT:IMM`, `*OPC?` i twardego deadline;
- connect/disconnect/E-STOP są konserwatywne.

Braki:

- `KeithleyAdapter.configure_source()` polega głównie na kolejce błędów; nie odczytuje
  po transakcji funkcji, compliance, NPLC, sense i zakresów;
- Rigol nie weryfikuje w podstawowym readbacku phase/duty/symmetry/pulse/load;
- Rigol modulation, hardware sweep i burst sprawdzają kolejkę błędów oraz OUTPUT OFF,
  ale nie pełny readback parametrów.

Nie można przez to stwierdzić „każde urządzenie jest prawidłowo zaprogramowane” bez HIL
i macierzy readback dla dokładnego modelu/firmware.

### P1-9. Ruff nie przechodzi

`python -m ruff check app tests` zgłasza:

- `app/ui/main_window.py:7910` — niezdefiniowane `_SWEEP_DIMENSIONS`;
- `app/ui/main_window.py:7999` — niezdefiniowane `_SWEEP_DIMENSIONS`.

W praktyce błąd ujawnia się dla wykonywalnego celu kompilatora, którego nie ma na liście
UI, np. aliasu `keithley.B.level`. Render drzewa może wtedy zakończyć się wyjątkiem.

## 6. Audyt budowania drzewa

### 6.1. Elementy wykonane dobrze

- Struktura YAML jest parsowana po każdej mutacji.
- Add/replace/delete/move są atomowe na poziomie źródła.
- Nie można usunąć ani przenieść korzenia.
- Przeniesienie do własnego potomka jest blokowane.
- Węzły nie mogą przechodzić do/ze `finally`.
- Identyfikatory węzłów muszą być unikalne.
- Undo/redo przechowuje pełne snapshoty źródła.
- Autosave/recovery i atomowy zapis pliku są zaimplementowane.
- Błąd renderowania nie nadpisuje poprzedniego drzewa ani YAML.
- ROI są widoczne jako klikalne podwiersze.
- Stałe ustawienie nie musi tworzyć sztucznej osi.
- YAML pozostaje dostępnym widokiem wtórnym i daje pełną inspekcję.
- Są alternatywy klawiaturowe dla Delete, Duplicate, Up/Down i Edit.
- Edycja planu nie komunikuje się z VISA.

### 6.2. Problemy drzewa i ergonomii

| Problem | Skutek |
|---|---|
| Drop Flow ignoruje branch/index | Inna kolejność lub gałąź niż wskazana przez operatora. |
| Po każdej mutacji `tree.clear()`, `expandAll()` i wybór korzenia | Utrata selekcji, scrolla i stanu rozwinięcia. |
| Brak Enter jako standardowej akcji edycji | Niespójność z dwuklikiem i `Open parameter editor`. |
| Duplicate działa tylko dla liścia | Nie można klonować kompletnego poddrzewa eksperymentu. |
| Brak Disable w menu kontekstowym | Użytkownik musi usuwać lub edytować YAML. |
| Edycja YAML nie odświeża drzewa do czasu kompilacji/load | Drzewo może prezentować starszą wersję; Run jest bezpiecznie wyłączony, lecz mentalny model jest niespójny. |
| Inspector pokazuje surowy JSON jako podstawowy detal | Dobry dla inżyniera, zbyt techniczny dla operatora. |
| Statusy nowych DeviceNode nie odpowiadają wykonalności | `SWEEP`/`FIXED` może oznaczać tylko poprawnie narysowany dokument. |
| Brak per-node pamięci rozmiaru/pozycji dialogu | Powtarzalna edycja dużego drzewa jest wolniejsza. |

### 6.3. Ocena kryteriów masterplanu

| Kryterium | Stan |
|---|---|
| Biblioteka zawiera urządzenia i ogólne Flow | PASS |
| Drop Keithley tworzy działający DeviceNode | **FAIL** |
| Dwuklik otwiera wspólny panel Keithley | PASS |
| Manual i Sweeps używają jednego pełnego modelu/snapshotu | PARTIAL |
| Stała wartość nie tworzy osi | PASS w starym modelu; nowy nie kompiluje |
| Wieloetapowy Keithley current | UI PASS, execution FAIL |
| Wykres pokazuje dokładny wektor i deduplikację granic | PASS |
| Niedozwolony punkt blokuje Run | PASS dla starego modelu; nowy bez walidacji urządzenia |
| Preview nie komunikuje się ze sprzętem | PASS |
| Provider kompiluje bez interpretacji pól przez Sweeps | **FAIL — provider nie istnieje** |
| Runner zmienia setpoint punktowo, bez hardware sweep | PASS tylko dla ręcznie napisanych `update_*` |
| OUTPUT pozostaje ON między punktami | FAIL dla generatora wizualnego |
| Cleanup działa dla wszystkich zakończeń | PASS programowo |
| Brak drugiej listy parametrów/limitów | **FAIL** |
| Fake provider bez zmian strony | **FAIL** |
| HDF5 odtwarza osie i widma w PyThat | PASS dla starego modelu |

## 7. Pokrycie parametrów Keithley

### 7.1. Macierz

| Parametr/operacja | Manual UI | Nowy DeviceNode UI | Wykonywalne dziś z drzewa wizualnego | Wykonywalne przez YAML | Oś automatyczna |
|---|---:|---:|---:|---:|---:|
| Kanał A/B | tak | tak | tylko stara ścieżka | tak | nie |
| Source mode current/voltage/measure-only | tak | tak | tylko stara ścieżka | tak | nie |
| Source level | tak | set/sweep | legacy, ale `configure` wyłącza OUTPUT | tak | tak; poprawne energetycznie tylko przez ręczne `update_keithley_level` |
| Compliance | tak | set/sweep | nie — DeviceNode zablokowany | fixed w `configure_keithley` | **nie w compiler/storage** |
| NPLC | tak | set | nie — DeviceNode zablokowany | fixed | nie |
| Settling time | tak | set/sweep | nie — DeviceNode zablokowany | fixed | **nie w compiler/storage** |
| Sense 2/4 wire | tak | set | nie — DeviceNode zablokowany | fixed | nie |
| Source autorange | tak | **brak selektora akcji** | nie | fixed | nie |
| Source range | tak | set | nie — DeviceNode zablokowany | fixed | nie |
| Measure V autorange | tak | **brak selektora akcji** | nie | fixed | nie |
| Measure V range | tak | set | nie — DeviceNode zablokowany | fixed | nie |
| Measure I autorange | tak | **brak selektora akcji** | nie | fixed | nie |
| Measure I range | tak | set | nie — DeviceNode zablokowany | fixed | nie |
| OUTPUT unchanged/on/off | tak | tak | nowy blok nie kompiluje; stary builder wystawia głównie OFF | tak z ARM | nie |
| Measure I/V/P | tak | osobny node | tak | tak | operacja, nie oś |
| Ramp | tak | tylko stary typ węzła | częściowo | tak | nie |

### 7.2. Wniosek Keithley

Panel wspólny jest dobrym krokiem, ale snapshot nie jest w pełni przenoszony do dokumentu.
Trzy checkboxy autorange są widoczne w panelu, a nie mogą zostać wybrane jako akcje. UI
obiecuje sweep compliance i settling time, mimo że dalsza warstwa nie zna takich osi.

Produkcja wymaga:

1. jednego descriptora wszystkich pól;
2. jawnych zależności mode/dimension;
3. kompilacji pełnej konfiguracji początkowej;
4. punktowego `update_source_level` zachowującego OUTPUT;
5. osobnej decyzji, czy compliance/settling mogą być osiami i jak są zapisywane w thaTEC;
6. readback pełnej konfiguracji.

## 8. Pokrycie parametrów Rigol

### 8.1. Macierz

| Grupa | Manual UI | Nowy DeviceNode | Receptura YAML | Oś |
|---|---:|---:|---:|---:|
| Kanał 1/2 | tak | tak | tak | wymiar w target |
| Waveform | tak | **brak** | fixed `configure_rigol` | nie |
| Frequency/period | tak | **brak** | fixed + `update_rigol_frequency` | frequency tak |
| High/Low | tak | **brak** | fixed | high/low tak, lecz pełne configure wyłącza OUTPUT |
| Vpp/offset | tak | **brak** | pośrednio przez high/low | nie jako natywne targety |
| Output load | tak | **brak** | fixed core config | nie |
| Phase | tak | **brak** | fixed core config | nie |
| Square duty | tak | **brak** | fixed core config | nie |
| Ramp symmetry | tak | **brak** | fixed core config | nie |
| Pulse width/edges | tak | **brak** | fixed core config | nie |
| Output polarity/mode/gate | tak | tylko on/off | **brak typu recipe** | nie |
| SYNC enable/polarity/delay | tak | **brak** | **brak typu recipe** | nie |
| Modulation wszystkie pola | tak | **brak** | **brak typu recipe** | nie |
| Wbudowany frequency sweep | tak manualnie | celowo brak | celowo brak | software sweep powinien go zastąpić |
| Burst wszystkie pola | tak | **brak** | **brak typu recipe** | nie |
| Phase sync | tak | **brak** | **brak typu recipe** | nie |
| ARM / OUTPUT | tak | policy, ale node nie kompiluje | tak w YAML | operacja |

### 8.2. Wniosek Rigol

To najsłabiej zintegrowane urządzenie w nowym Sweeps. Blok Rigol pozwala wybrać tylko kanał
i politykę OUTPUT. Nie używa wspólnego panelu ręcznego i nie pozwala skonfigurować przebiegu.

Stara lista osi obejmuje frequency/high/low obu kanałów. Tylko frequency ma bezpieczny,
punktowy update zachowujący OUTPUT, ale generator wizualny go nie używa. Dla High/Low nie
istnieje punktowy update z pełnym modelem prądu i readbackiem.

Nie należy dodawać sprzętowego sweepu Rigola do nowego drzewa. Należy zaimplementować
software loop z providerem i jawnie określić, które parametry mogą zmieniać się przy
OUTPUT ON.

## 9. Pokrycie parametrów Anritsu

### 9.1. Macierz

| Grupa | Manual UI | Nowy DeviceNode | Wykonywalne dziś | Oś |
|---|---:|---:|---:|---:|
| Start/Stop frequency | tak | set/sweep | legacy `configure_anritsu` | tak |
| Reference level | tak | set/sweep | legacy | tak |
| Trace points | tak | set | YAML fixed; nowy node nie kompiluje | nie |
| RBW mode/value | tak | set | YAML `configure_anritsu_advanced` | nie |
| VBW mode/value | tak | set | YAML advanced | nie |
| Detector | tak | set | YAML advanced | nie |
| Attenuation mode/value | tak | set | YAML advanced | nie |
| Preamplifier | tak | set | YAML advanced, firmware/HW gated | nie |
| Sweep-time mode/value | tak | set | YAML advanced | nie |
| SG frequency/power | tak | **brak w nowej bibliotece** | legacy/YAML | tak, ale configure wyłącza RF |
| SG ARM/OUTPUT | tak | **brak** | YAML | operacja |
| Acquire reference | tak | osobny blok | tak, single | nie |
| Acquire spectrum | tak | osobny blok | tak | nie |
| Averaging N× | tak manualnie | **brak** | brak równoważnej akcji recipe | nie |
| Reference processing | tak manualnie | brak edytora bloku acquisition | YAML | nie |
| Trace selection | praktycznie TRAC1 | blok bez edytora | TRAC1 kwalifikowany | nie |

### 9.2. Wniosek Anritsu

Podstawowa i zaawansowana konfiguracja adaptera jest dobrze zabezpieczona i ma readback.
Nowy dialog pokazuje większość pól, ale:

- nie ma providera;
- zaawansowany snapshot nie jest pobierany ze strony ręcznej;
- można wybrać niespójnie `RBW mode` bez `RBW value` lub odwrotnie;
- edycja może usunąć dzieci acquisition;
- generator SG pozostaje tylko w starej ścieżce;
- brak automatyzacji averagingu;
- profile i firmware lokalnie blokują akwizycję oraz SG.

## 10. thaTEC / PyThat / HDF5

### 10.1. Co jest udowodnione

Pełne testy potwierdzają dla starego modelu:

- zagnieżdżone osie w kolejności rodzic → dziecko;
- linear/log/piecewise i dokładne wartości SI;
- deduplikację wspólnych granic ROI;
- stałą kontrolkę jako indicator, a nie wymiar;
- dodatkową oś dla repeat i wielu acquisition;
- surowe i przetworzone widma;
- completed/aborted/faulted;
- transakcyjny rollback checkpointu;
- przebieg 100×20 = 2000 kompletnych widm;
- odczyt przez rzeczywisty `PyThat.MeasurementTree` 0.2.14.

To jest mocna lokalna baza.

### 10.2. Ograniczenia zgodności

1. Mapper rozpoznaje tylko `_TARGETS` starego modelu. Nowe compliance/settling i przyszłe
   descriptory nie są mapowane.
2. Nieznany lub niejednoznaczny topology powoduje fallback do osi `Checkpoint`. Dane nie
   giną, ale semantyka eksperymentu staje się mniej użyteczna.
3. `Hdf5RunWriter.close()` wykonuje walidację manifestu bez `require_pythat=True`.
   Rzeczywisty PyThat round-trip jest obowiązkowy w testach, nie w runtime.
4. PyThat 0.2.14 jest w `project.optional-dependencies.dev`, nie w zależnościach
   produkcyjnych.
5. Dla każdej osi segmentowej mapper ustawia `spacing="piecewise"`, natomiast writer
   zapisuje równanie `"log(x)"` dla wszystkiego, co nie jest dokładnie `"linear"`
   (`app/storage/thatec_writer.py:571`). Dane współrzędnych są jawne i testy przechodzą,
   ale metadana równania jest nieprawdziwa.
6. Brak dowodu importu do docelowego laboratoryjnego systemu inwentaryzacji.
7. Nie ma zatwierdzonej polityki wielu trace/kanałów w publicznym drzewie.
8. Ostrzeżenie binarnej zgodności NumPy wymaga zamrożenia i ponownej kwalifikacji
   środowiska.

### 10.3. Werdykt zgodności

**Zgodność lokalna:** PASS dla przetestowanych, starych receptur i PyThat 0.2.14.  
**Zgodność nowego drzewa:** NOT IMPLEMENTED.  
**Zgodność z zewnętrznym thaTEC/inventory:** EVIDENCE MISSING.  
**Gotowość do długoterminowej reprodukcji:** PARTIAL, do czasu przypięcia pełnego
środowiska i runtime validation przez PyThat.

## 11. UI/UX, ergonomia i design

Ocena została wykonana według zasad: natychmiastowa odpowiedź, przewidywalność, agency,
familiarity, spatial consistency, prostota, elastyczność, craft, dostępność i ograniczanie
ryzyka. W aplikacji pomiarowej przewidywalność i bezpieczeństwo mają pierwszeństwo przed
animacją lub dekoracją.

### 11.1. Mocne strony

- Trójkolumnowy układ Library → Tree → Inspector dobrze odwzorowuje zadanie.
- Biblioteka grupuje urządzenia, acquisition i flow.
- Wyszukiwanie biblioteki jest proste i użyteczne.
- Statusy mają tekst, nie opierają się wyłącznie na kolorze.
- Ikony urządzeń pomagają skanować duże drzewo.
- ROI jest edytowane tabelarycznie i pokazuje dokładne punkty.
- Wąski dialog ROI zmienia orientację splittera.
- Drzewo ma czytelne kolumny name/role/status.
- Inspector daje zarówno podsumowanie, jak i pełne pola.
- Undo/redo, autosave, recovery i atomowy zapis budują zaufanie.
- `Validate & preview` nie dotyka urządzeń.
- `Run plan` jest wyłączony po każdej zmianie do ponownej kompilacji.
- Operacje ręczne i uruchomienie są rozdzielone w workerach.

### 11.2. Problemy UI/UX

#### Prawdziwość informacji

Największym problemem UX nie jest wygląd, lecz obietnica. Status `SWEEP`, liczba punktów
i kompletne ROI sugerują gotowość, mimo że blok nie ma providera. Interfejs powinien
pokazywać globalny banner:

> Device blocks are preview-only; execution is unavailable.

albo nie udostępniać ich w produkcyjnym buildzie.

#### Spójność językowa

Większość aplikacji jest angielska, ale nowe selektory używają:

- `Bez zmian`;
- `Ustaw`;
- `Przejdź do ROI`.

Mieszanie języków w jednym dialogu obniża profesjonalizm i przewidywalność. Należy
wprowadzić jeden katalog tłumaczeń.

#### Motyw i design system

RecipePage i ROI mają liczne twardo wpisane kolory `#ffffff`, `#17212b` itd. W trybie
dark powstają jasne wyspy i niespójność z globalnym motywem. Style są powielone lokalnie
zamiast korzystać z semantycznych tokenów `surface`, `text`, `accent`, `warning`,
`selection`.

Brakuje także wyraźnego stanu `pressed` dla kart biblioteki i komend; hover jest,
natomiast natychmiastowa informacja na pointer-down jest słaba.

#### Responsywność układu

Test obejmuje 1900×850 i 1050×720. Dla szerokości poniżej około 900 px layout nadal
utrzymuje trzy kolumny o minimach 210 + 390 + 280. Nie ma:

- zwijania Library;
- chowania Inspector do drawer;
- trybu jednopanelowego;
- adaptacji do wysokiego skalowania DPI.

Dialog Anritsu ma minimum 900×620, co może nie mieścić się na 1024×600 lub przy dużym
skalowaniu tekstu.

#### Hierarchia akcji

- `Run plan` jest umieszczony w wierszu ścieżki pliku, a nie w wyraźnym obszarze
  preflight/run.
- Etykieta `Recipe file` znajduje się po polu, co osłabia mapowanie label→control.
- Dolne podsumowanie pokazuje dane dopiero po kompilacji; brakuje stale widocznego
  `blockers / warnings / points / spectra / time / size`.
- Niedokończone urządzenia są równie prominentne jak działające flow.

#### Dostępność

Plusy: tekstowe etykiety, accessible names dla QPushButton/QLineEdit, skróty klawiaturowe,
statusy tekstowe.

Braki:

- brak kompletnej obsługi Enter/Space dla edycji bieżącego węzła;
- brak testu screen reader dla syntetycznych ROI rows;
- twarde minima dialogów utrudniają duży tekst;
- brak zachowania focus/selection po przebudowie drzewa;
- kolorowe podkreślone ROI wyglądają jak link, ale nie mają pełnej semantyki linku.

### 11.3. Rekomendowany model interakcji

1. Drop urządzenia tworzy blok `Unconfigured` i natychmiast otwiera edytor.
2. Cancel usuwa nowy pusty blok albo pozostawia go jawnie disabled.
3. Apply tworzy kompletny, walidowany snapshot i provider plan.
4. Każdy parametr ma dokładnie `Unchanged / Set / Axis`, jeśli descriptor na to pozwala.
5. `Axis` pokazuje limit, unit, ROI, liczbę punktów i walidację inline.
6. Drzewo zachowuje selection/scroll/expanded state po zmianie.
7. Dolny pasek zawsze pokazuje stan preflight.
8. `Run` jest możliwy tylko dla planu bez blockerów i z aktualnym hashem.

## 12. Bezpieczeństwo wykonania

### 12.1. Elementy mocne

- Profile domyślnie blokują energizację.
- Adaptery wyłączają outputs przy connect/configure/disconnect.
- OUTPUT ON wymaga ARM i krótkiego TTL.
- DUT limits są osobne od station limits.
- Kompilator rozwija stare osie i sprawdza każdy punkt.
- Adaptery ponownie walidują request.
- Run ma retry policy, watchdog, stop/pause i safe checkpoint.
- Safe shutdown obejmuje całą stację, nie tylko urządzenia użyte w planie.
- Compliance i błędy wymuszają bezpieczny stan.
- HDF5 ma durable events i partial/faulted status.

### 12.2. Braki przed produkcją

- provider DeviceNode z analizą stanu;
- pełny HIL i fault injection na fizycznych urządzeniach;
- readback wszystkich programowanych parametrów;
- jawna polityka zmian dozwolonych przy OUTPUT ON;
- statyczne sprawdzenie configure/arm/on/update;
- zatwierdzone IDN/serial/firmware/options;
- zatwierdzony tor DUT i RF;
- test soak oraz zerwanie VISA podczas punktu;
- kwalifikacja wydajności 100k punktów;
- integracja nowego drzewa z recovery/resume i HDF5.

## 13. Luki testowe

Pomimo 293 zielonych testów brakuje następujących testów krytycznych:

1. `configuration_required=true` blokuje kompilację dzieci.
2. Nowy Keithley DeviceNode kompiluje do pełnej konfiguracji i punktowych update.
3. Nowy Rigol DeviceNode kompiluje carrier/output i punktową frequency.
4. Nowy Anritsu DeviceNode kompiluje base/advanced/acquisition bez utraty dzieci.
5. Drop Flow zachowuje branch oraz index.
6. Edycja Anritsu zachowuje osobny `acquire_spectrum`.
7. Wszystkie registry UI/compiler/storage mają ten sam zestaw targetów.
8. Każdy descriptor ma test fixed/set/axis/read-only.
9. Wszystkie ROI points przechodzą walidator urządzenia przed Run.
10. OUTPUT pozostaje ON między dwoma kolejnymi punktami z nowego drzewa.
11. Visual builder generuje `update_*`, nie pełne `configure_*`, gdy wymagana jest
    ciągłość energizacji.
12. Compile 100k punktów nie blokuje event loop.
13. Dark/light/high-DPI i duży tekst dla całego Sweeps.
14. PyThat round-trip jest wykonywany w ścieżce release/runtime.
15. Fizyczny HIL z podpisanym raportem.

## 14. Rekomendowany plan naprawczy

### Etap A — natychmiastowe zabezpieczenie, 1–2 dni

1. Blokować każdy `configuration_required` przed odwiedzeniem dzieci.
2. Dodać jawny badge `PREVIEW ONLY / NOT EXECUTABLE` dla DeviceNode.
3. Przywrócić działającą domyślną recepturę lub zmienić README i default path.
4. Naprawić Flow drop branch/index.
5. Zachować acquisition children przy edycji Anritsu.
6. Naprawić dwa błędy Ruff.
7. Dodać testy regresyjne dla powyższych przypadków.

### Etap B — jeden kontrakt parametrów, 3–5 dni

1. Utworzyć `ParameterDescriptor` i `DeviceSweepProvider`.
2. Usunąć trzy niezależne mapy targetów.
3. Descriptor powinien zawierać:
   - stabilne `parameter_id` i `axis_target`;
   - typ/wymiar/jednostkę;
   - fixed/sweepable/read-only;
   - limit provider/DUT;
   - zależności od mode/firmware/options;
   - funkcję walidacji i formatowania;
   - mapowanie thaTEC.
4. Wydzielić Sweeps z `main_window.py`.
5. Usunąć starą aktywną ścieżkę dialogów po migracji.

### Etap C — provider Keithley, 4–7 dni

1. Zapisać pełny snapshot konfiguracji początkowej.
2. Kompilować fixed parameters do jednego `configure_keithley`.
3. Kompilować source-level axis do `update_keithley_level`.
4. Ustalić politykę compliance/settling axes.
5. Walidować wszystkie ROI points.
6. Dowieść OUTPUT ON między punktami.
7. Dodać readback configuration.
8. Zintegrować thaTEC axis i fixed indicators.

### Etap D — provider Rigol, 5–8 dni

1. Udostępnić wspólny panel carrier/output.
2. Frequency axis kompilować do `update_rigol_frequency`.
3. Dla amplitudy zaimplementować bezpieczny update albo jawnie oznaczyć jako
   `requires_output_cycle`.
4. Nie używać hardware sweep.
5. Oddzielić parametry manual-only: modulation/burst/sync, dopóki nie powstaną
   typy recipe i testy.
6. Dodać pełny readback.

### Etap E — provider Anritsu, 4–7 dni

1. Wspólny snapshot base + advanced + SG.
2. Walidować grupy mode/value atomowo.
3. Zachować osobny acquisition node.
4. Dodać politykę averaging/reference processing.
5. Rozstrzygnąć, czy SG axis może aktualizować frequency/power przy RF ON.
6. Rozszerzyć mapper thaTEC.

### Etap F — UX i wydajność, 3–5 dni

1. Zachować selection/expanded/scroll.
2. Przenieść compile/estimate do workera.
3. Limitować i decymować preview.
4. Wprowadzić semantyczne tokeny light/dark.
5. Ujednolicić język.
6. Dodać collapse/drawer dla Library i Inspector.
7. Dodać inline errors oraz stale widoczny preflight bar.

### Etap G — kwalifikacja release, zależna od laboratorium

1. Zamrozić środowisko Python/NumPy/xarray/h5py/PyThat.
2. Usunąć warning binary ABI.
3. Uruchomić fizyczny HIL dla dokładnych IDN/serial/firmware/options.
4. Wykonać 1 punkt, 2×2, 100×20, stop, timeout, compliance, disconnect, storage fault.
5. Wykonać PyThat i zewnętrzny inventory round-trip.
6. Podpisać raport przez osobę odpowiedzialną.

## 15. Minimalne kryteria dopuszczenia do produkcji

Release Sweeps może otrzymać status produkcyjny dopiero, gdy:

- istnieje jedna aktywna ścieżka konfiguracji;
- każdy blok z biblioteki albo kompiluje się, albo jest jawnie disabled;
- żaden `configuration_required` nie przepuszcza dzieci;
- wszystkie descriptor registry są jednym kontraktem;
- każdy punkt osi przechodzi station ∩ DUT ∩ recipe validation;
- provider generuje punktowe update zachowujące stan OUTPUT tam, gdzie jest to wymagane;
- wszystkie fixed/sweep/read-only parametry mają macierz pokrycia;
- drzewo zachowuje branch/index i nie traci dzieci;
- Ruff, testy, compileall i diff-check są zielone;
- runtime/release wykonuje PyThat round-trip;
- zewnętrzny system przyjmuje plik;
- profil jest approved;
- fizyczny HIL jest zaliczony i podpisany.

## 16. Końcowa rekomendacja

Nie rozszerzać teraz UI o kolejne parametry ani kolejne urządzenia. Najpierw należy
zamknąć kontrakt `DeviceSweepProvider` dla Keithley i usunąć rozjazd między dokumentem,
kompilatorem, runnerem i storage. Obecny design wizualny warto zachować jako bazę, ale
statusy muszą odzwierciedlać wykonalność, a nie tylko poprawność renderowania.

Najbezpieczniejsza kolejność:

1. naprawić P0/P1 drzewa;
2. uruchomić jeden pełny provider Keithley end-to-end;
3. potwierdzić ciągłość OUTPUT i PyThat;
4. dopiero potem przenieść wzorzec na Rigol i Anritsu;
5. na końcu wykonać fizyczną kwalifikację stanowiska.

Do tego czasu działające, ręcznie zweryfikowane receptury starego typu mogą służyć do
symulacji i rozwoju, ale nowy DeviceNode nie powinien być prezentowany operatorowi jako
produkcyjny mechanizm budowania sweepu.
