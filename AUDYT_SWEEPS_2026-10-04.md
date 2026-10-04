# Audyt sweepów i powiązanego wykonania pomiarów — 4 października 2026

**Werdykt: kod umożliwia wykonanie żądanej topologii pomiaru, ale nie spełnia jeszcze całego kontraktu „wirtualnego pilota”. Są potwierdzone błędy kompilowania, zachowywania konfiguracji, potwierdzeń nastaw, jednostek eksportu i odzyskiwania pomiarów. Nie można uznać całej implementacji sweepów za poprawną ani zakwalifikować dużego pomiaru sprzętowego na podstawie obecnych testów.**

Wykonałem przegląd parsera i drzewa semantycznego, generatorów ROI, kompilatora, providerów urządzeń, runnera, adapterów, polityk bezpieczeństwa, kontrolera wykonania, projekcji na karty urządzeń, HDF5/CSV, eksportu thaTEC/PyThat, estymacji i recovery. Dodałem odtwarzalne testy diagnostyczne oraz pokazane i wyrenderowane strony Fluent. Raport dotyczy zastanego drzewa roboczego, zawierającego wcześniejsze zmiany użytkownika; bazowy HEAD: `c6697e2cee78277cd9125e8cc2a9e63c64ccc4a4`. To audyt, nie wdrożenie opisanych napraw.

Wszystkie wykonane połączenia z aparaturą pochodziły z symulatorów. Nie włączałem rzeczywistych wyjść, nie zmieniałem lokalnych uprawnień sprzętowych ani profili bezpieczeństwa. Odczytałem lokalne ustawienia, żeby odróżnić rzeczywistą konfigurację stanowiska od szablonu dystrybucyjnego. Podlinkowany katalog `.superpowers/brainstorm/` zawiera materiał HTML dotyczący wcześniejszego układu VISA, a nie dodatkowe instrukcje audytu.

## 1. Twój konkretny pomiar

Przyjąłem następującą kolejność pętli:

1. MOKEbox **VOUT0: 0–100 mV** — pętla zewnętrzna.
2. Keithley **B: −80–+80 mA** — dla każdego punktu MOKEbox.
3. Keithley **A: +0,2–+1,4 mA** — dla każdej pary MOKEbox/B.
4. Po ostatniej zmianie nastawy: **Wait 3 s**, kontrolne odczyty A/B i opcjonalnie Hall, następnie jedno nowe spektrum.

Zapis „A - 0.2 mA do 1.4 mA” potraktowałem jako separator przed dodatnim 0,2 mA. Jeżeli pierwsza wartość ma wynosić **−0,2 mA**, trzeba zmienić początek osi. Nie podałeś kroku ani liczby punktów, częstotliwości analizatora, compliance i sposobu uśredniania. Dlatego załączona receptura jest scenariuszem testowym, nie gotowym ustawieniem rzeczywistego eksperymentu.

### Co zostało potwierdzone wykonaniem

Scenariusz **2 × 3 × 3 = 18 punktów**, obejmujący krańce wszystkich trzech przedziałów, skompilował się i wykonał przez produkcyjny runner i adaptery podłączone do symulatorów. Plan zawierał 123 akcje: dwie początkowe konfiguracje Keithleya, dwa ustawienia MOKEbox, sześć zmian B i osiemnaście zmian A. Prądy były zmieniane dedykowanym `update_keithley_level`, bez ponownej pełnej konfiguracji kanałów w tej konkretnej recepturze.

Powstał HDF5 ze statusem `completed`, 18 kompletnymi checkpointami, 18 widmami, 18 surowymi zapisami akwizycji, 523 zdarzeniami i pustym `_pending`. Kolejność punktów odpowiada iloczynowi kartezjańskiemu; zapisano odczyty obu SMU i Hall. Plik przeszedł walidator kompatybilności z rzeczywistym odczytem PyThat. Artefakt: [cartesian-simulation.h5](docs/audits/2026-10-04-sweeps/cartesian-simulation.h5), wyciąg: [evidence.json](docs/audits/2026-10-04-sweeps/evidence.json).

Osobna kontrola [CSV](docs/audits/2026-10-04-sweeps/cartesian-simulation.csv) potwierdziła 18 wierszy oraz identyczne nastawy i pomiary jak w odpowiadających checkpointach HDF5. CSV jest podsumowaniem: nie zawiera pełnych widm, snapshotów i wszystkich metadanych. Archiwum naukowe należy zachować w HDF5.

W tym teście 18 oczekiwań po 3 s zastąpiono rejestratorem czasu żądanego, żeby sprawdzić kolejność bez opóźniania całej próby. **Osobny test rzeczywiście czekał co najmniej 3 s i sprawdził natychmiastowe przerwanie oczekiwania.** Nie jest to test fizycznej stabilizacji układu ani pełny pomiar trwający 54 s.

Istotne ograniczenie dowodu: test używa syntetycznego compliance 20 mV i modelu obciążenia symulatora. Dla nastawy B −80 mA model osiąga compliance i zapisuje około **−2 mA zmierzonego prądu**, mimo prawidłowo zaprogramowanego poziomu −80 mA. Test potwierdza sterowanie poziomem źródła, geometrię pętli i zapis, a nie osiągnięcie zadanego prądu w rzeczywistym DUT. W archiwum te dwa rodzaje wartości są rozdzielone.

### Zgodność z lokalną konfiguracją stanowiska

Odczyt `.config/settings.yml` wykazał:

| Właściwość | Stan lokalny | Znaczenie dla pomiaru |
|---|---|---|
| MOKEbox | włączony; protokół zakwalifikowany; sterowanie VOUT dozwolone; główny zatwierdzony profil kanału **0** | Błąd profili dodatkowych SW-01 nie blokuje obecnego VOUT0 |
| Keithley A | włączony; źródło −10…+10 mA; stały zakres 10 mA | +0,2…+1,4 mA mieści się w granicach |
| Keithley B | włączony; źródło −150…+150 mA; stały zakres 1 A | −80…+80 mA mieści się w granicach |
| OUTPUT Keithleya | włączenie dozwolone | Receptura nadal musi jawnie określić włączenie |
| Anritsu | akwizycja dozwolona; `standard_scpi_opc` | Lokalny profil różni się od niezakwalifikowanego szablonu |
| Compliance | `warn_clamp`; `stop_on_compliance: false` | Osiągnięcie compliance może ograniczyć prąd i pozostawić pomiar w toku |
| Trip prądowy B | skonfigurowany, ale wyłączony | Nie wolno zakładać działającego programowego progu prądowego B |

