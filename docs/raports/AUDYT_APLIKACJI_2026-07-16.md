# Audyt aplikacji Lab Control

**Data audytu:** 2026-07-16  
**Zakres:** backend, adaptery urządzeń, bezpieczeństwo fizyczne, obliczenia numeryczne, zapis danych, wątki oraz UI/UX  
**Urządzenia:** Rigol DG1032Z, Keithley 2600/2602A, Anritsu MS2830A  
**Charakter audytu:** analiza kodu i testów bez wykonywania zmian oraz bez Hardware-in-the-Loop na rzeczywistym DUT

## 1. Podsumowanie wykonawcze

Aplikacja ma dobre fundamenty: oddzielne adaptery urządzeń, centralne parsowanie jednostek, profile bezpieczeństwa, symulatory, receptury, wątki robocze i testy automatyczne. Nie jest jednak jeszcze gotowa do długiego, bezobsługowego sweepu na wartościowym DUT.

Najważniejsze ryzyka dotyczą:

1. pozostawienia aktywnego wyjścia Keithleya po przekroczeniu limitu pomiarowego;
2. braku kontroli maksymalnej możliwej mocy przed włączeniem Keithleya;
3. błędnego globalnego stanu dwukanałowych urządzeń;
4. niezgodności formatu HDF5 z Thatec/PyThat;
5. możliwości nadpisania lub częściowego zapisania pliku pomiarowego;
6. uśredniania wielokrotnie tej samej ramki Anritsu;
7. redukcji danych wykresu, która może ukrywać wąskie piki widma.

Problemy oznaczone jako **P0** powinny zostać rozwiązane przed wykorzystaniem aplikacji do automatycznych pomiarów z aktywnymi wyjściami.

## 2. Wyniki kontroli technicznych

- `pytest`: **67/67 testów zaliczonych**;
- kompilacja wszystkich modułów aplikacji i testów: poprawna;
- Ruff: nie był zainstalowany w środowisku, dlatego pełny lint nie został wykonany;
- używana wersja PySide6: **6.11.1**;
- największy problem utrzymaniowy UI: `app/ui/main_window.py` ma około **3010 linii** i łączy wiele niezależnych odpowiedzialności.

Przejście testów nie oznacza braku problemów bezpieczeństwa. Obecny zestaw testów nie obejmuje między innymi awarii w trakcie zapisu HDF5, jednoczesnego stanu obu kanałów ani obowiązkowego wyłączenia wyjścia po przekroczeniu limitu pomiarowego.

## 3. Problemy krytyczne — P0

### 3.1. Keithley nie wyłącza wyjścia po przekroczeniu limitu pomiarowego

W `app/devices/keithley/adapter.py`, w metodzie `measure()`, napięcie i prąd są walidowane po wykonaniu pomiaru. Jeśli `validate_keithley_measurement()` wykryje przekroczenie dozwolonego napięcia, prądu albo mocy, wyjątek trafia do GUI, ale wyjście źródła nie jest automatycznie wyłączane.

Parametr `stop_on_overpower` istnieje w modelu konfiguracji, lecz nie jest wykorzystywany przez adapter.

**Skutek:** podczas sterowania ręcznego DUT może pozostać zasilany mimo przekroczenia limitu bezpieczeństwa.

**Wymagana poprawka:**

- wykonać `emergency_off()` natychmiast po wykryciu przekroczenia trip limitu;
- ustawić stan urządzenia `FAULT` albo `OVERPOWER`;
- zapisać trwałe zdarzenie `SAFETY_TRIP` z wartościami V, I, P i limitem;
- zablokować ponowne włączenie do czasu jawnego potwierdzenia operatora;
- przetestować osobno kanały A i B oraz wszystkie tryby źródła.

### 3.2. Brakuje kontroli mocy przed włączeniem Keithleya

`app/safety/keithley.py` sprawdza osobno source level oraz compliance, ale przed ARM i OUTPUT ON nie sprawdza konserwatywnej maksymalnej mocy:

```text
abs(source_current × voltage_compliance)
abs(source_voltage × current_compliance)
```

Możliwe jest ustawienie poprawnego prądu i poprawnego compliance, których iloczyn przekracza `max_abs_power`. Przekroczenie zostanie wykryte dopiero po pomiarze, czyli już po podaniu energii na DUT.

