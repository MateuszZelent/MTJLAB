# Realizacja napraw raportu

Cel pozostaje pełny: wszystkie problemy z `report.md`, łącznie z tabelą
pozostałych ustaleń i dodatkowymi punktami Keithley. Ten plik nie zastępuje
zakresu raportu. Rejestr oryginalnej lektury/hashów pozostaje historycznym
dowodem wersji audytowanej, nie wersji po poprawkach.

Każda seria poniżej opisuje stan w chwili jej wykonania. Wczesne sekcje
„Nadal do wykonania” są historyczne; nie zastępują późniejszych ustaleń.
Nie wszystkie problemy raportu są zamknięte.

## Pierwsza seria zmian

- **A01 — poprawiono, weryfikacja częściowa:** Stop jest zapamiętywany
  przed powstaniem runnera; sprawdzany przed inicjalizacją i pomiędzy
  połączeniami; przekazywany runnerowi po inicjalizacji storage. Lokalna
  referencja runnera usuwa wyścig ze skasowaniem atrybutu przez cleanup.
  Testy obejmują Stop przed run, w connect i w konstruktorze storage.
  Nie wykonano fizycznych pomiarów ani pełnej kwalifikacji E-STOP.
- **Cleanup / błąd connected — poprawiono:** nieudany odczyt traktowany
  jako stan nieznany; próba shutdown i disconnect pozostaje wykonywana,
  błąd trafia do wyniku cleanup. Osobny test potwierdza abort i disconnect
  po wyjątku odczytu. Globalny deadline shutdown pozostaje do naprawy.
- Testy: `test_source_review_cancellation.py` oraz
  `test_sweep_worker_initialization_faults.py`: **8 passed**.
- `python -m ruff check app tests`: **All checks passed**.
- Szerszy przebieg `test_run_controller.py test_simulated_run.py`:
  **11 passed, 8 failed**. Nie jest to zielona kwalifikacja integracji.
  Wszystkie osiem porażek pochodzi z `test_simulated_run.py`: energized
  legacy DUT limits, four-device full state, 100x20 spectra, Cartesian
  PyThat round trip, raw/reference link, single checkpoint, processed
  Cartesian sweep oraz requested 10x100. Widoczne przyczyny obejmują brak
  jawnego fixed source range w scenariuszach i zatrzymanie na compliance
  przy 0.01667 A. Wymagają osobnej analizy scenariuszy/symulatora; nie należy
  osłabiać walidacji zakresów lub compliance, aby uzyskać zielone testy.

## Nadal do wykonania

A09–A10 pozostają otwarte. Otwarte pozostają też wszystkie dodatkowe
pozycje raportu poza powyższą obsługą błędu connected: kompilator, edytor,
estymacja, cache konfiguracji, adaptery, dry run/symulacja, storage/czytniki,
ręczny zapis i korekcja, wykresy, zadania GUI, dialogi, cache stanu oraz
dodatkowe ustalenia karty Keithley. Hipotezy H wymagają reprodukcji lub
udokumentowanego odrzucenia na podstawie aktualnego kodu.

## Druga seria zmian — kwalifikacja widma i prefiksy SI

- **A02 — poprawiono:** `_read_spectrum_identity` zawsze wykonuje odczyt
  przez kontrakt adaptera/proxy. Nie ma obejścia po `isinstance` ani cichego
  dopuszczenia braku readback. Błąd odczytu jest błędem akwizycji. Potwierdzenie
  RF OFF po pełnej konfiguracji również nie zależy już od klasy obiektu,
  lecz od zwróconego wyniku konfiguracji i zakresu planu obejmującego SG.
- Testy rzeczywistego `DeviceController` z symulowanym adapterem potwierdzają
  wykonanie odczytów w wątku transportu, stabilny fingerprint i propagację
  błędów obu odczytów. Integracja runner → proxy → adapter → HDF5 potwierdza
  zapis konfiguracji podstawowej i zaawansowanej w checkpointcie.
  `test_source_review_spectrum_identity.py`: **5 passed**.
- **A04 — poprawiono:** parser zachowuje wielkość prefiksu SI, dopuszczając
  dotychczasowe niesprzeczne warianty wielkości liter symbolu bazowego.
  `mHz` ma 1e-3 Hz, `MHz` 1e6 Hz, `mohm`/`mΩ` 1e-3 ohm,
  `Mohm`/`MΩ` 1e6 ohm. Nieobsługiwane mega V/A/W oraz błędne prefiksy
  złożonych jednostek są odrzucane, a nie zmieniane na milli.
  Starsze ręcznie pisane `mhz` oznacza teraz milliherc; mega wymaga `MHz`.
  Nie wykonano automatycznego przepisania plików użytkownika.
- Parser, Settings/safety, precision: **61 passed, 23 subtests passed**.
- Kompilator i adaptery/runner: **157 passed, 11 subtests passed**.
- Ruff całego `app tests`: **All checks passed**.
- Dłuższy przebieg identity + reference recovery + timed reference:
  **14 passed w 455.73 s**, w tym pełny test 297 × Avg32 × 10001 bins,
  minimum 30 s tła, porównanie średniej z raw i walidacja PyThat archiwum.
  To symulacja; oczekiwania 5 s przy nastawach były świadomie omijane
  przez istniejący test i sprawdzane w jego rejestrze wywołań.

## Trzecia seria zmian — kolejka i rezerwacja urządzeń

- **A03 — częściowo poprawiono:** `_RunCall` ma atomową granicę startu
  i anulowania. Timeout usuwa możliwość późniejszego wykonania żądania,
  jeśli jeszcze nie wystartowało. Dla rozpoczętej operacji komunikat jawnie
  mówi o niepewnym wyniku i zakazie ponowienia mutacji; nie udaje anulowania.
  Odczyty atrybutów korzystają z tego samego mechanizmu.
- Błąd publikacji stanu urządzenia nie blokuje już `completed` i nie
  zastępuje wcześniejszego błędu operacji.
- RunWorker nabywa rzeczywistą rezerwację transportu każdego potrzebnego
  kontrolera. Komendy ręczne przechodzą przez istniejący ponowny guard
  w wątku transportu. Rezerwacje są zwalniane po cleanup, również przy
  częściowym błędzie inicjalizacji. Nieudane zwolnienie pozostawia blokadę
  i raportuje niepewność, zamiast pozwalać na nowe operacje.
- Cleanup po przerwaniu rezerwacji dopuszcza również abort, disconnect,
  ramp_to_zero oraz jawne SG OFF. Nie dopuszcza SG ON.
- Testy kolejki + lease + proxy: **17 passed**. Testy kontrolera uruchomień,
  anulowania, inicjalizacji i lease: **22 passed**. Dodatkowe regresje
  częściowej rezerwacji i nieudanego release wraz z kolejką: **10 passed**.
- A03 nadal wymaga oddzielenia deadline całej operacji od timeoutu
  pojedynczego VISA I/O; obecny limit oczekiwania `timeout_s + 15` nie
  został w tej serii zastąpiony. Globalny deadline shutdown też pozostaje otwarty.

## Czwarta seria zmian — deadline operacji i recovery ról

- **A03 — uzupełniono poprzednią serię:** runner przekazuje proxy pozostały
  czas całej akcji. `operation_timeout` utrzymuje jeden monotoniczny termin
  dla wszystkich wywołań i zagnieżdżeń; kolejna komenda nie resetuje budżetu.
  `io_timeout` nadal ogranicza pojedynczą komunikację VISA. Oczekiwanie na
  wynik nie jest już wyliczane jako `io_timeout + 15`.
  Przekroczenie terminu przed dispatch nie emituje żądania do transportu.
  Wywołania poza akcją nadal mają skończony domyślny limit 60 s; odczyty
  atrybutów są dodatkowo ograniczone do 10 s.
- **A05 — poprawiono:** jeden rejestr ról checkpointu w schema mapperze
  jest używany przez zapis dowodów nastaw, odtwarzanie indeksu wierszy,
  obcinanie danych podczas resume i ograniczanie odczytu do committed
  checkpoints. Nie zmieniano nazw ani wersji publicznego schematu.
- Nowy test zapisuje dwa punkty requested/applied/readback, cofa archiwum
  do pierwszego, dopisuje inny drugi punkt i sprawdza brak duplikacji ról,
  usunięcie poprzednich wartości oraz zgodność PyThat.
- Recovery ról + kolejka/deadline + dotychczasowe recovery:
  **10 passed**. Ruff `app tests`: **All checks passed**.
- Szersza regresja kolejki/deadline, lease, proxy z HDF5, RunController
  oraz fault injection storage: **37 passed**.
- Globalny deadline całego shutdown, A06 (częściowy append) i pozostałe
  pozycje raportu nadal wymagają pracy.

## Piąta seria zmian — częściowy append i cache osi

- **A06 — poprawiono:** rollback używa zapisanej liczby zatwierdzonych
  checkpointów, zamiast odejmować jeden od potencjalnie częściowo
  zmienionych datasetów. Rejestruje zamiar mutacji przed append i zamiar
  utworzenia wiersza przed pierwszym create_dataset. Przywraca również
  numer kolejnego wiersza, rozmiar logu oraz dynamiczną oś checkpointów.
- Walidacja siatki, liczby punktów i kontraktu processed odbywa się przed
  mutacją publicznego punktu. Wyjątek wewnątrz tworzenia wiersza usuwa
  częściową definicję i grupę measurement.
- **Dodatkowy punkt Storage / cache id — poprawiono:** cache zachowuje
  referencję do niemutowalnej krotki zamiast samego id; mutowalne osie są
  walidowane ponownie. Sprawdzana jest też skończoność, minimalna liczba
  punktów i dodatni kierunek osi.
- Testy wstrzykują błąd po resize każdego data/timestamp/scale dla
  skalara/raw/processed i osi checkpointu oraz po częściowym utworzeniu
  wiersza każdego rodzaju. Sprawdzają zachowanie całej poprzedniej
  zawartości, brak osieroconych wierszy i ponowienie tego samego indeksu.
- Częściowy append + wcześniejsze storage fault injection + recovery:
  **31 passed**. Ruff `app tests`: **All checks passed**.
- Szersza regresja HDF5/thaTEC reader/mapper/validator: **34 passed,
  3 subtests passed, 4 skipped**. Cztery pominięcia wynikają z braku
  laboratoryjnego/licencjonowanego golden HDF5 w checkout; tej części
  zgodności nie uznajemy za zweryfikowaną. Nowe testy na wygenerowanych
  plikach wykonały rzeczywistą walidację PyThat.

## Szósta seria zmian — trwałość audytu i blokady shellu

- **A07 — poprawiono:** logger zatrzaskuje pierwszy błąd write/flush/fsync,
  przekazuje go przez `check_health`, record, flush i close. Event oznacza
  zakończenie obsługi żądania, a sukces wymaga kontroli błędu. Timeout
  oczekiwania na trwałość również jest błędem, nie sukcesem.
- `wait_durable=True` wymusza fsync nawet bez flagi critical. `flush` używa
  bariery kolejki z fsync i skończonym oczekiwaniem. Numeracja i enqueue są
  we wspólnej sekcji krytycznej. `put_nowait` zamiast blokującego put zgłasza
  nasycenie kolejki i zatrzaskuje utratę audytu.
- Strumień zamyka wyłącznie wątek piszący; close nie zamyka go równolegle
  z niedokończonym write. Nieudana inicjalizacja też zatrzymuje writer.
- Shell odczytuje stan loggera przed startem i chronionymi operacjami oraz
  co 250 ms bez operacji dyskowych na GUI. Pierwsza awaria aktualizuje
  dashboard, blokuje nowe ON/runy i żąda zatrzymania aktywnego sweepa.
- **Dodatkowy punkt guard/lease — poprawiono:** wyjątki pozwalające na
  OUTPUT OFF, ramp_to_zero i pozostałe operacje zatrzymujące są rozpatrywane
  przed blokadą ręcznych operacji z powodu lease.
- Fault injection audytu + dotychczasowy schemat/sekwencja/redakcja/
  konkurencyjni producenci + pokazany shell z izolowaną bazą i Settings:
  **11 passed**. Osobny wcześniejszy przebieg z istniejącym testem blokad
  `test_main_window` również **11 passed** (155 niezwiązanych testów
  odfiltrowano). Ruff całego `app tests`: **All checks passed**.
- Nie wykonywano fizycznego odcięcia zasilania dysku ani kwalifikacji
  sprzętu. Zmiana nie zamyka pozostałych A08–A10 i dodatkowych ustaleń.

## Siódma seria zmian — potwierdzony stan Rigola

- **A08 — poprawiono:** pojedynczy readback aktualizuje tylko potwierdzone
  pole istniejącej konfiguracji. Amplituda/offset wyliczają poziomy z
  poprzednich potwierdzonych wartości, nie z formularza. Bez wcześniejszej
  pełnej konfiguracji pojedynczy odczyt nie tworzy pozornego pełnego stanu.
- Readback jest zapisywany również dla niewidocznego kanału oraz gdy
  edytor ma focus i projekcja tekstu jest celowo wstrzymana. Renderowanie
  execution korzysta z tego samego mechanizmu częściowego potwierdzenia.
- Test z pokazanym shellem: niezastosowana faza 15 stopni nie zostaje
  potwierdzona przez zmianę częstotliwości. Metadata zachowują fazę 0,
  a OUTPUT ON wybiera pełne configure, zamiast pomijać ustawienie fazy.
  Nie wysyłano komend do fizycznego sprzętu.
- Nowe testy: **2 passed** (w tym pokazane okno), następnie **2 passed**
  w przebiegu testów bez okna po dodaniu przypadku niewidocznego kanału.
- Dotychczasowe testy niezależnego stanu OUTPUT i wspólnego draftu Rigola
  przeszły. Test projekcji suwaka używał 200 mV przy limicie 100 mV,
  więc próba kroku z maksimum nie emitowała zmiany. Zmieniono wejście
  testu na 20 mV; limity i implementacja suwaka pozostają niezmienione.
  Powtórzenie poprawionego testu: **1 passed**, 155 niezwiązanych odfiltrowano.
- Ruff `app tests`: **All checks passed**.

## Ósma seria zmian — projekcja snapshotów urządzeń (weryfikacja w toku)

- **A09 — zaimplementowano:** Keithley rozpoznaje już obsłużony rekord
  pomiarowy po treści wraz z recorded_at_utc. Heartbeat nie dopisuje go
  ponownie; nowy czas przy identycznych wartościach nadal oznacza nowy pomiar.
  Historia zapisuje czas rekordu runnera i od niego wylicza oś czasu.
- Karty Keithley/Rigol pomijają renderowanie tej samej konfiguracji
  tego samego kanału oraz ponowne ustawianie niezmienionego stanu OUTPUT.
  Zmiana wybranego kanału lub konfiguracji nadal odświeża formularz.
- Cache projekcji jest zerowany przed nowym runem i przy disconnect.
  Keithley usuwa też poprzednie ręczne pomiary/odczyt konfiguracji na
  disconnect. Nie jest to jeszcze naprawa wszystkich cache innych urządzeń.
- Nowy test pokazanej aplikacji sprawdza 100 identycznych snapshotów:
  1 pomiar, 1 projekcja na kartę; nowy timestamp daje drugi pomiar, nowa
  częstotliwość daje kolejne renderowanie. Sprawdzany jest rzeczywisty
  timestamp historii i czyszczenie cache między runami.
- Trwają dwa przebiegi: sesja **83989** (nowy test, telemetry latency,
  execution responsiveness; dotychczasowe przypadki przeszły) oraz
  **76918** (audit rendering + production followup, wyjście w
  `.tmp-source-review-projection-regression.txt`). Nie uznawać ich za
  zakończone bez odebrania końcowego wyniku. Ruff `app tests` przeszedł.

### Wyniki ósmej serii i dalsza lektura

- Sesja 83989 zakończona: **17 passed, 1 failed**. Deduplikacja przechodzi,
  lecz test 1000 punktów zmierzył maksymalną przerwę GUI **461 ms** przy
  progu 350 ms (punkt 416). Aktualizacja drzewa maks. 29 ms, preview 31 ms.
  Nie uznawać całej responsywności Execution za naprawioną. Test działał
  jednocześnie z drugim przebiegiem GUI; wymaga dalszej diagnozy bez
  równoległego obciążenia, nie podwyższania progu.
- Sesja 76918: **39 passed, 4 failed**. Jeden test oczekiwał kompilacji
  zabronionego 4wire; jego nominalny scenariusz zmieniono na 2wire.
  Trzy warianty renderowania ujawniły początkowy napis OUTPUT OFF przy
  nieznanym stanie. Poprawiono inicjalizację i zachowanie UNKNOWN przy
  czyszczeniu compliance; powtórzenie weryfikacji trwa.

## Dziewiąta seria — jawne wartości Set w edytorach (część A10)

- Prześledzono `load_plan_actions` → pola formularza →
  `planned_parameter_actions` i `configuration_snapshot` w obu edytorach.
  Potwierdzono pomijanie `action['value']` podczas wczytywania.
- Keithley odtwarza osiem obsługiwanych pól, w tym zakresy wraz z ich
  autorange; usuwa poprzednie selekcje i ROI przed ponownym wczytaniem.
  Nie zmieniono egzekucji sprzętowej ani ograniczenia do 2wire.
- Rigol odtwarza wartości i wybiera zgodną reprezentację High/Low lub
  Amplitude/Offset, następnie przelicza reprezentację alternatywną.
  Sprzeczne użycie obu reprezentacji w jednym węźle zgłasza błąd.
- Pokazane dialogi: testy obu kanałów Keithley, obu reprezentacji Rigola
  i istniejący test węzła: **5 passed, 87 deselected**. Pierwszy przebieg
  miał dwie porażki wyłącznie z powodu założenia szerokości >=800 przy
  domyślnym Rigolu 780; test teraz jawnie pokazuje okno 1100x800.
- Ruff `app tests`: **All checks passed**. Pozostałe skutki konfiguracji
  opisane w A10 nadal otwarte; nie jest to zamknięcie całego audytu.
- Uzupełnienie: istniejące testy edytora receptur dla Keithley/Rigola:
  **16 passed, 72 deselected**. Trzy warianty pokazanej karty z nieznanym
  OUTPUT (light/dark, szerokości 1360 i 1000): **3 passed, 6 deselected**.
- Nominalny test authoringu baseline z dozwolonym 2wire po korekcie
  fixture: **1 passed, 8 deselected**.

## Dziesiąta seria — warunki receptury

- Dalsza lektura `recipes/models.py` i `_evaluate_condition` potwierdziła,
  że `condition: 'false'` obok porównania przechodziło parser i dawało
  `bool('false') == True`, pomijając porównanie.
- Parser odrzuca nieboolowski condition nawet przy kompletnym porównaniu.
  Kompilator niezależnie waliduje typ oraz sprzeczne jednoczesne użycie
  condition i pól porównania, także dla RecipeNode zbudowanego bez YAML.
- Regresje ośmiu niepoprawnych typów/wartości na obu granicach, true/false
  i istniejące porównanie wartości sweepa: **11 passed, 46 deselected**.

## Jedenasta seria — pojedynczy zapis i domykanie archiwum

- Usunięto retry `append` po TypeError zawierającym `device_states`.
  Wyjątek wewnętrzny po częściowym zapisie nie może powodować drugiego
  append bez stanu urządzeń. Trzy testowe MemoryWriter przyjmują teraz
  jawnie to pole kontraktu; produkcyjny writer już je obsługiwał.
- Nowa reprodukcja wywołuje mutację, następnie TypeError: runner zapisuje
  dokładnie raz i propaguje błąd wraz z zachowaniem metadanych stanu.
- Konstruktor HDF5 zamyka otwarte HDF5/CSV przy błędzie inicjalizacji,
  zachowując częściowy plik jako faulted, kiedy zapis statusu jest możliwy.
  Błędy cleanup nie zastępują pierwotnego wyjątku.
- `close` podejmuje kolejne kroki finalizacji i zamknięcia mimo wyjątków,
  zapisuje faulted i szczegóły błędu przed zamknięciem, jeśli plik na to
  pozwala. Błąd jest zatrzaskiwany; kolejne close nie pozoruje sukcesu.
  Wyjątek samego walidatora końcowego również oznacza faulted.
- Pierwsza szeroka seria runner/policy: **118 passed, 9 subtests passed,
  2 failed**. Oba failures: stary fake Anritsu nie implementował potwierdzenia
  abort_acquisition w nominalnym shutdown. Uzupełniono fake o tę metodę;
  nie złagodzono produkcyjnego potwierdzania zatrzymania.
- Fault injection konstrukcji/finalizacji, append i regresje policy:
  **42 passed, 4 subtests passed**; Ruff `app tests` przeszedł.
- To nie zamyka transakcyjności starych referencji, globalnego deadline
  shutdown ani pozostałych ustaleń raportu. Nie wykonano awarii zasilania
  ani testów na aparaturze fizycznej.

## Dwunasta seria — transakcja referencji i cleanup recovery

- `store_reference` zapisuje dane w `/_pending/reference_<index>`, wraz
  z prywatnym `reference_transaction_version=1` i znacznikiem complete.
  Flush danych poprzedza complete i publikację linku w `/references`.
  Alias `/reference` pozostaje zachowany dla pierwszej referencji.
- Błędy po utworzeniu datasetu, po publikacji linku i podczas końcowego
  flush wycofują nową referencję bez naruszania poprzedniej. Nieudany
  rollback zatrzaskuje storage fault i blokuje następne zapisy.
- Czytniki, recovery oraz checkpoint powołujący się na referencję
  odrzucają znacznik incomplete. Nieznana wersja transakcji jest błędem.
  Archiwa sprzed znacznika pozostają czytelne z dotychczasową walidacją;
  nie dopisujemy im fikcyjnego historycznego dowodu commit.
- `average_count` wymaga dodatniej liczby całkowitej (bool/float nie są
  poprawnym licznikiem). Obszar pending nie trafia do listy referencji.
- Naprawiono również wyciek CSV przy błędzie odbudowy indeksu podczas
  resume. Cleanup HDF5 i CSV podejmowany niezależnie; pierwotny błąd
  pozostaje widoczny.
- Testy transakcji, reader/recovery/append rejection, zgodności starszego
  pliku, istniejący pełny scenariusz reference resume i portable reference:
  **7 passed**. Następnie cleanup + run recovery + transakcja:
  **15 passed**. Ruff `app tests`: **All checks passed**.
- Zakres dowodu: kontrolowane wyjątki i ręcznie pozostawiony pending,
  bez fizycznej awarii dysku/zasilania. Pozostałe punkty raportu otwarte.

## Trzynasta seria — koszt trajektorii MOKE i tolerancja rampy

- Pełna walidacja niezmiennej trajektorii pozostaje przy configure i arm.
  Każdy kolejny punkt sprawdza binding/fingerprint, limity, kolejność
  i pojedynczą wartość, bez ponownego przebiegu po wszystkich celach.
  Retarget pojedynczego celu nadal podlega pełnej walidacji.
- Normalna rampa kończy się po wysłaniu docelowej wartości i odczycie
  zgodnym z tolerancją protokołu 1 mV, zamiast wymagać 1e-12 V. Wartość
  actual pozostaje rzeczywistym odczytem, a nie kopią requested/applied.
  Nie kończymy rampy na odczycie pośredniego kroku tylko dlatego, że
  leży blisko celu.
- STOP/zero zachowuje bardziej rygorystyczne potwierdzenie do jednego
  LSB. Po settling readback sprawdzany względem celu, co zapobiega
  sumowaniu dwóch kolejnych tolerancji odczytu. Odczyt poza zakresem
  stacji nie jest akceptowany nawet wtedy, gdy mieści się w tolerancji SET.
- Test trajektorii 10000 celów i wykonania 100 punktów: dokładnie dwie
  pełne walidacje; zmieniony profil blokuje kolejną mutację. Odczyt ±0.4 mV
  od applied kończy jeden docelowy SET, bez zapętlenia i bez fikcyjnego
  potwierdzenia bezpiecznego stanu.
- Nowe przypadki + dotychczasowe rampy, cancellation, live retarget,
  feedback i zero: **51 passed**. Dodatkowy przypadek granicy stacji:
  **4 passed** w powtórzeniu nowych testów. Ruff `app tests` przeszedł.
- Weryfikacja na symulatorze/fake readback. Nie zmieniano ani nie
  kwalifikowano analogowego zachowania układu MOKE/Kepco.

## Czternasta seria — semantyka edycji i aktualność preflight

- Edytory konfiguracji Keithley/Rigol/Anritsu/Anritsu SG oraz akcje i eLab
  zachowują jawny disabled podczas zmiany ustawień. Wyłączenie węzła
  nie znika wskutek rekonstrukcji jego słownika. Zarządzana akwizycja
  zachowuje własny disabled także przy aktualizacji nadrzędnego Anritsu.
- Duplikowanie przepisuje identyfikatory całego poddrzewa, następnie
  wewnętrzne managed_acquisition_id, również w gałęzi else. Powiązanie
  prowadzi do skopiowanej akwizycji, nie do węzła w oryginale.
- Przy edycji akwizycji usuwane są stare minimum_duration, purpose i
  inter_sweep_delay przed nałożeniem nowych pól. Ustawienie 0 s nie
  zachowuje już ukrytego starego minimum 30 s.
- Preflight w tle otrzymuje głęboką kopię Settings. RecipePage rozlicza
  generację ustawień; set_settings żąda anulowania starego workera,
  a wynik starej generacji jest odrzucany nawet przy niezmienionym YAML.
- Nowe regresje + wybrane dotychczasowe scenariusze edytora:
  **15 passed**, potem **17 passed** po zmianie preflight. Weryfikowano
  pokazany dialog i rzeczywiste przejście dialog → serializacja → parser.
  Ruff `app tests` przeszedł. Pełny test_recipe_builder trwa osobno.
- Dodano także egzekwowanie blokady w samych metodach New oraz Apply YAML
  podczas wykonania planu; blokada przycisku nie jest jedyną ochroną.
  Nowy zestaw wraz z regresją tych metod: **9 passed**; Ruff przeszedł.
- Pełny `tests/test_recipe_builder.py`: **88 passed, 4 subtests passed**,
  74.70 s. Nie wykonywano operacji na fizycznych urządzeniach.

## Piętnasta seria — anulowanie configure→ON i prezentacja compliance

- Ręczne OFF usuwa oczekujące auto-enable dla wskazanego kanału przed
  wysłaniem OFF. Wyłączenie przez toggle działa również przy dotychczasowym
  stanie OFF/UNKNOWN, kiedy w kolejce czeka configure. Późne configure
  nie może już dopisać nowego ON. Grupowe OFF również odwołuje zamiar ON
  dla kanałów należących do wskazanej grupy.
- Usunięto wtórne ustawianie boolowskiego proxy po ustawieniu comboboxa
  compliance. Wartość skip nie zostaje już zamieniona na warn_clamp przez
  aktualizację False. Dotyczy readiness, Settings, sukcesu i błędu żądania.
- Teksty charakteryzacji nie zalecają już podłączania przewodów 4-wire;
  informują o zakazie remote sense i wymaganym 2wire. Guardy bezpieczeństwa
  pozostają bez zmian.
- Nowe testy pokazanego okna i kolejności odpowiedzi dla A/B oraz policy:
  **3 passed**. Testy zakazu 4-wire w profilu, adapterze, walidacji i
  kompilatorze: **14 passed**. Ruff `app tests` przeszedł.
- To nie rozwiązuje osobnego ustalenia o przypisywaniu wielu odpowiedzi
  tego samego rodzaju do kanałów przez `_pending_channels`; nadal otwarte.
- Dotychczasowe regresje configure→ON, grupowego OUTPUT i compliance
  kanałowego: **3 passed, 167 deselected**.

## Szesnasta seria — spójność osi i świeżość widma Anritsu

- Fast binary fetch odczytuje aktualne start/stop/points przed transferem
  i potwierdza je po transferze. Cache służy tylko do ponownego użycia
  tablicy osi przy identycznych potwierdzonych parametrach. Zmiana zakresu
  na panelu przy tej samej liczbie punktów nie zachowuje starej osi;
  zmiana podczas transferu odrzuca ramkę i unieważnia cache.
- `acquire_fresh_trace` korzysta z kwalifikowanego single sweep i jego
  identyfikatora/czasów. Usunięto heurystykę zmienności próbek i fallback
  do pierwszego/starego bufora po timeout lub wyjątku. Niezweryfikowany
  profil nie otrzymuje sztucznego dowodu świeżości.
- `acquire_single_sweep` przyjmuje opcjonalny deadline oczekiwania;
  niepoprawny/nieskończony timeout jest odrzucany przed rozpoczęciem sweepa.
  `wait_complete` ustawia czas VISA według pozostałego budżetu akwizycji:
  query za *WAI może oczekiwać dłużej niż zwykły timeout komunikacji.
  Poprzedni timeout sesji jest przywracany także po błędzie.
