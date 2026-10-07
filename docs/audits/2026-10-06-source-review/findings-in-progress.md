# Ustalenia z czytania źródeł — rejestr roboczy

To nie jest raport końcowy ani deklaracja gotowości produkcyjnej. Status
przeczytania konkretnych plików znajduje się w `coverage.json`. Poniższe
ścieżki wykonania wynikają z lektury kodu; reprodukcje opisano osobno, jeśli
zostały wykonane. Dotychczas w tym przeglądzie nie uruchamiano sprzętu.

## Łańcuch GUI → worker → runner

1. **Pomijanie kwalifikacji Anritsu przez proxy.** `MainWindow._start_run_from_ready_dialog`
   przekazuje `device_controllers`; `RunWorker._adapter_for_run` pobiera
   `DeviceController.adapter_for_run()`, czyli `RunDeviceAdapter`.
   `RecipeRunner._read_spectrum_identity` zwraca `None` dla obiektu, który
   nie jest `AnritsuAdapter`. Proxy nie dziedziczy tej klasy. Zwykła
   akwizycja traci kontrolę fingerprintu konfiguracji przed/po pomiarze,
   a importowana referencja i odtwarzanie referencji wymagają tego odczytu.
   To różnica między bezpośrednim adapterem w testach a produkcyjnym GUI.

2. **Stop w inicjalizacji nie jest zapamiętywany.** `RunWorker` tworzy
   `_early_stop_requested` i sprawdza ją podczas inicjalizacji, ale
   `request_stop` nie ustawia tej flagi; działa dopiero, gdy istnieje
   `_runner`. Należy sprawdzić scenariusz Stop/E-STOP podczas połączenia
   lub otwierania pliku: zakończone niezależne OFF nie może poprzedzać
   późniejszego, nadal dozwolonego ON z rozpoczętego workera.

3. **Timeout oczekiwania nie usuwa komendy z kolejki.**
   `DeviceController.call_for_run` po przekroczeniu czasu kończy oczekiwanie,
   ale `_RunCall` pozostaje wykonalny. Limit oczekiwania oparty o timeout
   pojedynczej komendy może być krótszy od całej akwizycji. Późne wykonanie
   po zatrzymaniu wymaga sprawdzenia w odniesieniu do kolejki OFF.

4. **Lease istnieje w API, lecz ścieżka sweepa go nie nabywa.**
   `adapter_for_run()` tworzy proxy bez owner token. Główne okno blokuje
   nowe wywołania ręczne przez `_leased_run_devices`, ale nie jest to
   rezerwacja kolejki transportowej i nie unieważnia już zakolejkowanych
   operacji. Dodatkowo guard sprawdza lease przed wyjątkami dla ręcznego
   `set_output(..., False)` i `ramp_to_zero`.

5. **Cleanup może zostać przerwany odczytem `connected`.** W końcowej pętli
   `RunWorker` ten odczyt znajduje się poza lokalnym `try`. Błąd proxy
   może pominąć cleanup kolejnych urządzeń.

6. **Resume nie powtarza warunków startu.**
   `_start_resume_from_ready_dialog` nie sprawdza `_emergency_inhibit`
   ani `dashboard.evaluate_readiness`, w przeciwieństwie do zwykłego
   startu. Po prześledzeniu `RunController.start`: zatrzask E-STOP jest tam
   sprawdzany, więc hipoteza obejścia zatrzasku przez Resume została odrzucona.
   Brak ponownej oceny dashboard readiness pozostaje osobnym zagadnieniem.

7. **Logger: hipoteza o synchronicznym dysku odrzucona; ujawniony inny błąd.**
   Po przeczytaniu `audit/logger.py`: zapis odbywa się w osobnym wątku,
   a `visa.py` skraca odpowiedzi spektrum. Jednak kolejka loggera ma
   blokujące `put` bez timeoutu: przy pełnej kolejce GUI może czekać na dysk.
   Co ważniejsze, `_writer_loop` po błędzie write/flush/fsync wykonuje
   `except Exception: pass` i mimo błędu ustawia potwierdzenie `ack`.
   `wait_durable` ignoruje wynik timeoutu. Stan `_audit_healthy` w oknie
   nie otrzyma informacji o awarii dysku, więc blokada nowych ON/runów
   przy utracie trwałego audytu nie działa. Nadanie numeru sekwencji oraz
   wstawienie do kolejki odbywa się poza wspólną sekcją krytyczną, co
   pozwala przestawić kolejność wpisów różnych wątków.
   Resume nadal czyta HDF5, parsuje i kompiluje plan na GUI.