Lokalne limity compliance to A 10–670 mV i B 10–710 mV; limity mocy to odpowiednio 10 mW i 105 mW. Dla krańców żądanego prądu oraz najwyższego dozwolonego compliance iloczyny wynoszą A 0,938 mW i B 56,8 mW. To sprawdzenie arytmetyczne limitów programu, **nie dobór bezpiecznych nastaw DUT i nie kwalifikacja okablowania**. Szablon ustawień ma inne ograniczenia i nie pozwala bez zmian wykonać tego samego eksperymentu.

### Ile punktów i czasu

Liczba rejestracji wynosi `N_MOKE × N_B × N_A × average_count`. Przy `average_count = 1` liczba checkpointów widmowych równa się liczbie kombinacji. Kroki w tabeli są tylko przykładami, nie przyjętym wyborem eksperymentalnym.

| Przykład | Punkty MOKE/B/A | Kombinacje | Samo oczekiwanie 3 s przed każdym widmem |
|---|---:|---:|---:|
| Próba audytowa | 2 / 3 / 3 | 18 | 54 s |
| Kroki 10 mV / 10 mA / 0,2 mA | 11 / 17 / 7 | 1 309 | 1 h 5 min 27 s |
| Kroki 1 mV / 1 mA / 0,1 mA | 101 / 161 / 13 | 211 393 | 176 h 9 min 39 s |

Do tego dochodzą akwizycja, pomiary kontrolne, rampy MOKEbox, komunikacja i zapis. Jeżeli liczyć wyłącznie przerwy pomiędzy widmami, zamiast stabilizacji również przed pierwszym, odejmuje się 3 s. Obecna estymacja rozmiaru i ograniczenie ekspansji nie zapewniają wiarygodnego zabezpieczenia dużego planu — SW-09 i SW-19.

## 2. Najważniejsze ustalenia

`P1` oznacza błąd wymagający naprawy przed deklaracją poprawnego działania danego scenariusza: zmienia konfigurację poza wyborem użytkownika, pomija sterowanie, daje mylący dowód lub zagraża integralności pomiaru. `P2` oznacza ograniczenie funkcji, precyzji, prezentacji lub walidacji. Priorytet jest techniczny, nie jest oceną ryzyka konkretnego DUT.

„Test” oznacza wykonane odtworzenie problemu. „Kod” oznacza bezpośredni przegląd ścieżki; zakres tego dowodu podano w opisie. Znane usterki mają testy `xfail(strict=True)`: **to oczekiwane niepowodzenia kontraktu, a nie potwierdzenie poprawności**.

| ID | Priorytet | Ustalenie | Dowód |
|---|---|---|---|
| SW-01 | P2 | Compiler/provider MOKEbox ignorują zatwierdzony profil dodatkowego kanału | test + kod |
| SW-02 | P1 | „Configure selected parameters” wysyła pełne konfiguracje i domyślne, nie tylko wybrane zmiany | test Anritsu + kod Keithley/Rigol |
| SW-03 | P1 | Jawna oś analizatora gubi konfigurację i generuje niekompatybilny obiekt | dwa testy |
| SW-04 | P1 | Oś A może usunąć jawne ustawienie B, bo porównuje tylko typ akcji | test + kod drzewa |
| SW-05 | P1 | Wyjątki normalizacji są połykane; niedozwolone zagnieżdżenie osi przechodzi | test |
| SW-06 | P1 | Oś bez konfiguracji może zapisać nastawy, nie wysyłając ich do urządzenia | test |
| SW-07 | P1 | Powrót ROI traci indeks punktu i etapu przy powtarzających się wartościach | test |
| SW-08 | P2 | Sweep czasu stabilizacji wykonuje inne czasy niż zapisuje; samo pole settling nie gwarantuje Wait | test + kod |
| SW-09 | P1 | Limit punktów nie jest egzekwowany jako limit punktów; iloczyn powstaje przed ochroną | test + kod |
| SW-10 | P2 | ROI dopuszcza ciche obcięcie niecałkowitej liczby punktów | dwa testy |
| SW-11 | P1/P2 | MOKEbox ma rozbieżne requested/applied/readback; rampa akceptuje wcześniejszy kod DAC | dwa testy + archiwum |
| SW-12 | P1 | Nastawy surowych widm, checkpointów i osi publicznych mają różną semantykę | test + archiwum |
| SW-13 | P1 | Błąd flush cofa punkt na dysku, ale nie cofa licznika w pamięci | fault injection |
| SW-14 | P1 | Akwizycja nadpisuje potwierdzenie osi i zapisuje `verification: readback` bez readback | test + archiwum |
| SW-15 | P1 | Recovery błędnie liczy checkpointy czujników i nie odtwarza pełnego stanu | test licznika + kod |
| SW-16 | P1 | Literówki pól konfiguracji mogą zniknąć bez błędu | test |
| SW-17 | P2 | Karty nie odzwierciedlają wszystkich potwierdzonych parametrów; rampa nie wysyła postępu do UI | test renderowania + kod |
| SW-18 | P1 | Eksport przypisuje niektórym skalarnym wielkościom błędne jednostki | trzy testy |
| SW-19 | P1 | Deklarowana górna estymacja HDF5 jest mniejsza od otrzymanego pliku | test + rzeczywisty plik |
| SW-20 | P1 | Odłączony adapter może zostać uznany za potwierdzone bezpieczne wyłączenie | test |

## 3. Szczegółowe problemy konfiguracji i kompilowania

### SW-01 — profil dodatkowego kanału MOKEbox

Źródła: [compiler.py](app/engine/compiler.py) — gałąź `configure_moke_box`; [sweep_provider.py](app/devices/moke_box/sweep_provider.py) — walidacja wiązania. Obie ścieżki pobierają `control_profile_from_settings(...)` bez docelowego kanału, a następnie porównują kanał receptury z profilem głównym. Adapter poprawnie potrafi pobrać niezależny profil `get_control_profile(0)`.

Odtworzenie: główny profil kanału 2 i osobny zatwierdzony profil kanału 0. Adapter akceptuje profil 0; kompilator odrzuca sweep 0. **Obecny lokalny profil główny jest kanałem 0, więc ten konkretny wariant u Ciebie przechodzi.** Naprawa powinna przekazywać kanał do wspólnej walidacji profilu; nie należy automatycznie przepinać profilu głównego ani rozszerzać uprawnień.

### SW-02 — wybór pojedynczego parametru nie oznacza pojedynczej zmiany

Źródła: [compiler.py](app/engine/compiler.py) — `_visit_keithley_device_node` od ok. 934, `_visit_anritsu_device_node` od ok. 1632; [adapter Keithleya](app/devices/keithley_2600/adapter.py) — `configure_source`; [adapter Anritsu](app/devices/anritsu_ms2830a/adapter.py) — `configure_spectrum`.