- Błąd oczekiwania powoduje abort acquisition, bez zmiany aplikacji na SG
  i bez dodatkowej mutacji RF. Błąd abort nie zastępuje pierwotnego błędu.
  Odpowiedzialność runnera za końcowy shutdown posiadanych wyjść pozostaje.
- Nowe regresje + fast acquisition: **17 passed**. Dalsze testy adaptera,
  kolejności komend, deadline i regresji protokołu: **29 passed,
  101 deselected**. Ruff `app tests`: **All checks passed**.
- Testy simulator/fake transport. Nie oznacza to kwalifikacji firmware
  fizycznego MS2830A ani atomowego snapshotu przy zmianach front-panel ABA.

## Siedemnasta seria — luki na wykresie, hold i koszt estymacji tła

- Powtórzono kwalifikację Execution 1000 punktów bez równoległych testów
  GUI: **1 failed**, maksymalna przerwa **387.96 ms** przy limicie 350 ms,
  tym razem przed pierwszym zapisanym punktem. Drzewo maks. 34.68 ms,
  preview 27.16 ms. Wszystkie 1000 punktów zapisano. Artefakt:
  `artifacts/sweeps-spectrum/stress-runtime.json`. Następny kierunek:
  profilowanie startu wykonania; problem responsywności nadal otwarty.
- Bazowy SpectrumPlotWidget zachowuje próbki brakujące i rysuje połączenia
  tylko między skończonymi wartościami. Nie skleja fragmentów po usunięciu
  NaN; eksport CSV zachowuje brakujące wartości. Peak search obsługuje
  także całkowicie pustą skończoną serię bez nanargmax exception.
- Bazowy Max/Min hold resetuje się po zmianie osi również przy tej samej
  liczbie punktów; nie łączy danych z innych częstotliwości. Hold zachowuje
  wcześniejszą skończoną obserwację przy brakującej nowej próbce.
- Rolling noise floor dla szerokich okien używa drzewa liczności po
  kompresji współrzędnych: O(N log N), pamięć O(N). Zachowuje 30. percentyl
  z interpolacją liniową i brzegiem edge. Małe okna pozostają wektorowe.
  Nie zmieniano fizycznego znaczenia ani parametrów estymacji tła.
- Regresje renderowanej luki/CSV, osi hold, percentile dla danych losowych,
  stałych, powtarzanych i monotonicznych, test braku macierzy Nxwindow,
  dotychczasowe plot/analysis/workbench: **62 passed**. Ruff przeszedł.
- Nadal otwarte m.in. dopasowanie wielu modeli pików i pozostałe ścieżki
  obliczeń/I/O w GUI. Nie utożsamiać tej optymalizacji z pełną naprawą FPS.

## Dalsza lektura — start Execution

- Ponownie prześledzono `semantic_tree_snapshot`, `run_started`,
  `_build_live_manifest`, `RunController.start`, reset modelu/rozwijanie
  drzewa oraz blokowanie formularzy w głównym oknie. Przeczytano cały
  `app/ui/execution/plan_timeline.py`. Rejestr `coverage.json` pozostaje
  historycznym zapisem pierwotnej lektury, nie hashami obecnych napraw.
- Źródła potwierdzają synchroniczne parsowanie receptury, budowę timeline
  i blokowanie kontrolek na ścieżce startu. Samo występowanie tych operacji
  nie dowodzi, która spowodowała wcześniejsze 388 ms opóźnienia.
- Dodano osobny profil startu planu 1000 punktów w pokazanym oknie,
  z izolowanymi ustawieniami/katalogiem i symulacją. Wynik: **1 passed**,
  Ruff przeszedł. Odcinek profilowany: 0.390 s; `set_plan`: 0.068 s;
  cztery wywołania `processEvents`: łącznie 0.061 s. To cProfile z narzutem,
  z natychmiastowym Stop i obsługą zakończenia, nie pomiar zwykłego runu.
  Nie obejmuje całego preflight ani blokowania formularzy głównego okna.
- Próba nie usuwa ani nie wyjaśnia jeszcze regresji limitu 350 ms.
  Potrzebny rozdzielony pomiar etapów normalnego startu i stosów oczekiwania.
  Nie kwalifikowano sprzętu ani całej aplikacji do produkcji.

## Osiemnasta seria — tożsamość osi Rigola i tryb continue

- Normalizacja receptury odrzuca Rigol binding, którego endpoint lub
  parameter_id nie odpowiada targetowi. Provider niezależnie sprawdza
  zgodność targetu z kanałem właściciela, parametrem i wymiarem wielkości.
  Nie można skierować wartości częstotliwości do gałęzi offsetu przez
  sprzeczne pole binding. Walidacja zachodzi przed wykonaniem planu.
- Przy szukaniu bazowych poziomów provider wybiera configure_rigol tylko
  dla właściwego kanału; brak takiej konfiguracji jest błędem zamiast
  przejęcia poziomów drugiego kanału.
- `output_policy: continue` stosuje również jawne stałe Set. Po sprawdzeniu
  stanu wejściowego aktualizuje wybrane parametry i pierwszy punkt ROI,
  bez ponownej pełnej konfiguracji i przełączania OUTPUT w tych akcjach.
  Usunięto duplikację aktualizacji pierwszego punktu.
- Pierwsza aktualizacja ROI zachowuje target/requested_si/applied_si.
  Stała zmiana poziomów przy sweepie częstotliwości nie dostaje tożsamości
  tego punktu ROI. Dla amplitudy applied_si jest high-low, dla offsetu
  (high+low)/2; wcześniej obie gałęzie używały low_level_v.
- Testy nowych wiązań/continue/metadata oraz dotychczasowe semantic tree,
  compiler i selected mutations: **100 passed, 6 subtests passed**.
  Ruff `app tests`: **All checks passed**. Testy bez fizycznej aparatury;
  nie dowodzą analogowej płynności przejścia ani kwalifikacji firmware.
- Pozostały zakres raportu, w tym wydajność Execution i pozostałe ukryte
  skutki konfiguracji/OUTPUT, nadal wymaga domknięcia.

## Dziewiętnasta seria — interpretacja szeregów HDF5/CSV

- Hdf5SeriesReader używa ciągłego prefiksu complete checkpoints. Nie pokazuje
  niezacommitowanego ogona i nie używa go do wyboru dostępnych kanałów.
  Lista kanałów uwzględnia także kanały pojawiające się po pierwszym punkcie.
- Brak lub błędna wartość wybranego Y nie jest zastępowana innym pomiarem
  ani zerem. Brak X wybranego setpointu nie jest zastępowany indeksem.
  Czytniki HDF5 i CSV zachowują pary i miejsca braków jako NaN prezentacji;
  nie zapisują tych zastępników do archiwum źródłowego. Jawna preferencja
  nieistniejącego kanału pozostaje brakującym kanałem, bez zmiany wielkości.
- Jednostki pochodzą z persisted_quantity_unit zamiast fragmentów nazw:
  power_w pozostaje W, power_dbm pozostaje dBm, field_t pozostaje T.
  Nieznane nazwy nie otrzymują zgadywanej jednostki Oe/dBm.
  Priorytet pojedynczej litery r nie wybiera już np. temperature.
- Pomocnicza ścieżka widma sprawdza commit oraz długości/skończoność tablic.
  Naprawiono konstrukcję pustego widma bez wymaganego y_values.
- Wykres inventory w trybie jednej i wielu krzywych zachowuje luki:
  finite connections, bez downsamplingu i clippingu dla danych z lukami.
  Test sprawdza pokazane okno i brak odcinka QPainterPath ponad brakiem.
- Czytniki + renderowanie + MOKE: **27 passed**. Po końcowej zmianie
  preferencji CSV: **14 passed**. Ruff app/tests przeszedł.
- Szerszy przebieg characterization report: **20 passed, 2 failed**.
  Niezależne oczekiwania testów: domyślne Source Autorange True (model ma
  False), historyczna narracja dielectric breakdown (obecny raport nie
  wnioskuje braku uszkodzeń z compliance). Nie zmieniano tych testów ani
  polityki źródła w tej serii; regresje pozostają do uporządkowania.
- Podczas śledzenia odbiorcy stwierdzono dodatkowo, że summary_items
  inventory.analysis nadal formatuje Hc/offset jako Oe bez przekazanego
  kontraktu jednostki osi. Nie utożsamiać naprawy etykiet czytnika z naprawą
  całej analizy inventory. Materializacja dużych serii/I/O GUI pozostaje otwarta.

## Dwudziesta seria — niezależne nastawy symulowanego Keithley

- Leveli i levelv mają odrębne readbacki. Zapis nieaktywnej nastawy nie
  zmienia aktywnego źródła, symulowanego pomiaru ani compliance. Zmiana
  source.func wybiera wcześniej zaprogramowaną nastawę właściwego trybu.
  Dotyczy obu kanałów; nie zmieniano komend fizycznego adaptera.
- Domyślne limity modelu i readback są zgodne: 0.1 V / 0.1 A zamiast
  readback 0.1 przy faktycznym braku ograniczenia (infinity).
- Nieznane pola przypisania i nierozpoznane write Keithley są odrzucane
  przed mutacją modelu, zamiast cichej akceptacji literówek. Nie jest to
  pełny interpreter TSP; walidacja wszystkich wartości enum i pełna
  ścisłość symulatora Anritsu pozostają osobnym zakresem.
- Nowe testy sprawdzają readbacki, przełączenia obu trybów, compliance,
  niezależność A/B i brak mutacji po nieobsługiwanym zapisie.
- Szerszy przebieg ujawnił 13 starych testów bez jawnego zakresu źródła.
  Fixture'y otrzymały jawne stałe zakresy. Trzy scenariusze przełączania
  kanałów dodatkowo żądały compliance B przekraczającego dwustronną
  granicę trip; zmniejszono ich nastawy do 1 mV / 0.4 mA, zachowując
  sprawdzanie kolejności ON/OFF i izolacji kanałów. Limitów aplikacji,
  ochrony compliance i walidacji bezpieczeństwa nie osłabiono.
- Końcowy przebieg: source-review simulator levels + simulators +
  Keithley coupled ranges: **73 passed, 26 subtests passed**. Ruff
  `app tests`: **All checks passed**. Wyłącznie symulacja, bez sprzętu.

## Dwudziesta pierwsza seria — ograniczenie alokacji i uszkodzone HDF5

- Discovery liczy liczbę użytecznych adresów IPv4 przed materializacją
  subnet.hosts(), z poprawnym traktowaniem /31 i /32. max_hosts musi być
  dodatnią liczbą całkowitą także w skanowaniu jawnego zakresu IP.
  Test /8 zabrania wejścia do hosts(); testy nie łączą się z siecią.
- Zamknięcie MokeCalibrationRunStore i kontrola hash pliku golden używają
  hashlib.file_digest zamiast read_bytes. Hash nadal powstaje po udanym
  zamknięciu archiwum, bez alokacji zawartości całego pliku.
  Samo przeniesienie pozostałych operacji hashowania/UI do workerów jest
  odrębnym, nadal otwartym ustaleniem raportu.
- Zweryfikowano aktualny resolve_platform_env_path: parents[2] wskazuje
  katalog repozytorium. Historyczne ustalenie o katalogu app nie jest już
  obecne w tej wersji; nie wykonywano zbędnej zmiany tej funkcji.
- Walidator HDF5 zamienia błędy kształtu/typu/odczytu struktury na jawne
  CompatibilityIssue i kontynuuje niezależne sekcje. Nie wywołuje PyThat
  dla pliku z błędami. Typy atrybutów root są sprawdzane przed int().
- Regresje obejmują atrybut tekstowy/tablicowy zamiast liczby, dataset
  zamiast grup devices/scan_definition, grupę zamiast tree/row/data,
  tabelę numeryczną zamiast tekstowej i tablicowy status run.
- Discovery + bounded IO + MOKE calibration: **49 passed**. Walidator:
  **12 passed, 1 skipped, 3 subtests passed**. Skipped: brak laboratoryjnego
  golden HDF5 w checkout; nie jest to dowód jego zgodności. Ruff przeszedł.

## Dwudziesta druga seria — koszt dopasowania pików

- Fit Gaussian/Lorentzian zachowuje siatkę 21 centrów × 20 szerokości
  na model i regresję baseline/amplitude, ale używa wsadowego SVD zamiast
  840 osobnych wywołań lstsq i pętli Python. Próg wartości osobliwych
  odpowiada lstsq(rcond=None). Macierze są ograniczone oknem 101 próbek.
- Kandydaci są najpierw mierzeni i sortowani po SNR/prominencji; dopasowania
  są obliczane leniwie do wypełnienia żądanej liczby zaakceptowanych pików.
  Reguły odrzucania nakładania się pików pozostają. Pełne grupy dokładnych
  remisów są dopasowywane przed wyborem według RMSE, bez zmiany tie-break.
- Testy porównują z wcześniejszym skalarnym algorytmem oba modele, trzy
  szerokości i dane z/bez szumu. Sprawdzają ograniczone macierze, zatrzymanie
  dopasowań po limicie oraz zachowanie wyboru RMSE przy remisie.
- Nowe i dotychczasowe testy analizy: **23 passed**. Workbench + noise floor:
  **44 passed**. Ruff przeszedł. Lokalny mikrobenchmark: najlepszy czas z
  5 serii po 10 fitów: stary 54.86 ms/fit, wsadowy 4.96 ms/fit.
  Nie zastępuje to kwalifikacji responsywności całego Execution ani naprawy
  pozostałych obliczeń wykonywanych w GUI i zamykania workera analizy.

## Dwudziesta trzecia seria — jednostki delta i pochodzenie hold

- Delta markerów SpectrumWorkbench zachowuje jednostkę wielkości liniowej
  (W, V, ratio); jedynie różnica dBm jest etykietowana dB. Różnica mocy
  nie jest już opisywana jako linear ratio. Wartości nie są przeskalowywane.
- Bazowy SpectrumPlotWidget czyści Max/Min hold przy zmianie jednostki osi
  oraz głównego śladu. Nie łączy hold poprzedniego źródła z nowym mimo
  identycznej siatki. Użytkownik ponownie włącza hold dla nowego kontekstu.
- Testy obejmują pokazany workbench i delta W/V/dBm/dB/ratio oraz zmianę
  jednostki i Raw → Residual w bazowym hold. Łącznie z dotychczasowymi
  plot/workbench: **39 passed**. Ruff app/tests przeszedł.

## Dwudziesta czwarta seria — ograniczone zamykanie analizy

- SpectrumAnalysisController.close zwraca wynik zakończenia wątku i czeka
  najwyżej zadany timeout (domyślnie 100 ms), bez wcześniejszego fallbacku
  wait() bez limitu. Odrzuca kolejne submit, usuwa pending i ignoruje późne
  wyniki/błędy; nie niszczy ani nie terminates działającego wątku.
- Strona Anritsu i główne okno sprawdzają wynik obu kontrolerów. Żądanie
  zamknięcia jest ignorowane, gdy obliczenia jeszcze trwają; własność QThread
  pozostaje zachowana do kolejnej próby. Oba workery dostają żądanie stop.
- Dodano kontrole interruption przed obliczeniami, po cleanup, po pikach,
  podczas konwersji referencji oraz przed materializacją spektrogramu.
  Trwającego pojedynczego wywołania NumPy nie przerywa się destrukcyjnie.
- Regresje: blokowany worker, bounded close, brak późnego wyniku/pending,
  zakończenie po zwolnieniu pracy, obsługa obu kontrolerów oraz pokazane
  okno główne odrzucające close do zakończenia analizy. Wraz z wcześniejszymi
  worker/shared-background tests: **18 passed**. Ruff przeszedł.
- Ta poprawka dotyczy analizy widma; nie rozstrzyga pozostałych deadline
  zamykania aparatury ani wszystkich źródeł opóźnienia Execution.

## Dwudziesta piąta seria — pochodzenie potwierdzeń w dry run

- Lektura RecipeRunner wykazała, że każdy tryb inny niż measurement
  etykietował potwierdzenie jako simulated_ack, również fizyczny dry run.
  Rodzaj dowodu wynika teraz z dostępnego readback, zakończonego wait
  lub wykonania akcji. Tryb symulacji pozostaje osobnym metadanym runu.
- _confirmed_semantic_value nie traktuje już applied_si z payloadu planu
  jako odczytu urządzenia, gdy brakuje wartości w kontekście wykonania.
  Zachowuje wartość planowaną, ale readback pozostaje None.
- Test rzeczywistej ścieżki adapterów z podstawionym transportem sprawdza
  potwierdzenia w HDF5, faktyczne wartości widm z transportu oraz brak ON.
  Dodano sześć przypadków braku dowodu dla aktualizacji Keithley, Rigol
  i Anritsu. Z testami production_followup: **52 passed**. Ruff przeszedł.
- Nie komunikowano się z fizycznym sprzętem. Nie jest to kwalifikacja
  wszystkich pól konfiguracji ani wszystkich źródeł runtime context.

## Dwudziesta szósta seria — kolejność potwierdzeń Keithley A/B

- Prześledzono request_output_off/group, configure → ON, _result/_error,
  _reset_output_toggle, live compliance, DeviceController.call oraz
  adapter.update_source_compliance. Controller może zgłosić błąd synchronicznie
  z guardu, dlatego nie wystarcza dopisanie kolejki nazw kanałów do odbiornika
  odpowiedzi. Strona wysyła najwyżej jedno żądanie danego rodzaju naraz.
- Kolejne pojedyncze OFF zachowują kanały do obsłużenia, zamiast nadpisywać
  kanał oczekujący na odpowiedź. Grupowe OFF zgłoszone podczas trwającej
  operacji grupowej są łączone i wysyłane po jej sukcesie lub błędzie.
  To obsługa kolejki normalnych komend, nie niezależny sprzętowy E-STOP.
- ON nie nadpisuje trwającego configure/OUTPUT/ramp. Kontynuacja configure →
  ON jest anulowana, jeśli w międzyczasie trwa inna operacja OUTPUT;
  ponowne włączenie wymaga jawnego żądania operatora.
- Nieboolowskie lub brakujące potwierdzenie OUTPUT pozostawia UNKNOWN.
  Błąd usuwa właściwy pending i oznacza właściwy kanał jako UNKNOWN;
  reset przycisku nie podnosi już starego cache do potwierdzonego stanu.
  Wynik grupowy rozróżnia ON/OFF/MIXED/UNKNOWN; niepełny wynik unieważnia
  brakujące potwierdzenie, zamiast zachować wcześniejszy stan kanału.
- Zmiany compliance przechowują kanał, tryb i SI z chwili żądania.
  Podczas trwającej operacji zachowywana jest ostatnia oczekująca wartość
  każdego kanału. Potwierdzenie używa readback adaptera i jednostki trybu
  tego żądania, a nie obecnego formularza. Błąd/brak odczytu odrzuca kolejkę;
  OFF usuwa oczekujące zmiany danego kanału. Wyłączenie live/disconnect
  zatrzymuje wysyłanie oczekujących zmian.
- Regresje: **9 passed** (kontynuacja i pokazany shell), **22 passed**
  (kolejka compliance, blokowanie nakładającego ON, dual plots),
  **3 passed** (istniejące shell tests normalnego ON/OFF i grupowego OUTPUT).
  `ruff check app tests` przeszedł. Wyłącznie mocki/symulacja, bez sprzętu.
- Pierwsze wspólne uruchomienie compliance/dual plots: 21 passed, 1 failed
  — fixture shell ustawiała globalnie Segoe UI 10 i przy 1280×720 ujawniła
  44 px pionowego scrolla. Test geometrii uruchomiony osobno przeszedł.
  Nowe testy samej strony nie używają już fixture zmieniającej globalną
  czcionkę; ponowne 22 testy przeszły. Responsywność strony przy zwiększonej
  czcionce pozostaje osobnym otwartym ograniczeniem, nie naprawą tej serii.

## Dwudziesta siódma seria — luki pomiarowe i cykl życia charakteryzacji

- Przeczytano ścieżkę _on_point_acquired → wykresy, renderowanie zapisanych
  krzywych, closeEvent/prepare_application_shutdown oraz set_data wykresu
  V/I. Live nie zastępuje już nieokreślonej rezystancji poprzednią wartością
  ani zerem. Nieważny punkt pozostawia lukę także w I–V. Krzywe używają
  connect=finite; skończona rezystancja o dowolnym znaku jest zachowywana.
- TwinAxis zachowuje wspólną oś czasu i niezależne luki V/I. Brak prądu
  nie usuwa poprawnego napięcia i odwrotnie. Markery compliance są rysowane
  niezależnie dla dostępnych wielkości. Readout ostatniej próbki pokazuje
  brak wartości zamiast cofać się po cichu do starszego pomiaru. Niezgodne
  długości/kształty wejść są odrzucane przed zmianą wykresu.
- Zamknięcie karty używa wspólnej kontroli zakończenia pomiaru, odzyskiwania,
  lease, przywrócenia polityki i raportowania. Aktywny pomiar dostaje Stop;
  close jest ignorowane do zakończenia pracy. Usunięto wait(2000) w GUI,
  którego wynik wcześniej pomijano. Nie niszczy się działającego workera.
- Szersza regresja ujawniła brak podłączenia reservation_changed oraz
  field_policies_changed do strony Keithley. Dodano blokowanie ręcznej
  konfiguracji/ON/pomiarów na czas rezerwacji, wstrzymanie Live, zachowanie
  jego wyboru po release oraz projekcję potwierdzonych polityk A/B.
  VERIFIED odebrane podczas lease nie zastępuje polityki workera domyślną.
  OFF jest dostępne również przy pending ON lub zarezerwowanym urządzeniu.
  Istniejący _RunAccess dopuszcza OFF i przerywa właściciela rezerwacji.
- Test rezerwacji używa sygnałów Qt kontrolera i karty, a nie bezpośredniego
  wywołania samej metody projekcji. Testy zamykania obejmują sześć rodzajów
  aktywnej pracy, brak wait oraz ponowne skuteczne close po jej zakończeniu.
- Poprawiono fixture testów pola: izolowane QSettings, jawny wybór A,
  jawny zakres B obejmujący testowane 10 mA (żeby test deadline docierał do
  tej walidacji) i isRunning=False dla zakończonego fikcyjnego workera.
  Nie osłabiono walidacji produkcyjnych. Wstępna regresja miała 7 błędów;
  ujawniła opisany brak obsługi rezerwacji oraz zależności fixture.
- Końcowe testy field scenario, nowych luk/close, kolejki compliance
  i dual plots: **42 passed**. Device lease i field worker: **32 passed**.
  Pełna regresja characterization UI: **34 passed**. `ruff check app tests`
  przeszedł. Łącznie 108 testów w końcowych zestawach, bez fizycznego sprzętu.
- Nie zmieniano zapisanych wartości pomiarowych ani komend protokołu aparatury.
  Pozostaje osobny problem synchronicznych operacji I/O i analizy w GUI
  charakteryzacji oraz pozostałe otwarte pozycje raportu.

## Dwudziesta ósma seria — pochodzenie ręcznie zapisywanych widm

- Prześledzono ManualSpectrumArchive, kontrakt opcji dialogu, konfigurację
  kontekstu przez MainWindow, wybór payloadu, projekcję Execution oraz
  przekazywanie raw snapshot do wyników analizy.
- MainWindow przekazuje rzeczywisty tryb backendu do archiwum ręcznego.
  Nowy HDF5 zapisuje go w simulation_json; resume odrzuca próbę dopisania
  danych o innym trybie. Nie poprawia się wstecz oznaczeń starych plików,
  których rzeczywistego pochodzenia nie da się odtworzyć z samej flagi.
- Zniknięcie wybranego wariantu kończy zapis błędem zamiast zastępować go
  inną krzywą z niezgodną etykietą trace_variant.
- Podglądy Execution mają oddzielne oznaczenie w stronie. Przycisk zapisu
  jest dla nich wyłączony; sama metoda zapisu także je odrzuca. Pełny wynik
  pozostaje w archiwum sweepa. Zmiana między preview a ręcznym śladem czyści
  historię spektrogramu i unieważnia stare analizy/raw snapshot, także gdy
  obie reprezentacje mają identyczną siatkę.
- Wartości metadanych są pobierane ponownie podczas każdego zapisu.
  Zapis selected wymaga dostępności wszystkich wybranych kluczy, all bierze
  aktualnie dostępny zestaw, none nie pobiera danych urządzeń. Awaria
  providera nie powoduje użycia starej kopii z dialogu. Punkt zawiera czas
  zebrania metadanych i metadata_snapshot_kind=last_confirmed_at_save,
  odróżniający je od timestampu wcześniejszej akwizycji.
- **24 passed**: nowe regresje provenance/metadata/preview, dotychczasowy
  writer manualny (w tym walidacja require_pythat=True) i pipeline przetwarzania.
  **4 passed**: ręczny zapis/modal przez główne okno, wraz z asercją
  simulation=True w rzeczywistym pliku testowej stacji. Ruff app/tests przeszedł.
- Pozostaje przeniesienie ręcznego I/O poza GUI oraz pełne uzupełnienie wieku
  odczytów i unieważniania cache w providerach poszczególnych urządzeń.
  Ta seria nie potwierdza aktualności sprzętowej wszystkich cached metadata.

## Dwudziesta dziewiąta seria — timestamp i unieważnianie metadanych MOKE/Lake Shore

- Prześledzono tworzenie ManualMetadataValue, eksport descriptorów do HDF5,
  odbiór VOUT/Hall/Lake Shore oraz obsługę utraty połączenia na obu stronach.
  Eksport korzysta z oddzielnego ostatniego odczytu, nie z ostatniej pozycji
  historii wykresu. Historia pozostaje dostępna po disconnect, ale nie jest
  źródłem metadanych nowego pomiaru po reconnect.
- DISCONNECTED/FAULT i nieużyteczne UNKNOWN unieważniają eksportowane
  odczyty oraz bieżące wskazania. Dla MOKE zachowano rozróżnienie UNKNOWN
  stanu Kepco przy nadal działającym połączeniu DAC/Hall; nie dowodzi ono
  utraty potwierdzonego odczytu DAC.
- ManualMetadataValue ma opcjonalny recorded_at_utc: timestamp bez strefy
  jest odrzucany, ze strefą normalizowany do UTC. Hall i Lake Shore przekazują
  timestamp odczytu; VOUT zapisuje czas otrzymania odpowiedzi przez aplikację
  i opisuje to jawnie w source. Nie przypisuje się czasu akwizycji widma
  do odczytów innych urządzeń. Pozostali providerzy mogą nadal mieć czas
  nieznany, zapisany jawnie jako null.
- Deskryptory HDF5 zachowują recorded_at_utc, a podpowiedź wartości w dialogu
  pokazuje datę lub unknown. Czas snapshotu przy zapisie z serii 28 pozostaje
  oddzielny od czasu samego odczytu.
- Testy nowych timestampów, disconnect/reconnect oraz writer manualny:
  **9 passed**. Po uzupełnieniu czyszczenia bieżących wskazań ponowne
  **5 passed**; regresja provenance ręcznego zapisu **4 passed**.
- Szerszy zestaw obejmujący workflow MOKE, writer i pierwsze testy wieku:
  **41 passed**, 12 ostrzeżeń deprecation QMouseEvent.pos w zewnętrznym
  QFluent sliderze. Ruff app/tests przeszedł. Testy bez fizycznych urządzeń.

## Trzydziesta seria — jednoznaczny wynik workera MOKE po cleanup

- MokeFieldWorker nie emituje już succeeded przed zwolnieniem rezerwacji.
  Najpierw kończy strumień Live i próbuje zwolnić wszystkie leases, następnie
  emituje dokładnie jeden wynik: sukces albo zbiorczy błąd, a na końcu finished.
- Błąd zamknięcia strumienia lub jednej rezerwacji nie pomija kolejnych
  zwolnień. Błąd operacji, awaryjnego wyłączenia i cleanup zachowuje osobne
  przyczyny w jednym komunikacie. Niepotwierdzone release pozostaje błędem,
  a blokada kontrolera nie jest obchodzona ani oznaczana jako zwolniona.
- Przy błędzie uzyskiwania początkowych rezerwacji próbowane są wszystkie
  zwolnienia już nabytych zasobów; błąd release nie maskuje pierwotnego błędu.
  Niepełny zestaw leases także kończy się cleanup i finished.
- Zachowano własność cleanup kalibracji w MokeCalibrationRunner: błąd
  preflight kalibracji nadal nie zeruje wcześniejszej nastawy DAC.
- **31 passed**: macierz błędów operacji/Live/release, kolejność sygnałów,
  inicjalizacja oraz regresje kalibracji. **4 passed**: zero, błąd readback,
  zakończenie Live i zamykanie aktywnego workera przez UI MOKE.
  Bez sprzętu; Ruff app/tests przeszedł.

