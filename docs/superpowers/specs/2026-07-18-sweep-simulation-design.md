# Projekt wdrożenia pełnej symulacji SWEEP

**Data:** 2026-07-18

**Status:** projekt zatwierdzony kierunkowo, przekazany do przeglądu przed przygotowaniem planu implementacji

**Zakres urządzeń:** Rigol DG1000Z, Keithley 2600, Anritsu MS2830A, MOKE Box

**Poza zakresem:** Lake Shore Gaussmeter, kwalifikacja HIL i potwierdzenie zgodności z fizycznym stanowiskiem

## 1. Cel

Celem jest uruchomienie kompletnego planu SWEEP bez dostępu do sprzętu i przeprowadzenie
go przez tę samą ścieżkę aplikacji, która jest używana podczas prawdziwego pomiaru:

1. zbudowanie lub otwarcie planu w zakładce **Sweeps**;
2. kompilacja i preflight;
3. wykonanie planu przez `RunWorker` i `RecipeRunner`;
4. użycie symulowanych adapterów wszystkich urządzeń wymaganych przez plan;
5. wizualne potwierdzenie zastosowanych konfiguracji oraz odebranych danych;
6. zapis kompletnego, samowystarczalnego pliku HDF5;
7. walidacja kontraktu thaTEC/PyThat;
8. otwarcie danych w zakładce **Results** przy użyciu `PyThat.MeasurementTree`.

Symulacja ma potwierdzać integrację modułów i kompletność danych. Nie ma udowadniać
poprawności fizycznej urządzeń, protokołów SCPI/TCP ani toru pomiarowego.

## 2. Stan obecny

Repozytorium ma już większość potrzebnych elementów:

- `lab-control --simulate` tworzy izolowany profil i nie łączy się ze sprzętem;
- Rigol, Keithley i Anritsu mają deterministyczne symulatory VISA;
- Anritsu zwraca niepłaskie widmo;
- Keithley symuluje rezystancyjny DUT i compliance;
- istnieje pełny przebieg testowy 100 × 20 zapisujący 2000 widm;
- `Hdf5RunWriter.close()` wykonuje walidację z `require_pythat=True`;
- `ResultsPage` potrafi wyświetlić metadane, punkty i widmo przez `Hdf5RunReader`.

Braki względem celu:

- MOKE Box jest jawnie niedostępny w trybie symulacji;
- symulatory nie korzystają ze wspólnego, zapisywanego seedu;
- aktualny checkpoint przechowuje osie, pomiary i część `safety_context`, ale nie ma
  formalnego kontraktu pełnego stanu zastosowanej konfiguracji urządzeń;
- parametry stałe urządzeń nie są jawnie weryfikowane pod kątem kompletności;
- Run Monitor nie pokazuje jednolicie `requested → readback` dla wszystkich urządzeń;
- Results używa PyThat do walidacji przy zamknięciu pliku, ale nie używa
  `MeasurementTree` jako źródła danych pomiarowych w interfejsie.

## 3. Ustalone decyzje

1. Symulacja obejmuje Rigol, Keithley, Anritsu i MOKE Box.
2. Lake Shore pozostaje poza zakresem.
3. Używamy istniejącego globalnego trybu `--simulate`.
4. Nie tworzymy osobnego syntetycznego runnera ani generatora gotowych plików HDF5.
5. Te same kompilator, preflight, worker, runner, writer i Results obsługują sprzęt oraz
   symulację.
6. Dane są pseudolosowe i odtwarzalne.
7. Seed jest zapisywany w HDF5.
8. Kompletność punktu wynika z planu. Urządzenie nieużywane przez plan nie jest
   sztucznie odpytywane przy każdym checkpointcie.
9. Receptura akceptacyjna używa wszystkich czterech urządzeń.
10. Parametry stałe nie stają się sztucznymi osiami PyThat.
11. Results otwiera osie i dane pomiarowe przez PyThat. Reader natywny pozostaje
    źródłem metadanych technicznych, zdarzeń oraz danych recovery.

## 4. Architektura

### 4.1. Jedna ścieżka wykonania

Przepływ pozostaje wspólny dla sprzętu i symulacji:

```text
Sweep document
  -> RecipeCompiler
  -> preflight / PlanEstimator
  -> RunWorker
  -> adaptery urządzeń
  -> RecipeRunner
  -> Hdf5RunWriter
  -> ThatecCompatibilityValidator + PyThat
  -> ResultsPage
```