**Wymagana poprawka:** dodać power preflight do wspólnego walidatora używanego przez GUI, receptury i adapter, przed wykonaniem jakiejkolwiek komendy włączenia wyjścia.

### 3.3. Błędny globalny stan urządzeń dwukanałowych

Adaptery Keithleya i Rigola ustawiają globalny `DeviceState` na podstawie ostatnio obsłużonego kanału.

Przykład:

1. kanał A jest włączony;
2. operator wyłącza kanał B;
3. aplikacja pokazuje całe urządzenie jako `OUTPUT_OFF`, mimo że kanał A nadal pracuje.

**Wymagana poprawka:**

- utrzymywać potwierdzony stan osobno dla każdego kanału;
- agregować stan urządzenia: `OUTPUT_ON`, jeżeli aktywny jest przynajmniej jeden kanał;
- odpytywać oba kanały po connect, E-STOP i każdej zmianie wyjścia;
- pokazywać stan globalny oraz stany kanałów bez polegania wyłącznie na ostatniej operacji.

### 3.4. HDF5 nie jest zgodny z Thatec/PyThat

Porównano strukturę aktualnego writera z przekazanym plikiem referencyjnym Thatec.

Plik referencyjny zawiera główne przestrzenie:

```text
/devices
/labbook
/measurement
/scan_definition
```

Aktualny `Hdf5RunWriter` tworzy:

```text
/run
/points
/spectra
/events
```

Nie jest to zgodność strukturalna wymagana przez PyThat. Problem obejmuje również organizację wymiarów, `scale`, `scan_definition/tree_view`, metadane wierszy i reprezentację wielowymiarowych widm.

**Wymagana poprawka:**

- utworzyć `ThatecSchemaMapper`;
- utworzyć `ThatecHdf5Writer`;
- zachować aktualny schemat wyłącznie jako opcjonalną dodatkową przestrzeń nazw;
- dodać golden-file test;
- przypiąć kwalifikowaną wersję PyThat;
- w CI otwierać wygenerowany plik przez PyThat i sprawdzać `dims`, `coords`, jednostki, metadane oraz wartości.

### 3.5. Ryzyko utraty lub uszkodzenia pliku HDF5

Nazwa pomiaru ma dokładność jednej sekundy, a plik jest otwierany w trybie `"w"`. Kolizja nazwy może nadpisać istniejący wynik.

Zapis punktu także nie jest transakcyjny. Awaria po utworzeniu grupy punktu, ale przed zakończeniem zapisu widma albo CSV, pozostawia częściową grupę i niespójny indeks.

**Wymagana poprawka:**

- stosować UUID albo timestamp z mikrosekundami;
- tworzyć plik bez zgody na nadpisanie istniejącego wyniku;
- najpierw zapisywać punkt do grupy tymczasowej oznaczonej `incomplete`;
- dopiero po pełnym zapisie oznaczać punkt jako `complete`;
- przy wznowieniu rekonstruować licznik punktów ze struktury pliku;
- wymusić `flush` po zdarzeniach bezpieczeństwa i zmianach statusu przebiegu;
- dodać fault-injection tests dla awarii HDF5 i CSV.

## 4. Audyt numeryczny i fizyczny

### 4.1. Niesynchroniczny pomiar napięcia i prądu Keithleya

Keithley wykonuje osobne zapytanie o napięcie i osobne o prąd. Dla dynamicznego DUT wartości mogą pochodzić z różnych chwil. Obliczone `V × I` oraz `V / I` nie muszą wtedy odpowiadać jednemu stanowi układu.

**Zalecenie:** użyć bufora i trigger modelu albo jednej kwalifikowanej procedury TSP zwracającej spójną parę I/V.

### 4.2. Utrata znaku rezystancji

GUI oblicza rezystancję jako `abs(V / I)`. Ukrywa to znak wyniku, który może być istotny dla aktywnych elementów, odwrotnej polaryzacji albo analizy przepływu energii.

**Zalecenie:** pokazywać `R = V/I` ze znakiem, a opcjonalnie osobno `|R|`. Dla prądu bliskiego zeru pokazywać stan `undefined/open`, nie przypadkowo dobraną wartość.