## Kompilacja, jednostki i widoczność działań

- `quantities._canonical_unit` ignoruje wielkość liter prefiksów SI:
  podejrzane reinterpretacje `MV` jako `mV`, `mHz` jako `MHz`.
- Parser `if` dopuszcza nieboolowskie `condition` obok pełnego porównania,
  a kompilator daje pierwszeństwo `bool(condition)`.
- Rigol provider nie sprawdza pełnej zgodności target/endpoint/parameter_id;
  Keithley sprawdza kanał, ale wymaga sprawdzenia zgodności parameter_id.
- Rigol DeviceNode `continue` może pominąć jawne stałe wiersze Set.
- Semantic tree nie dodaje końcowego OFF dla lokalnego `output_policy: on`,
  chociaż kompilator tworzy tę akcję.
- Frozen dataclasses planu zawierają mutowalne payload/setpoints; trzeba
  ustalić, czy istnieje rzeczywista mutacja po obliczeniu hasha.
- Autorange Keithleya pochodzi z Settings mimo jawnego pola receptury;
  wymaga sprawdzenia kontraktu UI i rozróżnienia odrzuconego/ignorowanego pola.
- `dut_limits` receptury jest metadata-only; sam zapis nie stanowi ochrony.

## Dane, stan i estymacja — do prześledzenia

- **Recovery pomija nowe role publiczne.** `ThatecHdf5Writer.append` tworzy
  role `requested`, `applied`, `readback`, ale jego `resume` rejestruje tylko
  `setpoint` i `measurement`. Również `Hdf5RunWriter._truncate_public_thatec`
  pomija te trzy role. Po resume stary ogon pozostaje, a kolejne punkty
  mogą utworzyć drugie wiersze dla tych samych dowodów nastaw.
- **Niepełny rollback publicznych wierszy.** `_append_spectrum` i
  `_append_processed_spectrum` powiększają datasety przed walidacją
  liczby punktów/siatki. Flaga rollback jest ustawiana dopiero po powrocie
  z metody. Wyjątek po resize pozostawia ogon nieobjęty rollbackiem.
  Analogicznie częściowy zapis `_append_scalar` nie trafia na listę
  `_last_scalar_rows`; tworzenie wiersza rejestruje go dopiero na końcu.
- **Cache walidacji siatki używa wyłącznie `id`.** `_validate_uniform_grid`
  nie utrzymuje referencji do obiektu. Ponowne użycie identyfikatora przez
  Python może pominąć walidację innej siatki; mutowalny obiekt też nie jest
  chroniony. Ustalić rzeczywiste typy od wywołujących.
- **Referencja nie ma transakcji pending/complete.** `store_reference`
  tworzy bezpośrednio `/references/<index>`, w odróżnieniu od checkpointów.
  Sprawdzić zachowanie recovery po przerwaniu zapisu referencji.
- **Zamykanie writerów nie ma bezwarunkowego cleanup.** Wyjątek przy
  ustawianiu statusu/flush/close może pozostawić uchwyty otwarte. Konstruktor
  również nie obejmuje całej inicjalizacji ochronnym `try/finally`.

- Etykieta `simulated_ack` zależy od trybu wykonania różnego od measurement,
  więc obejmuje również dry run i manual step na fizycznym sprzęcie.
- `stop_moke_voltage` nie aktualizuje `actual_v` w aktywnym kontekście;
  rampowanie Keithleya do zera nie unieważnia wszystkich potwierdzeń osi.
- Końcowy OFF i cache aktywnych outputów/kontekstu mogą się rozchodzić.
- Jedna aktywna referencja w runnerze: prześledzić kontrakt background/reference.
- Estymacja bez konfiguracji SA opiera liczbę punktów na defaults, chociaż
  akwizycja ma zachować aktualne ustawienia fizycznego analizatora.
- Archiwizacja surowych przebiegów przy retry może przekroczyć estymację
  storage; retry upper nie uwzględnia pełnego czasu powtórzonej akwizycji.

## Adaptery — dodatkowe ścieżki wymagające kwalifikacji

