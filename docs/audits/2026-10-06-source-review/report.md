# Audyt źródeł sweeps — 2026-10-06

> Ten raport opisuje wersję źródeł z chwili audytu. Późniejsze zmiany,
> sprawdzenia i ograniczenia są rozliczane w [repairs.md](repairs.md).
> Historyczne hashe w coverage.json nie opisują plików po naprawach.

**Wniosek: obecnego kodu nie kwalifikuję jako gotowego do dużego sweepa produkcyjnego.** Analiza ujawniła problemy sterowania, kwalifikacji widm, recovery danych i obciążenia GUI. Wcześniejsze pozytywne wyniki testów nie wystarczają do kwalifikacji tych ścieżek.

## Zakres i metoda

Przeczytano w całości **210 plików, 91 026 linii**. [coverage.json](coverage.json) zawiera ścieżki, SHA256, liczby linii, przeczytane zakresy oraz notatki. Końcowe porównanie hashy nie wykazało zmian źródeł względem wersji czytanych. Liczba linii obejmuje komentarze i puste wiersze; nie jest pokryciem zachowania.

Zakres obejmuje receptury i edytor, kompilator, runner, recovery, providery, adaptery Anritsu/Keithley/Rigol/MOKE i powiązany Lake Shore, transport, symulację, bezpieczeństwo, ustawienia, akwizycję/przetwarzanie widm, zapis i odczyt HDF5/CSV, Execution oraz powiązane strony, dialogi i przekazywanie stanu przez główne okno.

Jest to pełna lektura **jawnej listy zakresu**, nie całego repozytorium. Biblioteki zewnętrzne oraz cały osobny backend charakteryzacji Keithley, inventory i integracji sieciowych nie zostały przeczytane w całości. Karta charakteryzacji została przeczytana jako zależność strony Keithley; nie oznacza to audytu jej całego odrębnego silnika pomiarowego.

Nie wysyłano komend do aparatury, nie wykonywano pomiarów ani pełnego zestawu testów. Wykonano cztery małe reprodukcje bez sprzętu. Nie zmieniono kodu produkcyjnego: ten dokument rozlicza analizę źródeł, a nie ukończone naprawy.

## Klasyfikacja

- **P1:** blokuje rekomendację dużego pomiaru — sterowanie, poprawność danych lub istotna dostępność.
- **P2:** błąd funkcjonalny, prezentacji lub wydajności wymagający naprawy.
- **R:** odtworzono bez sprzętu; **S:** prześledzono w źródłach, bez pełnego scenariusza; **H:** hipoteza wymagająca dodatkowego sprawdzenia.

## Ustalenia priorytetowe

### A01 — P1 / R: wczesny Stop ginie

`app/ui/run_worker.py:683`, `RunWorker.request_stop`, działa tylko, gdy istnieje `_runner`. Tworzony i sprawdzany `_early_stop_requested` nie jest tu ustawiany. Reprodukcja z `_runner=None` pozostawiła flagę False.

Stop podczas przygotowania nie zapamiętuje żądania zakończenia. Nie odtwarzano fizycznego ON po Stop. Naprawa wymaga trwałej flagi od początku życia workera, kontroli przed kolejnymi fazami inicjalizacji i przekazania anulowania nowemu runnerowi. Scenariusz musi obejmować połączenie i otwieranie archiwum.

### A02 — P1 / R: proxy GUI pomija kwalifikację konfiguracji Anritsu

`app/engine/runner.py:1935`, `_read_spectrum_identity`, wymaga `isinstance(..., AnritsuAdapter)`. GUI przekazuje `RunDeviceAdapter`, który nie spełnia tego warunku. Reprodukcja zwróciła None.

Zwykła ścieżka GUI nie uzyskuje fingerprintu potrzebnego do kontroli konfiguracji przed/po akwizycji; import/recovery referencji również zależy od tego odczytu. Test bezpośredniego adaptera nie sprawdza tej ścieżki. Naprawa: jawny kontrakt możliwości proxy zamiast rozpoznawania konkretnej klasy oraz test GUI → proxy → runner → fake transport.

### A03 — P1 / S: timeout nie anuluje operacji w kolejce