Blok Keithleya zaczyna od pełnego snapshotu konfiguracji, uzupełnia go wartościami domyślnymi i nakłada wybrane wiersze. Zwykle generuje pełne `configure_keithley`, które wyłącza OUTPUT i ustawia funkcję źródła, compliance, zakresy, sense, poziom oraz NPLC. W zagnieżdżonym drzewie taki blok może wykonać tę pełną konfigurację wielokrotnie. Wybór „zmieniam tylko prąd” nie zapewnia wtedy zachowania pozostałych parametrów aktualnego urządzenia.

Blok Anritsu generuje konfigurację podstawową również bez wybranych zmian podstawowych. Wybranie tylko detektora tworzy konfigurację zaawansowaną z domyślnym RBW auto, VBW auto, attenuation auto, preamp OFF i sweep time auto. Test z jedynym wybranym `advanced.detector: POS` potwierdził niezamierzone uzupełnienie innych ustawień. Konfiguracja podstawowa dodatkowo ustawia m.in. tryb VBW, typ śladu i tryb inicjacji. Podobne pełne odtworzenie snapshotu występuje w bloku Rigola.

Dedykowane `update_keithley_level` jest znacznie bliższe wymaganemu kontraktowi: sprawdza poziom i OUTPUT, a mutacja dotyczy poziomu źródła. Tak właśnie przygotowano pozytywny scenariusz audytowy. Nie dowodzi to poprawności wszystkich receptur tworzonych przez edytor.

`output_policy: continue` wymaga dopasowania pełnego oczekiwanego stanu, w tym pierwszej wartości ROI. Po poprzednim przejściu osi prąd pozostaje na wartości końcowej; ponowne wejście w taki blok nie musi spełnić warunku. `on_keep` nie eliminuje pełnej początkowej konfiguracji. Nie należy traktować żadnej z tych opcji jako ogólnego obejścia błędu.

**Naprawa:** rozdzielić jednorazową, jawną konfigurację początkową od operacji zmiany konkretnych pól; używać typowanych zmian z maską pól. Każda domyślna decyzja operacyjna analizatora powinna być jawna w kontrakcie akwizycji i widoczna w planie/UI. Testować listę rzeczywiście wysłanych komend oraz zachowanie pól niewybranych.

### SW-03 — sweep analizatora ma hardcoding i kończy się błędem adaptera

Źródła: [AnritsuSweepProvider](app/devices/anritsu_ms2830a/sweep_provider.py), `_SpectrumConfig` i `compile_point`; [compiler.py](app/engine/compiler.py), `_remember_literal_configuration` ok. 2037.

Po jawnej konfiguracji 300 MHz–6 GHz, 10 001 punktów, sweep samego reference level generuje konfiguracje **1 MHz–2 MHz, 1 001 punktów**. Kompilator nie zapamiętuje konfiguracji widmowej Anritsu w kontekście providera, a provider używa stałych zastępczych.

Osobny test wykonania wykazał drugą usterkę: prywatny `_SpectrumConfig` providera nie zawiera pól wymaganych przez adapter, np. `rbw_auto`. Runner kończy się błędem atrybutu zamiast akwizycją. Analogicznie dla SG provider ma wartości zastępcze 1 MHz/−30 dBm, gdy nie ma kontekstu partnera; to również wymaga usunięcia jako niejawnej konfiguracji.

**Naprawa:** jedna wspólna typowana konfiguracja analizatora, pełny potwierdzony baseline i zmiana wybranego pola. Brak niezbędnego baseline powinien oznaczać błąd preflight, zamiast nastaw zastępczych.

### SW-04 — jawna zmiana innego kanału znika

Źródła: [compiler.py](app/engine/compiler.py), `_axis_update_matches` ok. 170 i gałąź sweep; [semantic_tree.py](app/recipes/semantic_tree.py), budowanie dzieci osi.

Oś prądu A zawierająca jawny `update_keithley_level` dla B usuwa aktualizację B z planu. Kompilator usuwa dzieci na podstawie samego rodzaju akcji, pomijając kanał, target i tożsamość operacji. Drzewo semantyczne ma podobną filtrację: może ukryć odpowiednią czynność przed operatorem.

**Naprawa:** scalać tylko jednoznacznie przypisaną operację obsługującą tę samą oś i to samo urządzenie/kanał. Dzieci dotyczące innych kanałów muszą pozostać i być widoczne. Test obejmuje oczekiwane dwie zmiany B, których obecny plan nie zawiera.

### SW-05 — błędy normalizacji nie zatrzymują kompilacji

Źródło: [compiler.py](app/engine/compiler.py), `compile` ok. 272. Ogólne `except Exception` zeruje drzewo semantyczne i wiązania, po czym kontynuuje starszą ścieżką.

Zagnieżdżenie dwóch aktywnych osi `keithley.B.current` poprawnie wywołuje `ConfigurationError` w normalizatorze, ale ten sam dokument jest akceptowany przez kompilator i rozwijany. To narusza walidację planu, nie jest wyłącznie utratą wyglądu drzewa.

**Naprawa:** propagować błędy receptury; ewentualna obsługa starszego formatu musi rozpoznawać format jawnie, a nie traktować każdego wyjątku jako zgodę na pominięcie walidacji.

### SW-06 — checkpoint może udawać wykonany sweep

Źródło: [compiler.py](app/engine/compiler.py), `_binding_configured` i gałąź sweep.

Oś fizycznego prądu B bez wcześniejszej konfiguracji, z samymi checkpointami w środku, zapisuje zadane wartości w kontekście, ale nie generuje zmian poziomu źródła. Plan może wyglądać jak pomiar wielu nastaw, chociaż aparatura nie została ustawiona.

**Naprawa:** fizyczna oś wymaga jawnego baseline, walidacji i akcji nastawienia albo preflight kończy się błędem. Wirtualne zmienne pętli muszą mieć osobny typ, który nie podszywa się pod nastawę urządzenia.

### SW-07 — błędna identyfikacja punktów przy powrocie ROI

Źródła: [compiler.py](app/engine/compiler.py), dobór `point_index`, `_semantic_axis_action`, `_legacy_axis_context`; [semantic_tree.py](app/recipes/semantic_tree.py), `collect_contexts`.

W ROI `0 → 1 mA → 0`, po usunięciu wspólnego krańca etapów, pięć punktów otrzymuje indeksy `[0, 1, 2, 1, 0]` zamiast `[0, 1, 2, 3, 4]`. Powrót może dostać indeks etapu pierwszego przejścia. Przyczyna: wyszukiwanie pierwszego dopasowania wartości lub `.index(value)` zamiast zachowania pozycji wystąpienia.