- Keithley `measure_only`: konfiguracja zachowuje poprzednią funkcję, poziom
  i compliance źródła; walidacja włączenia i `_verify_applied_configuration`
  dla tego trybu nie sprawdzają tych parametrów. Należy odtworzyć możliwość
  późniejszego ON z poprzednimi nastawami na fake transport, bez sprzętu.
- Keithley `update_source_level` wysyła pojedynczy poziom. Zachowanie OUTPUT
  nie oznacza rampy przez wartości pośrednie. Pomiar może dodatkowo zapisać
  `source.offmode = OUTPUT_NORMAL`, jeśli odczyt wykryje inny offmode.
- Rigol `set_output(True)` rozpoczyna od OFF nawet dla już aktywnego kanału.
  Aktualizacje nastaw są odrębnymi metodami; trzeba odróżnić je od ponownego
  węzła ON lub pełnej konfiguracji.
- Rigol opcjonalne probe capabilities kontynuują kolejne zapytania po timeout,
  co wymaga sprawdzenia resynchronizacji odpowiedzi na fake transport.
- MOKE waliduje cały plan trajektorii przy każdej nastawie; długie trajektorie
  mogą wprowadzać koszt kwadratowy. Tolerancja potwierdzenia SET (1 mV) jest
  większa od tolerancji zakończenia rampy (1e-12 V): akceptowany odczyt może
  prowadzić do ponawiania docelowego SET aż do timeout.
- Anritsu szybki binarny fetch używa cache siatki częstotliwości; zmiana zakresu
  z panelu przy tej samej liczbie punktów wymaga unieważnienia tego cache.
  `acquire_fresh_trace` ma ścieżki oddania trace bez dowodu nowego sweepa;
  trzeba śledzić, którzy wywołujący traktują wynik jako świeży ilościowo.

## Konfiguracja stacji i prezentacja — dodatkowe ustalenia z pełnych plików

- `SettingsRepository.repair_known_issues` bezwarunkowo zastępuje
  `anritsu.safety.reference_level` zakresem -120..+50 dBm zarówno podczas load,
  jak i save. Zapisany węższy zakres operatora nie przetrwa tej operacji.
  `AnritsuSafety` nie wywołuje walidacji wymiaru/kompletności reference_level.
- Rigol `fixed_source_resistance` dopuszcza wartości większe niż fizyczne
  50 omów. Potwierdza to wcześniejszą obawę o zaniżenie modelowanego prądu;
  trzeba odróżnić rezystancję wewnętrzną od jawnego dodatkowego rezystora.
- `RigolChannelLimits` próbuje normalizować ujemny combined_voltage_limit
  przez przypisanie do modelu frozen: taka normalizacja zgłasza błąd zamiast
  zwrócić zamierzoną wartość dodatnią.
- Readiness rozpoznaje energizowanie przez OUTPUT ON; lista nie obejmuje
  aktualizacji DAC MOKE. Komunikat „Plan contains no OUTPUT ON action” nie
  może być interpretowany jako brak fizycznego pobudzania układu.
- Execution preview robi równomierny subsampling, który może zgubić wąskie
  piki, mimo dostępnej funkcji peak-preserving. Oś czasu nie obejmuje wszystkich
  aktualizacji parametrów; część zależy od setpoints context i rejestru.
- Model drzewa czyta stan potomków dla rodziców, ale `apply_states` emituje
  dataChanged tylko dla zmienionego wiersza i wybranej osi. Rodzic z wyliczonym
  stanem może pozostać wizualnie stary do kolejnego odmalowania.
- Spectrum preview stackuje do 64 pełnych tablic; limit cache konwersji nie
  ogranicza tej macierzy roboczej. Detekcja pików wykonuje rolling percentile
  z oknem około N/5 i dopasowania 840 modeli dla każdego kandydata przed
  ograniczeniem liczby wyników. To konkretne miejsca kosztu CPU/RAM do
  powiązania z workerem, GIL i opóźnieniem GUI.
- Detekcja pików odwraca malejącą siatkę; ścieżka liniowa odtwarza oryginalny
  indeks, ścieżka dBm/dB go nie odtwarza. Na malejącej siatce marker indeksowy
  może wskazywać inny bin niż raportowana częstotliwość.
- Starszy `LinearPowerAverager` i reference math sprawdzają skończoność dBm,
  ale nie wynik potęgowania ani jednowymiarowość. Nowszy `dbm_to_w` ma te
  kontrole. Należy ujednolicić kontrakt bez zmiany poprawnej matematyki
  uśredniania w mocy liniowej i zachowania ujemnych reszt w watach.