`app/ui/workers.py:520`, `call_for_run`, przestaje czekać, lecz pozostawia `_RunCall` wykonalny. Cała akwizycja może trwać dłużej niż limit oparty na timeout pojedynczej komunikacji. Sweep używa proxy bez nabycia dostępnego lease kolejki; blokada formularzy nie unieważnia wcześniej zakolejkowanych wywołań ręcznych.

Timeout nie dowodzi, że mutacja już się nie wykona. Potrzebne są właściciel/generacja runu, anulowanie niewystartowanych operacji oraz osobne stany timeoutu oczekiwania i operacji w toku. Nie powtarzać mutacji w ciemno.

### A04 — P1 / R: parser myli wielkość prefiksów SI

`app/domain/quantities.py:125`, `_canonical_unit`:

| Wejście | Uzyskane SI | Problem |
|---|---:|---|
| `1 MV` | 0.001 V | mega jako milli |
| `1 MA` | 0.001 A | mega jako milli |
| `1 mHz` | 1 000 000 Hz | milli jako mega |

Naprawa: zachować wielkość liter symboli SI; nieobsługiwane symbole odrzucać. Dotyczy też wartości importowanych z YAML.

### A05 — P1 / S: recovery pomija role publiczne HDF5

`app/storage/thatec_writer.py:153` i `app/storage/hdf5_writer.py:329`: zapis obsługuje `requested`, `applied`, `readback`, lecz odtwarzanie indeksów i obcinanie publicznych danych nie obejmuje ich tak jak `setpoint`/`measurement`. Po wznowieniu może zostać niezacommitowany ogon lub powstać duplikacja.

Naprawa: wspólny rejestr ról dla zapisu, resume, truncation i czytników; fault injection na etapach checkpointu. Poprawny plik z nieprzerwanego runu nie weryfikuje recovery.

### A06 — P1 / S: append może pozostawić dane poza rollbackiem

`app/storage/thatec_writer.py:487` i `:603`: resize datasetów poprzedza część walidacji, a rejestracja zakończonego append następuje po powrocie metody. Analogiczne ryzyko dotyczy częściowego wiersza skalarnego.

Naprawa: walidacja przed resize i zapamiętanie starych rozmiarów przed pierwszą mutacją; rollback również przy wyjątku wewnątrz append.

### A07 — P1 / S: logger potwierdza trwałość mimo błędu

`app/audit/logger.py:103`: pętla zapisu pochłania wyjątki write/flush/fsync, po czym ustawia potwierdzenie. Oczekiwanie nie propaguje nieudanego timeoutu. Blokujące `queue.put` może zatrzymać producenta w GUI.

Naprawa: przekazywać wynik zapisu i stan trwałości, połączyć awarię loggera ze stanem stacji; sam event potwierdzenia nie jest dowodem sukcesu.

### A08 — P1 / S: Rigol uznaje draft za potwierdzony stan

`app/devices/rigol_dg1000z/ui/page.py:1037`, `_record_visible_quick_readback`, rekonstruuje pełny config z formularza po pojedynczym readbacku/zmianie. Niezastosowana faza, load lub waveform może zostać uznana za confirmed carrier. Cache wpływa na decyzję o pominięciu configure przed OUTPUT i na metadata.

Naprawa: potwierdzać tylko pola rzeczywiście objęte odpowiedzią, z czasem/generacją; oddzielić draft/requested/applied/readback.

### A09 — P2 / R+S: snapshot ponownie tworzy pomiar w GUI

`app/devices/keithley_2600/ui/page.py:2975` przetwarza ostatni pomiar A/B przy każdym evencie ze snapshotem. W `:3984` dopisuje historię z bieżącym czasem GUI i przerysowuje wykres.

Reprodukcja: 10 identycznych snapshotów → 10 wywołań aktualizacji, jeden zestaw wartości. Użyto lekkiego odbiorcy, bez pomiaru FPS całego okna. Skutek: fałszywa świeżość/gęstość historii oraz niepotrzebne rysowanie. Rigol też ponownie renderuje cache kanałów i wyzwala sygnały preview.

Naprawa: dopisywać historię tylko dla nowego pomiaru, zachować czas akwizycji, koaleskować prezentację. Ograniczenie odświeżania UI nie może usuwać pomiarów z archiwum.