## Trzydziesta pierwsza seria — spójność temporalnego uśredniania Anritsu

- Przeczytano start/request/result/finish temporalnego uśredniania oraz
  acquire_single_sweep i przywracanie Continuous w adapterze.
- Pierwsza poprawna ramka ustala siatkę częstotliwości, generację konfiguracji
  i nazwę śladu dla serii. Kolejne ramki muszą pasować; NaN, nieuporządkowana
  siatka lub różne długości osi/danych są odrzucane. Zmiana przerywa serię,
  czyści częściowy akumulator i zachowuje wcześniejszy ukończony wynik.
- Uśrednianie podczas Live używa teraz tego samego kwalifikowanego
  single_sweep co uśrednianie poza Live. Odczyt aktualnego bufora nie jest
  już liczony jako dowód nowego sweepa. Ewentualna odpowiedź od wcześniejszego
  pollingu Live jest pomijana; następnie żądany jest nowy sweep.
  Istniejący adapter przywraca wcześniejszy Continuous po zakończeniu odczytu.
- Nie zmieniano zwykłego trybu Live ani rolling preview average. Pozostają
  one oddzielnymi ścieżkami wyświetlania wymagającymi dalszej kwalifikacji
  świeżości ramek; nie przypisuje się im gwarancji z tej poprawki.
- Fixture testów temporalnych przekazują wynik przez single_sweep zamiast
  udawać zakończony sweep odpowiedzią fetch_current_trace_fast/fetch_trace.
  Nowa regresja osobno sprawdza odrzucenie rzeczywistej odpowiedzi polling.
- **51 passed**: nowe regresje tożsamości, fast acquisition i unified
  spectrum workflow. **2 passed**: uśrednianie i zachowanie RAW przez główne
  okno. Ruff app/tests przeszedł. Bez fizycznego sprzętu.

## Trzydziesta druga seria — odporność numeryczna akumulatora widm

- Przeczytano cały app/spectrum/processing.py. W LinearPowerAverager
  brakowało kontroli wymiaru tablicy i reprezentowalności mocy po konwersji
  dBm → mW. Dodawanie in-place mogło nieodwracalnie przepełnić akumulator.
- Odrzucane są dane wielowymiarowe, niezgodne długości, NaN/Inf oraz
  konwersje dające zero lub nieskończoność. Nowa suma jest sprawdzana przed
  zatwierdzeniem; błąd zachowuje poprzednią sumę i licznik próbek.
- Wynik używa różnicy logarytmów sumy i liczby próbek: nie dzieli bardzo
  małej mocy przed logarytmowaniem i nie podnosi jej sztucznie do 1e-300 mW.
  Nie zmienia to definicji średniej w liniowej mocy.
- 26 passed: przypadki uszkodzonych ramek, przepełnienia sumy, bardzo
  małej reprezentowalnej mocy, dotychczasowa matematyka i integracja
  temporalnego uśredniania. Ruff dla zmienionych plików przeszedł.
- Pozostałe operacje reference math w tym module wymagają osobnego
  sprawdzenia granic numerycznych; ta poprawka nie kwalifikuje ich ani
  fizycznej akwizycji.

## Trzydziesta trzecia seria — reference math i potwierdzenie kopiowania Anritsu

- Operacje referencji odrzucają tablice wielowymiarowe zamiast dopuszczać
  broadcast NumPy. Stosują obliczenia logarytmiczne do sumy, ilorazu i
  iloczynu, unikając przepełnienia pośredniej konwersji obu widm na mW.
  Odejmowanie korzysta z expm1, zachowując małe różnice; signed W nadal
  zachowuje znak, a logarytmiczna reszta niepozytywna pozostaje NaN.
  Wyniki niereprezentowalne są błędem, nie zerem lub nieskończonością.
- Sprawdzono wywołania w runnerze, display model i preview processing.
  **102 passed**: granice numeryczne, podstawowa matematyka, przetwarzanie
  sweepów, Results i unified spectrum workflow.
- Przeczytano cały readback_dialog.py oraz tworzenie dialogu, porównanie
  formularza, przypisywanie pojedyncze/zbiorcze i transformacje częstotliwości
  w page.py. Usunięto optymistyczne MATCH po samym wyemitowaniu żądania.
  Strona ponownie odczytuje formularz i odświeża wszystkie wiersze, również
  zależne center/span; nieobsługiwana liczba punktów pozostaje rozbieżnością.
- Start/Stop/Center/Span są przypisywane według znaczenia fizycznego w obu
  reprezentacjach formularza. Nieprawidłowe granice są odrzucane przed
  zmianą pól. Operacja kopiuje do formularza, bez komunikacji z aparaturą.
- VBW OFF jest importowane jako OFF, osobno od Auto i Manual. Brak wartości
  w trybie Auto nie stanowi już zgodności z OFF. Nieporównane parametry są
  szare; tolerancja MATCH odpowiada precyzji formatowania, nie 1e-5.
- **26 passed**: nowe regresje przypisywania i fast acquisition; następnie
  **13 passed** po uzupełnieniu fixture renderowania. Ruff dla zmienionych
  plików przeszedł. Testy korzystają z mocków/symulatora, bez sprzętu.
- Render dialogu 1000×720 jest zapisywany do
  artifacts/source-review/anritsu-readback.png. Regressja sprawdza widoczną
  geometrię po show i przetworzeniu zdarzeń. Nie jest to kwalifikacja
  wszystkich okien ani zachowania fizycznego analizatora.

## Trzydziesta czwarta seria — jednostki i klasyfikacja analityki inventory

- Przeczytano cały inventory/analysis.py i measurement_card.py, model serii
  oraz trzy wywołania analityki w przeglądarce pomiarów. Wszystkie przekazują
  teraz x_unit/y_unit z odczytanej serii.
- Usunięto klasyfikowanie dowolnej długiej serii jako MR oraz wnioskowanie
  wymiarów z fragmentów nazw. Parametry MR wymagają osi pola i rezystancji;
  nieznane jednostki nie generują pozornych parametrów fizycznych. Seria
  rezystancji względem czasu ma min/max w ohm, bez fikcyjnego Rp/Rap/Hc.
- Rezystancje są normalizowane do ohm wspólnym parserem jednostek. I–V
  przelicza prefiksy V/A przed wyznaczeniem rezystancji. Ekstrema prądu
  przestały być prezentowane jako R_min/R_max.
- Hc i offset zachowują jednostkę wejściowej osi pola w metadanych wyniku;
  karta i summary_items używają tego samego formatowania. Nie ma domyślnej
  etykiety Oe ani niejawnej konwersji między B i H.
- Poszukiwanie przełączeń nie skleja punktów przez NaN/Inf. Wyznaczanie
  Hc nadal jest heurystyką przecięcia połowy wysokości, nie kwalifikowaną
  analizą wszystkich typów pętli; nie zmieniano tej definicji fizycznej.
- **17 passed**: jednostki T/mT/Oe/A/m, prefiksy rezystancji i I–V,
  odrzucenie fałszywego MR, luki i pokazana karta wyników oraz istniejące
  testy analityki i wykresów. **3 passed**: przeglądarka/drzewo pomiarów.
  Ruff dla zmienionych plików przeszedł. Bez fizycznych urządzeń.

## Trzydziesta piąta seria — measure-only nie autoryzuje OUTPUT Keithley

- Prześledzono pełne i selektywne configure_source, weryfikację konfiguracji
  i zakresów, set_output/set_output_group oraz przepływ autoryzacji w
  kompilatorze. UI już odrzucało ON w measure-only, ale adapter tego nie
  wymuszał. Konfiguracja pomiarowa pozostawia poprzednią funkcję i poziom
  źródła w aparaturze; ich sprawdzanie jest pomijane dla measure-only.
- Adapter blokuje teraz ON w measure-only przed komendą włączenia.
  Grupowe ON waliduje wszystkie kanały przed pierwszą mutacją, więc
  measure-only na B nie pozwala wcześniej włączyć A. OFF nadal działa.
  Ponowna jawna konfiguracja current/voltage przywraca zwykłą ścieżkę ON.
- Kompilator usuwa autoryzację źródła po configure measure-only; wcześniejsza
  konfiguracja prądowa/napięciowa nie uprawnia już późniejszego ON.
- Selektywna konfiguracja measure-only odrzuca pola źródła przed I/O.
  Dotychczas mogła przez gałąź napięciową zapisać niezamówione zera levelv/
  limiti po normalizacji danych pomiarowych. Ustawienia pomiaru pozostają
  dostępne bez tych pól.
- **65 passed**: A/B × retained current/voltage × single/group ON,
  zachowanie nastawy w pamięci symulatora, brak komendy ON po odmowie,
  ponowna konfiguracja, selektywne pola, kompilator, sprzężone zakresy oraz
  zakaz 4-wire. Ruff przeszedł. Testy używają transportu symulowanego;
  nie wykonywano fizycznego włączenia aparatury.

## Trzydziesta szósta seria — jawny konflikt source_autorange zamiast nadpisania

- Prześledzono _compile_keithley, validate_keithley_source, politykę panelu
  plan/manual i budowanie requestu ze snapshotu. Zasada Settings-only jest
  celową blokadą bezpieczeństwa i pozostaje zachowana.
- Kompilator nie nadpisuje już jawnego source_autorange=False wartością
  True z Settings. Konflikt w dowolnym kierunku jest błędem z nazwą węzła
  i obiema wartościami. Pominięte pole nadal dziedziczy politykę Settings;
  jawna zgodna wartość przechodzi bez zmiany. Measure-only wymaga False.
- Ręczny request zachowuje wartość widoczną w przekazanym snapshocie.
  Jeśli snapshot pochodzi sprzed zmiany Settings, walidacja zgłasza
  niezgodność zamiast wysyłać do adaptera potajemnie zmieniony tryb.
- Istniejąca regresja oczekująca cichego nadpisania została zastąpiona
  sprawdzeniem odmowy, następnie jawnego zgodnego AUTO. Zachowano jej
  sprawdzenia transportu, charakteryzacji i cofnięcia zezwolenia w Settings.
- **33 passed**: macierz pól jawnych/pominiętych, nieaktualny snapshot,
  measure-only oraz pełna regresja polityki zakresów. Ruff przeszedł.
  Nie komunikowano się z fizyczną aparaturą.

## Trzydziesta siódma seria — Rigol powtórne ON bez przerwy wyjścia

- Prześledzono set_output, kontrolę sprzężenia kanałów, odczyt trybów
  zaawansowanych, konfigurację ścieżki wyjścia i pełną weryfikację nośnej.
  W aktualnym kodzie pełne przeprogramowanie przy ON było już usunięte,
  lecz pozostało bezwarunkowe OFF przed każdym ON.
- Jeśli adapter ma potwierdzone ON, ponowne ON odczytuje i kwalifikuje
  aktualną konfigurację z expected_output=True, bez żadnej mutacji.
  Weryfikowane są nadal interlock, poziomy, częstotliwość, load, ścieżka
  wyjścia, sprzężenie i tryby zaawansowane. Pierwsze ON zachowuje dotychczasową
  sekwencję OFF/weryfikacja/ON; OFF pozostaje dostępną operacją.
- Rozjazd stanu, zmiana front-panelowa lub błąd odczytu nie są uznawane za
  zgodność: zachowano fallback wyłączenia obu kanałów i weryfikację OFF.
- **27 passed, 2 subtests passed**: nowa macierz oraz dotychczasowe testy
  adaptera/runnera dotyczące Rigola. Po zaostrzeniu sprawdzania przyczyn
  błędów **5 passed** nowych testów. Ruff przeszedł. Dziennik transportu
  symulowanego dowodzi braku komend mutujących przy poprawnym powtórnym ON;
  nie jest to pomiar ciągłości przebiegu analogowego fizycznego generatora.

## Trzydziesta ósma seria — jawne odrzucanie nieobsługiwanego SCPI w symulacji Anritsu

- Przeczytano AnritsuSimulator, wspólną obsługę transportu symulatorów oraz
  komendy inicjalizacji/akwizycji/formatu używane przez adapter. Usunięto
  domyślny sukces nieznanych zapisów; nieobsługiwana komenda zgłasza DeviceError.
- FORM i FORM:BORD mają pełne dopasowanie obsługiwanych argumentów i
  stan odczytywany przez FORM?/FORM:BORD?. Poprzednio każdy napis zaczynający
  się od FORM był akceptowany, a FORM? zawsze odpowiadało ASC,0.
- Zapytania TRAC:TYPE? i TRAC? TRAC1 muszą pasować w całości, bez dowolnego
  ogona czy innego numeru śladu. Usunięto nieosiągalną drugą odpowiedź
  TRAC:TYPE?. Obsługiwana postać TRAC:TYPE 1,VIEW/WRIT faktycznie zmienia stan.
- *CLS, ABORT i *WAI pozostały jawnie obsługiwane; sterowanie single/continuous
  używane przez aplikację nadal działa. Lista jest kontraktem symulatora
  aplikacji, nie deklaracją pełnego zestawu SCPI urządzenia.
- **17 passed**: błędne komendy/argumenty, niezmienność konfiguracji po
  odrzuceniu i odczyt stanu formatów. **28 passed**: fast acquisition,
  regresja protokołu i sweep Anritsu-only z tłem/referencją. Ruff przeszedł.
  Nie kwalifikuje to czasów VISA ani wszystkich zachowań bufora fizycznego
  analizatora; sprzętu nie używano.

## Trzydziesta dziewiąta seria — walidacja argumentów TSP symulatora Keithley

- Przeczytano cały zapis/odczyt i model pomiaru KeithleySimulator. Znane
  pola przyjmowały dowolny tekst, zanim sprawdzano funkcję/poziom/wyjście.
  Powodowało to rozjazd programmed i modelu pomiarowego oraz częściową
  mutację po błędzie konwersji liczbowej.
- Przed zmianą stanu weryfikowane są obsługiwane enumy, składnia liczb,
  skończoność, dodatniość zakresów/NPLC/delayfactor, nieujemne opóźnienia
  i compliance oraz maksima sprzętowe zakresów, poziomów i limitów.
  Nieobsługiwane argumenty nie są potwierdzane jako zaprogramowane.
- Liczbowe 0/1 funkcji, wyjścia i autorange są normalizowane do symboli
  używanych przez model. Dzięki temu funkcja i OUTPUT mają ten sam stan
  w odczycie i w obliczeniu pomiaru. Nie rozszerzano symulatora na dowolne
  skrypty TSP; nieznane wyrażenia pozostają jawnie nieobsługiwane.
- Zapytanie o kilka zakresów wymaga również końcowego nawiasu zamiast
  ignorować ostatni znak. Zachowano wcześniejszą kolejkę błędu sense dla
  nieobsługiwanych symboli SENSE; aplikacyjny zakaz 4-wire jest niezależny.
- **90 passed**: A/B, niepoprawne enumy/liczby/przekroczenia, niezmienność
  stanu po odmowie, funkcja/output liczbowe, stare regresje poziomów,
  zakresy sprzężone i ochrona measure-only. Ruff przeszedł. Bez sprzętu.

## Czterdziesta seria — porcjowanie widm podczas końcowej konwersji PyThat

- Prześledzono writer.close, validator, validation_worker i cały most PyThat;
  przeczytano również źródła zainstalowanego PyThat 0.2.14 dotyczące drzewa,
  Group.get_data, reshape, skali i zapisu NetCDF. Samo usunięcie dataset.load
  nie wystarczało: PyThat materializowało widma już w np.reshape.
- Dodano inspect_measurement_tree dla walidacji: wielowymiarowe publiczne
  dane są przekazywane jako tablice Dask, porcjowane po jednym punkcie.
  Obsłużono zarówno dostęp drzewa, jak i bezpośredni dostęp grup do HDF5.
  Pełna konwersja PyThat i zapis tymczasowego NetCDF nadal obejmują wszystkie
  próbki, z jednowątkowym harmonogramem obliczeń. Nie zastępuje się ich
  próbkowaniem ani samym sprawdzeniem nagłówka.
- Skale odczytują tylko dwie potrzebne liczby pierwszego punktu, zamiast
  całego powtarzanego datasetu skali. Po walidacji zwracane są wyłącznie
  nazwy zmiennych i rozmiary osi. Uchwyty i pliki tymczasowe są zamykane
  przez dotychczasowy finally; nie powstaje sąsiedni plik .nc użytkownika.
- Zwykłe open_measurement_tree nadal jest importem pełnym z kontrolą RAM.
  Nie zniesiono wszystkich limitów/preflight ani nie wykazano stałego RAM
  całego pipeline: osie/skalary, metadane i graf Dask nadal rosną z pomiarem.
  Obecna zmiana usuwa pełną materializację macierzy widm z finalizacji.
  Obsługiwana skala strumieniowej walidacji jest płaska, zgodnie z publicznym
  zapisem aplikacji; nie ogłoszono zgodności wszystkich zewnętrznych wariantów.
- Test blokuje h5py.Dataset.__array__ dla źródłowych macierzy widm i
  xr.Dataset.load podczas close, wymaga porcji po jednym punkcie i porównuje
  wszystkie wartości NetCDF z wejściem oraz pełnym importem PyThat.
- **82 passed, 3 subtests passed, 1 skipped**: writer, validator, uszkodzenia,
  referencje, korekcje i fault injection. Pominięty test wymaga nieobecnego
  laboratoryjnego golden HDF5. Dodatkowy test ze ścieżką walidacji w procesie
  potomnym przeszedł. Ruff dla zmienionych plików przeszedł. Bez sprzętu.

## Czterdziesta pierwsza seria — zatwierdzanie pełnych wierszy dziennika HDF5

- Przeczytano inicjalizację/resume/append_event/close writera, odczyt granic
  recovery, sprawdzanie końcowego DAC zero w kalibracji MOKE i walidację
  prywatnego stanu archiwum. Poprzedni rollback działał tylko po wyjątku
  obsłużonym w procesie; brakowało znacznika kompletnego wiersza po przerwaniu.
- Nowy events.committed_count jest aktualizowany dopiero po zapisie czterech
  kolumn i flush danych, następnie flush znacznika. Odczyt recovery i MOKE
  uwzględnia tylko zatwierdzony prefiks. Resume obcina końcówki wszystkich
  kolumn do wspólnego zatwierdzonego zakresu przed dopisaniem run_resumed.
- Starsze pliki bez znacznika korzystają ze wspólnej długości kolumn
  i odrzucenia niekompletnego ostatniego wiersza. Nie można odtworzyć
  historycznej granicy transakcji, gdy wszystkie dawne komórki wyglądają
  poprawnie; nowy znacznik usuwa tę niejednoznaczność dla nowych zapisów.
- Uszkodzony znacznik nie jest ograniczany w ciszy. Zamknięte archiwum
  z niezatwierdzoną końcówką nie przechodzi walidacji. Nieudany rollback
  blokuje kolejne zdarzenia i zachowuje pierwotny wyjątek zapisu.
- **51 passed, 3 subtests passed, 1 skipped**: zdarzenia, recovery referencji,
  fault injection, validator i kalibracja MOKE. Po dodaniu awarii rollback
  **13 passed** testów zdarzeń. Pominięty test wymaga golden HDF5 z laboratorium.
  Ruff przeszedł. Scenariusze modelują przerwanie między kolumnami oraz błędy
  flush; nie dowodzą odporności HDF5 na fizyczną utratę zasilania dysku.

## Czterdziesta druga seria — VERIFIED nie potwierdza OUTPUT OFF

- Przeczytano obsługę stanów kart Keithley i Rigola, obsługę błędów Rigola,
  kwalifikację połączenia w obu adapterach oraz korelację żądań UI Rigola.
  Adaptery odczytują wyjścia i wyznaczają osobny stan agregowany; samo VERIFIED
  oznacza kwalifikację urządzenia, nie potwierdzone wyłączenie obu kanałów.
- Usunięto nadpisywanie stanu kanałów na OFF po VERIFIED oraz resetowanie
  diagnostyki compliance Keithley na podstawie tego sygnału. Dotychczasowe
  dowody kanałowe pozostają zachowane; OUTPUT_OFF nadal potwierdza wyłączenie.
- Błąd configure/configure_output/set_output Rigola przy VERIFIED pozostawia
  kanał bez potwierdzonego stanu zamiast deklarować OFF. Nie zmieniano komend
  protokołu, limitów ani kolejności załączania urządzeń.
- Testy obejmują obie karty, ON/OFF/UNKNOWN i trzy rodzaje błędów Rigola:
  **9 passed**. Wcześniejszy wspólny przebieg sześciu nowych przypadków
  z regresjami kolejki compliance i potwierdzeń Rigola: **15 passed**.
  Ruff przeszedł. Wyłącznie kontrolery mock/symulacja, bez fizycznego sprzętu.

## Czterdziesta trzecia seria — wspólna blokada zapisu po awarii rollbacku

- Przeczytano publiczne ścieżki mutacji Hdf5RunWriter, append_event, obsługę
  rollbacku punktów/referencji oraz close. Flaga awarii dziennika blokowała
  wyłącznie kolejne zdarzenia, a flaga awarii checkpointu nie obejmowała
  wszystkich zapisów widm i przetwarzania. Close mógł zignorować samą flagę,
  jeżeli pozostała struktura pliku przechodziła walidację.
- Wspólny _require_writable blokuje dziewięć ścieżek dopisywania po obu
  rodzajach nieudanego rollbacku, przed walidacją payloadu i mutacją archiwum.
  Flush pozostałych danych i zamknięcie uchwytów nadal są wykonywane.
- Close zapisuje faulted, usuwa publiczną flagę running i zgłasza utrwalony
  błąd także przy ponownym close. Żądanie completed nie ukrywa wcześniejszej
  awarii. Udany rollback pojedynczej transakcji nadal pozwala kontynuować zapis.
- **42 passed**: dziennik, zamknięcie, rollback referencji i punktów.
  Szerszy przebieg: **81 passed, 1 failed**; jedyna porażka wynikała ze starego
  oczekiwania cichego close po nieudanym rollbacku. Po aktualizacji wymagania
  cały moduł fault injection: **12 passed**. Pozostałe 70 testów zapisu korekcji,
  bloków końcowych i decyzji przeszło w szerszym przebiegu. Ruff przeszedł.
  Bez sprzętu; nie jest to kwalifikacja fizycznej utraty zasilania nośnika.

## Czterdziesta czwarta seria — rollback publikacji modeli przetwarzania

- Przeczytano transakcje store_background_profile,
  store_interference_calibration i store_finalized_block. Wszystkie trzy
  usuwały tylko rekord tymczasowy po wyjątku. Błąd po move (w tym końcowego
  flush) pozostawiał opublikowany rekord mimo zgłoszonej porażki; kolejna
  próba mogła uznać go za istniejący poprawny wynik. Błąd rollbacku mógł też
  zastąpić pierwotną przyczynę i nie ustawiał wspólnej blokady zapisu.
- Wspólny commit usuwa po awarii zarówno lokalizację docelową, jak i pending,
  próbuje wszystkich czynności rollbacku, zachowuje pierwotny wyjątek jako
  cause i dopisuje szczegóły wtórnych awarii. Nieudany rollback ustawia
  _storage_faulted. Istniejące rekordy są odrzucane przed rozpoczęciem nowej
  transakcji; dotychczasowe sprawdzanie tożsamości i zależności pozostaje.
- Sześć nowych przypadków obejmuje awarię zapisu, wyjątek po udanym move
  i końcowy flush, każdy z udanym lub nieudanym rollbackiem; sprawdza też
  zachowanie wcześniejszego profilu i ponowny zapis po udanym cofnięciu.
  **68 passed** z regresjami zapisu korekcji i bloków końcowych;
  **34 passed** dla kalibracji interferencji i dziennika transakcyjnego.
  Ruff przeszedł. Testy awarii nowej procedury używają rzeczywistego profilu
  tła; pozostałe dwa wywołania sprawdzono istniejącymi testami integracyjnymi.
  Nie badano fizycznej utraty zasilania w trakcie operacji HDF5.

## Czterdziesta piąta seria — zgodna skala symulowanego Halla MOKE

- Przeczytano transport symulowany MOKE, konwersję Hall w models.py,
  dekoder bipolarnego AD7734 oraz wspólny model magnesu SimulationContext.
  Simulator dzielił pole przez 0.04, podczas gdy przelicznik aplikacji używa
  1 T/V; odtworzone pole było około 25 razy za duże. Symulator odwraca teraz
  ten sam liniowy przelicznik, pozostawiając szum wyrażony w woltach wejścia.
- Poprawiono też asymetrię kodowania ADC: ujemny pełny zakres używa 0x800000,
  a dodatni 0x7FFFFF, zgodnie z dekoderem. Oba nasycenia są dokładne.
  Metadane symulacji zawierają moke_hall_model_version=2, aby odróżnić nowe
  wyniki od starych. Nie zmieniano fizycznej kalibracji ani komend urządzenia.
- Nowe testy przechodzą przez binarny zapis DAC i odczyt/dekodowanie Halla
  dla pięciu napięć obu znaków oraz sprawdzają obydwa krańce ADC.
  Wykryty przy okazji stary test losowości Anritsu używał TRACE1; poprawiono
  go na TRAC1, czyli polecenie stosowane przez adapter i kwalifikowany symulator.
- **61 passed, 4 subtests passed**: nowe przypadki, protokół, kontekst
  symulacji, sterowanie napięciem i dwa wyjścia MOKE. Ruff przeszedł.
  Jest to spójność modelu syntetycznego, nie kwalifikacja czułości czujnika
  fizycznego ani pełnego stanowiska pomiarowego.

## Czterdziesta szósta seria — niezmienne dane gotowego ExecutionPlan

- Przeczytano modele PlanAction/ExecutionPlan, tworzenie hasha i końcowego
  planu, mutacje payloadów podczas kompilacji oraz serializację runnera.
  Frozen dataclass nadal udostępniał mutowalne słowniki parametrów, setpointów
  i limitów, więc zwykła edycja współdzielonego obiektu mogła zmienić wykonanie
  bez zmiany hasha.
- ExecutionPlan przejmuje teraz niezależną kopię zagnieżdżonych konfiguracji:
  słowniki mają blokowane mutatory, sekwencje są krotkami, zbiory frozenset.
  Kopiowane są także pola zagnieżdżonych dataclass. Kompilator nadal może
  modyfikować swoje robocze PlanAction przed utworzeniem gotowego planu.
  Zachowano obsługę dict/JSON, asdict, deepcopy i pickle używaną przez odbiorców.
- Test zmienia pierwotne payloady, limity, setpointy i ustawienia uploadu,
  sprawdza brak wpływu na plan, blokady mutatorów oraz round-trip serializacji.
  Ta ochrona dotyczy normalnego API konfiguracji, nie złośliwego kodu Python
  wywołującego bezpośrednio bazowe dict.__setitem__ lub object.__setattr__.
- **58 passed, 6 subtests passed**: kompilator i sweepy MOKE;
  **127 passed, 5 subtests passed**: nowa regresja, adaptery/runner,
  kontynuacje Rigola i ustawienia autorange. Ruff przeszedł. Bez sprzętu.
  Pełna kwalifikacja wydajności dużego planu pozostaje otwarta.

## Czterdziesta siódma seria — zbędne ponowne nakładanie stylów

- Przeczytano start Execution, budowanie osi akcji, snapshot drzewa,
  blokowanie formularzy i filtr motywu. Profil startu wskazał ponowne
  setStyleSheet przy Show, także gdy arkusz nie zmienił się ani o znak.
  Qt ponownie przelicza wtedy styl potomków. Dodano porównanie treści przed
  nadpisaniem arkusza kart, przycisków, walidacji, dialogów i powierzchni.
  Nie pomija się weryfikacji aktualnego arkusza: Fluent może go zastąpić
  niezależnie, a motyw może zmienić się podczas ukrycia widgetu.
- Nowe testy pokazują kartę/przycisk w oknie 1000×720, sprawdzają geometrię,
  render, dziesięć ponownych zastosowań bez wywołania setStyleSheet oraz
  przywrócenie stylu po jego zewnętrznym usunięciu. Z regresjami design system:
  **15 passed**. Pierwszy przebieg przerwał access violation Qt po zwolnieniu
  QApplication w nowym teście; poprawiono fixture na sesyjną instancję oraz
  obsługę DeferredDelete przed następnym testem. Ponowny wspólny przebieg przeszedł.
- Profil startu przed/po: 0.354/0.333 s łącznie, run_started 0.176/0.169 s;
  są to pojedyncze pomiary z profilerem, nie dowód stabilnego przyspieszenia.
  W drugim profilu zniknęło setStyleSheet z 25 najdroższych pozycji.
  Oba testy startu przeszły; Ruff przeszedł. Test startu celowo natychmiast
  żąda Stop, więc nie kwalifikuje całej akwizycji. Poprzednia niezaliczona
  kwalifikacja 1000 punktów nadal pozostaje otwarta.