## Edytor sweepów — po przeczytaniu całego `ui/recipes/page.py`

- Wynik asynchronicznego preflight sprawdza zgodność YAML i trybu dry-run,
  lecz nie wersję Settings. Zmiana Settings w trakcie kompilacji unieważnia
  `_plan`, ale stary worker może następnie przywrócić plan ze starych limitów.
- Menu kontekstowe dodaje „New empty sweep” przed sprawdzeniem blokady
  edycji. `new_recipe` i `_apply_builder_source` nie sprawdzają wykonania;
  w czasie runu można zastąpić widoczny dokument mimo komunikatu read-only.
- Funkcje `_configured_*_node` budują nowe mappingi i nie zachowują m.in.
  `disabled` z oryginalnego węzła. Zmiana konfiguracji wyłączonego bloku
  może go ponownie uaktywnić bez jawnego użycia Enable.
- `_clone_node_mapping` zmienia ID dzieci, ale nie `managed_acquisition_id`.
  Edycja sklonowanego bloku Anritsu może nie odnaleźć swojej akwizycji i
  dopisać drugą zamiast zastąpić istniejącą.
- Starsza ścieżka „Add device control / point generator” generuje pełne
  configure w każdej iteracji, z wartościami domyślnymi pozostałych pól.
  Keithley otrzymuje m.in. compliance z maksimum Settings, NPLC 1 i 100 ms.
  Nie wolno utożsamiać tego z update-only DeviceNode/continue.
- W tej samej ścieżce Rigol obsługuje podstawianie frequency/high/low,
  ale nie amplitude/offset mimo ich obecności w wyborze parametrów. Wybrana
  stała amplitude/offset nie trafia do konfiguracji, a sweep tworzy konfigurację
  z domyślnymi poziomami zamiast użyć tej osi.
- Edytor natywnego sweepa Keithley pozwala zmienić kanał/tryb dialogu, lecz
  zachowuje pierwotny `target`. Modyfikuje też pierwszy napotkany Wait lub
  dopisuje Wait na końcu dzieci, zamiast zachować jego semantyczne miejsce.
- Generowanie MOKE jawnie dodaje configure, arm i końcowe stop/zero.
  Biblioteka udostępnia tylko główny profil MOKE, pomijając zatwierdzone
  niezależne kanały; `set_settings` nie przebudowuje tej listy parametrów.
- Edycja akwizycji usuwa część starych pól, lecz pozostawia minimum_duration,
  purpose i inter_sweep_delay, jeśli nowy dialog nie zwróci tych pól.
  Trzeba sprawdzić faktyczny kontrakt `node_fields` przed oceną skutków.
- `_node_selected` wielokrotnie parsuje pełny YAML, a sygnały clicked,
  currentChanged i selectionChanged mogą wywoływać tę pracę kilka razy na
  jedno kliknięcie. Indeks nie jest cache'owany na granicy zmiany dokumentu.

## Dialogi — powiązanie z edytorem

- Potwierdzona ścieżka minimum_duration: ustawienie 0 s w samodzielnym
  edytorze referencji pomija to pole w `node_fields`, a RecipePage nie usuwa
  poprzedniej wartości. Nie da się w ten sposób wyłączyć poprzedniego czasu
  minimalnego; import może odziedziczyć czas należący do akwizycji.
- `RigolNodeEditorDialog.load_plan_actions` ładuje tryb i ROI, ale nie `value`
  wiersza Set. Dla YAML, w którym value różni się od configuration, otwarcie
  i zatwierdzenie dialogu zastępuje jawny Set wartością snapshotu.
- Porównanie Rigola liczy pola jako Set także w `continue`, nie pokazuje
  OUTPUT ani resetowania trybów sprzętowych; etykieta „Leave OUTPUT unchanged”
  nie odpowiada konfiguracji z wymuszonym OFF. Sama zielona tabela nie jest
  jeszcze kompletnym manifestem rzeczywistych mutacji adaptera.
- Anritsu advanced panel ma własny VBW Video/Power oraz control w basic panel.
  RecipeDialog czyta/zapisuje `panel.vbw_mode`, nie advanced odpowiednik.
  Należy sprawdzić, czy advanced odpowiednik jest nadal widoczny (definicja
  panelu w dużym pliku urządzenia); wtedy powstaje edytor bez skutku w planie.