W monotonicznych osiach z Twojego prostego scenariusza problem nie wystąpił. Jest jednak istotny dla histerezy, powrotów i wielokrotnych punktów referencyjnych. **Naprawa:** generować rekordy `(wartość, indeks punktu, indeks etapu)` od początku i przenosić je bez ponownego odgadywania po wartości.

### SW-09 — ochrona dużego planu działa na niewłaściwej wielkości

Źródła: [compiler.py](app/engine/compiler.py), `max_actions = max_expanded_points * 10`; [semantic_tree.py](app/recipes/semantic_tree.py), `collect_contexts`; [sweep_points.py](app/recipes/sweep_points.py).

Przy limicie dwóch rozwiniętych punktów kompilator akceptuje trzy checkpointy. Pilnuje liczby akcji, nie zadeklarowanej liczby punktów. Ponadto normalizator materializuje pełny iloczyn kartezjański wraz z kontekstami przed sprawdzeniem części ograniczeń. Duże plany zużywają pamięć podczas normalizacji, budowania list akcji i serializacji hasha. Ochrona milionowego rozwinięcia w niektórych ścieżkach generatora kroku nie jest spójną ochroną całej topologii.

Ustawienia rampy Keithleya, takie jak `sweep_points_max` i ograniczenie kroku rampy, nie są globalnym limitem każdej osi sweepa. **Naprawa:** przed rozwijaniem policzyć rozmiar osi i iloczyn, osobno ograniczyć checkpointy, surowe widma i akcje; zapewnić przerwanie i generowanie leniwe albo ograniczone porcje. To warunek dużego eksperymentu.

### SW-10 — cicha konwersja liczby punktów

Źródło: [sweep_points.py](app/recipes/sweep_points.py), `generate_sweep_stage_points`. `int(raw_points)` akceptuje np. `2.5` jako dwa punkty oraz tekst `"3"`. Jawny parser osi ma ostrzejsze reguły, więc formaty są niespójne.

**Naprawa:** wymagać rzeczywistej liczby całkowitej w każdym wejściu ROI, odrzucać ułamki i niejawne typy, a kroki i krańce nadal sprawdzać wymiarowo w SI.

### SW-16 — literówka zostaje zastąpiona wartością domyślną

Źródła: [parser receptur](app/recipes/models.py), [compiler.py](app/engine/compiler.py). Konfiguracja Keithleya z `settlng_time: '3 s'` jest akceptowana; rzeczywiste pole pozostaje domyślne, zamiast wywołać błąd.

MOKEbox ma w części ścieżek kontrolę nieznanych pól, więc zachowanie nie jest wspólne dla modułów. **Naprawa:** jawne schematy dozwolonych pól dla każdej operacji i ostrzeżenie/błąd migracji, zamiast cichego pomijania. Przy znaczeniu 3 s dla eksperymentu taka literówka jest szczególnie groźna dla interpretacji wyniku.

## 4. Stabilizacja, rzeczywiste nastawy i bezpieczeństwo

### SW-08 — czas oczekiwania a zapisany parametr

Źródła: [runner.py](app/engine/runner.py), `_actual_setpoint_value` ok. 1846; [compiler.py](app/engine/compiler.py), obsługa `measurement.settling_time`.

W teście osi settling 1 s → 3 s runner prawidłowo otrzymuje Wait 1 s i Wait 3 s, lecz oba checkpointy zapisują settling **1 s**. Mechanizm podstawiania „rzeczywistych” nastaw odczytuje początkową konfigurację kanału, której Wait nie aktualizuje.

Samo `configure_keithley settling_time: '3 s'` nie oznacza w każdej ścieżce automatycznej przerwy po każdej zmianie. Starszy blok urządzenia może generować Wait; ogólna konfiguracja i dedykowana aktualizacja poziomu nie stanowią takiej gwarancji.

W recepturze `recipes/untitled_sweep2.yml` występują m.in. krótkie automatyczne settling i Wait po akwizycji. **Pauza po poprzednim spektrum nie zastępuje pełnych 3 s po następnej zmianie prądu.** Dla Twojego eksperymentu Wait należy umieścić wewnątrz najbardziej wewnętrznej osi, po zmianach i przed akwizycją. Nie należy hardcodować 3 s globalnie w runnerze.

Przy `average_count > 1` pętla surowych akwizycji nie ma takiego Wait między poszczególnymi ramkami. Obecna konstrukcja gwarantuje pauzę przed blokiem, nie przed każdym składnikiem średniej. Jeżeli „każda rejestracja” oznacza również surowe ramki, potrzebny jest jawny interwał między nimi albo `average_count = 1`.

MOKEbox dodatkowo stosuje settling swojego profilu/planu po zmianie napięcia; w symulacji nie odtwarza fizycznego czasu tej stabilizacji. Jest to oczekiwanie dla zmiany zewnętrznej osi, nie zastępstwo przerwy po wszystkich wewnętrznych zmianach.

### SW-11 — trzy różne napięcia MOKEbox i mylące potwierdzenie

Źródła: [adapter MOKEbox](app/devices/moke_box/adapter.py), `ramp_vout` i `_ramp`; [compiler.py](app/engine/compiler.py), `_prepare_moke_trajectories` ok. 356; [runner.py](app/engine/runner.py), `_confirmed_semantic_value` ok. 710.

Dla nominalnych 100 mV i roboczego przedziału 0–100 mV:

| Rodzaj wartości | Wynik |
|---|---:|
| Żądanie operatora | 100,000000 mV |
| Wartość zastosowana wyliczona przez przygotowany plan DAC | 99,795526 mV |
| Końcowy odczyt kodu DAC w wykonanym teście | 99,490341 mV |
| `applied_si` w akcji providera i „potwierdzenie” semantyczne | 100,100711 mV |

Provider wylicza applied przed zawężeniem roboczego przedziału przez kompilator. Kompilator przygotowuje inny plan, lecz nie aktualizuje applied w payloadzie. Rampa kończy się również o jeden kod DAC za wcześnie, bo test osiągnięcia celu dopuszcza różnicę jednej jednostki DAC. Rozbieżność 0,510 mV względem żądania mieści się w obecnej tolerancji adaptera 1 mV, ale ogranicza deklarowaną dokładność.

Jeszcze poważniejsze: projekcja semantyczna może oznaczyć wartość providera jako **readback**, chociaż nie jest to odczyt wyniku rampy i leży ponad 100 mV. Checkpoint zapisuje natomiast poprawnie końcowy odczyt adaptera. To błąd spójności i dowodu, nawet jeżeli fizyczny limit nie został przekroczony.