## Czterdziesta ósma seria — ponowna pełna kwalifikacja Execution

- Uruchomiono izolowany test 1000 punktów × 10001 próbek. Wynik:
  **1 failed, 10 deselected**, 262.55 s. Największa przerwa GUI wyniosła
  **405.21 ms**, nadal powyżej wymaganych 350 ms, przy points=0 i aktywnym
  workerze. Nie zmieniono progu ani nie pominięto fazy startu.
- Artefakt artifacts/sweeps-spectrum/stress-runtime.json zawiera wynik:
  5928 taktów GUI, maksymalnie 31.43 ms aktualizacji drzewa i 29.92 ms
  podglądu. Import Results pozostał odroczony, bez aktywnego czytnika.
- Ponieważ asercja opóźnienia przerwała test przed kontrolą danych, osobno
  otwarto wynikowy HDF5 i potwierdzono status completed, 1000 punktów,
  1000 widm po 10001 próbek oraz referencję 10001 próbek. Brak zgłoszonej
  porażki workera nie jest tu jedynym dowodem kompletności archiwum.
- Rozszerzono diagnostyczny test startu: Stop następuje po 1 sekundzie,
  a nie bezpośrednio po uruchomieniu. Obejmuje teraz pierwsze zdarzenia
  inicjalizacji; **1 passed**. Profil zawiera też funkcje workera i oczekiwanie
  finalizacji, więc jego sum czasów nie można utożsamiać z blokadą GUI.
  Dalszy pomiar musi wyodrębnić czas handlerów startu w wątku GUI.
- Przy lekturze spectrum_decision_store.py znaleziono pozostały wariant
  błędu transakcyjnego: initialize_decisions nie ma rollbacku, a
  append_decision nie cofa opublikowanego rekordu ani count/last_sha256.
  Ta ścieżka pozostaje do naprawy. Niniejsza seria nie deklaruje usunięcia
  opóźnienia 405 ms ani zamknięcia kwalifikacji produkcyjnej.

## Czterdziesta dziewiąta seria — transakcje dziennika decyzji przetwarzania

- Ponownie przeczytano spectrum_decision_store.py i ścieżki wywołania writera.
  Inicjalizacja korzysta teraz ze wspólnego commit/rollback rekordów zamiast
  pozostawiać częściowy kontekst po błędzie. Append cofa rekord w obu
  lokalizacjach oraz count i last_sha256, a następnie flushuje cofnięcie.
- Każda czynność rollbacku jest podejmowana niezależnie. Wtórne błędy nie
  zastępują pierwotnej przyczyny; są dopisywane do komunikatu i ustawiają
  wspólną blokadę dalszych zapisów. Wcześniej istniejąca transakcja jest
  odrzucana przed mutacją, aby cofnięcie nie usuwało cudzego rekordu.
- 20 nowych przypadków: inicjalizacja/append, awaria zapisu/move/licznika/
  hasha/końcowego flush, każdy z udanym lub nieudanym rollbackiem. Sprawdzono
  poprzedni hash i licznik, odczyt łańcucha oraz ponowne dopisanie po udanym
  rollbacku. **49 passed** łącznie z replay, transakcjami modeli i zdarzeniami.
  Ruff przeszedł. Testy dotyczą wyjątków w procesie; nie dowodzą atomowości
  wielu atrybutów HDF5 przy fizycznej utracie zasilania.

## Pięćdziesiąta seria — wyodrębnienie kosztu GUI przy starcie

- Zastąpiono mieszany profil cProfile pomiarem perf_counter ograniczonym
  do identyfikatora wątku GUI. Odczytano RunController.start/finish/dispose,
  MainWindow._run_event i blokowanie formularzy. Pomiar obejmuje start,
  handlery, pierwsze processEvents oraz malowanie drzewa i pyqtgraph.
- Pierwszy pomiar: start synchroniczny 102.84 ms, najdłuższe processEvents
  436.57 ms. Test kończył się błędem starego wywołania pstats po wyłączeniu
  profilera; usunięto nieaktualny kod raportowania i ponowiono przebieg.
- Drugi pomiar: **1 passed**, 27.67 s; processEvents 447.98 ms,
  start synchroniczny 152.64 ms, timeline 123.04 ms, malowanie drzewa
  maksymalnie 53.70 ms, GraphicsView 22.44 ms. Dokładne dane zapisano w
  startup-gui-timings.json. Ruff przeszedł.
- Nie ma podstaw do przypisania całych 448 ms pojedynczemu zmierzonemu
  handlerowi ani paintEvent. Pozostają zdarzenia layout/style, agregacja wielu
  callbacków i konkurencja o czas wykonania; wymagają rozdzielenia w kolejnym
  pomiarze. Test diagnostyczny nie ma progu płynności, więc jego zaliczenie
  nie zastępuje nadal niezaliczonego testu 1000 punktów. W tej serii nie
  zmieniano produkcyjnego wykonania ani progu 350 ms.

## Pięćdziesiąta pierwsza seria — koaleskowanie odmalowań wykresów Execution

- Pomiar QApplication.notify ograniczony do aktywnej fazy diagnostyki
  wskazał MetaCall sceny podglądu widma: do 164 ms, z zagnieżdżonymi
  UpdateRequest głównego okna. LayoutRequest Execution osiągał 80 ms.
  Dane z typami zdarzeń i ścieżkami rodziców: startup-qt-events.json.
  Przeczytano też obsługę scen i viewport w lokalnym pyqtgraph oraz host strony.
- Dla dwóch wykresów Execution włączono jawne odświeżanie viewportu:
  zmiany sceny uruchamiają jeden single-shot timer 33 ms. Kolejne zmiany
  nie resetują jego terminu. Timer wywołuje update(), a nie synchroniczny
  repaint. Nie zmieniono danych, rejestracji ani częstotliwości zdarzeń runnera.
  Pozostałe wykresy aplikacji zachowują dotychczasową politykę.
- Nowy test pokazuje wykres 1000×720, podaje 100 zmian, sprawdza ograniczoną
  liczbę odświeżeń, obecność ostatnich danych oraz zmianę renderowanego obrazu.
  **13 passed, 1 deselected** z testami timeline i krótkimi regresjami Execution.
  Pominięto w tym przebiegu pełny test 1000 punktów. Ruff przeszedł.
- Krótka diagnostyka startu: **1 passed**, najdłuższe processEvents 308.08 ms
  wobec wcześniejszych 447.98 ms, start synchroniczny 115.83 ms. Zapisano
  startup-gui-coalesced.json. To pojedynczy pomiar porównawczy; nie zastępuje
  ponownego pełnego testu 1000 punktów, który nadal wymaga wykonania.

## Pięćdziesiąta druga seria — pełny test płynności i widoczne lokalne OFF

- Pełny test po koaleskowaniu wykresów: **1 passed, 10 deselected**, 259.38 s.
  Zapisano 1000 widm po 10001 próbek i referencję o tej samej długości.
  Największa przerwa GUI **333.98 ms < 350 ms**; 6305 taktów GUI,
  aktualizacja drzewa maks. 34.56 ms, podglądu 33.87 ms. Dane zachowano
  w execution-1000-coalesced.json. Jest to zaliczony scenariusz symulacyjny,
  nie kwalifikacja dowolnego komputera, aparatury i konfiguracji.
- Po zakończeniu tego pomiaru przeczytano ścieżki lokalnych output_policy
  w kompilatorze i normalizacji drzewa. Dla on kompilator generował OFF
  po całym bloku, lecz drzewo pomijało ten wiersz. Dodano końcowy wiersz
  z ID zgodnym z kompilatorem, po dzieciach albo całej lokalnej osi.
  on_keep nadal nie tworzy lokalnego OFF; off pozostaje przed ciałem.
- Kanał wiersza jest teraz pobierany z configuration z tym samym
  pierwszeństwem co w kompilatorze. Poprzednio brak kanału na poziomie
  głównym mógł pokazać domyślne B/CH1 mimo innego kanału w konfiguracji.
- Nowe testy porównują akcje kompilatora i drzewo dla A/B, CH1/CH2,
  trzech polityk oraz Set i lokalnej osi. **24 passed**. Wcześniejszy
  przebieg Set z testami geometrii dotychczasowego shutdown: **15 passed**.
  Początkowe błędy fixture poprawiono przez jawny zakres źródła Keithley
  oraz częstotliwość w limicie CH2, bez osłabiania walidacji. Ruff przeszedł.
- Dodany semantic_id końcowego OFF wpływa na hash nowych planów zawierających
  taką politykę. Wznowienie starego planu z innym hashem podlega istniejącej
  kontroli zgodności; nie wyłączano tej kontroli. Pozostały zakres audytu otwarty.

## Pięćdziesiąta trzecia seria — starszy generator Rigola bez pełnej konfiguracji

- Przeczytano _fixed_node_from_dialog, _sweep_node_from_generator,
  _edit_selected_generator, parser osi i wykonanie natywnej osi przez
  kompilator/provider Rigola. Starsza ścieżka dodawała configure_rigol
  w każdym punkcie z waveform/load i nastawami z Settings; dla amplitude
  i offset nie wykorzystywała nawet wybranej wartości.
- Nowe osie Rigola korzystają z częściowych aktualizacji providera, z jawną
  wcześniejszą konfiguracją bazową. Stała wartość tej ścieżki jest zapisana
  jako jednopunktowa oś, z identyczną semantyką zmiany wybranego parametru.
  Nie dodaje się configure ani ON/OFF. Brak baseline jest odrzucany preflight.
- Parser dopuszcza oś bez dodatkowego dziecka: natywna oś już wykonuje
  własną operację ROI. Pusty Repeat nadal jest błędem. Początkowy test wykrył
  ten dawny wymóg parsera; naprawiono go zamiast dodawać sztuczne Wait/Comment.
- Dziesięć przypadków obejmuje frequency/high/low/amplitude/offset, Set
  i oś dwupunktową po baseline + ON. Sprawdzono rodzaje wszystkich akcji,
  wartości żądane i pary high/low oraz błąd braku konfiguracji.
  **63 passed, 6 subtests passed** z kompilatorem i kontynuacjami Rigola;
  następnie **14 passed, 84 deselected** z wybranymi regresjami generatorów UI.
  Ruff przeszedł. Bez fizycznego sprzętu; testy tej serii kwalifikują generację
  i kompilację, nie analogowy przebieg sygnału.
- Analogiczne starsze generatory Keithley i Anritsu oraz edycja ich opcji
  nadal wymagają migracji; nie zadeklarowano zamknięcia całego A10.

## Pięćdziesiąta czwarta seria — generator Anritsu zmienia tylko wybraną nastawę

- Prześledzono generację stałej wartości i osi w RecipePage, compile_point
  providera Anritsu, dispatch runnera oraz configure_spectrum i
  update_signal_generator adaptera. Starszy generator dopisywał pełne
  konfiguracje z domyślnymi częstotliwościami, poziomem odniesienia lub mocą.
- Nowe osie i jednopunktowe nastawy Anritsu korzystają z natywnej osi
  z changed_fields. Wymagają jawnej wcześniejszej konfiguracji. Nie generują
  dodatkowej pełnej konfiguracji ani przełączenia RF. Nieznany target jest
  odrzucany zamiast trafiać do domyślnej gałęzi generatora sygnału.
- Adapter widma sprawdza zgodność pozostałych nastaw z baseline i zapisuje
  tylko wybrane pola; przy rozbieżności odrzuca operację. Adapter aktualizacji
  SG zachowuje pozostałą nastawę z readback i sprawdza ciągłość stanu outputu.
  Ta seria nie aktywuje SG w ustawieniach stacji ani nie zmienia jego uprawnień.
- Dziesięć nowych przypadków pokrywa start/stop/reference widma oraz
  frequency/power SG, każdy jako Set i oś dwupunktowa. Sprawdzają dokładną
  maskę zmiany, wartości SI, zachowanie pozostałych pól i odrzucenie braku
  baseline. **57 passed, 6 subtests passed** wraz z testami kompilatora;
  wcześniejszy przebieg generatorów UI: **20 passed, 84 deselected**.
  `ruff check app tests`: wszystkie kontrole zaliczone. Bez sprzętu fizycznego.
- Starsze generatory i edytor Keithley nadal dopisują compliance/NPLC/settle
  oraz pełną konfigurację; pozostają otwartą częścią A10. Istniejących receptur
  z jawnymi dziećmi configure nie przepisano automatycznie. Cały audyt pozostaje
  otwarty; zaliczenie tych regresji nie jest kwalifikacją całej aplikacji.

## Pięćdziesiąta piąta seria — generatory Keithley bez konfiguracji w każdym punkcie

- Przeczytano generatory i ich wywołania, edytor osi, dialog źródła,
  provider Keithley oraz walidację zakresu źródła. Prosta stała wartość
  i zwykła oś korzystają teraz z natywnej aktualizacji poziomu; nie dopisują
  compliance, NPLC, sense, zakresu ani Wait. Brak wcześniejszej konfiguracji
  jest odrzucany przez kompilator. Edycja takiej osi otwiera edytor ROI,
  bez dodatkowych pól konfiguracji, których operacja nie wykonuje.
- Dialog z jawnymi opcjami generuje widoczną sekwencję: jedna konfiguracja
  na pierwszym poziomie osi, następnie oś z aktualizacją poziomu i wybranym
  Wait. Nie dodaje ON/OFF. Włączenie outputu użytkownik umieszcza jawnie
  między konfiguracją a osią. Usunięto fallback do lab-limit compliance,
  NPLC=1 i 100 ms z tej ścieżki; niekompletne opcje są błędem.
- Pierwszy test kompilacji ujawnił brak zakresu źródła w dawnym dialogu.
  Dodano jawne pole Source range i jego porównanie z bieżącą konfiguracją.
  Zmiana trybu current/voltage czyści zakres, aby nie przenosić błędnej
  jednostki. Kompilator nadal sprawdza zakresy sprzętowe i limity; nie
  osłabiono walidacji ani nie wstawiono zakresu tylko do fixture.
- Nowe testy: A/B, Set i oś po baseline + ON; jawna konfiguracja raz
  przed osią, 0 s/250 ms, odrzucenie niepełnych opcji, ujemnej/niepoprawnej
  przerwy i 4wire. Test pokazanego dialogu 1180×720 sprawdza widoczność
  pola, geometrię, grab oraz czyszczenie zakresu przy zmianie trybu.
  Wynik końcowy z kompilatorem i wybranymi regresjami UI:
  **64 passed, 83 deselected, 6 subtests passed**. Ruff app/tests zaliczony.
- Nie przepisano automatycznie starszych receptur z konfiguracją wewnątrz
  pętli. Ich specjalizowany edytor oraz pełna widoczność zaawansowanych
  nastaw konfiguracji pozostają do przeglądu. Ta seria nie potwierdza
  analogowej płynności zmian ani wykonania na fizycznym Keithley.

## Pięćdziesiąta szósta seria — edycja ROI nie zmienia dzieci starszej receptury

- Ponownie przeczytano _edit_selected_generator. Specjalna gałąź Keithley
  pozwalała zmienić kanał/tryb, lecz zapisywała pierwotny target osi. Ponadto
  nadpisywała opcje wszystkich bezpośrednich configure_keithley, niezależnie
  od kanału, oraz pierwszy Wait bez sprawdzenia jego przeznaczenia. Gdy Wait
  nie istniał, dopisywała go po pozostałych dzieciach, także po pomiarze.
- Edycja ROI używa teraz wspólnego edytora punktów również dla starych osi.
  Zachowuje target, konfiguracje, Wait, pomiary, ich kolejność i disabled.
  Nastawy urządzeń i przerwy pozostają edytowalne przez ich własne węzły;
  nie oferuje się pól, których zapis mógłby niejawnie zmienić inne operacje.
  Nowe tworzenie sekwencji z jawną konfiguracją z serii 55 pozostaje dostępne.
- Cztery scenariusze A/B × zatwierdzenie/anulowanie pokazują rzeczywisty
  dialog i sprawdzają jego geometrię oraz grab. Receptura zawiera dwa różne
  kanały, pomiar przed jawnym Wait i niestandardowe nastawy. Porównanie całej
  listy dzieci wykazuje brak zmian; zmieniają się wyłącznie segmenty ROI.
  **13 passed** w module regresji edycji. Ruff app/tests zaliczony.
- Nie przenosi się samoczynnie konfiguracji istniejących receptur poza
  pętlę: mogłoby to zmienić jawnie zapisany eksperyment. Poprawka usuwa
  mutacje uboczne edytora; nie jest kwalifikacją wszystkich pełnych
  konfiguracji i pozostających problemów raportu.

## Pięćdziesiąta siódma seria — spójność identyfikatorów osi Keithley i Anritsu

- Przeczytano walidację providerów, budowanie bindingów w semantic_tree
  i wykonanie osi w kompilatorze. Keithley sprawdzał kanał, lecz nie zgodność
  parameter_id/dimension z target. Sprzeczny identyfikator mógł kierować
  compile_point do aktualizacji compliance zamiast poziomu źródła.
  Provider Anritsu nie sprawdzał parameter_id względem target.
- Keithley sprawdza teraz kanoniczny parametr, wymiar i zgodność trybu
  źródła; alias level zachowuje kontrakt prądowy z rejestru. Settling time
  pozostaje dostępny także w measure_only. Anritsu sprawdza tożsamość
  parametru, z zachowaniem jawnie obsługiwanych aliasów sg/signal_generator.
- Normalizacja drzewa odrzuca sprzeczne endpoint/parameter również dla
  Keithley i Anritsu, przed ekspansją punktów. Jest to potrzebne także dla
  starszych osi z jawnym configure w ciele, które nie zawsze wywołują
  compile_point providera. Dotychczasowa kontrola Rigola pozostaje aktywna.
- 21 nowych przypadków obejmuje A/B, poziom prądu/napięcia, compliance,
  czas stabilizacji, cele widma/SG, aliasy oraz niepoprawne bindingi YAML
  odrzucane zarówno przez drzewo, jak i kompilator. Końcowa regresja
  z kompilatorem, generatorami i bindingami Rigola: **100 passed,
  6 subtests passed**. Ruff app/tests zaliczony. Bez aparatury fizycznej.
- Ta seria kwalifikuje spójność nazw i jednostek osi. Nie zamyka pozostałych
  ustaleń dotyczących deadline shutdown, pracy GUI i pełnych konfiguracji.

## Pięćdziesiąta ósma seria — odrzucanie ramki anulowanego uśredniania

- Przeczytano start/finish/request/result/error uśredniania i ręcznych
  odczytów Anritsu. Cancel zostawiał żądanie w workerze; ponowny Start mógł
  zaliczyć jego spóźnioną odpowiedź do nowej średniej mimo niezależnej serii.
- Anulowanie oznacza trwającą ramkę do odrzucenia. Slot fetch pozostaje
  zajęty do odpowiedzi; po jej odrzuceniu nowa seria zamawia własny sweep.
  Ręczny odczyt i pojedyncza referencja nie nakładają żądania na zajęty slot.
  Błąd transportu usuwa znacznik i pozostaje raportowany jako błąd — nie
  dodano automatycznego ponawiania po niepewnym stanie komunikacji.
- Poprawiono tooltip sugerujący pasywne odczyty: uśrednianie wykonuje
  kwalifikowane pojedyncze sweepy. Testy obejmują Cancel/Start dla sygnału
  i referencji oraz zachowanie ukończonego widma po spóźnionej odpowiedzi.
  Pierwszy przebieg modułu: **13 passed**. Dodano następnie przypadek błędu
  starego żądania i uruchomiono szerszą regresję ze stronami urządzeń.
- Szerszy przebieg nadal aktywny w chwili wpisu: sesja exec **76974**, PID
  **9044**, katalog `.tmp-average-cancel-regression`. Wypisuje kolejne
  ukończone przypadki, ale nie ma jeszcze końcowego wyniku; nie kwalifikować
  go jako zaliczony i nie uruchamiać duplikatu bez sprawdzenia sesji.
  Ruff app/tests zaliczony. Pozostały zakres raportu nadal otwarty.

## Pięćdziesiąta dziewiąta seria — aktualny kontekst każdego ręcznego zapisu

- Ponownie przeczytano _save_configured_manual_spectrum i cały kontrakt
  ManualSpectrumArchive.save/_open_for. Wartości pomiarowe były odświeżane,
  lecz settings_source, device_idn i operator_context pobierano tylko przy
  pierwszym utworzeniu obiektu archiwum. Także kolejne pliki timestamped
  tego obiektu dziedziczyły nieaktualny kontekst pierwszego pliku.
- GUI pobiera kontekst przy każdym zapisie. Punkt zawiera opcjonalne,
  wersjonowane capture_context_at_save z ustawieniami, IDN, operatorem
  i trybem symulacji. Nazwa i dotychczasowy metadata_snapshot_kind wyraźnie
  odróżniają chwilę zapisu od chwili akwizycji widma. Nie deklaruje się
  fizycznego ponownego odczytu urządzeń.
- Nowe pliki używają bieżącego kontekstu jako metadanych głównych. Tożsamość
  istniejącego pliku append pozostaje niezmienna, a poszczególne punkty
  dokumentują kontekst zapisów. Serializacja snapshotu i kontrola zgodności
  trybu simulation następują przed otwarciem/mutacją pliku.
- Testy rzeczywistych HDF5 potwierdzają osobne konteksty dwóch punktów,
  bieżące settings_yaml w kolejnych nowych plikach i brak pliku po błędzie
  pochodzenia symulacji. **10 passed** z testami manual_spectrum_writer.
  Ruff app/tests zaliczony. Ręczny zapis nadal wykonuje I/O synchronicznie;
  przeniesienie tej pracy z GUI pozostaje otwartą częścią raportu.
- Regresja GUI z serii 58 (sesja **76974**, PID **9044**) była wielokrotnie
  sprawdzana: pozostaje aktywna i kończy kolejne testy. Brak wyniku końcowego;
  nie uruchomiono drugiego egzemplarza. Przyczyny długiego wykonania nie
  ustalono, więc nie przypisano jej bez dowodu do aplikacji ani fixture.

## Sześćdziesiąta seria — katalog kalibracji MOKE poza GUI

- Przeczytano repozytorium kalibracji oraz listowanie, odczyt aktywnego
  modelu, aktywację i obsługę profilu w field_control. Listowanie w GUI
  hashowało i otwierało wszystkie powiązane HDF5; dodatkowo odczyt aktywnego
  modelu wykonywał tę walidację synchronicznie po get_control_profile.
- Odczyt aktywnego modelu i całego katalogu przeniesiono do dedykowanego
  QThread. Walidacja hashy, statusu runu i potwierdzenia zera pozostaje
  niezmieniona. Worker nie komunikuje się ze sprzętem. Wynik jest stosowany
  wyłącznie dla nadal zgodnego profilu i katalogu; kolejne odświeżenia są
  łączone w jedno żądanie. Load/Activate czekają na zakończenie weryfikacji.
- Zamknięcie aplikacji nie niszczy działającego workera plikowego.
  Zatrzymanie czynnej operacji sprzętowej zachowuje pierwszeństwo przed
  oczekiwaniem na katalog. Uchwyt workera jest usuwany dopiero po finished.
- Test z celowo wstrzymanym I/O potwierdza wykonanie poza GUI, dostępność
  obsługi zamknięcia, odrzucenie wyniku po zmianie profilu i zakończenie
  wątku. Z regresją cleanup **10 passed**; z repozytorium kalibracji
  **25 passed**. Pełny scenariusz kalibracji/aktywacji i zachowanie wyboru
  przy operacji live: **2 passed, 31 deselected**. Pierwszy scenariusz
  wymagał poprawienia testu: kliknięcie Activate musi czekać na dostępność
  przycisku po asynchronicznej weryfikacji. Ruff app/tests zaliczony.
- Osobne Load for review i Activate nadal wykonują ponowną walidację pliku
  synchronicznie; pozostają do migracji. Nie użyto cache zamiast walidacji.
- Domknięcie serii 58: sesja **76974** zakończyła się kodem **0**;
  **37 passed, 13 subtests passed, 553.83 s**. Jest to wynik wersji
  załadowanej przy starcie tej regresji, nie późniejszych zmian MOKE.

## Sześćdziesiąta pierwsza seria — odczyt i aktywacja modelu MOKE poza GUI

- Przeczytano ponownie osobne Load/Activate i mechanizm workera katalogu.
  Obie operacje korzystają teraz z tego samego kontrolowanego wątku
  repozytorium. Aktywacja nadal wywołuje repository.activate z tożsamością,
  fingerprintem, trybem symulacji i jawnym reviewed; pełna weryfikacja raw
  HDF5 i atomowy zapis wskaźnika nie zostały pominięte.
- Load sprawdza teraz także zgodność trybu simulation modelu, oprócz
  fingerprintu. GUI stosuje rezultat dopiero po zakończeniu wątku i tylko
  przy zgodnym bieżącym kontekście. Stary wynik nie podmienia aktywnego
  ani przeglądanego modelu po zmianie profilu. Aktywacja dotyczy profilu
  utrwalonego przy kliknięciu, a zmiana profilu unieważnia jej wynik w GUI.
- Stan ładowania/weryfikacji jest widoczny; wyboru modelu i kolejnego
  Load/Activate nie można nakładać na trwające I/O. Przed aktywacją
  wyłączany jest tryb Live i unieważniane przygotowane plany ręczne.
  Oczekiwanie na zakończenie przy zamknięciu aplikacji obejmuje te operacje.
- Testy katalog/load/activate z wstrzymanym I/O i zmianą profilu oraz
  repozytorium: **29 passed**. Po dodaniu błędów checksum i potwierdzenia
  przekazania reviewed=False: **8 passed** dla workera. UI: scenariusz
  kalibracji/aktywacji przeszedł; scenariusz zachowania wyboru początkowo
  zakładał synchroniczny Load, po dodaniu oczekiwania na finished:
  **1 passed, 32 deselected**. Ruff app/tests zaliczony.
- Listowanie, aktywny model, odczyt do przeglądu i aktywacja nie wykonują
  już walidacji plików kalibracji w wątku GUI. Inne otwarte operacje
  plikowe z raportu, w szczególności ręczny zapis widm, pozostają do naprawy.

## Sześćdziesiąta druga seria — ręczny zapis i zamknięcie HDF5 poza GUI

- Przeczytano pełny cykl _save_configured_manual_spectrum, zmianę pliku,
  close_manual_archive_session, zamknięcie strony i głównego okna oraz
  ManualSpectrumArchive.save/_open_for/close. Zapis, flush, zamknięcie
  poprzedniego pliku i walidacja były synchroniczne w GUI.
- ManualArchiveWorker wykonuje dokładnie jedną operację. Kolejne zapisy
  i zamknięcia nie nakładają się; GUI nie odczytuje stanu archiwum podczas
  mutacji. Worker nie używa widgetów. Widmo, wybrany wariant, metadata
  i kopia kontekstu operatora pochodzą z kliknięcia Save. Późniejsza zmiana
  widma lub danych dostawcy nie zmienia rozpoczętego zapisu.
- Potwierdzenie sukcesu oraz opcjonalny upload następują po zakończeniu
  workera. Aplikacja i strona odmawiają zamknięcia podczas I/O; zamknięcie
  otwartego append jest także operacją workera. Nie przerywa się wątku
  zapisującego ani nie zamyka jego uchwytu HDF5 z GUI.
- Szerszy test ujawnił długi pierwszy import pandas/xarray przy walidacji
  PyThat w procesie Qt. Sama zmiana limitu oczekiwania 5→30 s nie pomogła.
  Diagnostyka stosu pokazała ManualArchiveWorker → Hdf5RunWriter.close →
  pythat_bridge → xarray/pandas → shibokensupport.feature/inspect/linecache.
  Włączono istniejącą izolowaną walidację dla nowych i wznawianych archiwów
  tworzonych przez stronę. Nie pominięto kontroli PyThat. Diagnostyka
  `.tmp-manual-worker-debug.py` zakończyła się; nie jest częścią aplikacji.
- Testy z celowo blokowanym I/O obejmują save/close, sukces/błąd, brak
  równoległego zapisu, ochronę shutdown i utrwalenie snapshotu. Regresja
  końcowa workera, rzeczywistych HDF5 i metadanych: **15 passed**.
  Test zmiany pliku i błędu niezgodnej siatki po izolacji walidacji:
  **1 passed, 22 deselected**, 28.98 s. Wcześniejszy szerszy przebieg UI:
  7 passed i jeden opisany wyżej timeout z błędem teardown; nie zaliczono
  go jako pełnego sukcesu. Ruff app/tests zaliczony.