### 4.3. Compliance jest tylko wnioskowane

Adapter uznaje, że compliance wystąpiło, kiedy zmierzona wartość zbliża się do zaprogramowanego limitu. Nie korzysta z potwierdzonego dla danego firmware rejestru statusowego.

Może to powodować fałszywe alarmy albo niewykrycie stanu compliance.

**Zalecenie:** zakwalifikować właściwy atrybut/status TSP dla używanego modelu i firmware. Obecny mechanizm można pozostawić jako dodatkowy konserwatywny fallback.

### 4.4. Uśrednianie Anritsu może używać tej samej ramki wielokrotnie

Uśrednianie aplikacyjne wykonuje kolejne odczyty `TRAC?`, ale nie synchronizuje każdego odczytu z nowym, zakończonym sweepem. Przy szybkim odczycie 200 próbek aplikacja może wielokrotnie pobrać tę samą ramkę.

**Wymagana poprawka:**

- preferować sprzętowe averaging Anritsu, jeżeli zostanie zakwalifikowane;
- albo dla każdej próbki uruchamiać i potwierdzać nowy sweep;
- rejestrować numer ramki, czas sweepu i czas pobrania;
- wykrywać duplikaty.

### 4.5. Pamięć uśredniania

Wszystkie trace są przechowywane w liście tuple. Dla 200 widm po 10001 punktów zużycie pamięci może wynieść dziesiątki MB.

**Zalecenie:** wykorzystać inkrementalny akumulator NumPy w domenie liniowej mW, licznik próbek oraz opcjonalnie wariancję/SEM.

### 4.6. Operacje na widmie referencyjnym

- Uśrednianie w domenie liniowej mW jest poprawne.
- `subtract_power` po ujemnym wyniku podstawia bardzo małą dodatnią moc, co daje około `-300 dBm` i ukrywa błąd fizyczny.
- `multiply_linear` zwraca `mW²`; jest to operacja matematyczna, ale nie widmo mocy.
- Osie częstotliwości są porównywane przez dokładną równość floatów.

**Zalecenie:**

- zwracać maskę nieważnych lub przyciętych punktów;
- pokazywać liczbę punktów z ujemnym residual power;
- oznaczyć `mW²` jako operację diagnostyczną;
- stosować tolerancję częstotliwości i jawną politykę interpolacji;
- zapisywać metodę przetwarzania i jej parametry w HDF5.

### 4.7. Brak kontroli NaN/Inf w trace Anritsu

Adapter kontroluje liczbę punktów, ale nie odrzuca `NaN` ani `Inf`. Takie wartości mogą zepsuć autoskalowanie, peak search, uśrednianie i zapis metadanych.

**Zalecenie:** walidować skończoność osi i amplitud bezpośrednio po odpowiedzi urządzenia.

### 4.8. Nieaktywne zabezpieczenia RF Anritsu

Pola:

- `max_expected_power_at_connector`;
- `external_attenuation`;
- `minimum_internal_attenuation`;
- `preamplifier_allowed`

są w dużej mierze deklaracyjne. Adapter nie wymusza minimalnego tłumienia ani stanu przedwzmacniacza.

**Zalecenie:** dodać kwalifikowane komendy ustawienia i readbacku tłumika/preampu, obliczać efektywny poziom wejściowy oraz blokować akwizycję przy niespójnym stanie.

### 4.9. Rigol — ograniczenia modelu bezpieczeństwa

Model prądu wykorzystuje konserwatywny model źródła Thévenina 50 Ω i zadeklarowaną minimalną impedancję DUT. Jest to estymacja, a nie pomiar rzeczywistego prądu.

**Zalecenie:**

- zachować wyraźne oznaczenie `estimated`;
- dodać limit estymowanej mocy na DUT;
- zapisywać zadeklarowaną impedancję do metadanych każdego punktu;
- przy krytycznych DUT stosować niezależny pomiar prądu/mocy.

## 5. Wątki, sesje VISA i niezawodność

### 5.1. Możliwe zawieszenie podczas zamykania GUI

`DeviceController.close()` wykorzystuje `BlockingQueuedConnection`. Jeżeli wątek urządzenia utknie w długim zapytaniu VISA, GUI może bezterminowo czekać jeszcze przed wywołaniem ograniczonego `thread.wait()`.