- `StationDialog` zawiera obecnie ustawienie AA_DontCreateNativeWidgetSiblings
  przed konstruktorem frameless oraz własną obsługę WM_NCCALCSIZE. To konkretna
  poprawka dla artefaktu „ekran w ekranie”; lektura źródła nie zastępuje
  oględzin na Windows przy DPI i przewijaniu operatora.
- Dialogi tworzone z rodzicem zwykle nie są `deleteLater` po exec. Zamknięcie
  nie musi zniszczyć widgetów i ich połączeń z globalną zmianą motywu.
  Powtarzane otwieranie wymaga sprawdzenia liczby żywych obiektów i pamięci.

Każdy punkt wymaga końcowej kwalifikacji: błąd potwierdzony, ograniczenie
projektowe, luka wymagająca dowodu albo hipoteza odrzucona. Sam zapis na
tej liście nie oznacza, że wszystkie opisane konsekwencje zostały odtworzone.

## Dalsza lektura — symulatory i czytniki wyników

- KeithleySimulator i AnritsuSimulator nie odrzucają wszystkich nieznanych
  poleceń write. Anritsu akceptuje przez regex dowolny wariant TRAC:TYPE?.
  Zaliczenie symulacji nie potwierdza zgodności tych poleceń z firmware
  urządzenia, które wcześniej zgłaszało timeout tego zapytania.
- KeithleySimulator ma jeden wspólny `level` na SMU dla leveli i levelv,
  mimo osobnych wpisów programmed. Zmiana nieaktywnego poziomu może wpłynąć
  na symulowany pomiar aktywnego trybu; readback obu poziomów zwraca ten sam
  level. Domyślne limity programmed=0.1 różnią się od limitów modelu=inf.
- SimulatedMokeBoxTransport koduje Hall jako field_t/0.04, a pomocniczy
  hall_field_from_voltage stosuje 1 T/V. Lake Shore zwraca field_t.
  Wymaga ustalenia użycia przelicznika przed oceną konsekwencji w GUI.
- ThatecRunReader._committed_row_limit pomija nowe role requested/applied/
  readback, podobnie jak wcześniej opisane truncate/recovery. Takie publiczne
  szeregi mogą ujawnić niezakończony ogon transakcji.
- Hdf5SeriesReader używa numeric_names zamiast committed_point_names;
  brak wybranego pomiaru zastępuje pierwszym innym pomiarem lub zerem.
  Etykiety powstają z fragmentów nazw: power_w otrzyma dBm bez przeliczenia,
  ogólne field otrzyma Oe. Priorytet podciągu `r` wybiera także dowolne
  nieoporowe kanały zawierające tę literę.
- Hdf5SeriesReader oraz CharacterizationCsvReader dopisują X przed konwersją
  Y. Gdy konwersja Y nie powiedzie się, pozostaje X bez odpowiadającego Y,
  a późniejsze punkty przesuwają parowanie krzywej.
- ManualSpectrumArchive nowym plikom narzuca simulation_metadata.enabled=False.
  Konstruktor nie przyjmuje pochodzenia backendu. Trzeba prześledzić wywołanie
  z symulowanej strony Anritsu; format nie ma obecnie drogi przekazania True.
- ThatecSchemaMapper nie sprawdza disabled i odwiedza obie gałęzie if.
  Niezgodna liczba zwykle kończy się jawnym checkpoint_fallback, ale nie jest
  to odwzorowanie faktycznie skompilowanej aktywnej topologii.
- ThatecCompatibilityValidator po wykryciu złego typu struktury nadal używa
  int(value), .shape lub .asstr(). Uszkodzony plik może wywołać wyjątek zamiast
  zwrócić raport z błędem; walidator nie jest jeszcze pełną barierą wejściową.

## Dalsza lektura — obliczenia widmowe

- Nowe comparison_view odejmuje w W i jawnie pomija niedodatnie wyniki przy
  konwersji do dBm; nie stosuje abs() do ujemnych reszt. To poprawne zachowanie
  wyświetlania, a nie usunięcie szumu z pomiaru.
- Finalizacja propaguje wspólną niepewność referencji jeden raz; nowe szkolenie
  zakłóceń weryfikuje hash profilu z surowych REF w obu przebiegach. Bootstrap
  i test różnic oddzielają założenia operatora od laboratoryjnej kwalifikacji.
  Lektura tych ścieżek nie wykazała podstaw do automatycznego deklarowania
  lepszej jakości danych po samym wydłużeniu tła.