**Naprawa:** jedna definicja kwantyzacji i prepared applied; rampa do dokładnego końcowego kodu; event z rzeczywistym wynikiem adaptera. Odczyt kodu DAC opisuje nastawę rejestru urządzenia, nie niezależny pomiar napięcia na DUT. Protokół już używa `round` — nie potwierdziła się hipoteza dodatkowego błędu obcinania w samym enkoderze.

### SW-20 — DISCONNECTED nie potwierdza OUTPUT OFF

Źródło: [runner.py](app/engine/runner.py), `_shutdown_owned_device` ok. 1789; adapterowe `emergency_off`.

Przy adapterach bez sesji `emergency_off` może nic nie wysłać i pozostawić `DISCONNECTED`. Runner odrzuca `UNKNOWN`, ale samo `DISCONNECTED` przechodzi jako bezpieczne wyłączenie. Test z niepołączonymi produkcyjnymi adapterami symulacyjnymi i planem checkpoint-only otrzymał stan `SAFE`, którego nie uzasadnia potwierdzenie OFF.

Normalny RunWorker najpierw łączy urządzenia, więc odtworzenie nie dowodzi, że każdy zwykły pomiar kończy się tym błędem. Ujawnia jednak wadliwy warunek biblioteczny i istotny przypadek utraty sesji. **Naprawa:** SAFE wymaga konkretnego potwierdzonego OFF lub równoważnego dowodu bezpiecznego stanu. Brak sesji pozostaje niepotwierdzony.

### Dodatkowe warunki bezpieczeństwa wynikające z przeglądu kodu

- Kontrola zmierzonego I/V, mocy, tripów i compliance wymaga wykonania `measure_keithley`. Aktualizacja poziomu sprawdza nastawę i OUTPUT, lecz nie zastępuje odczytu rzeczywistego obciążenia. Dla proponowanego pomiaru kontrolne odczyty obu kanałów powinny być w każdej iteracji.
- Lokalna polityka `warn_clamp` świadomie pozwala kontynuować przy compliance. Dla eksperymentu trzeba jawnie zdecydować, czy zapisywać ograniczony prąd z flagą, czy przerwać; raport nie zmienił tej polityki. Rzeczywisty prąd należy oceniać w kolumnie pomiarowej, nie tylko na osi nastawy.
- Zmiany `update_keithley_level` są bezpośrednimi skokami. Ograniczenia kroku dedykowanej rampy nie ograniczają automatycznie każdej osi. Dotyczy to także powrotu A z 1,4 do 0,2 mA i zmian B. Jeżeli układ wymaga rampy, musi ona być jawna i nie może zmieniać compliance/ranges/sense przy okazji.
- Przy przejściu pętli zewnętrznej przez pewien czas istnieje kombinacja nowych i poprzednich nastaw wewnętrznych. Kolejność zmian, ewentualne wyłączenie wyjść i ponowne uzbrojenie muszą wynikać z wymagań eksperymentu. Sama poprawność iloczynu punktów nie kwalifikuje stanów przejściowych.
- Watchdog runnera żąda anulowania; kontroler ma niezależny worker awaryjnego OFF dla Rigola, Keithleya i Anritsu. Ten niezależny tor nie obejmuje MOKEbox. Zwykły stop MOKEbox próbuje dojść do zera, ale brak potwierdzenia pozostawia nieznany stan. Nie wykonano kwalifikacji zerwania połączenia fizycznego podczas rampy.
- `dut_limits` w recepturze jest **jawnie oznaczane jako historyczne metadane, nie aktywna ochrona** (`legacy_metadata_only`, `enforced: false`). Aktywne ograniczenia pochodzą z ustawień stanowiska. Nie należy interpretować samego wpisu YAML jako zabezpieczenia DUT.
- Końcowe wyłączenie ma zakres całego posiadanego stanowiska: obejmuje także wyjścia nieużyte w danej osi. Operacyjne przełączanie analizatora w SPECT i ewentualne RF OFF generatora SG również istnieje. To musi być opisane jako kontrakt wykonania, aby operator rozumiał wszystkie czynności urządzeń.

## 5. Dane, jednostki i metadane

### Co zapis działa poprawnie w wykonanej próbie

Zapis obejmuje źródło receptury, ustawienia i hash planu, identyfikację/capabilities urządzeń, flagę symulacji, czas i indeks checkpointu, nastawy, pomiary, snapshoty stanów urządzeń, kontekst bezpieczeństwa, zdarzenia, widma oraz surowe źródła akwizycji. Nominalna próba zachowała wszystkie 18 punktów, widma i ich surowe zapisy; odczyt PyThat odtworzył geometrię danych. Referencje mają własną ścieżkę zapisu i metadane; mechanizm obróbki przechowuje dane źródłowe, zamiast zastępować je wyłącznie wynikiem.

Uśrednianie mocy widm odbywa się po przejściu do liniowej mocy, a nie przez zwykłą średnią dBm. Walidacja zakresów i SI oraz stałych zakresów źródła Keithleya jest realnym zabezpieczeniem: wiele dawnych fixture'ów zostało poprawnie odrzuconych za brak jawnego zakresu. Nie należy usuwać tych zabezpieczeń, żeby uzyskać zielone stare testy.

To nadal nie jest dowód kompletności wszystkich metadanych ani odporności na każdą awarię.

### SW-12 — nominalna oś i rzeczywista nastawa nie mają wspólnego kontraktu

Źródła: [runner.py](app/engine/runner.py), zapis `RecipeSpectrumSweep` w akwizycji i `_apply_actual_setpoints`; [thatec_writer.py](app/storage/thatec_writer.py), definicje osi i wykluczanie `axis_targets` z dodatkowych wskaźników.

Surowy zapis widma używa żądanych nastaw akcji. Checkpoint po podstawieniu readback używa nastaw odczytanych. Oś publiczna thaTEC pochodzi z nominalnego ROI. Dla MOKEbox surowa ramka ma 0,1 V, a checkpoint około 0,099490341 V. To nie musi oznaczać błędnej akwizycji, ale identyczne nazwy bez określenia rodzaju wartości są mylące. Publiczny eksport nie dodaje dla osi osobnego scalar indicator z jej rzeczywistą nastawą.

**Naprawa:** zachować nominalne współrzędne siatki oraz odrębne, jednoznacznie nazwane `requested`, `applied` i `readback` dla każdej osi w checkpointach, surowych ramkach i publicznym eksporcie. Nie zastępować osi nominalnej po cichu nierównomierną osią readback.

### SW-13 — rollback nie cofa licznika