**Zalecenie:** stosować ograniczone timeouty VISA, asynchroniczny shutdown state machine oraz eskalację do best-effort E-STOP bez blokowania głównego wątku.

### 5.2. Disconnect może zostać pominięty

W części ścieżek `emergency_off()` i `disconnect()` są we wspólnym bloku `try`. Jeżeli OFF zgłosi wyjątek, disconnect nie zostanie wykonany.

**Zalecenie:** każdą akcję cleanup wykonywać w osobnym `try/finally` i raportować wszystkie błędy.

### 5.3. Run Engine łączy wszystkie urządzenia

Silnik receptur łączy Rigola, Keithleya i Anritsu niezależnie od tego, których urządzeń używa plan. Pomiar tylko jednym modułem może nie wystartować, jeśli pozostałe urządzenia są odłączone.

**Zalecenie:** kompilator planu powinien zwracać zbiór wymaganych urządzeń, a Run Engine powinien uzyskiwać sesje tylko dla tego zbioru.

### 5.4. Deadline `*OPC?`

Samo zapytanie VISA `*OPC?` może blokować dłużej niż programowy deadline pętli. Deadline musi być egzekwowany także przez timeout sesji VISA.

## 6. Audyt UI/UX

### 6.1. Mocne strony obecnego UI

- Event Log jest już oddzielnym dockiem;
- Anritsu korzysta ze splittera;
- dostępne są motywy jasny i ciemny;
- Rigol dynamicznie pokazuje pola zależne od rodzaju przebiegu;
- istnieją per-device limit fields i helpery;
- Keithley ma podgląd obu kanałów i podstawowe mierniki.

Jest to dobra baza, ale wymaga ujednolicenia i wydzielenia design systemu.

### 6.2. Monolityczna architektura frontendu

`app/ui/main_window.py` łączy:

- budowę wszystkich stron;
- komunikację z urządzeniami;
- walidację formularzy;
- obliczenia widm;
- stan kontrolek;
- persystencję ustawień;
- prezentację wyników.

Proponowana struktura:

```text
app/ui/
  pages/
    dashboard_page.py
    rigol_page.py
    keithley_page.py
    anritsu_page.py
    recipes_page.py
    execution_page.py
    results_page.py
    settings_page.py
  widgets/
    quantity_edit.py
    limit_field.py
    device_status.py
    meter_tile.py
    notification_banner.py
    spectrum_plot.py
  viewmodels/
    rigol_viewmodel.py
    keithley_viewmodel.py
    anritsu_viewmodel.py
  design_system/
    tokens.py
    theme.py
    dark.qss
    light.qss
```

### 6.3. Zmiana motywu ukrywa legendę Anritsu

Kod po zmianie motywu wywołuje `legend().hide()` dla wszystkich wykresów. Powoduje to usunięcie legendy Raw/Average/Reference/Processed, mimo że została wcześniej włączona na stronie Anritsu.

**Poprawka:** każdy komponent wykresu powinien sam definiować widoczność legendy, a mechanizm motywu nie powinien zmieniać funkcjonalnego stanu widoku.

### 6.4. Redukcja danych może ukryć pik

Live Anritsu i Results wybierają co N-ty punkt. Wąski pik znajdujący się pomiędzy wybranymi indeksami może całkowicie zniknąć. Autoskalowanie również może wtedy nie uwzględnić rzeczywistego maksimum.

Jest to błąd prezentacji danych pomiarowych, nie tylko problem estetyczny.

**Wymagana poprawka:** użyć downsamplingu min/max envelope, peak-preserving albo LTTB z zachowaniem ekstremów.

### 6.5. Responsywność

W kodzie pozostają sztywne ograniczenia szerokości i wysokości, między innymi minimum 620 px dla panelu Anritsu, 520 px dla edytora receptur i maksimum 250 px dla szczegółów wyników.

**Zalecenie:**

- stosować `QSplitter` i odpowiednie `QSizePolicy`;
- pozwolić zwijać panele konfiguracji;
- usunąć większość sztywnych szerokości;
- zaprojektować układ dla 1024×768, 1366×768, Full HD i 4K;
- testować skalowanie 100%, 125%, 150% i 200%.