## Strona Anritsu — przeczytane pełne 5973 linie

- Odrzucona hipoteza o podwójnym edytorze VID/POW: advanced panel ma VBW
  auto/manual/off, a basic ma osobny wybór VID/POW. To dwa różne parametry.
- `_save_configured_manual_spectrum` wywołuje ManualSpectrumArchive.save
  bez workera; `_save_reference_to` i `_load_reference_from` też robią I/O
  HDF5 w wątku GUI. Nowy correction_controller poprawnie oddziela te zadania,
  ale nie jest używany w starym ręcznym zapisie/reference export.
- Ręczny zapis przekazuje `options.metadata_values` z chwili konfiguracji
  dialogu, nie odświeża wybranych kluczy przy zapisie. Opis last-confirmed
  może dotyczyć nieaktualnej chwili. Potwierdzona też ścieżka do hardcoded
  simulation_metadata=False w ManualSpectrumArchive.
- `_show_execution_trace` tworzy SpectrumTrace z przeskalowanego podglądu
  eventu i umieszcza go w `_latest_trace`. Ręczny zapis może następnie potraktować
  te nieliczne punkty jako raw; liczba source_points nie jest przenoszona do
  danych/proweniencji ręcznego zapisu. Trzeba wykluczyć ten eksport albo sięgnąć
  do pełnego zatwierdzonego checkpointu.
- `_manual_trace_payload` przy braku wybranego wariantu cicho wybiera analizę
  albo raw. Metadata trace_variant pozostaje żądanym wariantem, więc opis może
  być sprzeczny z zapisanymi wartościami.
- `_result` uśrednia powers bez kontroli zgodności częstotliwości i generacji
  między ramkami; wynik otrzymuje oś i sweep evidence ostatniej ramki.
  W Live kolejne odczyty mogą być tym samym buforem urządzenia. Liczba ramek
  nie jest dowodem liczby niezależnych pełnych sweepów.
- `_apply_readback_parameter(start_hz/stop_hz)` wpisuje wartości do edytorów
  bez uwzględnienia center/span. Pojedyncze Assign w tej reprezentacji zmienia
  inne znaczenie parametru niż etykieta wybranego odczytu.
- `_error` nie obsługuje stop_live: po błędzie zatrzymania pozostaje
  `_live_transition_pending=True` i stan STOPPING, przy zatrzymanym timerze.
- `set_settings` nie aktualizuje `_settings`/processor_config w istniejącym
  correction_workspace; nowe nagranie używa tam starej polityki korekcji.
- `_update_peak_tracking` ponownie uruchamia detect_spectrum_peaks w GUI;
  surowy spektrogram robi np.stack pełnej historii w GUI. Zastosowanie osobnego
  workera analizy nie usuwa tych ścieżek obciążenia.
- SpectrumAnalysisController.close po 3 s przechodzi do nieograniczonego
  QThread.wait; zwykła analiza widma nie sprawdza interruption w trakcie
  obliczeń. Zamknięcie strony może zamrozić interfejs do końca analizy.
- BackgroundCorrectionAssistant ustawia phase=collecting i ownership przed
  start_acquisition, które po błędnej walidacji po prostu zwraca. Np. błędna
  wartość tau może zostawić asystenta w zbieraniu bez działającej akwizycji;
  timeout przygotowania został już zatrzymany.
# Dodatkowa lektura wykresów i magazynów przetwarzania

- SpectrumPlotWidget usuwa niefinitywne próbki przed rysowaniem; może łączyć
  krzywą przez luki nieokreślonego dBm reszty. Wymaga sprawdzenia prezentacji
  luk i eksportu, bez zastępowania nieokreślonych wartości zerami.
- Bazowy hold porównuje długości, nie siatkę częstotliwości. Nadpisanie
  w SpectrumWorkbench sprawdza siatkę — nie przypisywać błędu obu ścieżkom.
- SpectrumWorkbench opisuje różnicę amplitud innych niż dB/dBm jako
  `linear ratio`, również dla odejmowania W: błędna jednostka etykiety.
- Finalizacja, trening i diagnostyka archiwów sprawdzają hashe źródeł przed
  i po operacji; nowe wyniki nie zastępują źródeł. Dialogi tych operacji
  używają workerów oraz nieblokującego oczekiwania na zamknięcie.