- Testy UI i fixture oczekują teraz jawnie zakończenia asynchronicznego
  zapisu/zamknięcia przed odczytem lub usunięciem plików. Limit oczekiwania
  na I/O nie zastępuje testu dostępności GUI. Pozostały zakres raportu
  (m.in. deadline shutdown aparatury i pełne nastawy Rigola) nadal otwarty.

## Sześćdziesiąta trzecia seria — porównanie nastaw Rigola zgodne z wykonaniem

- Przeczytano RigolNodeEditorDialog, oba miejsca jego użycia w RecipePage,
  _visit_rigol_device_node w kompilatorze i configure_channel w adapterze.
  Pełna konfiguracja configure_rigol wymusza OFF, natomiast węzeł wybranych
  parametrów emituje aktualizacje z zachowaniem wcześniejszej konfiguracji.
  Stary opis i tabela nie rozróżniały poprawnie tych ścieżek.
- Porównanie oznacza niewybrane pola jako Preserve. Polityka unchanged
  zachowuje OUTPUT w węźle aktualizacji. Pełna konfiguracja pokazuje jawne
  OFF oraz wymagania: wyłączone tryby zaawansowane i jednostka VPP.
  Nie udaje odczytu tych ustawień z aparatury: ich bieżący stan jest Unknown.
  Niespełnione znane wymaganie ma status Requirement not met, a nie Changes.
- Pola ignorowane przez kompilator są widoczne, lecz zablokowane i opisane.
  Edytor configure_rigol nie pozwala wybierać ignorowanych lokalnych ROI
  ani polityki OUTPUT. Węzeł aktualizacji nie pozwala zmieniać ignorowanych
  nastaw kształtu, obciążenia, fazy i wyjścia.
- Regresja porównania i pokazanej geometrii modalu: **11 passed**, w tym
  dwa rodzaje edytora × pięć polityk OUTPUT. Kompilator, generatory i
  powtórne OUTPUT ON Rigola: **21 passed**. Ruff zmienionych modułów zaliczony.
- Nie zmieniono komend aparatury. Ręczna pełna konfiguracja Rigola,
  precyzyjny podgląd sprzężonych wartości względem wcześniejszego stanu
  receptury oraz pozostałe otwarte punkty raportu wymagają dalszej pracy.

## Sześćdziesiąta czwarta seria — edycja Rigola nie dopisuje pominiętych nastaw

- Dalsza lektura _edit_legacy_rigol_configuration wykazała, że Apply
  bezwarunkowo dodawało output_load, phase_deg i nastawy kształtu do YAML.
  Kompilator i adapter już respektowały maskę jawnych pól, ale modal
  rozszerzał ją podczas zwykłego otwarcia i zatwierdzenia receptury.
- Dialog otrzymuje zbiór oryginalnych pól. Porównanie rozróżnia Preserve
  od Set; zapis dodaje tylko rzeczywiście edytowane pominięte pola.
  Śledzenie poziomów uwzględnia ich reprezentację napięciową, bez uznawania
  przeliczenia pomocniczego Vpp/offset za edycję obu końców zakresu.
- Testy rzeczywistego RecipePage → modal → YAML sprawdzają zatwierdzenie
  bez edycji, dopisanie wyłącznie fazy oraz zmianę HighL z zachowaniem LowL.
  Razem z porównaniem i geometrią Rigola: **14 passed, 13 deselected**.
  Testy wybranych mutacji i generatorów, w tym dzienniki komend symulatora
  potwierdzające zachowanie pominiętych load/phase: **34 passed**.
  Ruff zmienionych modułów zaliczony. Nie wykonywano operacji sprzętowych.
- To domyka dopisywanie nastaw przez ten edytor. Nie zamyka pozostałych
  pozycji raportu ani kwalifikacji całego systemu na aparaturze.

## Sześćdziesiąta piąta seria — zmiana kanału nie importuje nastaw z karty

- Prześledzono _channel_changed → _load_snapshot w edytorze Rigola.
  Zmiana kanału pełnej konfiguracji pobierała draft z karty urządzenia,
  zastępowała jawne wartości receptury i rozszerzała maskę zapisów.
  Teraz zmienia adresata konfiguracji, zachowując jej nastawy i maskę.
  Porównanie nadal korzysta z baseline wybranego kanału.
- Regresja podstawia dla kanału 2 odmienną częstotliwość, poziomy, fazę
  i obciążenie, zmienia kanał i zatwierdza modal. Wynikowy YAML różni się
  tylko kanałem. Zestaw testów edytora Rigola: **14 passed, 14 deselected**.
  Ruff zaliczony. Nie łączono się z aparaturą.
- Dalsza lektura ręcznego panelu Rigola potwierdza otwarty punkt A10:
  opis Apply nie wymienia jeszcze wszystkich resetów pełnej konfiguracji
  (tryby zaawansowane, VPP, dla USER tryb FREQ). Ta ścieżka pozostaje
  oddzielnym zadaniem, nie jest naprawiona zmianą edytora receptur.

## Sześćdziesiąta szósta seria — jawne skutki pełnej konfiguracji ręcznej Rigola

- Przeczytano configure, request_output oraz configure_channel. Pełne
  ręczne Apply wyłącza OUTPUT, MOD, hardware sweep, burst, harmonics i SUM,
  ustawia VPP, a dla USER wybiera FREQ. OUTPUT ON uruchamia tę ścieżkę,
  gdy widoczna konfiguracja nie odpowiada już potwierdzonemu carrierowi.
- Dodano stale widoczny opis tych efektów nad zakładkami panelu, wspólny
  również dla podpowiedzi Apply waveform / shape. Usunięto sugestię,
  że wystarczy sprawdzić jedynie częstotliwość i amplitudę. Komendy i zakres
  konfiguracji nie zostały zmienione; opis dotyczy pełnej konfiguracji ręcznej,
  nie częściowych aktualizacji sweepa.
- Test pokazanej strony w obu motywach sprawdza geometrię opisu, dostępność
  OUTPUT OFF i obecność informacji we wszystkich zakładkach: **2 passed**.
  Regresje potwierdzania wyjść i powtórnego ON: **14 passed**. Ruff zaliczony.
  Pierwszy zrzut miał brakujące fonty środowiska offscreen; fixture jawnie
  ładuje Segoe UI, po czym powtórzono test i obejrzano poprawiony zrzut
  [rigol-effects-light.png](rigol-effects-light.png), 1280×900.
- Pozostałe problemy raportu, w tym łączny deadline shutdown i ciężkie
  operacje odczytu/analizy wyników, nie są zamknięte tym opisem UI.

## Sześćdziesiąta siódma seria — potwierdzenie shutdown nie wynika z VERIFIED

- Podczas lektury _safe_shutdown i _shutdown_owned_device znaleziono
  akceptowanie DeviceState.VERIFIED po emergency_off zwracającym None.
  Stan identyfikacji urządzenia nie dowodzi wyłączenia jego wyjść.
- Runner wymaga teraz jawnego sukcesu lub OUTPUT_OFF dla starszego
  kontraktu bez wyniku. Jawne False zawsze oznacza błąd, nawet przy
  zachowanym wcześniejszym stanie OUTPUT_OFF. Anritsu emergency_off zwraca
  bool: False bez sesji lub przy błędzie OFF/readback/ABORT, True dopiero
  po zakończonej procedurze. Nie dodano komend RF do runów spectrum-only.
- Testy trzech urządzeń obejmują VERIFIED, UNKNOWN, OUTPUT_OFF i jawny
  wynik; test adaptera Anritsu wstrzykuje błędy OUTP? oraz ABORT.
  Końcowy moduł potwierdzenia: **18 passed**. Szerszy zestaw z runami
  analizatora i izolacją nieużywanych urządzeń: **29 passed** (przed
  dodaniem trzech przypadków False + stary OUTPUT_OFF). Ruff zaliczony.
- Pierwszy szerszy przebieg: 23 passed, 6 failed z powodu nieaktualnych
  atrap kontrolera bez acquire_run_lease. Zaktualizowano kontrakt atrap
  i dodano asercje release dla używanego urządzenia oraz braku release
  dla nieużywanych. Nie zmniejszano wymagań produkcyjnej rezerwacji.
- Wspólny deadline zamykania pozostaje otwarty. Lepsze potwierdzenie
  wyniku nie ogranicza jeszcze łącznego czasu wielu operacji I/O.

## Sześćdziesiąta ósma seria — potwierdzenie E-STOP i cleanup inicjalizacji

- Sprawdzono wszystkich odbiorców emergency_off w RunWorker i osobnym
  EmergencyStopWorker. Ten ostatni nadal przyjmował VERIFIED jako dowód
  OFF; cleanup nieudanego startu całkowicie pomijał wynik metody.
- E-STOP wymaga jawnego sukcesu lub OUTPUT_OFF dla starszego kontraktu;
  False jest zawsze błędem. Spectrum-only wymaga jawnego sukcesu ABORT
  i nie używa stanu wyjść do potwierdzenia przerwania akwizycji.
  Cleanup przed utworzeniem runnera także raportuje brak potwierdzenia,
  nadal próbując rozłączyć sesję i obsłużyć dalsze urządzenia.
- Uściślono kontrakt DeviceAdapter.emergency_off (bool lub historyczne
  None). Nie dodano komend do aparatury ani nie zmieniono zakresu E-STOP.
- Testy obejmują wyniki None/False/True, VERIFIED/OUTPUT_OFF, tryb ABORT,
  przerwany start dla Anritsu/Rigola/Keithleya i zapis ostrzeżenia cleanup.
  Końcowy zestaw potwierdzenia oraz anulowania: **35 passed**. Wcześniejszy
  zestaw z izolacją urządzeń E-STOP: **34 passed**. Ruff zaliczony.
- Wspólny deadline kończenia pozostaje osobnym otwartym problemem.

## Sześćdziesiąta dziewiąta seria — wspólny budżet kolejnych wywołań VISA

- Lektura DeviceAdapter.io_timeout i RecipeRunner._execute_with_policy
  wykazała, że adapter bez kolejki GUI nie implementował operation_timeout.
  Runner kontrolował upływ czasu dopiero po zakończeniu całej procedury,
  a każde kolejne I/O mogło ponownie wykorzystać pełny timeout.
- Dodano zakres operation_timeout dla istniejącej sesji VISA w jej wątku
  właściciela. write/query/odczyt binarny i podstawowe odczyty surowe
  otrzymują minimum limitu sesji i pozostałego czasu operacji. Po deadline
  nowa komenda nie jest wysyłana. Zakresy zagnieżdżone nie wydłużają
  zewnętrznego limitu; timeout jest przywracany także po błędzie. Wyjście
  z zakresu nie odtwarza sesji usuniętej przez disconnect.
- Testy sterowanego zegara, zagnieżdżenia i odłączenia oraz regresje osi
  Anritsu/polityki wykonania: **20 passed, 4 subtests passed**.
  Wybrane mutacje sweepa i kompletne runy analyzer-only: **30 passed**.
  Ruff zaliczony. Nie używano aparatury.
- To podstawa dalszego ograniczenia shutdown, nie zamknięcie całego
  punktu. Nie przerywa kodu Python ani sterownika ignorującego timeout.
  Transporty bez _session (MOKE) zachowują własną obsługę terminów.
  Kolejka GUI ma limit oczekiwania, lecz musi jeszcze przekazać absolutny
  deadline do wykonywanej procedury adaptera; wspólny deadline wszystkich
  działań cleanup i operacje magazynu danych pozostają otwarte.

## Siedemdziesiąta seria — deadline kolejki przekazany do adaptera

- Przeczytano _RunCall, RunDeviceAdapter, call_for_run/read_for_run oraz
  InstrumentWorker._invoke_member. Limit oczekiwania kończył czekanie
  klienta, lecz uruchomiona procedura dostawała jedynie timeout per I/O.
- Żądanie przechowuje teraz absolutny termin wyznaczony przed emisją
  do kolejki. try_start odrzuca wygasłe żądanie także bez wcześniejszego
  anulowania przez oczekującego. Worker przekazuje pozostały czas do
  operation_timeout adaptera, z zachowaniem osobnego limitu per I/O.
  Zakres obejmuje również pobranie właściwości adaptera.
- Test przeprowadza żądanie przez call_for_run → invoke_for_run → adapter
  → sesję: z budżetu 1 s po 0,5 s w kolejce zostaje 0,5 s; kolejne I/O
  otrzymują coraz krótszy limit, a komenda po deadline nie wychodzi.
  Testowane są także przywrócenie sesji/timeoutu, wygasanie bez waitera
  oraz niepewny wynik operacji już rozpoczętej.
- Kolejka, deadline i rezerwacje: **21 passed**. Potwierdzenie shutdown
  i polityka wykonania: **38 passed, 4 subtests passed**. Ruff zaliczony.
- Nie oznacza to twardego przerwania niekooperującego sterownika ani
  operacji CPU. Wspólny budżet całego cleanup i osobny transport MOKE
  wymagają dalszej pracy; nie uznano pełnego punktu shutdown za zamknięty.

## Siedemdziesiąta pierwsza seria — budżet operacji MOKE obejmuje TCP i rampę

- Przeczytano MokeBoxTcpTransport oraz io_timeout, _ramp i stop_vout
  adaptera. Odbiór pojedynczego rekordu miał deadline, ale kolejne rekordy
  i wysyłanie żądania nie dzieliły terminu operacji przekazanego z kolejki.
- Transport ma teraz zagnieżdżany operation_timeout. Connect i send
  korzystają z pozostałego czasu; recv_exact bierze wcześniejszy z terminu
  rekordu i całej operacji. Zakres przywraca poprzedni termin. Adapter
  przekazuje go do transportu i uwzględnia w I/O, rampie oraz retargetingu.
  Krótki budżet nie omija limitu szybkości ani odstępu kroków rampy.
- Test sterowanego zegara obejmuje send + dwa odbiory i brak dalszego I/O
  po wygaśnięciu. Test rampy używa profilu z rzeczywistymi ograniczeniami
  czasowymi, ale wyłącznie transportu pamięciowego: brak zapisu DAC,
  brak potwierdzenia zera, zamknięcie niepewnej sesji.
- Początkowy test ujawnił błędny opis wygasłego terminu jako niedodatniego
  timeoutu; poprawiono komunikat. Kolejna próba wykazała, że profil
  simulation celowo omija fizyczne opóźnienia rampy; test poprawiono na
  profil z ograniczeniami czasowymi (bez fizycznego sprzętu).
- Budżet MOKE, recovery połączenia i sterowanie napięciem: **36 passed**.
  Protokół, trajektoria i kolejka: **26 passed, 4 subtests passed**.
  Ruff zaliczony. Wspólny limit wszystkich działań shutdown pozostaje
  otwarty; ta zmiana ogranicza pojedynczą operację i jej składowe.

## Siedemdziesiąta druga seria — shutdown wszystkich kanałów MOKE należących do runu

- Lektura _safe_shutdown i stop_moke_voltage ujawniła, że runner wywoływał
  stop_vout bez kanału, zerując tylko ostatnio wybrany profil. Wcześniej
  uzbrojone VOUT mogły zostać niezerowe mimo sukcesu ostatniego kanału.
- Runner zachowuje zbiór uzbrojonych kanałów i kanałów z próbą mutacji.
  Jawny Stop i końcowy shutdown próbują wyzerować każdy z nich, bez
  poleceń do pozostałych wyjść. Błąd jednego kanału nie pomija próby dla
  następnego. Zbiór jest resetowany dopiero przy rozpoczęciu nowego runu.
- Metadane zachowują dotychczasowy ostatni dac_shutdown oraz dodają
  osobne dac_shutdown_N dla każdego kanału. DAC zero nie zmienia statusu
  zasilacza Kepco na potwierdzone OFF.
- Test dwóch uzbrojonych kanałów korzysta z rzeczywistego adaptera i
  pamięciowego transportu: oba wracają do zera, dziennik zawiera wyłącznie
  ich numery. Testy błędu/niepotwierdzenia pierwszego kanału weryfikują
  próbę drugiego. Pierwszy zestaw wraz z MOKE sweep execution: **14 passed**.
  Ruff zaliczony.

- Rozszerzono test o jawny Stop i uruchomiono regresje timed reference,
  w tym pełne archiwum 297 punktów × Avg32. Ten przebieg nadal trwa;
  jego wyniku nie zaliczono jako sukcesu. Wspólny deadline cleanup
  pozostaje otwarty.

## Siedemdziesiąta trzecia seria — brak starego potwierdzenia po błędzie zerowania

- Sprawdzono projekcję dac_shutdown na kartę MOKE. Zachowany ostatni
  rekord zapewnia zgodność wyświetlania, a rekordy per kanał pozwalają
  rozliczyć wielokanałowe cleanup. W runnerze znaleziono jednak możliwość
  zachowania poprzedniego safe_target_confirmed=True po kolejnej nieudanej
  próbie zerowania tego samego kanału.
- Przed nową próbą runner zapisuje stan niepotwierdzony z actual_v=None
  zarówno w rekordzie ostatniej operacji, jak i rekordzie kanału. Dopiero
  wynik stop_vout może ponownie potwierdzić zero. Niepewna próba nie może
  dziedziczyć pozytywnego wyniku wcześniejszej operacji.
- Regresja wykonuje udane zerowanie, następnie wstrzykuje utratę readbacku
  i sprawdza oba rekordy. Zestaw zakresu shutdown MOKE: **5 passed**.
  Ruff zaliczony.
- Dłuższy przebieg z serii 72 nadal działa. Odczyt CSV podczas jego pracy
  wykazał 132 wiersze razem z nagłówkiem, a proces nadal zużywa CPU.
  Nie restartowano testu ani nie uznano trwającego przebiegu za zaliczony.

## Siedemdziesiąta czwarta seria — wspólny deadline cleanup aparatury

- Runner zachowuje jeden termin od pierwszej operacji aparatury w finally
  lub końcowym fallback. Termin nie odnawia się przy obsłudze kolejnego
  błędu ani powtórnym _safe_shutdown. Jest resetowany przy nowym runie.
- ExecutionPolicy ma walidowany shutdown_timeout_s. Konfiguracja YAML:
  `execution.shutdown_timeout: "60 s"` (domyślnie 60 s). Zdarzenie
  shutdown_budget_started utrwala efektywny limit i jego zakres.
- Finally rezerwuje część czasu dla późniejszych OFF; fallback dzieli
  pozostały czas między pozostałe urządzenia. Operacje korzystają z budżetu
  całej procedury i timeoutu I/O. Watchdog obejmuje także cleanup.
  Przekroczenie lub wyczerpanie terminu jest błędem i uruchamia istniejącą
  ścieżkę watchdog/E-STOP; nie jest potwierdzeniem bezpiecznego stanu.
- Test kontrolowanego zegara obejmuje trzy urządzenia, przekroczenie
  przydziału pierwszego, obsłużenie następnych, współdzielenie czasu przez
  finally i fallback oraz brak nowego budżetu po powtórnym shutdown.
  Konfiguracja i niepoprawne limity są walidowane. W pierwszym przebiegu
  test używał nieobsługiwanej jednostki min; zastąpiono ją obsługiwanym ms.
- Regresja MOKE i deadline: **20 passed**. Końcowa polityka i deadline:
  **13 passed, 4 subtests passed**. Ruff zaliczony.
- Granica tej naprawy jest jawna: budżet dotyczy sterowania aparaturą
  w runnerze. Flush/close HDF5, cleanup przed powstaniem runnera i czas
  zamknięcia osobnych sesji E-STOP nie są nim ograniczone. Sterownik
  ignorujący timeout może nadal blokować swój wątek; watchdog zgłasza
  taki przypadek, nie zabija wątku posiadającego sesję.
- Trwający test 297 punktów ma już 298 wierszy CSV z nagłówkiem; pozostaje
  końcowy wynik procesu i walidacji archiwum. Ten proces załadował kod
  przed niniejszą zmianą deadline, więc nie kwalifikuje jej wydajności.
- **Wynik końcowy tego procesu:** 13 passed, 453.16 s. Pełne 297 punktów,
  Avg32, surowe źródła, dwie referencje, wymagane metadata i walidacja
  PyThat przeszły. Dowód archiwum zapisano w
  [simulation-297-avg32.json](../2026-10-05-requested-sweep/simulation-297-avg32.json).
  Wcześniejsze wpisy o trwającym przebiegu są historyczne; proces zakończył
  się kodem 0 i nie wymaga dalszego odpytywania.

## Siedemdziesiąta piąta seria — sesje E-STOP i otwieranie VISA w budżecie

- Przeczytano EmergencyStopWorker oraz connect adapterów VISA. Zakres
  operation_timeout otwarty przed connect nie obejmował nowo utworzonej
  sesji. Dodano wspólne _open_session dla Rigola, Keithleya, Anritsu
  i Lake Shore: open_timeout korzysta z pozostałego czasu, a późniejsze
  zapytania kwalifikacyjne zachowują ten sam deadline. Sesja zwrócona
  po terminie jest zamykana i nie jest uznawana za poprawnie otwartą.
- E-STOP współdzieli jeden termin między równoległymi sesjami. Zakres
  obejmuje connect, OFF/ABORT i disconnect. Kończenie po terminie zwraca
  błąd, nie sukces. Bezpośrednie close sterownika, które ignoruje timeout,
  nadal nie daje się bezpiecznie przerwać przez zabicie wątku.
- Testy obejmują czas zużyty w open przed *IDN?, brak kolejnego zapytania
  po deadline, zamknięcie spóźnionej sesji i objęcie trzech etapów E-STOP
  jednym zakresem. Końcowy moduł potwierdzeń i terminów: **38 passed**.
- Szerszy przebieg ujawnił dwie atrapy DeviceAdapter bez sesji VISA,
  dziedziczące timeout wymagający sesji. Dodano jawny bezblokujący zakres
  dla tej pamięciowej atrapy. Wzmocniono też asercję RunResult.error;
  po poprawieniu dostępu do result w słowniku wyniku workera oba testy
  izolacji przeszły. Pozostałe regresje tego przebiegu: **18 passed**.
  Ruff zmienionych modułów zaliczony.
- Cleanup przed zbudowaniem runnera i zamykanie HDF5 nadal wymagają
  osobnego rozliczenia czasu; pozostały zakres raportu nie jest zamknięty.

## Siedemdziesiąta szósta seria — cleanup sprzętu przed zamykaniem błędnego archiwum

- Lektura except/finally RunWorker wykazała writer.close("faulted") przed
  OFF/ABORT i disconnect. Powolny dysk lub walidacja archiwum mogły więc
  opóźniać pierwszą próbę wyłączenia urządzeń po nieudanym starcie.
- Zamknięcie błędnego pliku przeniesiono za próby cleanup aparatury.
  Błąd close nie pomija zwolnienia rezerwacji i nie zastępuje pierwotnej
  przyczyny awarii. Wynik workera nadal jest publikowany po cleanup.
- Poprawiono również uznawanie runnera za właściciela wykonanego shutdown:
  samo skonstruowanie obiektu nie wystarcza. Nieoczekiwany wyjątek,
  który wydostał się z runner.run, powoduje zapasowy cleanup workera.
  Zwykły zwrot RunResult zachowuje dotychczasową odpowiedzialność runnera.
- Testy sprawdzają kolejność ABORT → disconnect → close dla awarii
  konstruktora i run(), każdorazowo z sukcesem/błędem ABORT i close.
  Regresje anulowania i błędów inicjalizacji: **18 passed**. Ruff zaliczony.
- Ta naprawa usuwa zależność pierwszego OFF od czasu HDF5. Nie ustanawia
  twardego limitu flush/close i nie kończy całego audytu wydajności.

## Siedemdziesiąta siódma seria — błąd końcowego cleanup zmienia wynik runu

- Sprawdzono wynik RunWorker po poprawnym zwrocie runnera. Błędy
  disconnect/release były emitowane jako worker_cleanup_warning, ale
  końcowy RunResult mógł pozostać SAFE bez error i nadpisać prezentację
  błędu na ekranie Execution.
- Wynik jest teraz FAULT z opisem nieukończonego cleanup. Zachowuje
  wcześniejszy błąd pomiaru, completed_actions, stored_points oraz ścieżkę
  archiwum. Payload wyniku zawiera cleanup_errors; ostrzeżenie otrzymuje
  także path, aby powiązać błąd z konkretnym plikiem.
- Testy wstrzykują osobno błąd disconnect i release, po sukcesie i po
  błędzie pomiaru. Regresje cleanup i anulowania: **18 passed**.
  Ruff zaliczony. Nie otwierano ponownie zamkniętego HDF5 ani nie
  zmieniano danych pomiarowych; stan końcowego cleanup jest wynikiem
  workera i zdarzeniem diagnostycznym, późniejszym od zamknięcia pliku.
- Regresje izolacji urządzeń i zakresu E-STOP: **8 passed**.

## Siedemdziesiąta ósma seria — diagnostyka walidacji bez nieograniczonego bufora RAM

- Przeczytano validate_archive_isolated, zamykanie Hdf5RunWriter i zakres
  konwersji PyThat. stderr procesu był przechwytywany przez PIPE do RAM,
  mimo że później używano tylko końcówki. Ignorowany stdout PyThat trafiał
  do StringIO, także rosnącego bez limitu przez całą konwersję.
- stderr trafia teraz do automatycznie usuwanego pliku tymczasowego;
  do komunikatu błędu odczytuje się wyłącznie ostatnie 2000 bajtów.
  Nieużywany stdout PyThat trafia do systemowego urządzenia null.
  Walidacja, jej timeout i wyniki naukowe nie zostały pominięte.
- Nowy test generuje ponad 2 MiB diagnostyki, sprawdza zachowanie końcówki,
  ograniczony rozmiar komunikatu i zamknięcie pliku tymczasowego.
  Regresje rzeczywistej walidacji, błędów procesu i strumieniowej konwersji:
  **7 passed**. Ruff zaliczony.
- Plik diagnostyczny może rosnąć na dysku do końca procesu; naprawa
  usuwa nieograniczone przechwytywanie logów w pamięci. Nie ustanawia
  limitu czasu samego HDF5 flush/close ani nie kwalifikuje wszystkich
  ścieżek odczytu dużych danych.

## Siedemdziesiąta dziewiąta seria — uszkodzona skala nie staje się osią indeksów

- Przeczytano cały pythat_bridge.py i implementację get_scales w zainstalowanym
  PyThat 0.2.14. Obie ścieżki konwersji mogły zastąpić niezgodną długość
  skali przez arange, tracąc rzeczywiste współrzędne bez błędu importu.
- Walidacja strumieniowa i pełny import używają teraz tej samej kontroli
  kształtu i długości skali. Nieprawidłowe dane powodują ExecutionError.
  Odrzucane są także niefinitywne parametry oraz przepełnienie przy
  tworzeniu współrzędnych. Brak opcjonalnej skali zachowuje dotychczasowy
  kontrakt None. Nadal używana jest pierwsza zapisana skala, zgodnie
  z PyThat; nie jest to walidacja zgodności wszystkich skal w archiwum.
- Regresje wstrzykują złą długość, zły kształt, NaN i przepełnienie do
  rzeczywistego HDF5, osobno dla obu trybów importu. Sprawdzono także
  poprawną strumieniową konwersję oraz izolowaną walidację: **15 passed**.
  Ruff zaliczony. Pierwszy przebieg ujawnił błąd selekcji grupy w nowym
  teście; poprawiono fixture i powtórzono cały powyższy zestaw.

## Osiemdziesiąta seria — wspólna oś PyThat wymaga zgodnych skal wszystkich widm

- Prześledzono zapis surowych i przetworzonych wierszy thaTEC oraz
  construct_tree/get_scales PyThat. Każdy rekord ma własną skalę w HDF5,
  lecz import budował jedną współrzędną z pierwszego rekordu. Późniejsza
  zmiana zakresu lub uszkodzenie skali mogły pozostać niezauważone.
- Oba tryby importu porównują teraz skale wszystkich rzeczywistych
  rekordów, porcjami po 4096, bez materializacji całego datasetu skal.
  NaN lub rozbieżność powoduje jawny błąd, zamiast przypisania widmu
  cudzej osi. Oryginalne osie per punkt w HDF5 pozostają dostępne.
- Puste checkpointy aplikacji rozpoznawane są po roli wiersza,
  znaczniku czasu NaN i wyłącznie NaN w danych. Ich techniczne skale
  nie uczestniczą w wyborze wspólnej osi. Dane bez timestampu nie
  mogą zostać w ten sposób ukryte jako luka.
- Testy obejmują zmianę offsetu/kroku i NaN w drugim rekordzie,
  oba tryby importu oraz luki przed pierwszym i między późniejszymi
  widmami. Walidacja/streaming: **22 passed**. Czytniki, mapper,
  walidator i rollback zapisu: **29 passed, 4 skipped, 3 subtests passed**.
  Pominięcia dotyczą nieobecnych licencjonowanych plików golden HDF5.
  Ruff zaliczony.