### 6.6. Brak zapisu układu workspace

Po restarcie nie są odtwarzane rozmiary docków i splitterów, pozycja okna ani ostatnia zakładka.

**Zalecenie:** użyć `QSettings`, `saveGeometry()`, `QMainWindow.saveState()` oraz `QSplitter.saveState()`.

### 6.7. Wykres analizatora widma

Profesjonalny wykres powinien oferować:

- zoom, pan i reset widoku;
- crosshair z odczytem X/Y;
- marker oraz delta marker;
- peak search i tabelę pików;
- max hold, min hold i clear hold;
- wybór widocznych trace;
- eksport PNG, SVG i CSV;
- skalę liniową/logarytmiczną, jeśli ma sens dla danego wyniku;
- widoczne RBW, VBW, detector, sweep time, points i averaging;
- informację, czy widmo jest Raw, Averaged, Reference czy Processed;
- ostrzeżenie o clippingu i invalid points.

### 6.8. Komunikaty i dostępność

Rutynowe błędy formularza są często pokazywane jako modalny `QMessageBox`. Powoduje to przerywanie pracy operatora.

**Zalecenie:**

- walidację pól pokazywać inline;
- używać nieblokujących bannerów/toastów dla typowych błędów;
- pozostawić modalne okna dla ARM, OUTPUT ON, E-STOP i krytycznych zmian profilu;
- nie opierać stanu wyłącznie na kolorze;
- dodać ikony, tekst i accessible name/description;
- określić logiczną kolejność Tab oraz skróty klawiaturowe.

## 7. Rekomendowany framework i design system

### 7.1. Pozostać przy PySide6/Qt Widgets

Port do webowego frontendu nie poprawi komunikacji VISA ani bezpieczeństwa. Doda serwer lokalny, IPC, kolejną warstwę stanu i dodatkowe scenariusze awarii.

Qt Widgets pozostaje odpowiednim rozwiązaniem dla lokalnej aplikacji laboratoryjnej. Największy zwrot da refaktoryzacja architektury, design system i wymiana biblioteki wykresów.

### 7.2. Wykresy: PyQtGraph

Aktualna aplikacja używa `QtCharts`, który od Qt 6.10 jest modułem deprecated. Qt rekomenduje Qt Graphs, ale jego 2D API jest silnie związane z Qt Quick/QML i nie jest prostą wymianą dla obecnego QWidget UI.

Rekomendowany kierunek dla aplikacji laboratoryjnej:

- **PyQtGraph** dla live spectrum, wyników i podglądu waveform;
- NumPy jako źródło danych;
- `clipToView`;
- downsampling `peak`;
- własne markery i warstwy trace.

### 7.3. Motyw i wygląd

Zalecany jest własny lekki design system oparty na semantycznych tokenach:

```text
surface
surface_raised
panel
border
text_primary
text_muted
accent
success
warning
danger
output_active
focus_ring
```

Następnie tokeny powinny zasilać `QPalette`, pliki QSS oraz kolory wykresów. Zamiast dwóch dużych stringów QSS w `app/main.py` należy użyć osobnych zasobów motywu.

Powinny istnieć tryby:

- Light;
- Dark;
- System — reagujący na `QStyleHints.colorScheme`.

`qdarktheme` może być wykorzystany jako baza, ale nie zastąpi kompletnego design systemu.

QFluentWidgets wygląda nowocześnie, ale wymaga GPLv3 albo licencji komercyjnej. Nie powinien być dodawany bez świadomej decyzji licencyjnej.

## 8. Docelowa koncepcja UI

### 8.1. Główna nawigacja

Zamiast szerokiego paska wielu podobnych zakładek warto zastosować kompaktową nawigację boczną:

```text
Dashboard
Devices
  Rigol
  Keithley
  Anritsu
Automation
  Recipes
  Execution
Data
  Results
Settings
```

Górny pasek powinien zawsze pokazywać:

- stan połączeń wszystkich urządzeń;
- profil `APPROVED/LOCKED`;
- aktywne wyjścia;
- stan receptury;
- globalny E-STOP.

### 8.2. Strona urządzenia

Każda strona urządzenia powinna mieć ten sam szkielet:

1. kompaktowy Device Header;
2. status połączenia i IDN;
3. per-channel status/output/compliance;
4. lewy, zwijany panel konfiguracji;
5. główny obszar wykresu lub mierników;
6. pasek bezpiecznych akcji;
7. sekcję Diagnostics/Raw commands dostępną tylko w trybie eksperckim.

### 8.3. Hierarchia przycisków

- jeden wyraźny primary action na sekcję;
- OUTPUT ON i E-STOP zawsze rozróżnione kolorem, ikoną i tekstem;
- typowe akcje jako małe przyciski z ikonami;
- akcje niebezpieczne nie mogą zmieniać położenia po dynamicznej zmianie formularza;
- stan `busy` powinien blokować powtórne wysyłanie tej samej komendy.

## 9. Braki w testach

Należy dodać:

### P0 — safety tests

- przekroczenie prądu, napięcia i mocy zawsze wyłącza Keithleya;
- power preflight blokuje ARM;
- kanał A ON i kanał B OFF daje poprawny stan globalny;
- E-STOP próbuje wyłączyć wszystkie kanały mimo błędu jednego urządzenia;
- RF attenuation/preamp są ustawione i odczytane przed akwizycją.

### P0 — storage tests

- writer nigdy nie nadpisuje istniejącego pliku;
- awaria w każdej fazie `append()` pozostawia wykrywalny `incomplete` point;
- wynik otwiera się przez PyThat;
- golden-file manifest jest zgodny z plikiem referencyjnym;
- widma i metadane zachowują wymiary oraz jednostki.

### P1 — numerical tests

- property-based tests parsera jednostek, w tym `1e9`, `kHz`, `MHz`, prefiksy i wartości graniczne;
- NaN/Inf są zawsze odrzucane;
- downsampling zachowuje wąski pik;
- averaging nie używa dwóch identycznych identyfikatorów ramki;
- referencyjne odejmowanie zgłasza ujemny residual power.

### P1 — GUI tests

- screenshot regression dla light/dark/system;
- rozmiary 1024×768, 1366×768, 1920×1080 i 4K;
- skalowanie DPI;
- legenda Anritsu pozostaje widoczna po zmianie motywu;
- docki i splittery odtwarzają stan;
- pełna obsługa klawiaturą.

### P2 — Hardware-in-the-Loop

Testy HIL powinny wymagać jawnej zmiennej środowiskowej i osobnego profilu laboratoryjnego. Domyślnie wyjścia pozostają OFF. Należy testować readback, timeouty, niepoprawne odpowiedzi oraz odłączenie kabla w trakcie operacji.

## 10. Rekomendowana kolejność wdrożenia

### Etap P0 — bezpieczeństwo i integralność danych

1. Keithley safety trip i wykorzystanie `stop_on_overpower`.
2. Power preflight przed ARM.
3. Per-channel state dla Keithleya i Rigola.
4. Aktywne wymuszanie konfiguracji RF Anritsu.
5. Writer Thatec/PyThat.
6. Ochrona przed nadpisaniem i transakcyjny zapis punktów.

### Etap P1 — poprawność pomiarowa

1. Świeży sweep dla każdej próbki averagingu.
2. Zsynchronizowane I/V Keithleya.
3. Walidacja NaN/Inf.
4. Poprawna semantyka operacji referencyjnych.
5. Peak-preserving downsampling.
6. Rejestrowanie RBW, VBW, detectora, sweep time i całej proweniencji trace.

### Etap P2 — architektura

1. Rozbicie `main_window.py` na strony, widgety i viewmodele.
2. Jeden Instrument Manager i jawne ownership sesji VISA.
3. Łączenie tylko urządzeń wymaganych przez recepturę.
4. Ujednolicenie stanów operacji, błędów i busy indicators.
5. Wydzielenie warstwy storage i Thatec mappera.

### Etap P3 — profesjonalne UI

1. Design tokens i modularne QSS.
2. PyQtGraph.
3. Responsywne splittery i panele collapsible.
4. Zapisywanie workspace przez QSettings.
5. Kompaktowa nawigacja i przyciski.
6. Markery, peak table, hold traces i eksport.
7. Dostępność i testy wizualne.