- MokeCalibrationRunStore.close liczy hash przez path.read_bytes(): cały
  plik jest alokowany w pamięci. Repozytorium load używa strumieniowego hasha.
- app/platform/paths.py: fallback określony jako repository root używa
  parents[2], czyli katalogu app, zamiast katalogu repozytorium.

# Pozostałe mniejsze moduły UI

- AnritsuReadbackDialog po emitowaniu assign_requested natychmiast ustawia
  MATCH/Applied bez potwierdzenia przyjęcia wartości przez odbiorcę. Form value
  None jest traktowane jako zgodność/info, także dla opcjonalnych parametrów.
- discover_tcp_endpoints materializuje wszystkie subnet.hosts() przed
  sprawdzeniem max_hosts. Duża sieć może wyczerpać RAM przed walidacją;
  wariant discover_tcp_ip_range sprawdza liczność przed alokacją.
- PeakTableDialog opisuje fit_rmse_db zawsze jako dB; przy rozwoju modeli
  liniowych kontrakt jednostki musi być jawny (obecnie fitting w UI tylko dBm).
- MokeVoltageHistory odświeża wszystkie serie co 50 ms, gdy widoczne;
  obciążenie jest ograniczone do 6000 punktów, nie rośnie przez cały sweep.
- KeithleyTwinAxisPlot usuwa wspólnie niefinitywne V/I i łączy sąsiadów.
  Pojedynczy brak I usuwa również poprawne V; luka czasowa nie jest oznaczona.

# Przegląd stron MOKE / Lake Shore / Results

- SweepTreePanel._render_selected_row wykonuje scalar_series i buduje
  QTreeWidgetItem dla całej serii w GUI. _populate_tree_content materializuje
  całe drzewo checkpointów/setpointów/pomiarów bez stronicowania.
- SweepTreePanel tworzy Processed spectrum także dla punktów mających tylko
  raw; obecność przetworzonego wariantu nie jest sprawdzana w tym widoku.
- MokeFieldWorkflow._list_saved_models ładuje i weryfikuje każdy model z
  pełnym hashowaniem jego HDF5 w GUI; również activate/active/load robią to
  synchronicznie. Lista kalibracji może blokować interfejs przy połączeniu.
- MOKE apply_execution_event pokazuje potwierdzony DAC w readoucie, ale
  show_execution_voltage nie wpisuje wartości do edytora target. Widoczny
  draft może różnić się od wykonywanego sweepa; odróżnienie tych stanów jest
  konieczne dla wymaganego zachowania wirtualnego pilota.
- Historyczne manual metadata MOKE/Lake Shore nie są czyszczone na disconnect
  i nie niosą czasu odczytu w tworzonych ManualMetadataValue. Po reconnect
  można zapisać stare odczyty jako ostatnie potwierdzone bez informacji o wieku.
- MokeFieldWorker emituje succeeded przed release leases; awaria release może
  następnie emitować failed. Stan ukończenia i rezerwacji trzeba rozróżnić.

# Rigol — pełna lektura strony urządzenia

- _record_visible_quick_readback odtwarza pełny config z formularza po
  pojedynczym odczycie/zmianie i zapisuje go jako confirmed carrier.
  Niezastosowane zmiany fazy, load lub waveform mogą w ten sposób zostać
  uznane za potwierdzone; manual_metadata_values dziedziczy tę deklarację.
  request_output porównuje ten cache z formularzem, aby pominąć configure.
- apply_execution_event renderuje cache obu kanałów przy każdym evencie.
  Kolejne setText/currentTextChanged uruchamiają przeliczenia i wiele redraw
  waveform preview; _quick_control_projection blokuje publikację draftów,
  ale nie blokuje sygnałów przerysowujących. To dodatkowy koszt GUI.
- _execution_readbacks nie jest czyszczone w set_execution_controlled ani
  _device_state_changed; dane poprzedniego uruchomienia mogą pozostać w UI.
- Debounce timery Live nie są powiązane z kanałem z chwili edycji; callback
  odczytuje aktualnie wybrany kanał. Zmiana kanału przed timeoutem może
  wysłać zmianę formularza nowego kanału. Wymaga reprodukcji bez sprzętu.
- _update_modulation_parameter_ui resetuje wartość do stałej domyślnej także
  przy zmianie source INT/EXT, jeżeli editor nie ma focus; nie tylko przy
  zmianie typu modulacji.