- To usuwa ciche przekłamanie osi, ale nie dodaje reprezentacji zmiennej
  siatki w PyThat: taki import jest teraz jawnie odrzucany. Zapis prywatny
  zachowuje osie każdego pomiaru; pełna obsługa zmiennych siatek przez
  publiczną konwersję pozostaje osobnym, niezamkniętym ograniczeniem.

## Osiemdziesiąta pierwsza seria — deadline cleanup także przed startem runnera

- Prześledzono try/except/finally RunWorker i przekazywanie deadline
  przez RunDeviceAdapter. Po awarii inicjalizacji lub wyjątku uciekającym
  z runnera cleanup nie korzystał z operation_timeout ani wspólnego
  budżetu, chociaż ścieżka zwykłego runnera już je miała.
- Cleanup workera korzysta teraz z shutdown_timeout polityki i dzieli
  pozostały czas między urządzenia należące do planu. Zakres obejmuje
  connected, OFF/ABORT i disconnect, także dla proxy kolejki urządzenia.
  Błąd jednego urządzenia nie pomija pozostałych prób. Brak czasu lub
  przekroczenie budżetu powodują błąd cleanup, nigdy potwierdzenie SAFE.
- Zachowano ABORT bez komend RF dla samego analizatora, izolację urządzeń
  spoza planu i kolejność sprzęt przed zamykaniem uszkodzonego archiwum.
  Błędna polityka nie blokuje samej próby cleanup: używa ona domyślnego
  limitu i raportuje nieprawidłową konfigurację.
- Testy z kontrolowanym zegarem sprawdzają wspólny budżet, rezerwę czasu,
  wyjątek OFF, zakres kontekstu także dla connected/disconnect i spóźniony
  powrót niekooperującego urządzenia. Regresje inicjalizacji i zakresu
  shutdown: **27 passed**. Ruff zaliczony.
- Nie jest to gwarancja przerwania natywnego wywołania, które ignoruje
  timeout. HDF5 close i zwalnianie rezerwacji nadal nie mają wymuszonego
  przerwania; timeout cleanup nie oznacza potwierdzonego wyłączenia.

## Osiemdziesiąta druga seria — zmiana kanału Rigola zachowuje autorstwo planu

- Przeczytano RigolNodeEditorDialog, w szczególności _channel_changed,
  _load_snapshot, _waveform_changed i serializację akcji. Zmiana kanału
  zwykłego węzła importowała draft docelowej karty. Nadpisywała wartości,
  a zmiana SIN na DC ukrywała selektory i zerowała wybrane akcje.
- Usunięto automatyczne ładowanie draftu przy zmianie kanału. Zmiana
  adresata zachowuje wpisane wartości, tryby Set/Sweep, ROI oraz politykę
  OUTPUT. Porównanie nadal odświeża się względem wybranego kanału.
  Nie są wysyłane komendy sprzętowe ani dodawana konfiguracja bazowa.
- Regresja obejmuje Set/Sweep, zmianę CH1→CH2→CH1, różne drafty SIN/DC,
  edycję wartości przed przełączeniem, zgodność serializowanych akcji
  oraz pokazany dialog w jasnym i ciemnym motywie: **18 passed**.
  Ruff zaliczony. Obejrzano rigol-retarget-light.png (1120×780), bez
  nakładania paneli; zapisano również wariant dark.
- Ta poprawka dotyczy utraty edycji przy zmianie kanału. Nie zamyka
  odrębnego zagadnienia porównania sprzężonych poziomów z konfiguracją
  poprzedzającą węzeł w recepturze.

## Osiemdziesiąta trzecia seria — Period zapisuje rzeczywistą wybraną częstotliwość

- Prześledzono synchronizację pól, comparison, configuration_snapshot,
  planned_parameter_actions oraz accept Rigola. Serializacja używała
  ukrytego pola frequency niezależnie od wybranej reprezentacji czasu.
  Bez editingFinished pozostawała stara wartość; błędny okres także
  mógł pozostawić poprawną, lecz nieaktualną częstotliwość do zapisu.
- W trybie Period wspólna funkcja wylicza częstotliwość z aktualnego
  okresu dla snapshotu, akcji i porównania. Okres musi być dodatni,
  skończony i mieć wymiar czasu; wynik również musi być skończony.
  Accept przechwytuje błąd i blokuje zatwierdzenie, także bez settings.
- Regresje potwierdzają 250 us → 4000 Hz bez editingFinished oraz
  odrzucenie zera, ujemnego okresu, błędnego tekstu i jednostki napięcia.
  Testy Rigola: **23 passed**; po współdzieleniu funkcji przez comparison
  powtórzono pięć nowych przypadków: **5 passed**. Ruff zaliczony.

## Osiemdziesiąta czwarta seria — błędne Amplitude/Offset nie zapisuje starych poziomów

- Analiza _sync_levels_from_vpp_offset wykazała, że błąd parsowania był
  ignorowany również podczas serializacji. Poprzednie High/Low mogły
  trafić do snapshotu pełnej konfiguracji mimo błędnego widocznego wpisu.
- Synchronizacja podczas pisania nadal toleruje niekompletny tekst,
  ale snapshot i zapis akcji wymagają poprawnego przeliczenia.
  Nieprawidłowe jednostki, tekst i ujemna amplituda blokują zatwierdzenie.
- Nowe regresje sprawdzają zachowanie poprzednich pól podczas pisania,
  odrzucenie ich jako wyniku zapisu oraz poprawne 6 mV Vpp / 1 mV offset
  → High 4 mV / Low -2 mV bez editingFinished. **29 passed**, Ruff zaliczony.
- Starszy test buildera wymagał automatycznego importu draftu CH2, czyli
  zachowania usuniętego w serii 82. Zmieniono oczekiwanie na zachowanie
  autorstwa i osobne porównanie z CH2. Regresje Rigola w builderze:
  **4 passed** po tej aktualizacji (wcześniej 1 niezgodne oczekiwanie).

## Osiemdziesiąta piąta seria — skrót PyThat bez ładowania próbek i poprawne mHz

- Przeczytano thatec_reader.py i pythat_reader.py. Skrót używany przez
  Results zwraca tylko dimensions/variables, lecz wywoływał pełny import
  z Dataset.load. Przełączono go na inspect_measurement_tree: zachowuje
  pełną konwersję/walidację publiczną, ale nie materializuje wszystkich
  próbek w pamięci. Pozostałe ścieżki Results nie są tym samym uznane
  za całkowicie strumieniowe.
- W _normalise_frequency_axis nadal istniał lokalny casefold, przez
  który mHz było interpretowane jako MHz. Czytnik korzysta teraz ze
  wspólnego parsera wielkości z kontrolą wymiaru częstotliwości.
  Nieznane i inne jednostki pozostają bez zmiany wartości i etykiety.
- Test z rzeczywistą konwersją blokuje Dataset.load podczas odczytu
  skrótu i porównuje jego zawartość z pełnym importem. Syntetyczne
  publiczne pliki HDF5 sprawdzają mHz/MHz/kHz/Hz/GHz i inną jednostkę,
  zachowując wartości amplitudy oraz dBm. **22 passed, 3 skipped**;
  pominięcia dotyczą brakującego licencjonowanego golden HDF5.
  Ruff zaliczony.

## Osiemdziesiąta szósta seria — czytnik thaTEC nie maskuje uszkodzonej osi

- Prześledzono row_slice, spectrum_slice, _scale_pair i _metadata_float.
  Zbyt krótka istniejąca skala uruchamiała fallback metadanych, a błędny
  tekst offset/multiplier był zamieniany na 0/1. Powstawała pozornie
  poprawna, ale niewynikająca z zapisu oś.
- Istniejąca skala musi mieć poprawny rząd i kompletny segment dla
  wybranego checkpointu oraz skończone liczby. Metadane obecne, lecz
  nieprawidłowe, powodują ExecutionError. Przepełnienie obliczeń osi
  i konwersji jednostek także jest odrzucane. Brak opcjonalnej skali
  nadal pozwala użyć poprawnych metadanych.
- Nie wymaga się poprawności niezacommitowanego końca skali do odczytu
  wcześniejszego checkpointu. Granica zatwierdzonych danych pozostaje
  sprawdzana przed pobraniem wartości.
- Testy rzeczywistych plików obejmują obcięcie, zły rząd, NaN, overflow,
  niepoprawny tekst i infinity w metadata oraz poprawny fallback.
  Regresje czytników: **27 passed**, Ruff zaliczony. Osobny przebieg
  z testami golden wykazał trzy pominięcia z powodu brakującego fixture.

## Osiemdziesiąta siódma seria — zgodność wartości i timestampów serii skalarnych

- Prześledzono scalar_series i jego użycie przez tabelę wyników,
  spectrum_tab i heatmap_coordinates. Czytnik niezależnie obcinał data
  i timestamp, bez sprawdzenia zgodności długości; brakująca część
  zatwierdzonego wiersza mogła zostać przedstawiona jak krótsza seria.
- Odczyt sprawdza kompletność danych do granicy commit, jednowymiarowe
  timestampy i zgodność liczby par w odczytywanym zakresie. Dla własnych
  zatwierdzonych punktów brak timestampu jest błędem. Zewnętrzne pliki
  bez opcjonalnego datasetu timestamp zachowują dotychczasową obsługę.
- Niezgodny, niezatwierdzony ogon data lub timestamp nie blokuje odczytu
  poprawnego prefiksu. Testy obejmują obie kolejności przerwanego append,
  braki wewnątrz commit, zły rząd timestampów i zewnętrzne pliki.
  **15 passed** wraz z istniejącymi regresjami granicy publicznych
  wierszy/osi. Ruff zaliczony. Nie uruchamiano w tej serii pełnych
  testów awarii procesu; wybrano ich przypadki dotyczące scalar guard.

## Osiemdziesiąta ósma seria — bezpośredni odczyt respektuje ciągły commit

- Prześledzono spectrum, spectrum_point_count, spectrum_correction,
  spectrum_acquisition i _require_committed_spectrum. Bezpośrednie
  odczyty ufały wyłącznie complete wybranego punktu, choć lista wyników
  kończy się na pierwszym brakującym/niekompletnym checkpointcie.
- Wszystkie cztery akcesory używają teraz tej samej granicy ciągłego
  zatwierdzonego prefiksu. Późniejszy punkt z complete=True za luką
  nie może zostać odczytany przez ręczne podanie indeksu. Korekcja
  i envelope odrzucają także indeks ujemny przed otwarciem pliku.
- Regresje obejmują brak i incomplete poprzednika oraz zachowanie
  odczytu wcześniejszego punktu: **26 passed** z testami serii.
  Zapis/odczyt rzeczywistych korekcji: **36 passed**. Ruff zaliczony.

## Osiemdziesiąta dziewiąta seria — walidacja tablic widm i referencji przy odczycie

- Przeczytano spectrum, references i reference. Sprawdzały długość
  wczytanych krotek, ale nie poprawność osi i amplitud. Uszkodzone
  dane mogły zawierać NaN/inf, duplikaty lub odwrócone częstotliwości;
  niepoprawny rząd/tablice zespolone powodowały niekontrolowane błędy
  konwersji zamiast jednoznacznego ExecutionError.
- Wspólny odczyt surowych osi sprawdza obecność, rząd 1, zgodną długość
  co najmniej dwóch punktów, rzeczywisty typ liczbowy, wartości skończone
  oraz ściśle rosnącą częstotliwość. Kontrakt odpowiada walidacji surowego
  SpectrumTrace po stronie zapisu. Wartości dBm nie są przeskalowywane.
- Osiem rodzajów uszkodzeń sprawdzono osobno dla widma, pojedynczej
  referencji i listy referencji. Łącznie z regresjami granicy commit
  oraz HDF5 writer: **56 passed**. Ruff zaliczony. Ta kontrola dotyczy
  surowych osi frequency_hz/power_dbm, nie kwalifikuje wszystkich
  dodatkowych produktów przetwarzania widma.

## Dziewięćdziesiąta seria — zakres Reference Level nie jest nadpisywany

- Ponownie zestawiono raport i pełne notatki z aktualnym kodem Settings.
  Ustalenie o nadpisywaniu reference_level pozostawało aktualne:
  repair_known_issues bezwarunkowo zastępowało limit przez -120..+50 dBm.
  AnritsuSafety nie walidowało tego pola, a validate_anritsu_spectrum
  sprawdzało tylko granice sprzętowe.
- Usunięto zastępowanie jawnego zakresu. Model kontroluje wymiar dBm,
  kolejność i kompletność granic; aktywna akwizycja wymaga kompletnego
  włączonego limitu. Wspólna walidacja adaptera i preflight egzekwuje
  zakres operatora oraz niezależnie granice sprzętu. Wyłączenie limitu
  operatora nie wyłącza ograniczenia sprzętowego.
- Test rzeczywistego zapisu/odczytu Settings sprawdza zachowanie
  -80..-10 dBm, dozwolone granice i odrzucenie nastaw spoza zakresu.
  Sprawdzono także błędne jednostki, odwrócone/niepełne granice i
  zachowanie enabled=False. Starszy test granic sprzętowych otrzymał
  jawny profil obejmujący te granice — wcześniej polegał na ignorowaniu
  węższego limitu fixture. **45 passed, 23 subtests passed**, Ruff zaliczony.
- Nie zmieniano lokalnego pliku Settings ani aparatury. Utraconych
  wcześniej limitów operatora nie można odtworzyć bez wcześniejszej
  kopii konfiguracji; poprawka zapobiega kolejnemu nadpisywaniu.

## Dziewięćdziesiąta pierwsza seria — model Rigola nie dopuszcza fikcyjnej rezystancji źródła

- Potwierdzono ustalenie z raportu: model Settings i estimate_rigol_current
  odrzucały tylko rezystancję poniżej 50 Ω, dopuszczając np. 5000 Ω.
  Dla HIGHZ oznaczało to zaniżenie prądu zwarcia i mocy w modelu
  sprzętowym. Pole nie stanowi dowodu istnienia zewnętrznego rezystora.
- Obie granice wymagają teraz fizycznych 50 Ω; po sprawdzeniu estymator
  oblicza wynik z dokładnie 50 Ω. Zmiana deklaracji LOAD nadal wpływa na
  przeliczenie wyświetlanego napięcia na napięcie rozwartego źródła.
  Nie dodano modelu niezweryfikowanego rezystora zewnętrznego.
- Testy odrzucają 0/49/51/5000 Ω w konfiguracji i nieprawidłową
  rezystancję przy bezpośrednim wywołaniu estymatora. Sprawdzono
  granice prądu/mocy HIGHZ i LOAD=50 oraz równoważne 0.05 kohm.
  Nowe regresje: **12 passed**. Istniejące Settings/safety/repository:
  **39 passed, 23 subtests passed** w przebiegu łącznym. Pierwszy przebieg
  ujawnił niewłaściwy format LOAD w jednym nowym fixture; poprawiono
  go na liczbowe omy zgodne z kontraktem i powtórzono moduł.
  Ruff zaliczony. Nie zmieniano lokalnych ustawień ani aparatury.

## Dziewięćdziesiąta druga seria — poprawna normalizacja limitu napięcia Rigola

- Potwierdzono próbę przypisania combined_voltage_limit wewnątrz
  model_validator po utworzeniu modelu frozen. Ujemny limit zamiast
  normalizacji kończył się błędem niemutowalności.
- Normalizacja wartości bezwzględnej odbywa się teraz w walidatorze
  pola, przed utworzeniem modelu. Zero, zły wymiar i wartości niefinitywne
  są odrzucane; wynikowy model nadal pozostaje niemutowalny.
- Sprawdzenie migracji ujawniło powiązane ryzyko: min(-2 V, 10 mV)
  wybierało -2 V, które po naprawieniu normalizacji stałoby się 2 V.
  Migracja porównuje teraz moduł istniejącego limitu, zachowując w tym
  przypadku 10 mV. Test zapisuje i ponownie ładuje taki profil.
- Regresje obejmują normalizację, frozen, egzekwowanie limitu przez
  walidator przebiegu, pięć błędnych wartości i migrację. Razem
  z repozytorium Settings: **16 passed**. Ruff zaliczony.

## Dziewięćdziesiąta trzecia seria — readiness rozpoznaje aktualizację DAC MOKE

- Potwierdzono ustalenie raportu: readiness rozpoznawało tylko osobne
  OUTPUT ON i mogło opisać plan sterowania DAC jako pozbawiony takiej
  akcji, pomijając kontekst deklaracji DUT. Estymator dodatkowo nie
  klasyfikował Anritsu SG ON jako powodu przeglądu DUT i okablowania.
- Readiness i estymacja używają wspólnej action_can_energize dla
  jawnych OUTPUT ON i update_moke_voltage. Również docelowe 0 V jest
  zmianą DAC, nie dowodem odłączenia wzmacniacza. Stop/OFF nie są
  klasyfikowane jako włączenie. Samo arm MOKE pozostaje operacją
  pamięciową bez komendy SET_VOUT, zgodnie z przeczytanym adapterem.
- Informacja o metadata-only DUT i ostrzeżenie estymacji pojawiają
  się także dla planu MOKE bez OUTPUT ON. Nie zmieniono wykonania
  komend ani nie przedstawiono deklaracji DUT jako egzekwowanej ochrony.
- Regresje klasyfikacji i dashboard readiness: **13 passed**.
  Regresje estymacji: **4 passed**. Ruff zaliczony.

## Dziewięćdziesiąta czwarta seria — stan gałęzi Execution odświeża się z dzieckiem

- Potwierdzono raportowane ustalenie w MeasurementTreeModel.apply_states.
  _descendant_state wylicza stan kontenerów z potomków, lecz dataChanged
  było emitowane tylko dla bezpośrednio zmienionego wiersza i najbliższej
  osi SET_ROI. Samo czyszczenie cache nie informuje widoku o rodzicach.
- Powiadomienia obejmują teraz pełny łańcuch przodków. Słownik affected
  deduplikuje je dla całej paczki; nie ma resetu drzewa, ponownego
  budowania modelu ani osobnych repaint dla każdego zdarzenia wejściowego.
- Zaktualizowano stary test wymagający jedynie dwóch powiadomień.
  Nowy test pokazuje widok 1000×600, rozwija gałęzie, przekazuje dwa
  kolejne stany liścia w jednej paczce i sprawdza APPLIED w czterech
  widocznych wierszach, jedno powiadomienie na wiersz i brak modelReset.
  Model i kontrakty odświeżania: **27 passed**. Ruff zaliczony.

## Dziewięćdziesiąta piąta seria — Resume ponownie ocenia gotowość stacji

- Porównano _start_run_from_ready_dialog i _start_resume_from_ready_dialog.
  Potwierdzono brak dashboard.evaluate_readiness w Resume. Zatrzask
  E-STOP nadal był kontrolowany przez RunController, lecz ścieżka GUI
  nie sprawdzała go przed przygotowaniem wznowienia.
- Resume sprawdza teraz E-STOP i aktualne blocking_items przed snapshotem
  drzewa, pobraniem kontrolerów i startem workera. Błąd pokazuje przyczynę
  i pozostawia dialog otwarty. Nie zmieniono danych recovery ani polityki
  OUTPUT przy poprawnym starcie.
- Testy wykonują rzeczywisty handler na lekkim odbiorcy: osobno awaria
  audytu, storage, urządzenia, E-STOP oraz poprawne wznowienie. Nie są
  testem całego okna ani fizycznego recovery. Razem z modelem readiness:
  **11 passed**. Ruff zaliczony. Synchroniczny odczyt/kompilacja archiwum
  w poprzedzającej ścieżce Resume pozostają osobnym tematem wydajności.

## Dziewięćdziesiąta szósta seria — błąd probe Rigola nie przesuwa odpowiedzi

- Prześledzono connect i _probe_capabilities. Każdy wyjątek zapytania
  był traktowany jako brak funkcji, po czym wysyłano następne zapytania.
  Końcowe *CLS nie dowodzi synchronizacji kanału odpowiedzi po timeout.
- Błąd transportu/query przerywa teraz connect; jego istniejący cleanup
  zamyka sesję, usuwa capabilities i zachowuje UNKNOWN. Nie wysyła się
  dalszych probe ani *CLS na niepewnej sesji. Poprawnie odebrana, lecz
  nieobsługiwana odpowiedź nadal oznacza tylko brak danej funkcji.
- Testy wstrzykują timeout na początku, w środku i przy końcu listy,
  sprawdzając ostatnią komendę, wcześniejsze próby OFF i zamknięcie.
  FakeVisaSession uzupełniono o jawny początkowy COUN OFF, którego
  wcześniej brak maskował catch-all. Poprawiono nowe oczekiwanie testu:
  connected w bazowym API obejmuje UNKNOWN; brak sesji jest sprawdzany
  osobno i nie oznacza deklaracji fizycznego OFF.
- Regresje Rigola: **26 passed, 2 subtests passed**. Ruff zaliczony.
  Nie dodano niezweryfikowanej procedury resynchronizacji VISA. Firmware
  powodujący timeout opcjonalnego probe wymaga ponownego połączenia
  i jawnej konfiguracji zakresu probe; nie jest automatycznie uznawany
  za sprawny na podstawie kolejnych, potencjalnie przesuniętych odpowiedzi.

## Dziewięćdziesiąta siódma seria — retry upper obejmuje pełne operacje

- Prześledzono estimate, ExecutionPolicy i pętlę retry runnera.
  Estymator dodawał dla powtórzeń tylko command_overhead + backoff,
  choć runner przydziela nowy deadline całej operacji na każdą próbę.
  Lista estymatora pomijała też część ponawianych konfiguracji/OFF,
  a błędnie uwzględniała measure_moke_hall.
- Wspólny retry_candidate opisuje dozwolone rodzaje operacji; runner
  nadal osobno wymaga nieaktywnego OUTPUT dla konfiguracji źródeł.
  Górny model czasu dodaje pełny deadline i backoff na każdą dopuszczoną
  próbę. Nie zmieniono liczby retry, warunków wykonania ani nominalnego
  czasu planu.
- Regresje sprawdzają dokładną różnicę dla trzech retry: spectrum,
  reference, OFF oraz brak retry dla ON, MOKE update i wait.
  Estymacja: **10 passed**. Polityka/runner: **9 passed, 4 subtests passed**.
  Ruff zaliczony. Model dysku dla surowych ramek pozostających po
  nieudanych próbach jest odrębnym ustaleniem i nie jest zamknięty tą zmianą.

## Dziewięćdziesiąta ósma seria — rezerwa dysku obejmuje surowe próby retry

- Przeczytano ścieżkę estimate → _execute_with_policy →
  _acquire_averaged_spectrum → store_recipe_spectrum_sweep oraz zapis referencji.
  Każda ramka jest commitowana przed kolejną akwizycją i przed końcowym
  odczytem konfiguracji. DeviceError tego odczytu może pozostawić cały blok
  RAW, po czym retry zapisuje nowy blok. Estymator liczył tylko pierwszy.
- Rezerwa RAW obejmuje teraz retry_count + 1 prób, z osobnym limitem 9999
  ramek na próbę tła zbieranego przez zadany czas. Lista indeksów referencji
  również uwzględnia ten limit, zamiast tylko minimalnej liczby uśrednień.
  Publiczna średnia, nominalna liczba pomiarów i model pamięci importu nie
  są mnożone. Referencja importowana z pliku nie dostaje budżetu akwizycji RAW.
- Test integracyjny wstrzykuje DeviceError końcowego readbacku referencji:
  archiwum zachowuje osiem ramek (3 z nieudanej próby, 3 poprawnej referencji,
  2 sygnału), a średnia referencji wskazuje wyłącznie indeksy 3, 4, 5.
  Końcowy plik przechodzi walidację z require_pythat=True.
- Testy estymacji obejmują zwykłe widmo, referencję, tło czasowe oraz import.
  Nadal otwarte: rozmiar osi wynikający z bieżącego sprzętu, gdy plan nie
  zawiera konfiguracji analizatora. Ta poprawka nie kwalifikuje tego przypadku.

## Dziewięćdziesiąta dziewiąta seria — filtry obsługują niemodyfikowalny plan

- Regresje integracyjne ujawniły błąd wykonania filtrów po zamrożeniu planu.
  Przeczytano freeze_configuration oraz cały spectrum_processing.py:
  kompilator zmienia listy w krotki, natomiast parser filtrów i zewnętrznej
  listy pasm chronionych odrzucał krotki. Poprawny YAML przechodził kompilację,
  ale wykonanie kończyło się komunikatem o niedozwolonym wyborze filtrów.
- Parser przyjmuje obie reprezentacje sekwencji; zachowano kontrolę nazw,
  duplikatów, jednostek, parametrów i par częstotliwości. Plan pozostaje
  niemodyfikowalny. Sprawdzenie pozostałych ograniczeń list w compiler,
  mapperze schematu oraz recovery wskazało na wejścia RecipeNode/JSON,
  więc nie zmieniano ich automatycznie.
- Pierwszy przebieg: 52 passed, 2 failed (filtry). Po poprawce: **56 passed**
  dla estymacji, surowych ramek/retry, przetwarzania sweepów i niemodyfikowalności
  planu. Ruff zaliczony. Testy używają symulacji, bez komend do fizycznej aparatury.

## Setna seria — estymacja bez założenia domyślnego stanu analizatora

- Prześledzono estymację, preflight RunWorker, konfigurację Anritsu oraz
  walidację dopuszczalnych liczebności osi. Estymator używał sweep_points
  z defaults, choć sweep bez konfiguracji pozostawia aktualną nastawę sprzętu.
  Mogło to zaniżać liczbę próbek, rezerwę dysku i model pamięci importu.
- Do pierwszej konfiguracji w planie estymacja używa maksimum wspólnej listy
  obsługiwanych liczebności (10001) i pokazuje ostrzeżenie o nieznanej nastawie.
  Po konfiguracji stosuje jej liczbę punktów, z zachowaniem kolejności akcji.
  Częściowa konfiguracja adaptera weryfikuje pominięte pola względem baseline,
  więc jej points również stanowi warunek poprawnego wykonania akwizycji.
- To wyłącznie model zasobów: nie dodano konfiguracji urządzenia, odczytów
  VISA ani zmian parametrów planu. Nie użyto defaults jako dowodu stanu sprzętu.
  Dla nieznanej osi model czasu transferu także używa maksimum; rzeczywisty
  czas fizycznego sweepa pozostaje zależny od sprzętu i ustawień pasm.
- Regresje obejmują brak konfiguracji, małą jawną konfigurację oraz konfigurację
  dopiero między akwizycjami. Sprawdzono także publiczny budżet importu i retry.
  **17 passed**, Ruff zaliczony. Brak kwalifikacji na fizycznym analizatorze.

## Sto pierwsza seria — surowy spektrogram poza wątkiem GUI

- Przeczytano bufor, wybór ścieżki raw/filtered, obsługę wyników i worker
  spektrogramu. Ścieżka bez filtrów wykonywała snapshot/np.stack całego okna
  w GUI, a widget obliczał kontrast. Ścieżka z filtrami miała już worker.
- Raw korzysta teraz z tego samego kontrolera z zastępowaniem oczekującego
  żądania najnowszym. Do workera trafiają współdzielone, niemodyfikowalne
  wiersze wyświetlania; składanie macierzy i kontrast odbywają się w workerze.
  Szybka ścieżka bez filtrów zachowuje wartości float64, bez odejmowania,
  uśredniania ani filtrów. Zapis pomiarowy nie został zmieniony.
- Klucz cache zawiera też długość okna i wybór ścieżki raw. Wcześniej zmiana
  okna mogła pozostawić wynik dla starego zakresu, gdy ramki warmup były te same.
- Regresja zabrania GUI-snapshot, rejestruje wątek np.stack, porównuje surowe
  wartości i sprawdza zmianę okna 30 → 60 s. Test powiązanego MainWindow
  oczekuje teraz wyniku asynchronicznego.
- Odrębne, nadal otwarte ustalenie: _update_peak_tracking ponownie wykonuje
  detect_spectrum_peaks w GUI; ta seria nie zamyka całej wydajności strony.
- Weryfikacja: **42 passed** (raw worker, wspólne tło, unified workflow),
  **2 passed** (MainWindow i pokazane okno pływające, geometria kontrolek).
  Ruff zaliczony. Nie wykonano nowej kwalifikacji opóźnienia całego dużego runu.

## Sto druga seria — detekcja śledzonego piku w workerze

- Przeczytano tworzenie requestów, obsługę wyników, start/close/clear trackera
  oraz detekcję w _update_peak_tracking. Drugie wyszukiwanie lokalnego piku
  odbywało się w GUI po zakończeniu workera, mimo pozornie asynchronicznej analizy.