Różnica występuje wyłącznie na granicy adaptera/transportu urządzenia oraz w metadanych
runu. Dzięki temu symulacja sprawdza rzeczywiste kontrakty modułów, a nie równoległą,
uproszczoną implementację.

### 4.2. `SimulationContext`

Każdy nowy run symulacyjny otrzymuje jeden `SimulationContext`:

```python
@dataclass(frozen=True, slots=True)
class SimulationRunConfig:
    seed: int
    model_version: str
    time_scale: float


class SimulationContext:
    config: SimulationRunConfig

    def random_stream(self, device_key: str, stream_key: str) -> random.Random: ...
```

Seed strumienia urządzenia jest wyprowadzany przez SHA-256 z:

```text
root seed + model version + device key + stream key
```

Nie wolno używać w tym celu `hash()` Pythona, ponieważ jego wynik nie jest stabilny
między procesami. Osobne strumienie powodują, że dodatkowy odczyt Rigola nie zmienia
późniejszych wartości Keithleya, widm Anritsu ani próbek MOKE.

Domyślnie aplikacja generuje nowy 64-bitowy seed przy rozpoczęciu runu. Operator może
podać seed jawnie, aby odtworzyć poprzedni przebieg. Seed jest pokazany w Run Monitor
i Results oraz zapisany w HDF5.

### 4.3. Zakres odpowiedzialności symulatora

Symulator:

- przyjmuje te same polecenia wysokiego poziomu co fizyczny adapter;
- zachowuje stan skonfigurowanych parametrów;
- zwraca readback, jeżeli dany adapter ma taki kontrakt;
- generuje skończone wartości w zakresie wynikającym z konfiguracji i limitów;
- generuje te same wartości dla tego samego seedu, planu i kolejności operacji;
- pozwala na deterministyczne fault injection w testach;
- nigdy nie otwiera fizycznego VISA, USB, serial ani TCP/IP.

Symulator nie udaje fizycznej kwalifikacji i nie może zmienić statusu profilu sprzętowego.

## 5. Pełny stan zastosowanej konfiguracji

### 5.1. Dlaczego same setpointy nie wystarczają

Jeżeli plan sweepuje tylko częstotliwość Rigola, HDF5 nadal musi opisać pełny stan
generatora użyty w danym punkcie: funkcję, częstotliwość, poziomy High/Low, offset,
load, polarity, output mode, gate, sync i pozostałe aktywne ustawienia.

Analogicznie:

- Keithley musi zachować tryb źródła, poziom, compliance, zakresy, sense, NPLC,
  settling i stan output, nawet gdy pomiarem jest tylko prąd;
- Anritsu musi zachować konfigurację osi, reference level, RBW, VBW, detector,
  attenuation, preamp, sweep time, tryb acquisition i parametry przetwarzania;
- MOKE musi zachować konfigurację toru odczytu oraz wartości Halla zapisane przy punkcie.

### 5.2. Kontrakt `AppliedDeviceState`

Każda akcja konfigurująca lub aktualizująca urządzenie aktualizuje typowany snapshot:

```python
@dataclass(frozen=True, slots=True)
class AppliedParameter:
    parameter_id: str
    requested_value: bool | int | float | str | None
    actual_value: bool | int | float | str | None
    unit: str | None
    verification: Literal["readback", "simulated_ack", "command_only"]


@dataclass(frozen=True, slots=True)
class AppliedDeviceState:
    device_key: str
    revision: int
    applied_at_utc: str
    parameters: tuple[AppliedParameter, ...]
```

Znaczenie sposobu potwierdzenia:

- `readback` — adapter wykonał obsługiwany odczyt wartości;
- `simulated_ack` — symulator przyjął polecenie, ale kontrakt fizycznego urządzenia nie
  przewiduje readbacku;
- `command_only` — wartość jest jedynie zapisem wysłanego polecenia; UI nie może
  przedstawiać jej jako odczytanej.

Symulacja nie może zamieniać `command_only` na fałszywy fizyczny readback. Wizualizacja
ma jawnie pokazywać rodzaj potwierdzenia.

### 5.3. Ledger stanu w runnerze

`RecipeRunner` utrzymuje ostatni kompletny `AppliedDeviceState` każdego urządzenia
wymaganego przez plan. Aktualizacja pojedynczej osi, na przykład częstotliwości, tworzy
nową rewizję pełnego snapshotu Rigola; nie usuwa pozostałych parametrów.

Przy checkpointcie zapisywane są:

1. współrzędne/setpointy punktu;
2. pomiary skalarne;
3. widmo lub inne dane akwizycji;
4. pełne snapshoty urządzeń obowiązujące w tym punkcie;
5. metadane bezpieczeństwa i przetwarzania.

Checkpoint pozostaje samowystarczalny i atomowy.

## 6. Modele urządzeń

### 6.1. Rigol

Rigol jest przede wszystkim źródłem konfiguracji, a nie urządzeniem pomiarowym.
Symulator ma:

- przechowywać pełny stan obu kanałów;
- obsługiwać istniejące konfiguracje carrier i output;
- zwracać readback tych parametrów, które czyta fizyczny adapter;
- potwierdzać punktową aktualizację częstotliwości lub poziomów;
- publikować po każdej zmianie pełny `AppliedDeviceState`;
- zachowywać informację o output, modulation, sweep, burst, load, polarity, gate i sync.

W recepturze akceptacyjnej tylko częstotliwość jest osią SWEEP. Pozostałe parametry są
stałymi indicators/metadanymi i muszą pozostać obecne w HDF5 oraz Results.

### 6.2. Keithley

Keithley łączy konfigurację źródła z wynikami pomiaru. Symulator ma:

- zachowywać pełną konfigurację kanałów A/B;
- generować I/V/P na podstawie ustawionego trybu, poziomu i modelu DUT;
- dodawać ograniczony, pseudolosowy szum readbacku;
- respektować compliance i istniejącą logikę bezpiecznego wyłączenia;
- przechowywać requested oraz actual dla ustawionego poziomu i compliance;
- zapisywać wszystkie aktywne parametry konfiguracji, a nie tylko sweepowany poziom.

Wyniki dla tego samego seedu muszą być identyczne. Dla różnych seedów muszą się różnić,
pozostając skończone i w dozwolonych granicach.

### 6.3. Anritsu

Symulator Anritsu ma generować kompletne widmo:

- ściśle rosnącą oś częstotliwości zgodną ze start/stop/points;
- tło szumowe;
- co najmniej jeden wyraźny pik;
- niewielką zmianę kolejnych ramek wynikającą z seedu i indeksu akwizycji;
- wartości zgodne z reference level i bez `NaN`/`Inf`;
- dane raw oraz processed zgodnie z operacją reference;
- pełną konfigurację base i advanced w snapshotach.

Widmo ma być wizualnie rozpoznawalne jako odebrany sygnał, a nie płaska lub stała tablica.

### 6.4. MOKE Box

Tryb symulacji otrzymuje działający adapter/transport MOKE zamiast
`UnavailableMokeBoxAdapter`. Symulator ma:

- zwracać syntetyczną tożsamość urządzenia;
- obsługiwać operacje odczytu używane przez plan;
- generować napięcie Halla, pole, odchylenie i surowe kody ADC;
- zachowywać skonfigurowane gainy i VOUT, jeżeli plan ich używa;
- nie otwierać socketu TCP;
- publikować pełny `AppliedDeviceState`.

Profil in-memory w symulacji włącza MOKE wyłącznie dla syntetycznego endpointu. Nie
modyfikuje `.config/settings.yml` i nie oznacza protokołu fizycznego jako zakwalifikowany.

## 7. Kontrakt danych planu

Kompilator tworzy wraz z `ExecutionPlan` jawny `RunDataContract`:

```python
@dataclass(frozen=True, slots=True)
class DeviceDataRequirement:
    device_key: str
    required_state_parameters: frozenset[str]
    required_measurements: frozenset[str]


@dataclass(frozen=True, slots=True)
class RunDataContract:
    devices: tuple[DeviceDataRequirement, ...]
    sweep_axes: tuple[str, ...]
    expected_points: int
    expected_spectra: int
```

Kontrakt wynika z planu, a nie z globalnej listy urządzeń. Przykładowo:

- Rigol użyty tylko jako źródło wymaga pełnego snapshotu konfiguracji, ale nie
  sztucznego pomiaru;
- `measure_keithley` wymaga wyników I/V/P i pól compliance;
- `measure_moke_hall` wymaga napięcia, pola, odchylenia i kodu raw;
- `acquire_spectrum` wymaga osi i widma o oczekiwanej liczbie punktów.

Brak wymaganego pola oznacza niekompletny checkpoint i błąd runu, a nie ostrzeżenie.

## 8. HDF5 i PyThat

### 8.1. Metadane symulacji

HDF5 otrzymuje `/run/simulation_json` z co najmniej:

```json
{
  "enabled": true,
  "seed": 123456789,
  "model_version": "1",
  "time_scale": 0.0,
  "devices": ["anritsu", "keithley", "moke_box", "rigol"]
}
```

W trybie sprzętowym `enabled` ma wartość `false`; pliki pozostają rozróżnialne bez
analizowania tekstu settings.

### 8.2. Snapshot punktu

Każda transakcja `_pending/<index>` otrzymuje `device_state_json`. Dataset jest
przenoszony razem z pozostałą częścią checkpointu do `/points/<index>`, dlatego
snapshot nie może zostać zapisany częściowo.

`device_state_json` zawiera pełne snapshoty wymaganych urządzeń. Powtórzenie części
stałych danych jest akceptowalne: prostota odczytu, samowystarczalność checkpointu
i niezawodne recovery mają pierwszeństwo przed minimalnym rozmiarem JSON. Kompresję
można zastosować, jeżeli wspiera ją używany typ datasetu bez pogorszenia kompatybilności.

### 8.3. Mapowanie PyThat

Mapowanie zachowuje następujące reguły:

- parametr sweepowany → coordinate/dimension;
- pomiar skalarny → data variable;
- częstotliwość analizatora → coordinate;
- widmo raw/processed → data variable;
- parametr stały urządzenia → indicator/metadane;
- techniczny snapshot requested/actual/verification → prywatna część HDF5 odczytywana
  przez `Hdf5RunReader`.

Parametr stały nie może tworzyć wymiaru długości jeden. Parametr zmienny, który nie jest
zdefiniowaną osią planu, pozostaje w historii stanu urządzenia i powoduje ostrzeżenie
walidatora, jeżeli nie ma jawnego mapowania publicznego.

### 8.4. Walidacja końcowa

Przed nadaniem statusu `completed` walidator sprawdza:

- liczbę punktów i widm względem `RunDataContract`;
- obecność wszystkich wymaganych urządzeń, IDN i capabilities;
- komplet wymaganych parametrów konfiguracji w każdym checkpointcie;
- komplet wymaganych pomiarów;
- brak `NaN` i `Inf`;
- monotoniczność osi widma;
- zgodność długości raw/processed;
- obecność seedu i wersji modelu dla symulacji;
- strukturalny manifest thaTEC;
- rzeczywiste otwarcie przez `PyThat.MeasurementTree`;
- zgodność wymiarów i nazw zmiennych PyThat z planem.

Nieudana walidacja ustawia status `faulted`, zapisuje `storage_validation_error`
i uniemożliwia przedstawienie runu jako poprawnie zakończonego.

## 9. Wizualne potwierdzenie

### 9.1. Run Monitor

Run Monitor otrzymuje sekcję stanu urządzeń. Dla każdego używanego urządzenia pokazuje:

- nazwę oraz znacznik `SIM`;
- status połączenia;
- numer rewizji konfiguracji;
- czas ostatniej aktualizacji;
- najważniejsze `requested → actual`;
- rodzaj potwierdzenia;
- liczbę zapisanych checkpointów;
- stan ostatniej akwizycji.

Anritsu nadal pokazuje podgląd widma. Rigol i Keithley pokazują ustawione parametry,
nawet jeżeli dana akcja niczego nie mierzy. MOKE pokazuje ostatni odczyt Halla.

Zdarzenie `device_state_applied` niesie skrócone dane do UI. Pełny snapshot trafia
bezpośrednio do writera i nie jest kopiowany do logu tekstowego.

### 9.2. Results

Results używa dwóch jawnych źródeł:

- `PyThat.MeasurementTree` — osie, wymiary, zmienne pomiarowe i widma;
- `Hdf5RunReader` — status, recipe/settings, operator, zdarzenia, simulation metadata,
  snapshoty urządzeń i recovery.

Widok wybranego punktu zawiera:

1. **Data** — wartości osi i measurements z PyThat;
2. **Device state** — pełne requested/actual/verification dla czterech modułów użytych
   przez plan;
3. **Spectrum** — raw i processed, jeżeli występują;
4. istniejące metadane runu, recepturę i settings.

Jeżeli PyThat nie może otworzyć pliku, Results nie przechodzi po cichu na własny reader
danych pomiarowych. Pokazuje błąd zgodności PyThat, pozostawiając dostęp jedynie do
diagnostycznych metadanych natywnych.

## 10. Receptury i przebiegi akceptacyjne

### 10.1. Szybki smoke test

Mały plan 2 × 3 używa:

- Rigola z osią częstotliwości;
- Keithleya z pełną konfiguracją oraz pomiarem I/V/P;
- Anritsu z jednym widmem na punkt;
- MOKE z odczytem Halla na punkt.

Oczekiwany wynik:

- 6 checkpointów;
- 6 kompletnych widm;
- 6 kompletów pomiarów Keithley;
- 6 kompletów danych MOKE;
- pełny snapshot Rigola, Keithleya, Anritsu i MOKE dla każdego punktu;
- poprawne otwarcie PyThat i Results.

### 10.2. Receptura akceptacyjna

Bazą jest istniejący:

`recipes/keithley_b_rigol_frequency_anritsu_reference_10x100.yml`.

Wariant symulacyjny zachowuje strukturę i dodaje `measure_moke_hall` wewnątrz
najgłębszej pętli przed akwizycją widma. Plan daje 1000 punktów i sprawdza:

- dwie zagnieżdżone osie;
- punktowe aktualizacje bez sprzętowych sweepów;
- pełne konfiguracje parametrów stałych;
- 1000 widm;
- 1000 odczytów Keithley;
- 1000 odczytów MOKE;
- zapis reference i processed spectrum, jeżeli są użyte;
- kompletność HDF5;
- round-trip PyThat;
- otwarcie wyniku w Results.

Test jest oznaczony jako wolny. Nie blokuje szybkiego zestawu testów deweloperskich.

### 10.3. Test istniejący 100 × 20

Obecny test 2000 widm pozostaje testem pojemności i regresji schematu. Nie zastępuje
czterourządzeniowej receptury akceptacyjnej, ponieważ nie obejmuje MOKE ani pełnego
kontraktu snapshotów.

## 11. Obsługa czasu i wydajność

Tryb symulacji nie emuluje rzeczywistych opóźnień transportu ani czasu sweepu analizatora.
Domyślny `time_scale=0.0` zachowuje kolejność operacji i eventy, ale nie wykonuje
sprzętowych opóźnień. Jawne testy watchdog/retry/fault injection mogą używać dodatniego
`time_scale`.

Testy dzielą się na:

- szybkie testy jednostkowe i smoke, uruchamiane zawsze;
- testy integracyjne średniej wielkości;
- wolne testy 1000/2000 punktów uruchamiane w kwalifikacji lub CI release.

## 12. Obsługa błędów

1. Próba otwarcia fizycznego resource/endpointu w symulacji kończy run przed połączeniem.
2. Wartość wygenerowana poza zakresem, `NaN` albo `Inf` powoduje `run_fault`.
3. Brak parametru wymaganego przez `RunDataContract` blokuje zamknięcie jako `completed`.
4. Niezgodność requested/readback używa istniejącego zachowania fail-closed adaptera.
5. Brak MOKE w planie nie jest błędem; brak MOKE przy wymaganiu planu jest błędem.
6. Awaria jednego symulatora uruchamia tę samą sekwencję safe shutdown co awaria sprzętu.
7. Deterministyczne fault injection zapisuje seed, model version, device stream i operację,
   dzięki czemu błąd można odtworzyć.
8. Błąd PyThat jest błędem kontraktu pliku, a nie ostrzeżeniem UI.

## 13. Strategia testów

### 13.1. Testy jednostkowe

- ten sam seed daje identyczne strumienie;
- inne klucze urządzeń dają niezależne strumienie;
- ten sam seed i plan dają identyczne wartości numeryczne poza czasami runu;
- inny seed zmienia wartości pseudolosowe;
- każda wartość mieści się w określonym zakresie;
- pełny snapshot zachowuje parametry niesweepowane po aktualizacji osi;
- verification ma poprawną wartość `readback`, `simulated_ack` lub `command_only`.

### 13.2. Testy kontraktów modułów

- każdy z czterech modułów tworzy adapter w symulacji;
- żaden adapter nie otwiera fizycznego transportu;
- każdy adapter zwraca syntetyczne IDN i capabilities;
- każda obsługiwana konfiguracja publikuje pełny `AppliedDeviceState`;
- MOKE działa w symulacji bez `protocol_qualified` fizycznego endpointu.

### 13.3. Test integracyjny runnera

Smoke 2 × 3 wykonuje rzeczywiste:

```text
compile -> preflight -> RunWorker -> adapters -> runner -> writer -> validator
```

Test nie konstruuje ręcznie `MeasurementPoint` ani gotowego HDF5.

### 13.4. Test HDF5

Test sprawdza jawnie:

- `/run/simulation_json`;
- IDN/capabilities czterech urządzeń;
- dokładną liczbę checkpointów;
- `device_state_json` w każdym punkcie;
- wszystkie wymagane parametry stałe Rigola;
- konfigurację i wyniki Keithleya;
- konfigurację oraz widmo Anritsu;
- dane Halla MOKE;
- eventy start/completion;
- status `completed`;
- brak pozostawionych `_pending`.

### 13.5. Test PyThat i Results

Plik jest otwierany przez `MeasurementTree(index=True, override=True)`. Test sprawdza
wymiary, współrzędne, nazwy zmiennych i rozmiar widma. Następnie offscreen `ResultsPage`
otwiera ten sam plik i potwierdza:

- widoczne osie oraz pomiary pochodzą z PyThat;
- widoczny jest pełny stan urządzeń;
- wykres ma oczekiwaną liczbę punktów;
- seed i znacznik symulacji są widoczne;
- błąd PyThat jest prezentowany jawnie.

## 14. Etapy wdrożenia

1. **Kontekst symulacji i seed**

   Wprowadzić `SimulationRunConfig`, stabilne podstrumienie, zapis metadanych i możliwość
   ponownego użycia seedu.

2. **Pełny kontrakt stanu urządzenia**

   Wprowadzić `AppliedParameter`, `AppliedDeviceState`, ledger runnera oraz event
   `device_state_applied`.

3. **Modele czterech urządzeń**

   Podłączyć kontekst do istniejących symulatorów, uzupełnić pełne readbacki oraz dodać
   działający symulator MOKE.

4. **Kontrakt danych planu**

   Rozszerzyć wynik kompilacji o wymagane urządzenia, parametry, pomiary i akwizycje.

5. **Atomowy zapis pełnych snapshotów**

   Rozszerzyć HDF5, reader i walidator o simulation metadata i `device_state_json`.

6. **PyThat jako źródło danych Results**

   Dodać warstwę loadera PyThat i połączyć ją z diagnostycznym readerem natywnym.

7. **Wizualne potwierdzenie**

   Rozszerzyć Run Monitor i Results o stan urządzeń, requested/actual/verification oraz seed.

8. **Receptury i testy akceptacyjne**

   Dodać smoke 2 × 3, wariant 10 × 100 z MOKE oraz macierz kontroli wszystkich pól.

9. **Dokumentacja operatora**

   Opisać uruchomienie, podanie/replay seedu, interpretację `SIM`, ograniczenia i sposób
   otwarcia wyniku.

## 15. Kryteria odbioru

Wdrożenie jest kompletne, gdy:

1. aplikacja uruchomiona z `--simulate` nie wykonuje fizycznych połączeń;
2. cztery urządzenia wymagane przez recepturę łączą się jako symulowane;
3. ten sam seed i plan odtwarzają te same dane numeryczne;
4. Run Monitor pokazuje zastosowaną konfigurację każdego urządzenia;
5. Rigol zachowuje w HDF5 wszystkie aktywne parametry mimo sweepowania jednej osi;
6. Keithley zachowuje pełną konfigurację i wyniki I/V/P;
7. Anritsu zapisuje pełną konfigurację i niepłaskie widmo;
8. MOKE zapisuje wymagane dane Halla;
9. każdy checkpoint spełnia `RunDataContract`;
10. HDF5 zawiera recipe, settings, operatora, IDN, capabilities, seed, pełne snapshoty,
    setpointy, measurements, widma i eventy;
11. finalna walidacja manifestu i PyThat przechodzi;
12. Results otwiera dane przez PyThat i pokazuje pełny stan urządzeń;
13. brak pola, uszkodzone widmo lub błąd PyThat nie może dać statusu `completed`;
14. szybki smoke test i wolna receptura akceptacyjna przechodzą;
15. istniejące testy sprzętowe i symulacyjne nie tracą dotychczasowych zabezpieczeń.

## 16. Ograniczenia i świadome wyłączenia

- Wynik `simulation_passed` lub `completed` w symulacji nie kwalifikuje sprzętu.
- Modele nie muszą dokładnie odwzorowywać fizyki DUT; muszą być stabilne, skończone,
  zakresowe i wystarczająco realistyczne do wizualnej kontroli danych.
- Nie implementujemy sprzętowych sweepów urządzeń.
- Nie rozszerzamy w tym projekcie obsługi Lake Shore.
- Nie zmieniamy formatu fizycznego profilu na podstawie symulacji.
- Nie ukrywamy braku readbacku: `command_only` pozostaje wyraźnie oznaczone.