Źródło: [hdf5_writer.py](app/storage/hdf5_writer.py), `append` ok. 815. Writer oznacza punkt jako kompletny, zwiększa `_point_count`, a potem wykonuje końcowy flush/commit. Wstrzyknięty błąd drugiego flush usuwa zapis punktu, lecz licznik nadal wynosi 1, mimo braku punktów na dysku.

Normalne wykonanie zakończy się błędem, więc nie jest to dowód cichej kontynuacji pomiaru po takim fault. Błąd dotyczy spójności raportowanego stanu i recovery. **Naprawa:** przesunąć inkrementację po udany commit lub odtwarzać licznik podczas rollback. Sprawdzić również publiczny zapis, indeksy widm/surowych ramek i CSV przy awarii poszczególnych etapów. Transakcja `_pending` i `flush` nie jest sama w sobie gwarancją atomowości przy nagłym zaniku zasilania dysku.

### SW-14 — readback jest nadpisywany przez czynności pomiarowe

Źródło: [runner.py](app/engine/runner.py), `_remember_semantic_confirmation` ok. 614, wywołanie po akcjach semantycznych.

Cache potwierdzeń osi jest aktualizowany również po Wait, pomiarach i akwizycji. Ostatnia akwizycja nadpisuje potwierdzenie aktualizacji prądu. Checkpoint z próby zawiera `applied_si: 0.0014`, **`readback_si: null`, `verification: "readback"`**, a jako operację potwierdzającą wskazuje `spectrum`.

Dokładniejsze dane bywają nadal obecne w snapshotach urządzeń i bezpieczeństwa, ale główne metadane nie powinny wymagać od czytelnika odgadywania takiej sprzeczności. Dodatkowo jedno potwierdzenie osi wewnętrznej nie zastępuje potwierdzeń wszystkich trzech osi.

**Naprawa:** potwierdzenie SET/ROI może aktualizować tylko właściwa mutacja z rzeczywistym wynikiem; akcje pomiarowe zapisują własną proweniencję. Persistować mapę potwierdzeń wszystkich aktywnych osi wraz z czasem, źródłem i jakością dowodu.

### SW-18 — niektóre jednostki eksportu są błędne

Źródło: [thatec_writer.py](app/storage/thatec_writer.py), `_describe_quantity` ok. 744. Rozpoznawanie po podciągach daje:

| Klucz | Jednostka obecnie | Prawidłowa interpretacja |
|---|---|---|
| `moke_box.field_estimated_ascending_t` | A | T — pole magnetyczne; `_a` z `ascending` wygrywa |
| `keithley.A.compliance_stop_required` | s | flaga bezwymiarowa; `_s` z `stop` wygrywa |
| `anritsu.sg.power` | W | dBm — nastawa mocy SG w obecnym kontrakcie |

Testy dotyczą bezpośrednio funkcji opisującej publiczne kolumny. Nie oznacza to, że wszystkie trzy pola pojawiają się w każdej recepturze. Jednostki widm Hz/dBm w wykonanej próbie są poprawne.

**Naprawa:** rejestr wielkości ze ścisłym wymiarem, jednostką przechowywania i jednostką prezentacji. Flagi i liczniki muszą mieć własne typy; dopasowanie dowolnego podciągu nie nadaje się do naukowego eksportu.

### Kompletność metadanych — ocena

| Obszar | Ocena |
|---|---|
| Źródło receptury, hash planu, konfiguracja stanowiska, identyfikacja, symulacja | zapisane w ścieżce testowej |
| Pełna geometria prostego monotonicznego iloczynu trzech osi | potwierdzona dla 18 punktów |
| Zmierzony prąd/napięcie A/B i flagi compliance | zapisane, jeżeli receptura jawnie wykonuje odczyty |
| Stan urządzeń i kontekst bezpieczeństwa na checkpoint | zapisane; nie każde pole oznacza niezależny odczyt fizyczny |
| Żądana/applied/readback każdej osi w każdym formacie | niespójne — SW-11, SW-12, SW-14 |
| Kierunek/etap i indeks ROI przy powrotach | błędne w odtworzonym przypadku — SW-07 |
| Jednostki wszystkich dodatkowych skalarów | błędne heurystyki — SW-18 |
| Interwał rzeczywisty po każdej zmianie oraz między ramkami średniej | częściowo wynika z eventów; brak jednolitej gwarancji dla każdego rodzaju akwizycji |
| Pełny potwierdzony baseline wszystkich zaawansowanych ustawień analizatora | nie można zadeklarować kompletności na podstawie obecnego planu/defaultów |
| Pochodzenie i kwalifikacja pola szacowanego MOKEbox | kalibracje mają metadane, ale estymacja nie zastępuje mierzonego pola; brak kwalifikacji nie powinien być ukrywany |
| Atomowość przy błędach zapisu i zaniku zasilania | happy path poprawny; jeden odtworzony błąd rollback; power-loss niekwalifikowany |
| Pełne wznowienie z referencją, MOKEbox i stanem źródeł | ograniczone — SW-15 |

## 6. Duże pomiary i odzyskiwanie

### SW-15 — recovery nie pokrywa całego wykonania

Źródło: [recovery.py](app/engine/recovery.py), `_latest_boundary` od ok. 78 i `_configuration_prelude` od ok. 126; [runner.py](app/engine/runner.py), reset referencji i `_record_safe_boundary`.

Test pokazał, że checkpoint `measure_moke_hall` jest liczony przez plan/runner, ale recovery uwzględnia tylko `acquire_spectrum` i `checkpoint`. Zapisana poprawna granica z jednym checkpointem czujnika jest odrzucana jako niespójna.

Przegląd wykazał również, że prelude odtwarza ostatnie pełne konfiguracje Rigola, Keithleya i analizatora, lecz nie odtwarza pełnego stanu wynikającego z aktualizacji poziomów, SG, uzbrojenia/planu MOKEbox i referencji. Runner przy nowym run resetuje referencję; kontynuacja obróbki zależnej od wcześniejszej referencji wymaga jej odtworzenia albo jawnego ponownego pozyskania. Te dodatkowe warianty wymagają osobnych testów wykonania po naprawie — nie zostały uznane za przeprowadzone testy sprzętowe.

W archiwum trzech osi jest **zero `safe_resume_boundary`**. Utrzymywane OUTPUT ON i niepotwierdzony bezpieczny stan poszczególnych urządzeń nie pozwalają zakładać automatycznego wznowienia. Odmowa recovery bez potwierdzonej granicy jest właściwa; błędem byłaby obietnica pełnego resume dla takiego długiego pomiaru.