- Worker otrzymuje identyfikator sesji, cel i bramkę śledzenia; wykonuje tę samą
  lokalną detekcję z progami 4 dB/2 dB, bez fit, na wybranym wejściu lub wyniku
  filtrów. GUI wyłącznie przyjmuje znaleziony pik lub status utraty.
  Auto peaks może pozostać wyłączone podczas śledzenia.
- Start nowego śledzenia i clear historii zmieniają identyfikator sesji.
  Wynik ze starej sesji lub poprzedniego celu/bramki jest odrzucany. Zamknięty
  tracker nie przyjmuje wyników. Numer ramki pochodzi z wyniku workera,
  zamiast z nowszej rewizji aktualnie wyświetlanej strony.
- Test rejestruje wątek detektora przy Auto peaks OFF; osobny test odrzuca
  starą sesję i sprawdza przypisanie numeru ramki. Regresje workera/shutdown:
  **5 passed**, Ruff zaliczony.
- Pokazane okno MainWindow: test przesunięcia piku i okna trackera zaliczony.
  Starszy test auto cleanup początkowo zakładał inne domyślne przełączniki
  i istnienie ukrytej krzywej Raw. Ustawia teraz jawnie Auto peaks, markery
  i wyłączenie overlay; dopuszcza usuniętą krzywą jako poprawny brak widocznego
  Raw. Po tej korekcie fixture również zaliczony. Łącznie **7 testów zaliczonych**.
  Nie zmieniano ustawień domyślnych aplikacji ani progów detekcji.

## Sto trzecia seria — walidacja przetworzonych widm w czytniku

- Przeczytano spectrum oraz kontrakt _validate_processed writera. Czytnik
  sprawdzał długość processed_values, ale nie odrzucał NaN/inf, macierzy Nx1,
  liczbowych napisów ani brakującej jednostki/operacji. Uszkodzone dane mogły
  trafić do wykresu mimo ostrzejszego kontraktu zapisu.
- Odczyt wymaga rzeczywistego wektora liczbowego o długości osi RAW, skończonych
  wartości oraz jawnej jednostki i operacji. Walidacja poprzedza decymację,
  aby redukcja podglądu nie ukrywała uszkodzonych próbek. Błędy są zgłaszane
  jako ExecutionError. Nie zmieniono schematu ani danych istniejących plików.
- Testy obejmują 10 wariantów uszkodzeń (w tym grupę zamiast datasetu) i poprawną
  podpisaną resztę w W z ujemnymi wartościami. Z regresjami writera i osi RAW:
  **55 różnych testów zaliczonych** (54 w pierwszym przebiegu i dodany pozytywny
  przypadek w osobnym przebiegu modułu 11 testów). Ruff zaliczony.

## Sto czwarta seria — wspólna decymacja RAW i wyniku przetwarzania

- Dalsza lektura spectrum wykazała wybór indeksów wyłącznie na podstawie RAW.
  Następnie tymi samymi indeksami redukowano processed_values. Wąski dodatni
  lub ujemny pik reszty po korekcji mógł przez to zniknąć z podglądu.
- Dla dwóch krzywych rozdzielono budżet próbek wewnętrznych, zachowując wspólne
  końce, i połączono indeksy ekstremów każdej krzywej. Wielkości w W i dBm
  nie są porównywane ze sobą. Limit max_points pozostaje zachowany; wartości
  obu krzywych nadal odpowiadają dokładnie tym samym częstotliwościom.
- Regresja zawiera cztery rozdzielone ekstrema RAW i podpisanej reszty W,
  sprawdza zachowanie ich przy budżecie od sześciu punktów, parowanie danych
  oraz limity 1/2/3/6/7/32/33. Bardzo mały budżet nie może zachować wszystkich
  ekstremów; jest to redukcja podglądu, bez zmiany pełnych danych archiwum.
- **39 passed** (czytniki i przetwarzanie), Ruff zaliczony.

## Sto piąta seria — anulowane tło nie staje się ukończonym pomiarem

- Przeczytano cały BackgroundCorrectionAssistant i ścieżki start/result/frame/
  reference/stop workspacu korekcji. Po Cancel warunek „stopping and enough”
  nadal wywoływał finish_reference, a callback reference zamykał sesję jako
  completed. Sygnał do asystenta oznaczał niepowodzenie, ale status archiwum
  mógł sugerować poprawne ukończenie, a profil był podmieniany.
- Po anulowaniu kończą się zapis aktualnej ramki RAW i sesja ze statusem
  aborted; nowa finalizacja profilu nie jest zlecana. Jeżeli była już w toku,
  jej późna odpowiedź nie aktywuje profilu i zamyka archiwum jako aborted.
  UI pokazuje Recording stopped. Poprzedni profil pozostaje bez zmian.
- Testy reprodukują anulowanie po minimalnej liczbie ramek i spóźnioną
  finalizację. Testy integracyjne obejmują anulowanie podczas przygotowania,
  kolekcji i importu oraz normalne ukończenie i pokazane modale.
- Pełny moduł początkowo kończył proces bez tracebacku między przypadkami.
  Fixture usuwał stronę mimo odrzuconego close podczas zatrzymywania workera.
  Teraz utrzymuje QApplication przez sesję, czeka na przyjęcie close i opróżnia
  DeferredDelete. Następny przebieg zakończył wszystkie przypadki: 18 passed,
  jeden błąd niepełnego test-double uprawnień. Uzupełniono mock odświeżania
  zdrowia audytu; ten test osobno przeszedł. **19 różnych testów zaliczonych**,
  Ruff zaliczony. Nie zmieniano reguł uprawnień ani sterowania źródłami.

## Sto szósta seria — błędna sesja czeka na potwierdzenie zamknięcia

- Sprawdzono _failed, _processed i kolejkę CorrectionController. Błąd zwalniał
  running/busy natychmiast, mimo asynchronicznego stop_session. Późne frame,
  reference lub start mogły nadpisać UI/profil albo zlecić następny odczyt.
- Sesja po błędzie pozostaje zajęta i zatrzymywana do potwierdzenia stop.
  Późne wyniki akwizycji/przetwarzania są odrzucane; obsługa stop nadal kończy
  blokadę. Błąd samego stop nie zleca kolejnego stop, co usuwa możliwość
  nieograniczonej pętli ponawiania nieudanego zamknięcia.
- Sześć testów obejmuje typy spóźnionych wyników. Test workspacu sprawdza
  blokadę nowej referencji do potwierdzenia zamknięcia, brak aktywacji późnej
  referencji i brak ponawiania błędu close. Z pełnym modułem asystenta:
  **26 passed**, Ruff zaliczony. Wyłącznie symulacja i wstrzyknięte błędy.

## Sto siódma seria — ograniczony odczyt zatwierdzonego prefiksu

- Prześledzono spectrum, spectrum_point_count i wspólną kontrolę commit.
  Otwarcie widma o indeksie 0 zbierało/sortowało nazwy całej grupy points
  i odczytywało flagi complete wszystkich punktów. Każdy dodatkowy odczyt
  metadata widma powtarzał pełny skan.
- Prefiks jest teraz odczytywany po kolejnych kanonicznych kluczach 0,1,2…,
  bez listowania i sortowania grupy. Dla pojedynczego widma kończy się na
  żądanym indeksie. Pełne listy punktów nadal sprawdzają cały ciągły prefiks.
  Brak punktu lub complete=False przerywa akceptację; nie zastosowano cache,
  który mógłby przeżyć zmianę pliku.
- Test instrumentuje dostęp do grupy: przy indeksie 2 dozwolone są tylko
  odczyty 0/1/2, a enumeracja lub późniejsze odczyty powodują błąd. Warianty
  luk oraz istniejące testy realnego HDF5 potwierdzają granicę commit.
  **36 passed**, Ruff zaliczony.
- To nie zamyka całej wydajności Results: przeglądanie wielu późnych indeksów
  osobnymi wywołaniami nadal powtarza kontrolę prefiksu; materializacja dużych
  serii i drzewa wymaga dalszej pracy.

## Sto ósma seria — wybór widma w Results bez odczytów HDF5 w GUI

- Przeczytano selekcję punktu, publicznego widma, referencji i wariantu oraz
  istniejący ResultReadTask. Próg 100000 próbek pozostawiał typowe widma
  analizatora w GUI; nawet większe wykonywały tam wstępny spectrum_point_count.
  Dodatkowo odczyt referencji następował w samym budowaniu listy wariantów.
- Każdy odczyt widma prywatnego/publicznego i referencji korzysta teraz z
  istniejącej puli oraz identyfikatorów żądań odrzucających stare wyniki.
  Usunięto dodatkowy synchroniczny odczyt liczby próbek. Wariant referencji
  wynika z jawnego reference_index; brak/uszkodzenie referencji jest zgłaszane
  przy jej asynchronicznym otwarciu, zamiast ukrywane przez catch-all.
- Zachowano żądany wariant przy nawigacji z drzewa. Zmiana filtrów podczas
  ładowania ponownie zleca anulowany odczyt, a gotowe dane są przetwarzane
  aktualnymi ustawieniami. Pierwsza regresja ujawniła brak tego wznowienia;
  naprawiono kod, bez osłabienia testu interakcji.
- Test pokazanej strony blokuje odczyt w workerze i sprawdza pracę timera GUI
  dla widma z trzema próbkami. Zabrania wstępnego spectrum_point_count oraz
  czytania referencji podczas budowania wariantów. Regresje DSP i pokazanych
  kontrolek 1D/heatmapy: **16 passed** (oba motywy, również szerokość 760).
  Ruff zaliczony. Ładowanie całych list punktów/drzew jest odrębnym otwartym
  zagadnieniem; ta zmiana dotyczy wyboru i wyświetlania pojedynczego widma.

## Sto dziewiąta seria — spójność asynchronicznego wyboru w Results

- Ponownie prześledzono load, show_reference, show_stored_spectrum oraz callback
  wyboru wariantu. Po zmianie na async przejście z oczekującej referencji do RAW
  nie unieważniało jej żądania. Późny błąd referencji mógł zastąpić poprawny
  RAW ekranem błędu. Zmiana wariantu unieważnia teraz oczekującą referencję.
- Otwarcie osobnej referencji od razu usuwa poprzedni wybór prywatny/publiczny
  i stan urządzeń. Powrót do tego samego wiersza punktu jawnie uruchamia odczyt,
  jeśli Qt nie emituje currentItemChanged (wiersz nadal był zaznaczony).
- load najpierw czyści stan i unieważnia żądania poprzedniego pliku, zanim
  zmiana list referencji może wywołać sygnały formularza przetwarzania.
- Test blokuje worker referencji, przełącza na RAW i dopiero wtedy zwalnia
  spóźniony błąd; wykres pozostaje RAW. Drugi test wraca do tego samego punktu
  po widoku referencji. Z regresjami renderowania i DSP: **18 passed**,
  Ruff zaliczony.

## Sto dziesiąta seria — katalog referencji bez materializacji widm

- Przeczytano _read_result_payload, katalog referencji, inspektor drzewa,
  kontrolki korekcji i automatyczny dobór referencji w ResultSpectrumProcessor.
  Samo otwarcie Results oraz szukanie unikalnego tła czytało pełne tablice
  każdej referencji. Ich koszt pamięci rósł z sumą wszystkich próbek.
- Dodano jawny StoredReferenceSummary i odczyt metadata_only. Obejmuje
  metadane, granicę commit, kształt/dtype osi i liczbę punktów; nie udaje
  pełnego widma i nie zawiera pustych zastępczych tablic. Results i dobór
  referencji korzystają z katalogu; wybrana referencja jest nadal odczytywana
  pełnym accessor i walidowana przed wykresem/przetwarzaniem.
- Inspektor drzewa obsługuje oba typy i pokazuje prawdziwą liczbę punktów.
  Walidacja wartości i rosnącej osi pozostaje częścią pełnego odczytu, a nie
  deklaracją wynikającą z samego katalogu metadanych.
- Test zabrania odczytu próbek HDF5 podczas budowy katalogu trzech referencji
  po 10001 punktów. Wybrana poprawna referencja jest czytana w całości,
  a zawierająca NaN odrzucana przy otwarciu. Regresje Results/DSP/czytników:
  **40 passed**, Ruff zaliczony. Materializacja wszystkich scalar points i
  rozbudowanych drzew Results pozostaje odrębnym otwartym ustaleniem.

## Sto jedenasta seria — szczegóły checkpointów tworzone przy rozwinięciu

- Przeczytano budowę drzewa i płaskiej listy punktów. Drzewo eager tworzyło
  dla każdego punktu również wszystkie nastawy, pomiary i pozycje widm Qt.
  Przy wielu kanałach liczba elementów GUI rosła wielokrotnie szybciej niż
  liczba checkpointów, również dla całkowicie zwiniętych gałęzi.
- Archiwa powyżej 100 punktów mają grupy po 100. Zawartość grupy powstaje raz,
  po rozwinięciu, z zachowaniem pełnych rekordów i działań widm. Czyszczenie
  drzewa usuwa oczekujące grupy. Mniejsze archiwa zachowują dotychczasowy układ.
  Poszerzono początkową kolumnę nazw, by zakresy grup były czytelne.
- Test pokazuje drzewo 1001 punktów po 40 wartości: początkowo 11 pustych grup;
  rozwinięcie ostatniej daje checkpoint 1000, jego komplet danych i akcję RAW.
  Ponowne rozwinięcie nie duplikuje dzieci. Artefakt obejrzany:
  [lazy-checkpoints.png](lazy-checkpoints.png). Pierwszy test renderowania
  poprawiono, aby wybierał rzeczywistą zakładkę danych zamiast ukrytego drzewa.
- **16 różnych testów zaliczonych** (15 regresji Results/DSP i test nowego
  drzewa po korekcie fixture), Ruff zaliczony. Płaska lista punktów i pełny
  odczyt scalar metadata nadal wymagają dalszej optymalizacji; nie deklaruje
  się zamknięcia całego zagadnienia dużych archiwów.

## Sto dwunasta seria — identity recovery przed połączeniem sprzętu

- Prześledzono _resume_run, _start_resume_from_ready_dialog, RunWorker i resume
  writera. GUI sprawdza ustawienia przed dialogiem, ale worker sprawdzał hash
  ponownie dopiero w Hdf5RunWriter.resume, po connect urządzeń. Zmiana ustawień
  po wstępnej kontroli mogła więc prowadzić do komend połączenia przed odmową.
- Wspólna read-only verify_resume_identity sprawdza plan, recepturę, ustawienia
  i status completed. Worker uruchamia ją na pliku otwartym r przed pętlą
  connect, a writer nadal wykonuje ją ponownie przed mutującym resume.
  Ten sam snapshot ustawień jest używany przez oba etapy. Nie osłabiono
  sprawdzeń granicy recovery ani nie przeniesiono uprawnień do workera.
- Testy zmieniają każdy hash i status na completed: w każdym przypadku
  connect nie jest wywołany, worker zgłasza błąd, a bajty HDF5 pozostają
  identyczne. Z istniejącymi testami recovery: **7 passed**, Ruff zaliczony.
- Synchroniczny odczyt i kompilacja w początkowym _resume_run GUI nadal są
  otwartym ustaleniem wydajności. Ta seria zamyka wcześniejszą granicę
  kontroli tożsamości, nie całą ścieżkę asynchronicznego recovery.

## Sto trzynasta seria — przygotowanie recovery poza GUI

- Wydzielono read-only prepare_resume: detail HDF5, serializacja snapshotu,
  porównanie ustawień, parsing YAML, kompilacja i inspect punktu recovery.
  MainWindow zleca całość przez ResumePreparation/ResultReadTask do jednego
  workera, przekazując głęboką kopię ustawień; żaden widget nie jest tam używany.
- Nowszy wybór archiwum anuluje oczekujące stare prace i odrzuca stare wyniki.
  Po powrocie GUI sprawdza brak aktywnego runu, niezmienione ustawienia/tryb,
  uprawnienia i zdrowie audytu, dopiero potem pokazuje dotychczasowy dialog
  potwierdzenia i readiness. Kontrola identity przed connect pozostaje w workerze
  wykonania, więc późniejsza zmiana ustawień też nie omija bramki.
- Zamknięcie aplikacji anuluje publikację wyniku, nie czeka blokująco na I/O
  i nie niszczy puli w trakcie odczytu: odrzuca close do zakończenia pracy.
  Jeśli użytkownik pozostaje w aplikacji, po zakończeniu można ponownie zlecić
  przygotowanie. Żadne urządzenia nie są używane przez prepare_resume.
- Test workera blokuje odczyt i potwierdza działający timer GUI, kopię ustawień,
  wybór najnowszego z trzech zleceń oraz szybkie close bez późnej publikacji.
  Test rzeczywistego HDF5 sprawdza compile/inspect, odrzucenie zmiany ustawień
  i identyczne bajty pliku. Z testami readiness, pre-connect i recovery:
  **15 różnych testów zaliczonych** (14 w pierwszym zestawie, dodatkowy real-file
  w osobnym przebiegu modułu 3 testów). Ruff zaliczony.

## Sto czternasta seria — pojedynczy raport charakteryzacji poza GUI

- Prześledzono zakończenie pomiaru, automatyczny eksport, odtworzenie polityki,
  publikację PDF i zamykanie karty. Generowanie pojedynczego PDF odbywało się
  w handlerze GUI; istniejący worker raportów serii pól nie obejmował tej ścieżki.
- SingleReportWorker renderuje PDF poza GUI po zakończeniu odtwarzania polityki.
  Otrzymuje kopię datasetu i parametrów, a publikacja wyniku i aktualizacja
  widgetów pozostają w GUI. Generator nadal używa wspólnego REPORT_RENDER_LOCK.
  Start i zamknięcie karty są zablokowane do obsłużenia zakończenia workera.
  Błąd przygotowania, renderowania lub brak artefaktu nie jest ogłaszany sukcesem.
- Testy blokują renderer i sprawdzają inny wątek, działający timer, niezmienny
  snapshot, odrzucenie close oraz przywrócenie dostępności po sukcesie i błędzie.
  Pokazano rzeczywistą kartę w rozmiarze 1366×768 i sprawdzono jej geometrię.
  To nie jest przegląd wyglądu wszystkich modali w natywnym Windows.
- Pierwszy przebieg: 18 passed, 2 failed. Oba niepowodzenia były starszymi
  oczekiwaniami: fixture ma source_autorange=False, a komentarz raportu nie
  stwierdza już, że compliance dowodzi braku uszkodzeń. Po porównaniu z kodem
  poprawiono oczekiwania. Ponowny przebieg tych samych trzech modułów:
  **20 passed**, Ruff dla zmienionych plików zaliczony.
- Nadal otwarte: analiza datasetu, CSV, hash oraz operacje katalogu pozostają
  synchroniczne na tej karcie. Ta zmiana nie kwalifikuje całej charakteryzacji
  ani całego interfejsu jako wolnego od blokowania.
- Ponownie przeczytano również pełne moduły policy, recovery, baseline_authoring,
  repository, sweep_points, spectrum_processing, resume_preparation, Results
  workers, reference_transaction i event_log. Bieżące hashe znajdują się w
  current-read-114.json. Historyczny coverage.json pozostaje niezmieniony.

## Sto piętnasta seria — analiza, CSV i hash poza GUI charakteryzacji

- Prześledzono _on_sweep_finished → zapis → _begin_policy_restore oraz
  zakończenie odtwarzania polityki i tworzenia PDF. Dotychczas analiza, oba
  eksporty CSV i read_bytes do hashowania blokowały GUI przed zleceniem restore.
  Wyjątek analizy mógł również przerwać tę ścieżkę przed zapisem surowych danych.
- SingleArtifactsWorker otrzymuje kopię datasetu i zapamiętany cel próbki.
  Tworzy osobny katalog runu, zapisuje surowy CSV, oblicza SHA256 strumieniowo
  w blokach 1 MiB, eksportuje Rigol equivalence i analizuje dane. Błędy etapów
  są niezależne: awaria analizy nie usuwa CSV; awaria CSV nie jest sukcesem zapisu.
- GUI zleca odtworzenie polityki w finally, bez oczekiwania na pliki. Obsługuje
  obie kolejności: restore przed artefaktami i artefakty przed restore. PDF
  nadal czeka na fazę idle. Start i zamknięcie nie niszczą aktywnych workerów.
  Publikacja zachowuje cel próbki z początku runu, mimo czyszczenia stanu GUI.
- Wartości analizy z poprzedniego pomiaru są czyszczone: błąd nowej analizy
  pozostawia brak wyniku, a nie stare R₀/G₀. Potwierdzenie OFF i przebieg
  sprzętowego pomiaru nie zostały zastąpione wnioskowaniem ze stanu workera plików.
- Testy blokują analizę, sprawdzają timer GUI, wątki, natychmiastowe zlecenie
  restore, obie kolejności zakończenia i zachowanie raw/hash przy błędach.
  Pierwszy zestaw: **25 passed**. Integracja zapisu do inventory i częściowego
  zakończenia: **2 passed**; cała karta: **34 passed**. Początkowo w aktualizacji
  fixture użyto niewłaściwej nazwy timeout_s zamiast timeout; poprawiono przed
  tymi udanymi przebiegami. Fixture czeka na zakończenie workerów przed deleteLater.
- Po dodaniu czyszczenia starych wyników ponowiono testy workerów, błędów
  analizy i cyklu życia karty: **18 passed**. Ruff zmienionych plików zaliczony.
- Nadal otwarte: wyznaczenie katalogu przez inventory i jego aktualizacja
  pozostają w GUI; także synchroniczne zapytania przy Start i Retry restoration.
  Nie uznaje się całej karty ani wszystkich ustaleń raportu za zamknięte.

## Sto szesnasta seria — odczyty przed Start i Retry poza GUI

- Przeczytano ścieżki Start/Retry karty, providery zwykłej strony Keithley,
  proxy connected, confirm_output_off adaptera oraz ponowną kontrolę stanu
  w runnerze charakteryzacji. Start wykonywał read_configuration i odczyt
  polityki synchronicznie; Retry wykonywał confirm_output_off synchronicznie.
- OutputProofWorker korzysta z istniejącego proxy/kolejki urządzenia w osobnym
  wątku. Start wymaga jawnego output_enabled=False i znanej polityki.
  Retry wymaga pozytywnego potwierdzenia confirm_output_off; False ani brak
  wyniku nie są sukcesem. Zachowano zachowanie tego polecenia: przy ON lub
  niepewnym odczycie adapter może wykonać emergency OFF. Worker nie włącza wyjść.
- Stop i close ustawiają anulowanie, a GUI ignoruje opóźniony wynik.
  Karta pozostaje żywa do zakończenia workera. Po odczycie sprawdzane są
  niezmienione ustawienia i konfiguracja; Retry sprawdza także kanał i fazę
  odtwarzania. Decyzje/dialog oraz publikacja stanu pozostają w GUI.
- Testy opóźniają readback i potwierdzają działający timer, pracę poza GUI,
  brak kontynuacji po Stop/close/zmianie konfiguracji lub ustawień, odmowę przy
  OUTPUT ON i timeout, a także odmowę restore przy confirm_output_off=False.
  Pierwszy zestaw nowych regresji i workerów artefaktów/PDF: **18 passed**.
- Pełny moduł UI charakteryzacji wraz z nowymi testami preflight:
  **43 passed**, proces zakończony kodem 0. Ruff zmienionych plików zaliczony.
  Testy używają kontrolowanych proxy i nie wykonują I/O do fizycznych urządzeń.
- Nadal otwarte są analogiczne odczyty przy starcie odrębnej serii pola,
  operacje inventory oraz szersza kwalifikacja fizycznych urządzeń. Nie należy
  rozciągać tej naprawy pojedynczej charakteryzacji na te niezbadane ścieżki.

## Sto siedemnasta seria — jawny dowód OFF w charakteryzacji i serii A/B

- Przeczytano przygotowanie, finally i obsługę błędów runnera pojedynczego
  pomiaru oraz przejścia OFF/recovery w field_series i field_worker. Wywołania
  confirm_output_off ignorowały zwrócony wynik; brak wyjątku mógł ustawić
  output_off_confirmed=True lub pozwolić na przywrócenie polityki mimo False.
- Wspólny require_output_off wymaga dokładnie True od confirm_output_off.
  Zachowano kontrakt starszego assert_output_state (sukces bez wartości),
  ale jawny False jest odrzucany. Nie pomylono dwóch różnych znaczeń:
  set_output(channel, False) zwraca stan False po prawidłowym wyłączeniu,
  natomiast confirm_output_off zwraca True jako dowód potwierdzenia.
- Kontrola obejmuje przygotowanie źródła, zakończenie pojedynczego pomiaru,
  diagnostykę błędu workera, recovery compliance, przejścia serii A/B,
  przywracanie obu polityk oraz preflight/retry GUI. Finalny runner wymaga
  niezależnego potwierdzenia OFF przed zwrotem zakończonego datasetu.
  Błąd jednego kanału nie pomija próby wyłączenia drugiego.
- Testy wstrzykują False/None/0/1/tekst zamiast dowodu, odrzucają start przed
  konfiguracją, blokują fałszywy sukces zakończenia i przywracanie polityk,
  a wynik błędnego field worker ma outputs_off=False. Fake adaptery zwracają
  teraz True po rzeczywistym sprawdzeniu swojego stanu, zgodnie z adapterem.
- Pierwszy przebieg: 56 passed, 3 failed — starsze testy oczekiwały OFF jako
  ostatniego wywołania. Zmieniono je na dokładną parę OFF → potwierdzenie OFF.
  Końcowy zestaw runnera, serii, workera serii, preflight GUI i fault injection:
  **84 passed**, Ruff zaliczony. Bez komunikacji z fizyczną aparaturą.

## Sto osiemnasta seria — preflight serii pola A/B poza GUI

- Przeczytano start serii i etap zatwierdzania scenariusza. Odczyty obu polityk
  i confirm_output_off A/B były nadal synchroniczne w _start_field_series.
  Rozszerzono istniejący OutputProofWorker o tę ścieżkę; nie utworzono drugiej
  kolejki sterującej ani połączenia ze sprzętem.
- Parametry przechodzą walidację przed odczytami. Worker wymaga znanych polityk
  i pozytywnego dowodu OFF obu kanałów, a GUI ponownie porównuje konfigurację
  i ustawienia. Dopiero potem tworzony jest przegląd scenariusza. Lease,
  zmiana polityki i wykonanie serii nadal wymagają akceptacji tego przeglądu.
- Stop/close podczas odczytu odrzuca kontynuację i kończy pracę przed kolejnym
  kanałem, gdy anulowanie zostało już odebrane. GUI pozostaje responsywne,
  a aktywna seria nie odzyskuje omyłkowo dostępnego przycisku Start po callbacku.
- **25 passed**: nowe przypadki opóźnionego odczytu, obu potwierdzeń, błędu B,
  anulowania i zmiany konfiguracji; istniejący preflight pojedynczy; dialog
  anulowany bez mutacji oraz zatwierdzona seria z identycznym przejrzanym
  snapshotem, rzeczywistymi plikami, raportami i wpisami inventory.
  Ruff zmienionych plików zaliczony. Testy wykonano bez fizycznej aparatury.
- Operacje inventory i koszt budowania/wyświetlania dużych scenariuszy nie
  zostały tą zmianą przeniesione poza GUI; pozostają osobnymi zadaniami audytu.

## Sto dziewiętnasta seria — Stop przed OUTPUT ON charakteryzacji

- Lektura run_sweep ujawniła, że pierwsze sprawdzenie cancel_event następowało
  dopiero w pętli po set_output(True). Wcześniejszy Stop mógł więc dopuścić
  konfigurację i chwilowe włączenie źródła. Dodatkowo początkowe configure_source
  znajdowało się poza blokiem finally kończącym pomiar.
- Runner sprawdza anulowanie przed recovery, między dowodem OFF a recovery,
  przed konfiguracją, przed ON i po odczycie pola przed zmianą nastawy.
  Konfiguracja jest objęta finally. Rozdzielono próbę konfiguracji i próbę ON:
  przed ON nie wykonuje się rampowania do zera; po próbie konfiguracji wysyła
  się OFF, a potwierdzenie OFF pozostaje obowiązkowe. Anulowanie zwraca jawnie
  cancelled z pustymi punktami, jeżeli nie zarejestrowano jeszcze pomiaru.
- Testy ustawiają Stop przed startem oraz podczas dowodu OFF, recovery i
  configure. Żaden z tych przypadków nie wywołuje ON ani ramp_to_zero;
  wcześniejsze etapy nie przechodzą do konfiguracji. Błąd configure powoduje
  OFF i potwierdzenie, zachowując informację o pierwotnym błędzie.
- Runner, seria A/B, worker serii oraz testy potwierdzeń i anulowania:
  **80 passed**, Ruff zaliczony. Nie kwalifikowano czasów fizycznego transportu
  ani możliwości wycofania komendy, która została już wysłana do urządzenia.