### A10 — P1/P2 / S: brak podstaw do obietnicy braku ukrytych zmian

- Keithley `adapter.py:1235`, `update_source_level`, wykonuje pojedynczą zmianę nastawy. To **nie jest programowa płynna rampa**. Przebiegu analogowego nie ustalono bez sprzętu.
- Rigol `adapter.py:1529`, `set_output`, przy ON przechodzi przez OFF i konfigurację. Ponowne ON nie jest neutralnym potwierdzeniem stanu.
- Pełna konfiguracja Rigola resetuje dodatkowe tryby/modulacje i jednostki; modal nie pokazuje wszystkich skutków.
- Source autorange Keithley pochodzi z Settings również w ścieżkach mających jawne pole receptury; trzeba rozstrzygnąć pierwszeństwo.
- Lokalny `output_policy: on` może wygenerować końcowe OFF niewidoczne w odpowiednim semantic tree.
- `load_plan_actions` w edytorach Keithley i Rigol nie odtwarza wartości jawnego Set z `action['value']`; zapis korzysta z formularza.

Naprawa: wspólny opis operacji dla kompilatora, drzewa i diffu UI; oddzielne operacje częściowe i pełne konfiguracje. Zweryfikować dokładny dziennik komend dla sąsiednich punktów oraz granic pętli.

## Pozostałe problemy

Pełne notatki znajdują się w [rejestrze lektury](findings-in-progress.md). Zachowano nazwę roboczą, ponieważ zawiera też historię hipotez i ich odrzucania. Poniżej skrót analizy statycznej:

| Obszar | Ustalenie |
|---|---|
| Cleanup | Odczyt `connected` poza lokalnym try może przerwać obsługę dalszych urządzeń; brak wspólnego deadline kończenia. |
| Kompilator | Nieboolowskie `condition` może dostać pierwszeństwo nad porównaniem; niepełna kontrola target/endpoint/parameter Rigola. |
| Edytor | Cache preflight pomija generację Settings; edycja może utracić disabled; clone zachowuje managed acquisition ID; legacy Add wstawia pełne konfiguracje przy punktach. |
| Estymacja | Różne założenia liczby punktów i rezerwy ramek tła powodują rozjazd czasu/pamięci/dysku. |
| Anritsu | Cache osi może przeżyć zmianę front panelu z tą samą liczbą punktów; Live nie dowodzi niezależnego pełnego sweepa. |
| Keithley | Measure-only zachowuje stan źródła; dalsze enable wymaga dokładnego fake-transport scenariusza. Obsługa potwierdzenia OFF i compliance ma niespójne ścieżki błędów. |
| MOKE | Powtarzana walidacja całej trajektorii daje koszt kwadratowy; rozbieżne tolerancje protokołu i zakończenia rampy mogą powodować timeout. |
| Dry run/symulacja | Sim_ack przy fizycznym dry run jest mylącym pochodzeniem dowodu; tolerancyjny symulator nie kwalifikuje SCPI sprzętu. |
| Storage | Cache walidacji siatki tylko po id; starsze ścieżki referencji nie są transakcyjne; finalny PyThat może materializować duże dane w RAM. |
| Czytniki | Niejednolity committed limit, fallback brakującego Y i etykiety W/dBm mogą zniekształcić interpretację. |
| Ręczny zapis | Hardcoded simulation=False, metadata z chwili konfiguracji dialogu, możliwość zapisania zredukowanego preview jako raw bez odpowiedniej proweniencji. |
| Korekcja | Uśrednianie na stronie Anritsu nie sprawdza generacji/osi każdej ramki; fallback wariantu śladu może nie zgadzać się z metadata. |
| Wykresy | Równomierna decymacja może zgubić wąski pik; usuwanie NaN łączy luki; linear ratio nie pasuje do różnicy W. |
| GUI Anritsu | Ręczny HDF5, ponowna detekcja pików i składanie spektrogramu pozostają w GUI; zamknięcie workera może czekać bez ograniczenia. |
| GUI Results/MOKE | Materializacja całych serii/drzew i hashowanie kalibracji w GUI mogą blokować interfejs. |
| Dialogi | Anritsu Assign oznacza MATCH bez potwierdzenia przyjęcia; start/stop w trybie center/span ma niewłaściwą semantykę przypisania. |
| Cache UI | Wybrane readback/metadata przeżywają disconnect lub następny run; brak wieku odczytu sugeruje aktualność. |