**Naprawa:** wspólny kontrakt „akcja produkuje checkpoint”, jawne punkty bezpiecznego zatrzymania, pełna rekonstrukcja konfiguracji/nastaw i danych referencyjnych, oraz ponowne potwierdzenie bezpiecznego stanu rzeczywistej aparatury przed uzbrojeniem. Do tego czasu UI powinien dokładnie informować, które plany można wznowić. Sam poprawny odczyt HDF5 nie oznacza możliwości ponownego uruchomienia aparatury od środka.

### SW-19 — estymacja nie jest wiarygodną górną granicą

Źródło: [estimation.py](app/engine/estimation.py), estymacja HDF5 ok. 124–153. Dla tej samej próby:

- deklarowany `uncompressed_hdf5_bytes`: **1 631 544 B**;
- rzeczywisty plik: **3 292 542 B**, około **2,02 razy większy**;
- nominalny czas w estymatorze: 60,333 s; wariant z retries: 80,408 s.

Koszt struktur HDF5, wielokrotnych snapshotów/eventów i metadanych jest większy niż uwzględniony budżet. Model czasu nie stanowi górnej granicy rzeczywistych ramp, stabilizacji sprzętowej i odczytów zależnych od NPLC. Nie znalazłem preflight gwarantującego wolną przestrzeń na pełny przewidywany zapis. Końcowy bridge PyThat ładuje publiczny dataset, co również wymaga kwalifikacji pamięci dla dużych pomiarów.

**Naprawa:** zmierzony model overhead dla liczby eventów, checkpointów, pól i surowych ramek; osobne estymacje nominalne i konserwatywny budżet; kontrola wolnego miejsca i czasu zamykania. Nie należy przenosić współczynnika 2,02 na każdą liczbę bins jako gwarancji. Potrzebne są benchmarki reprezentatywnego docelowego planu.

## 7. „Wirtualny pilot” i kontrola wizualna

### SW-17 — nastawa prądu działa, pełna projekcja stanu nie

Źródła: [MainWindow](app/ui/shell/main_window.py), routing/coalescing `run_event`; [KeithleyPage](app/devices/keithley_2600/ui/page.py), `apply_execution_event` i `_render_execution_channel`; [runner.py](app/engine/runner.py), wywołanie rampy MOKEbox.

Pokazałem okno, przetworzyłem zdarzenia i sprawdziłem widoczną, niezerową geometrię stron Sweeps i Execution w **1360×880 light**, **1360×880 dark** oraz **1000×760 light**. To sprawdzenie realnego drzewa layoutu Fluent, nie tylko wyboru route. W testowanych stronach nie było `QTabWidget`.

Test potwierdzonego zdarzenia Keithleya pokazuje poziom **1,4 mA** i OUTPUT ON na karcie; kontrolki sterujące są zablokowane i projekcja **nie wywołuje dodatkowych komend adaptera**. Jednak zdarzenie z rzeczywistą konfiguracją NPLC 8 pozostawia formularz na NPLC 1. Renderowanie aktualizuje poziom, compliance, tryb i output, lecz nie wszystkie zakresy, sense, NPLC i settling.

MOKEbox ma callback postępu rampy w adapterze, ale runner nie przekazuje go przy wykonywaniu receptury. UI otrzymuje informacje początkowe/końcowe zamiast kolejnych potwierdzonych kroków rampy. Ukryte karty są celowo pomijane przez część aktualizacji; przejście na kartę podczas oczekiwania może pokazać wcześniejszy stan aż do kolejnego odpowiedniego zdarzenia. Coalescing ogranicza obciążenie, ale musi zachować ostatni pełny stan każdego kanału.

Na zrzucie karty Keithleya część nagłówków/etykiet jest ucięta nawet przy 1360 px; wymaga to sprawdzenia także na rzeczywistym desktopie z docelowym DPI/fontami. Zrzut projekcji powstał z kontrolowanego zdarzenia testowego, a nie połączenia z aparaturą; status połączenia w nim nie dokumentuje aktywnego fizycznego run.

**Naprawa:** jeden model potwierdzonego stanu urządzenia, pełna projekcja na każdą kartę, inicjalizacja z najnowszego snapshotu po wejściu na route, osobne oznaczenie requested/applied/readback i jawna jakość potwierdzenia. Postęp rampy powinien być emitowany z adaptera i limitowany czasowo w UI. Dodać testy pokazanych kart dla obu kanałów, wszystkich ważnych pól, zmiany route podczas Wait i stanów fault/unknown.

Materiały wizualne:

- [Sweeps — desktop light](docs/audits/2026-10-04-sweeps/sweeps-1360-light.png) i [dark](docs/audits/2026-10-04-sweeps/sweeps-1360-dark.png).
- [Execution — desktop light](docs/audits/2026-10-04-sweeps/execution-1360-light.png) i [dark](docs/audits/2026-10-04-sweeps/execution-1360-dark.png).
- [Sweeps — 1000 px](docs/audits/2026-10-04-sweeps/sweeps-1000-light.png) i [Execution — 1000 px](docs/audits/2026-10-04-sweeps/execution-1000-light.png).
- [Keithley — potwierdzona projekcja 1,4 mA](docs/audits/2026-10-04-sweeps/keithley-live-projection.png).

## 8. Testy, zakres pewności i artefakty

Środowisko: Python 3.14.6, pytest 9.1.1, PySide6 6.11.2, PySide6-Fluent-Widgets 1.11.2, NumPy 2.5.2, h5py 3.16.0, PyThat 0.2.14, Ruff 0.16.6. UI testowano z izolowanym QSettings, inventory DB, profilem i logami; nie zapisywano ustawień użytkownika.

| Grupa | Wynik | Interpretacja |
|---|---|---|
| 14 istniejących plików testów sweep/compiler/runner/storage/UI | 266 passed, 29 failed; 15 subtests passed | 14 braków jawnego zakresu w dawnych fixture'ach; 15 problemów UI z zapisem inventory DB w lokalizacji tylko do odczytu |
| 17 kolejnych istniejących plików urządzeń, safety, danych, estymacji i edytora | 193 passed, 16 failed, 4 skipped; 30 subtests passed | szczegóły poniżej; [log](docs/audits/2026-10-04-sweeps/broader-tests.log), [JUnit](docs/audits/2026-10-04-sweeps/broader-tests.xml) |
| Nowe kontrakty audytu, główne wykonanie | 2 passed, 23 xfailed | pozytywna topologia i rzeczywisty Wait; odtworzone usterki, [log](docs/audits/2026-10-04-sweeps/contracts.log) |
| Dodatkowy kontrakt shutdown SW-20 | 1 xfailed | brak potwierdzenia OFF po DISCONNECTED; [log](docs/audits/2026-10-04-sweeps/shutdown-contract.log) |
| Izolowane testy renderowania/projekcji | 4 passed, 1 xfailed | geometria stron i poziom prądu poprawne; NPLC niezsynchronizowane; [log](docs/audits/2026-10-04-sweeps/rendering-tests.log) |
| Ruff `app tests` | 1 511 zgłoszeń | stan całego zastanego drzewa; [log](docs/audits/2026-10-04-sweeps/ruff.log) |