## 11. Kryteria gotowości do pracy z rzeczywistym DUT

Aplikację można uznać za gotową do kwalifikowanych pomiarów, gdy:

- każdy software safety trip automatycznie wyłącza odpowiednie wyjścia;
- ograniczenie mocy jest sprawdzane przed włączeniem oraz podczas pomiaru;
- GUI zawsze pokazuje prawdziwy stan obu kanałów;
- wszystkie operacje Anritsu używają zakwalifikowanych komend i pełnego readbacku;
- każdy trace pochodzi z jednoznacznie zakończonego sweepu;
- wynik HDF5 przechodzi test zgodności PyThat;
- pliki nie mogą zostać przypadkowo nadpisane;
- testy awarii zapisu i awarii komunikacji przechodzą;
- wykres nie może ukryć ekstremum przez downsampling;
- przeprowadzono HIL z bezpiecznym obciążeniem testowym;
- operator otrzymuje jednoznaczny, trwały zapis wszystkich alarmów i zmian stanu.

## 12. Konkluzja

Najlepszym kierunkiem jest dalszy rozwój aplikacji jako lokalnego programu **PySide6/Qt Widgets**, bez przepisywania na web. Priorytetem nie powinien być teraz dodatkowy framework wizualny, lecz poprawienie ścieżek bezpieczeństwa i integralności danych, rozbicie monolitycznego UI oraz zastosowanie biblioteki wykresów przeznaczonej do danych naukowych.

Po wykonaniu P0 i P1 można bezpiecznie przejść do pełnego redesignu. Wtedy połączenie modularnego Qt Widgets, semantycznego design systemu oraz PyQtGraph zapewni profesjonalny, responsywny interfejs bez zwiększania ryzyka w warstwie sterowania urządzeniami.

## 13. Stan realizacji zaleceń

**Aktualizacja:** 2026-07-16

Zrealizowano programowo:

- fail-safe trip, power preflight oraz per-channel state Keithleya;
- per-channel state i limit estymowanego prądu oraz mocy DUT Rigola;
- atomową parę I/V przez `measure.iv()`;
- timeout VISA obejmujący samo `*OPC?` Anritsu;
- świeży single sweep dla każdej próbki averagingu i inkrementalne uśrednianie mocy;
- walidację NaN/Inf, tolerancję osi, semantykę nieważnego residual power i peak-preserving display;
- zapis Thatec/PyThat, ochronę przed nadpisaniem, transakcyjne checkpointy, trwałe zdarzenia awarii i test round-trip przez PyThat;
- łączenie wyłącznie urządzeń wymaganych przez plan oraz ograniczony shutdown workerów;
- PyQtGraph dla Rigola, Anritsu i Results: zoom, pan, crosshair, peak marker, delta marker, max/min hold oraz eksport CSV/PNG/SVG;
- Light, Dark i reagujący na system tryb System;
- boczną nawigację, stałą belkę profilu/urządzeń/E-STOP, responsywne splittery i zapis workspace;
- inline notification banners dla rutynowych błędów formularzy, accessible names oraz skróty klawiaturowe;
- bezpieczne wykrywanie VISA przez samo `*IDN?`, przypisywanie adresów i atomowy zapis konfiguracji;
- pełny lint Ruff, kompilację modułów oraz rozszerzony zestaw testów automatycznych.

Pozostają celowo niewdrożone bez kwalifikacji sprzętowej:

- sprzętowy bit/rejestr compliance Keithley dla konkretnego modelu i firmware — działa konserwatywny fallback pomiarowy;
- wymuszanie i readback attenuation/preamp Anritsu — oficjalny podręcznik potwierdza funkcje, lecz komendy muszą zostać zakwalifikowane na używanym trybie SCPI/Native i firmware;
- testy Hardware-in-the-Loop, odłączenie kabla oraz pomiar z bezpiecznym obciążeniem wzorcowym;
- zatwierdzenie laboratoryjne limitu `estimated_load_power` Rigola; profil pozostaje `unverified` do decyzji operatora.

Powyższych punktów nie wolno uznać za zakończone wyłącznie na podstawie symulatora. Są kryteriami kwalifikacji stanowiska, nie brakującą implementacją możliwą do bezpiecznego odgadnięcia w kodzie.