## Ostatnie pliki Keithley — dodatkowe ustalenia

- `_pending_channels` ma jeden wpis na operację, nie na każde żądanie. Przypisanie odpowiedzi compliance do A/B przy wielu wywołaniach wymaga reprodukcji (**H**).
- `_result('configure')` kontynuuje ON, jeśli pozostało `_auto_enable_channel`. Ręczne OFF musi anulować tę kontynuację; potrzebny scenariusz kolejności odpowiedzi (**H**).
- Proxy przełącznika compliance mapuje False na warn_clamp. Aktualizacja comboboxa na skip, a potem proxy na False może pokazać inny stan niż wewnętrzny (**S**, potrzebna regresja renderowania).
- Karta charakteryzacji czyta konfigurację i potwierdza OFF synchronicznie z handlera Start. Analiza, CSV, hash i pojedynczy PDF też działają z GUI. Raport serii pól ma worker — nie przypisywać obu ścieżkom tego samego problemu.
- Podgląd R w charakteryzacji zastępuje nieokreślony wynik poprzednim R albo zerem. Powinien oznaczać lukę/status, zamiast tworzyć pozorny pomiar.
- `closeEvent` karty po wait(2000) przechodzi do zamknięcia bez sprawdzenia wyniku wait. Główne prepare_application_shutdown ma dodatkowe zabezpieczenia; nie wykazano awarii przy każdym zamknięciu.
- Pozostały teksty sugerujące możliwość 4-wire. Odczytane ograniczenia źródła/edytora wymuszają 2-wire; sam tekst nie dowodzi dopuszczenia 4-wire. Usunąć sprzeczną instrukcję UI.

## Odrzucone hipotezy i poprawne mechanizmy

- Logger ma wątek zapisu; problemem są pełna kolejka i propagacja błędów, nie całkowicie synchroniczny zapis.
- RunController.start sprawdza zatrzask E-STOP; hipotezę jego obejścia przez samo Resume odrzucono.
- VID/POW i VBW auto/manual/off są różnymi ustawieniami.
- SpectrumWorkbench ma kontrolę siatki hold; problem bazowego widgetu nie dotyczy automatycznie wszystkich użyć.
- Odejmowanie tła w mocy liniowej i zachowanie podpisanej reszty są właściwe; ujemnego wyniku nie należy ucinać do zera tylko dla wyglądu.
- Nowe ścieżki finalizacji/przetwarzania kontrolują hashe źródeł i tworzą oddzielne wyniki. Część nowych dialogów poprawnie korzysta z workerów.

## Wykonane reprodukcje i granice dowodu

[reproductions.json](reproductions.json): wczesny Stop, fingerprint przez proxy, trzy prefiksy SI oraz ponowne przetwarzanie snapshotu Keithley. Były to wywołania metod na lekkich obiektach bez fizycznego transportu, nie testy renderowania Windows lub kwalifikacja aparatury.

Nie rozstrzygnięto czasów VISA, analogowego przebiegu ramp, geometrii wszystkich modali ani odporności na fizyczne odcięcie zasilania. Ustalenia S/H nie są deklaracją odtworzenia awarii sprzętowej.

## Kolejność napraw i kwalifikacji

1. Wczesny Stop, kolejka/lease, fingerprint przez proxy i parser SI; sprawdzić brak późnych mutacji po Stop.
2. Transakcje HDF5/recovery i propagacja awarii audytu; fault injection podczas częściowego zapisu.
3. Oddzielenie draft od readback oraz widoczność wszystkich skutków komend w planie; dokładne sekwencje dla sąsiednich punktów.
4. Deduplikacja pomiarów i renderowania; pozostałe obliczenia/I/O poza GUI. Pomiar opóźnienia pętli zdarzeń podczas reference/spectrum.
5. Mały Anritsu-only z porównaniem zapisanych raw/background/reference i metadata, potem mały plan wielourządzeniowy. Dopiero po naprawach i kwalifikacji fizycznej aparatury — duży sweep.