Dwa rozłączne zestawy istniejących testów obejmują łącznie **508 przypadków: 459 passed, 45 failed, 4 skipped**. Nowe testy obejmują **31 przypadków: 6 passed, 25 xfailed**, w oddzielnych wywołaniach. Powtórzenie tej samej starej grupy UI nie zostało ponownie doliczone do zakresu.

Z 45 niepowodzeń istniejących testów: 23 dotyczą braku jawnego stałego zakresu źródła, 5 nieprawidłowego fixture'u bipolarnego progu prądowego B, 15 środowiskowego zapisu UI DB oraz 2 zachowania UI/importu sprzężonych zakresów. Te ostatnie pozostają nieuzgodnioną regresją kontraktu UI. Braki zakresów/progów pokazują rozbieżność testów z aktualnymi wymaganiami safety; nie są podstawą do osłabienia preflight. Cztery skipped dotyczą nieobecnego licencjonowanego wzorcowego pliku thaTEC, więc pełna zgodność z tym wzorcem nie została zweryfikowana.

Pierwsza grupa została wykonana i jej wynik zaobserwowany w sesji; nie zachował się pełny osobny log tej grupy. Jej lista plików i obserwowane liczniki są zapisane w [baseline-summary.json](docs/audits/2026-10-04-sweeps/baseline-summary.json), wyraźnie odróżnionym od przechwyconych logów. Izolowane nowe testy UI usunęły ograniczenie środowiskowe dla kontroli renderowania.

Nowe pliki diagnostyczne: [test_sweep_audit_contracts.py](tests/test_sweep_audit_contracts.py), [test_sweep_audit_rendering.py](tests/test_sweep_audit_rendering.py), [summarize_sweep_audit.py](tools/summarize_sweep_audit.py). Ich lokalna kontrola Ruff przechodzi. Testy xfail są celowo surowe: po naprawie nieoczekiwane przejście zgłosi konieczność aktualizacji oznaczenia. Nie należy przedstawiać 25 xfail jako zaliczonej kwalifikacji.

Wykonany audyt jest szeroki, lecz nie stanowi dowodu braku wszystkich błędów. Nie wykonywałem całego zestawu niezwiązanych funkcji analizy widm, prób na rzeczywistej aparaturze, długiego docelowego run, testu zaniku zasilania ani kwalifikacji fizycznej reakcji DUT. [Indeks artefaktów i odtwarzanie](docs/audits/2026-10-04-sweeps/README.md) zawiera szczegóły i sumy kontrolne źródeł/dowodów.

## 9. Zalecana kolejność napraw i warunki odbioru

1. **Sterowanie zgodne z drzewem:** naprawić SW-02/03/04/05/06/16. Jednorazowy jawny baseline; żadnych niejawnych wartości zastępczych; typowane zmiany tylko wybranych pól; dokładna identyfikacja kanału i operacji. Test rejestru komend musi udowodnić, że sweep prądu nie zmienia NPLC, compliance, ranges, sense, częstotliwości analizatora ani innego VOUT.
2. **Prawda o nastawach i danych:** naprawić SW-07/08/11/12/14/18. Jeden model requested/applied/readback dla wszystkich aktywnych osi, prawidłowe indeksy przejścia/powrotu, jednoznaczne jednostki i jakość potwierdzenia, kompletne metadane surowych i końcowych widm.
3. **Fault, shutdown i recovery:** naprawić SW-13/15/20. Wstrzykiwane błędy w każdym etapie commitu, utrata sesji, odczyt OFF, przerwanie Wait/rampy/akwizycji; brak fałszywego SAFE. Resume dopiero po potwierdzonym bezpiecznym stanie, z pełną rekonstrukcją.
4. **Duża skala:** naprawić SW-09/19 i kwalifikować docelową liczbę punktów/bins. Sprawdzić RAM, czas kompilacji, wolne miejsce, eventy, zapis, końcowy bridge i czas zatrzymania. Plan ponad limit musi zostać odrzucony przed alokacją iloczynu.
5. **Pełny wirtualny pilot:** naprawić SW-17 oraz zakres profili SW-01 i walidację SW-10. Potwierdzony stan wszystkich pól na kartach, bez dodatkowego I/O wywołanego renderowaniem; rampa z postępem; pokazane geometrie light/dark i stany fault/narrow.

Minimalny odbiór Twojego docelowego pomiaru powinien obejmować:

| Warunek | Oczekiwany dowód |
|---|---|
| Wybrane kroki/ROI i kompletne ustawienia początkowe | zatwierdzona receptura z jednostkami, zakresem źródła i jawnie dobranym compliance |
| Cały iloczyn MOKE0/B/A | dokładnie N checkpointów, właściwa kolejność, krańce i wartości wszystkich osi |
| Tylko wskazane mutacje | log komend: zmiany VOUT0 i poziomów A/B; pozostałe parametry zgodne z baseline |
| Stabilizacja | znacznik potwierdzonej ostatniej zmiany → początek akwizycji ≥3 s, osobno zdefiniowana polityka ramek średniej |
| Rzeczywisty prąd | pomiary A/B, flagi compliance/trip i decyzja stop/continue dla każdego punktu |
| Wirtualny pilot | karty zgadzają się z potwierdzonym stanem wszystkich ważnych pól, również po przejściu na ukrytą wcześniej kartę |
| Poprawne dane | raw/checkpoint/public axes rozróżniają nominalne i rzeczywiste wartości; wszystkie jednostki i metadane zgodne |
| Awaria/zatrzymanie | trwały kompletny prefiks danych, poprawny status, potwierdzone OFF lub jawne UNKNOWN |
| Wznowienie | jawne wsparcie albo czytelna odmowa; żadnego domyślnego odtwarzania nieznanych nastaw |
| Skala | preflight ograniczeń i przestrzeni oraz reprezentatywny benchmark bez przekroczenia budżetu |

Najbardziej użyteczna baza do dalszej pracy to pozytywny, jawnie skonfigurowany scenariusz trzech osi. Nie należy jednak skopiować jego syntetycznego compliance/częstotliwości do laboratorium ani uznać przejścia 18 punktów za rozwiązanie wszystkich znalezionych problemów.