- Dalsza lektura tego samego runnera wskazała dwie otwarte kwestie:
  catch TypeError przy update_source_level ponawia mutację inną sygnaturą;
  true_resistance_ohm używa żądanego prądu jako fallback przy zerowym odczycie.
  Wymagają oddzielnej reprodukcji i korekty, nie są zamknięte tym zestawem testów.

## Sto dwudziesta seria — bez ponawiania mutacji i zastępowania odczytu nastawą

- Potwierdzono w runnerze catch TypeError obejmujący całe update_source_level.
  Adapter i proxy mają ustalony kontrakt argumentów nazwanych. Usunięto
  ponowienie pozycyjne: TypeError po wykonanej mutacji jest błędem runu i
  prowadzi do finally, bez ponownego wysłania tej samej nastawy.
- Test w trybie prądowym i napięciowym symuluje przyjęcie nastawy, a potem
  TypeError. Każdy przypadek ma dokładnie jedno wywołanie nastawy i kończy
  się OFF oraz jego potwierdzeniem. Z regresjami runnera i serii: **40 passed**.
- Prześledzono R od runnera przez eksport CSV do czytnika wyników. Przy
  |I_measured| ≤ 1 pA dotychczasowy fallback używał I_demanded w polu
  true_resistance_ohm. Usunięto to podstawienie. Zachowano istniejący próg
  obliczeń; wynik nieokreślony ma NaN, natomiast apparent resistance nadal
  jawnie opisuje stosunek do nastawy. Surowy odczyt prądu pozostaje niezmieniony.
- Testy dla 0 i ±0.1 pA sprawdzają brak pozornej rezystancji, zachowanie
  apparent resistance oraz pełny CSV → czytnik z trzema lukami zamiast
  usunięcia punktów. Z regresjami raportów, runnera i wykresów:
  **42 passed**, Ruff zmienionych plików zaliczony. Nie zmieniano starszych
  archiwów ani nie kwalifikowano fizycznej dokładności w zakresie pA.

## Sto dwudziesta pierwsza seria — model widoku checkpointów Results

- Przeczytano tworzenie listy, wybór z drzewa, zmianę wariantu, filtrowanie
  i nawigację. Lista tworzyła QTreeWidgetItem dla każdego punktu i od razu
  serializowała pełne metadane do tooltipu, także dla niewidocznych wierszy.
- Zastąpiono listę Fluent TreeView z QAbstractTableModel. Model przechowuje
  istniejącą kolekcję rekordów; tekst, ikona i tooltip powstają na żądanie
  widoku. Nie ma limitu obcinającego punkty ani zastępczej fasady starego API.
  Wybór, nawigacja, odwołania drzewa i testy używają QModelIndex. Zmiana kolumny
  w tym samym wierszu nie uruchamia ponownego odczytu spektrum.
- Wyszukiwanie wskazanego checkpointu iteruje rekordy Python, bez tworzenia
  indeksów/elementów Qt dla wszystkich poprzedzających go wierszy.
- Test 10000 rekordów ze 100 polami metadata potwierdza współdzielenie
  kolekcji, formatowanie mniej niż 200 różnych wierszy po show, brak eager
  tooltipów, dostęp do ostatniego rekordu, Previous, filtr i clear.
  Pokazany i obejrzany widok 1440×900: [checkpoint-model.png](checkpoint-model.png).
- Starsze fixture korzystały ze starego API lub oczekiwały synchronicznego
  odczytu spektrum. Zaktualizowano je do modelu i oczekiwania na wynik.
  Testy shell otrzymały istniejącą izolację inventory/settings, zamiast używać
  katalogu użytkownika. Skorygowano oczekiwaną liczbę bloków QTextDocument:
  tekst z końcowym newline ma także końcowy pusty blok (9, nie 8).
- Szerszy pierwszy przebieg: 35 passed, 5 failed, 2 skipped. Po korektach:
  ResultsPage/Browser **21 passed, 2 skipped** (brak licencjonowanego golden
  HDF5); końcowa regresja modelu, asynchronicznych odczytów, DSP i Browser
  **29 passed**. Ruff zmienionych plików zaliczony.
- Nadal otwarte: pełna materializacja metadanych rekordów i publicznych
  checkpointów oraz budowa list filtrów. Ta seria usuwa eager elementy Qt
  i tooltipy, nie dowodzi ograniczonej pamięci całego odczytu dużego archiwum.

## Sto dwudziesta druga seria — publiczny indeks checkpointów poza GUI

- Prześledzono źródła ResultsPage, SpectrumResultsTab i ResultReadTask:
  po zakończeniu odczytu pliku publiczne scalar_series i budowanie rekordów
  checkpointów nadal wykonywały się w wątku GUI. Model widoku z poprzedniej
  serii nie usuwał tego kosztu.
- Wydzielono build_public_checkpoints do operacji workera. Funkcja nie
  odwołuje się do widgetów. Anulowanie sprawdzane jest przed odczytem
  kolejnego skalara i co 256 budowanych rekordów; trwającego natywnego
  odczytu HDF5 nie można w ten sposób przerwać.
- Generacja żądania odrzuca wynik poprzedniego pliku. Zmiana przetwarzania
  podczas indeksowania nie anuluje indeksu potrzebnego do pierwszego widma;
  nowe ustawienia zostają użyte przy jego wyświetleniu.
- Regresje sprawdzają rzeczywisty odczyt publicznego HDF5 poza GUI,
  działający timer Qt podczas zablokowanego odczytu, zmianę przetwarzania,
  pokazany widok, późny wynik starego pliku oraz anulowanie przed I/O.
  Końcowy przebieg wraz z Results async, postprocessing i Browser:
  **31 passed in 22.79 s**. Ruff zaliczony.
- Otwarte pozostają pełna materializacja rekordów i budowa filtrów.
  ResultsPage nadal wybiera synchroniczny początkowy odczyt dla plików
  mniejszych niż 4 MiB; rozmiar pliku nie gwarantuje krótkiego czasu I/O.
  Ta seria nie stanowi kwalifikacji całego GUI ani pomiarów fizycznych.

## Sto dwudziesta trzecia seria — początkowy odczyt Results zawsze w workerze

- Lektura _on_file_selected, _read_result_payload i obsługi generacji
  wykazała synchroniczny odczyt dla plików mniejszych niż 4 MiB lub przy
  błędzie stat. Obejmował on opis HDF5, drzewo, punkty, referencje i most
  PyThat. Rozmiar pliku nie ogranicza czasu tych operacji.
- Usunięto próg oraz synchroniczną gałąź. Każdy wybrany plik HDF5 korzysta
  z ResultReadTask, zachowując istniejące odrzucanie starych wyników/błędów,
  stan ładowania i blokadę Resume do przyjęcia danych.
- Nowa regresja celowo blokuje czytnik bardzo małego pliku, sprawdza
  wykonanie poza GUI, postęp timera Qt i widoczny stan ładowania po show.
  Następnie usuwa wybór i potwierdza, że późny błąd nie zmienia widoku.
  Testy przeglądarki czekają teraz na wynik workera zamiast zakładać
  ukończenie odczytu po pojedynczym processEvents.
- ResultsPage, Browser, asynchroniczne odczyty i sweep spectrum UI:
  **37 passed, 2 skipped** (brak licencjonowanego golden HDF5).
  Dodatkowa regresja postprocessingu, zewnętrznego pliku i async:
  **20 passed**. Ruff zaliczony. Jeden pośredni przebieg nie zebrał testów
  wskutek błędnej nazwy selektora; poprawiony przebieg jest podany powyżej.
- Dalsza lektura _populate_parameter_sets i _populate_parameter_values
  potwierdza porównywanie każdego nowego kandydata ze wszystkimi wcześniejszymi
  (koszt kwadratowy przy unikatowych wartościach) w GUI. Pełna materializacja
  metadanych oraz tworzenie filtrów nadal wymagają naprawy. Przeniesienie
  początkowego I/O nie zamyka tego oddzielnego kosztu.

## Sto dwudziesta czwarta seria — indeks kandydatów filtrów Results

- Zastąpiono sekwencyjne porównywanie każdego zestawu/wartości ze wszystkimi
  dotychczasowymi reprezentantami indeksem kandydatów. Indeks liczb używa
  przedziałów zależnych od wykładnika oraz sąsiednich przedziałów, a następnie
  dotychczasowego dokładnego predykatu isclose. Tolerancje mają wspólne stałe
  dla indeksu i końcowego porównania. Nie zaokrągla się danych pomiarowych.
- Przy wielu osiach wybierany jest najmniejszy zbiór kandydatów: stałe osie
  zewnętrzne nie wymuszają skanowania wszystkich wcześniejszych nastaw osi
  wewnętrznej. Zachowano kolejność i pierwszy reprezentant, również przy
  nieprzechodniości przybliżonej równości.
- Test różnicowy porównuje wynik z poprzednim algorytmem dla None, bool,
  tekstów, pustych kolekcji, NaN, nieskończoności, granic wykładnika,
  subnormalnych wartości, maksymalnego float oraz losowych bliskich nastaw.
  Wykrył brak dopasowania zera do subnormalnych wartości; poprawiono wybór
  przedziału dla zera. Dla 10000 unikatowych punktów z dwiema stałymi osiami
  i ich powtórzenia potrzebnych jest dokładnie 10000 pełnych porównań.
- Pierwsza regresja: 28 passed, 1 failed (opisany przypadek zera).
  Po naprawie indeks, model checkpointów, Browser i postprocessing:
  **29 passed**. Po ujednoliceniu stałych tolerancji: **3 passed**;
  Ruff zaliczony. Regresje GUI obejmują pokazane widoki i działanie filtrów.
- Pozostaje koszt tworzenia wszystkich pozycji comboboxów oraz pełnej
  materializacji metadanych. Indeks jest tymczasowy i zużywa pamięć
  proporcjonalną do liczby indeksowanych składników reprezentantów.
  Ta seria ogranicza porównania, nie dowodzi stałego czasu/pamięci całego GUI.

## Sto dwudziesta piąta seria — listy filtrów oparte na modelu

- Trzy potencjalnie duże selektory (parametr, wartość, zestaw parametrów)
  korzystają z QAbstractListModel i wirtualnego widoku. Rekordy są współdzielone
  z modelem, a tekst pozycji powstaje na żądanie. Nie ma limitu obcinającego
  dostępne wartości ani pojedynczego QAction/widgetu dla każdej pozycji.
- Użyto funkcjonalnego QComboBox wewnątrz strony z Fluent ListView.
  Shell pozostaje Fluent. Zmieniono wywołania strony na set_options zamiast
  fasady imitującej stare API. Rozmiar selektora nie wymaga mierzenia
  wszystkich etykiet; przy inicjalizacji wyświetlane są wartości „All”.
- Test ujawnił, że menu-style popup Qt nadal mierzy wszystkie 10000 pozycji.
  Lokalny QProxyStyle zmienia wyłącznie tę wskazówkę zachowania: popup
  przestrzega maxVisibleItems=15. Nie wprowadzono osobnych kolorów/QSS.
- Testy pokazują stronę 1440×900 i otwierają listę w jasnym i ciemnym motywie:
  model zachowuje komplet 10000 wartości, formatuje mniej niż 100 różnych
  pozycji przy otwarciu, a End/Enter wybiera ostatnią wartość. Obejrzano
  [jasny popup](filter-popup-light.png), [ciemny popup](filter-popup-dark.png)
  oraz widoki strony [light](filter-choices-light.png)/[dark](filter-choices-dark.png).
- Pierwsza regresja: 25 passed, 2 failed (pełne mierzenie popupu).
  Po korekcie: filtry, Browser, postprocessing i model checkpointów
  **28 passed**. Końcowe testy renderowania: **2 passed**, Ruff zaliczony.
- Nadal otwarte: grupowanie/sortowanie rekordów filtrów jest wykonywane
  w GUI, choć bez poprzedniego skanowania wszystkich par. Początkowy odczyt
  nadal materializuje pełne metadane. Wirtualny widok usuwa koszt elementów
  Qt i mierzenia całego popupu, nie rozwiązuje tych pozostałych kosztów.

## Sto dwudziesta szósta seria — grupowanie filtrów poza GUI

- Przeniesiono zbieranie kluczy, grupowanie zestawów oraz sortowanie do
  ResultReadTask. Wybór konkretnego parametru uruchamia analogiczny odczyt
  unikatowych wartości. Worker korzysta z funkcji klasowych i rekordów,
  bez dostępu do widgetów; GUI jedynie podmienia kolekcje modeli.
- Filtry mają osobną pulę z jednym wątkiem, generację i anulowanie.
  Anulowanie odczytu/przetwarzania spektrum nie usuwa pracy nad katalogiem
  filtrów. Zmiana pliku unieważnia wynik wcześniejszego katalogu. Wybór
  zestawu lub innego parametru unieważnia nieaktualną listę wartości.
  ResultsPage.shutdown anuluje i uwzględnia nową pulę w swoim deadline.
- Podczas obliczeń wyłączone są tylko selektory zależne od oczekiwanego
  wyniku. Błąd obliczeń jest wyświetlany w podsumowaniu filtrów. Anulowanie
  sprawdzane jest także przy skanowaniu rekordów bez wartości wybranego
  parametru, nie tylko przy znalezieniu kolejnego reprezentanta.
- Testy blokują obliczenia, sprawdzają wątek, timer Qt, geometrię po show,
  zmianę przetwarzania podczas obliczeń, wybór wartości i późny wynik starego
  katalogu. Browser czeka jawnie na gotowość filtrów. Regresja Browser,
  postprocessingu, odczytów publicznych i async: **34 passed**.
  Końcowy zestaw worker/indeks/renderowanie filtrów: **8 passed**;
  Ruff zaliczony.
- Otwarte pozostają pełna materializacja metadanych oraz liniowe wybieranie
  rekordów po zatwierdzeniu konkretnego filtra. Sortowania Python nie
  przerywa się w połowie; anulowanie zapobiega publikacji jego starego wyniku.

## Sto dwudziesta siódma seria — wybór checkpointów według filtra poza GUI

- Liniowy skan rekordów po wybraniu wartości lub zestawu parametrów
  przeniesiono do workera filtrów. Zachowano ten sam predykat wartości
  i podpisów zestawów oraz kolejność oryginalnych rekordów. Wynikiem jest
  kolekcja referencji do istniejących rekordów, bez kopiowania ich metadanych.
- GUI pokazuje „Filtering checkpoints...”. Kontrolki pozostają dostępne,
  żeby można było zmienić wybór lub usunąć filtr podczas obliczeń. Nowy wybór
  unieważnia wcześniejszą generację; Clear natychmiast przywraca pełną
  kolekcję bez skanu. Worker sprawdza anulowanie pomiędzy rekordami.
- Testy blokują poprzedni wybór i zwracają jego spóźniony wynik mimo
  anulowania. Sprawdzono zarówno przejście na inną wartość, jak i Clear;
  późny wynik nie nadpisuje widoku. Timer Qt działa w czasie oczekiwania.
- Regresja workera, Browser i postprocessingu: **28 passed**.
  Końcowe testy wyścigów, renderowania list i modelu checkpointów:
  **8 passed**, Ruff zaliczony.
- Pozostaje pełna materializacja metadanych podczas odczytu archiwum.
  Ta zmiana usuwa skan z GUI, nie ogranicza wielkości kolekcji w pamięci.

## Sto dwudziesta ósma seria — odrzucanie dużego runu przy imporcie referencji

- Lektura Hdf5RunReader.points i wszystkich jego wywołań ujawniła, że
  ReferenceHdf5Store.load najpierw materializował wszystkie checkpointy
  (łącznie z metadata/device_states JSON), a potem sprawdzał len(points)==1.
  Wybranie dużego runu jako referencji niepotrzebnie importowało pełne dane.
- Dodano single_point: sprawdza najwyżej dwa checkpointy zatwierdzonego
  ciągłego prefiksu, a dopiero po potwierdzeniu jednej pozycji odczytuje jej
  JSON. Import referencji korzysta z tej ścieżki. Wspólne _read_point
  zachowuje dotychczasowe pola i interpretację dla pełnego czytnika points.
- Testy plików z 0, 2 i 500 checkpointami zabraniają jakiegokolwiek odczytu
  JSON i sprawdzają ograniczenie prefiksu do dwóch pozycji. Test poprawnej
  pozycji sprawdza komplet metadata, device_states, setpoints i spectrum oraz
  nieuwzględnianie punktów za przerwą w commitach. Istniejący round trip
  referencji obejmuje też PyThat i proweniencję aparatury.
- Import referencji, bounded commit i spectrum commit prefix:
  **21 passed**, Ruff zaliczony.
- Odczyt całego katalogu punktów w Results nadal materializuje metadata
  i device_states. Wymaga to osobnej migracji odbiorców (inspektor drzewa,
  podgląd stanu urządzeń, tooltipy i przetwarzanie widm); tej pracy nie
  uznano za zakończoną przez zmianę importera referencji.

## Sto dwudziesta dziewiąta seria — zależne poziomy Rigola bez fałszywego MATCH

- Prześledzono _edit_rigol_module_node, _comparison_rows oraz kompilowanie
  poziomów w RigolSweepProvider. Kompilator przy samej amplitudzie zachowuje
  wcześniejszy offset receptury; przy samym offsecie zachowuje wcześniejszą
  amplitudę. Modal wyliczał oba poziomy z lokalnego formularza, więc mógł
  pokazać „Same” dla wartości, których kompilator faktycznie nie używa.
- Przy jednym aktywnym składniku dodano jawny wiersz żądanej amplitudy lub
  offsetu. High/Low pokazują wzór z wcześniejszym składnikiem receptury oraz
  stan Derived. Są liczone jako unknown i oznaczone kolorem ostrzegawczym,
  nie jako same/changed na podstawie wymyślonej wartości. Przy obu jawnie
  ustawianych składnikach oraz pełnym configure zachowano dotychczasowy diff.
- Wspólny ConfigurationComparison obsługuje Derived oddzielnie od Preserve:
  jest to zmieniany parametr zależny od planu, a nie parametr pomijany.
- Testy obejmują amplitude/offset × Set/Sweep, widoczną tabelę po show,
  brak fałszywego Same oraz przejście do dwóch jawnych składników.
  Regresja modali i generowania/kompilowania osi: **57 passed**.
  Końcowe renderowanie: **4 passed**, Ruff zaliczony. Obejrzano
  [rigol-coupled-review.png](rigol-coupled-review.png).
- Naprawiono fałszywą pewność porównania. Numeryczna projekcja całego
  wcześniejszego planu (w tym pętli i rozgałęzień) w modalu nadal nie istnieje;
  zależność jest teraz jawna. Nie kwalifikowano analogowych przejść sprzętu.

## Sto trzydziesta seria — wspólna regresja dotychczasowych napraw

- Uruchomiono razem wszystkie **104 pliki test_source_review_*.py**
  dostępne w aktualnym drzewie, z QT_QPA_PLATFORM=offscreen, bez fizycznej
  aparatury, cache pytest i z oddzielnym basetemp.
- Wynik pierwszego wspólnego przebiegu: **874 passed, 1 failed w 506.54 s**.
  To nie jest zielony pełny przebieg. Nie obejmuje wszystkich testów repo.
- Błąd: test_initialization_stop_survives_until_recipe_dispatch[connect].
  Przeczytano inicjalizację, rejestrację adapterów, _cleanup_devices i
  _cleanup_device. Nieograniczony Mock tworzył pozorną callable metodę
  operation_timeout, której wynik nie był context managerem; cleanup
  kończył się przed abortem. Nie był to dowód utraty Stop w produkcji.
- Zawężono spec atrapy do connected/capabilities/connect/abort/disconnect
  i ustawiono jawne True dla potwierdzenia abort. Dodano asercję braku
  cleanup incomplete. Nie zmieniono produkcyjnej semantyki deadline ani
  wymagań potwierdzania OFF/abort, aby dopasować je do atrapy.
- Po korekcie: cancellation, shutdown deadline, shutdown confirmation
  i initialization faults — **45 passed w 11.22 s**, Ruff zaliczony.
  Pełnych 104 plików nie uruchamiano ponownie po tej zmianie testu.
- Nadal wymagane są otwarte prace źródłowe, szersza integracja spoza tej
  grupy testów i kwalifikacja fizycznych urządzeń. Ten wynik nie stanowi
  deklaracji produkcyjnej gotowości całej stacji.

## Sto trzydziesta pierwsza seria — widma w czytniku serii inventory

- Ponownie prześledzono jednostki od persisted_quantity_unit przez
  Hdf5SeriesReader do calculate_mtj_metrics. W tej ścieżce nie potwierdzono
  ponownego nadawania Oe na podstawie samej nazwy H; brak jednostki pozostaje
  brakiem podstaw do obliczania parametrów MR.
- Wykryto błąd wyboru danych: istnienie /points powodowało bezwarunkowy
  wybór serii skalarnej, nawet gdy checkpoint zawierał wyłącznie widmo.
  Poprawiono wybór: przy braku kanałów skalarnych i braku jawnego preferred_y
  czytnik pokazuje zapisane spektrum. Jawny brakujący kanał nadal pozostawia
  luki w swojej jednostce, bez podstawiania innej wielkości.
- Pomocniczy czytnik widma używa teraz wspólnego _read_spectrum_axes:
  sprawdza jednowymiarowość, typ rzeczywisty, zgodne rozmiary, co najmniej
  dwa punkty, skończoność i ściśle rosnącą częstotliwość. Zachowano
  potwierdzenie przynależności do zatwierdzonego prefiksu checkpointów.
- Testy obejmują poprawny spectrum-only checkpoint, jawny brakujący kanał
  V oraz uszkodzenia: odwrócona/zdublowana oś, rank 2, complex, pojedynczy
  punkt i brak commitu. Czytniki, prywatne osie i analityka/jednostki:
  **60 passed**, Ruff zaliczony.
- Nie zmieniono zapisanych plików. Pełna materializacja metadanych Results
  i pozostałe otwarte pozycje raportu nie są zamknięte tą korektą.

## Sto trzydziesta druga seria — otwarcie historycznego sweepa z odczytanego snapshotu

- Prześledzono odbiorców StoredPoint.metadata/device_states: inspektor
  SweepTreePanel, widok stanu w SpectrumResultsTab, tooltip oraz
  ResultSpectrumProcessor (również dla heatmap). Nie można po prostu
  pominąć tych pól przy odczycie bez migracji ich odbiorców.
- W tym przepływie znaleziono _request_open_sweep, które ponownie wykonywało
  ThatecRunReader.tree w GUI. Drzewo było już odczytane w _ResultPayload,
  lecz po zastosowaniu payload nie zachowywano go do obsługi przycisku.
- Results zachowuje tę samą krotkę drzewa i przekazuje ją przy otwieraniu
  historycznego sweepa. Nie ma ponownego I/O, a starszy opis runu nie może
  zostać połączony z nowszą zawartością pliku. Refresh/zmiana/wyczyszczenie
  wyboru usuwa snapshot i blokuje otwarcie nieaktualnego drzewa.
- Test zabrania ponownego ThatecRunReader.tree po początkowym odczycie,
  sprawdza tożsamość przekazanego runu i drzewa oraz brak otwarcia po Clear.
  Z regresją ResultsPage: **12 passed, 2 skipped** (brak licencjonowanego
  golden HDF5), Ruff zaliczony.
- Pełna materializacja metadata/device_states pozostaje otwarta. Powyższa
  zmiana zamyka znaleziony synchroniczny odczyt, nie zastępuje migracji
  szczegółów checkpointów na odczyt przy wyborze.

## Sto trzydziesta trzecia seria — szczegóły checkpointów wczytywane przy wyborze

- Dodano jawne StoredPoint.details_loaded oraz points(include_details=False).
  Results używa lekkiego katalogu: setpoints, measurements, status/czas,
  obecność widma i dowody wymagane przez DSP (raw_recipe_sweep_indices,
  spectrum_processing_v1). Nie odczytuje device_states_json dla całej listy
  ani nie zatrzymuje pozostałych metadanych każdego checkpointu w pamięci.
- Dotychczasowe points() i single_point() zachowują pełny odczyt. Nowe
  point(path, index) odczytuje pełne szczegóły jednej pozycji dopiero po
  potwierdzeniu jej obecności w ciągłym zatwierdzonym prefiksie. Format
  zapisanych archiwów nie zmienił się.
- Wybór punktu w SpectrumResultsTab doczytuje szczegóły i widmo w workerze.
  Inspektor SweepTreePanel ma osobny worker z generacją żądań; zmiana
  wyboru/pliku unieważnia poprzedni wynik. Shutdown Results obejmuje jego
  pulę. Pełne rekordy nie zastępują wszystkich rekordów lekkiego katalogu.
- Tooltip katalogu jawnie wskazuje, że dodatkowe metadane wymagają wyboru
  w drzewie danych. Inspektor pokazuje stan ładowania zamiast sugerować,
  że pominięte snapshoty są pustymi metadanymi archiwum.
- Test zabrania odczytu device_states_json podczas tworzenia katalogu,
  sprawdza zachowanie dowodów DSP i odzyskanie pełnych metadanych 100000
  znaków po wyborze. Integracja Results potwierdza odczyt poza GUI,
  urządzenia w Spectrum i pełny inspektor, bez wczytywania szczegółów
  pozostałych punktów. Dodatkowo: commit gap i spóźniona odpowiedź inspektora.
- Browser/postprocessing/import/async: **33 passed**; szczegóły/lazy tree/
  commit prefix: **16 passed**; końcowe szczegóły/async/postprocessing:
  **23 passed**, Ruff zaliczony.
- Ograniczenie: katalog nadal przechowuje skalary i dowody DSP wszystkich
  punktów. Metadata JSON jest dekodowany po jednym rekordzie, aby wybrać
  dowody DSP; pozostała zawartość nie jest zachowywana. Nie jest to dowód
  stałej pamięci względem liczby checkpointów ani całkowitego braku I/O
  metadanych przy początkowym odczycie. Odczyt detalu sprawdza prefix do
  wybranego indeksu; nie wprowadzono niezweryfikowanego cache commitów.

## 134. Results: jawne Raw − reference i weryfikacja receptury analizatora

- Przeczytano recepturę oraz ścieżkę compiler → runner → zapis referencji/RAW
  → procesor Results → wspólne kontrolki Spectrum/Heatmaps.
- Dodano osobny wybór odejmowania referencji w W z zachowaniem ujemnych
  reszt; automatyczny baseline kwalifikuje purpose także przy istniejącym
  linku checkpointu. Nie zastępuje referencji tłem.
- Przełączanie rodzaju odejmowania zeruje jawny wybór baseline o innym celu.
  Pełne siatki, dowody konfiguracji, reset i synchronizacja widoków zachowane.
- Postprocessing **24 passed**; smoke/resources/shutdown **7 passed**; Ruff
  zaliczony. Obejrzano zachowane rendery light/dark, testowano też szerokość 760 px.
- Rzeczywisty odczyt wygenerowanego H5 symulacji: osobne background/reference,
  wszystkie 16 RAW, jeden nieprzetworzony checkpoint, metadane konfiguracji
  i źródeł; walidacja PyThat zaliczona.
- Dokładny opis i ograniczenia: [reference-subtraction-review.md](reference-subtraction-review.md).
  Plik użytkownika z dysku F: jest niedostępny; nie potwierdzono jego zawartości.

## 135. Przegląd bieżącego diffu Keithley i odtworzone błędy sterowania

- Przeczytano wszystkie diffy pakietu Keithley, safety oraz cztery nowe helpery;
  przejrzano powiązane ścieżki wspólnego sterowania. Zachowano diff i SHA256.
- Odtworzono i naprawiono brak readback OFF przed częściową konfiguracją,
  niewłaściwe uznawanie VERIFIED za OFF w dry run, utratę baseline NPLC/settling
  w kompilatorze oraz brak OFF po błędach aktualizacji compliance.
- Końcowe zestawy: sterowanie **114 passed**, kompilator **73 passed + 6 subtests**,
  dry run/Stop/shutdown **82 passed**, artefakty/PDF **9 passed**; Ruff zaliczony.
- Szerszy przebieg wcześniejszego stanu: **445 passed, 11 failed**. Pozostaje
  problem geometrii przy 1280×720 oraz aktualizacja i izolacja starych testów.
  Nie rozszerzano limitów aparatury. Nie wykonywano fizycznego I/O.
- Zwykłe przejście osi jest bezpośrednią zmianą poziomu, nie rampą. Konfiguracja
  wymusza OFF; zachowanie OUTPUT zależy również od jawnej polityki receptury.
- Pełny opis zmian, dowodów i ograniczeń:
  [keithley-diff-review.md](keithley-diff-review.md).
