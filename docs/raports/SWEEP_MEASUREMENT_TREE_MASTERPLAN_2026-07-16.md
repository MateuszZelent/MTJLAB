# Masterplan modułowego kreatora sweepów i drzewa pomiarowego

**Projekt:** Lab Control  
**Zakres:** Keithley 2600/2602A jako implementacja referencyjna, następnie Rigol DG1032Z i Anritsu MS2830A
**Format danych:** thaTEC:OS / PyThat / HDF5  
**Pierwsza wersja:** 2026-07-16
**Rewizja architektury:** 2026-07-17
**Status:** reset architektury — dokument nadrzędny przed dalszą implementacją UI

## 1. Decyzja nadrzędna

Zakładka obecnie nazywana **Recipes** zostanie przemianowana w interfejsie na **Sweeps**.
Nie będzie ona kolejnym modułem sterowania urządzeniami. Będzie wyłącznie integratorem:

- istniejących modułów urządzeń;
- wspólnych operacji przepływu, np. `Wait`, `Sequence`, `Repeat`, `Comment`;
- zagnieżdżonych osi pomiarowych;
- kompilacji, preflightu, wykonania i zapisu HDF5.

Każde urządzenie pozostaje właścicielem swoich:

- parametrów i jednostek;
- wartości domyślnych;
- ograniczeń profilu i capabilities;
- walidacji bezpieczeństwa;
- modeli konfiguracji i serializacji;
- komponentów formularza;
- komend wykonawczych, readbacku, rampy i wyłączenia;
- ikon, nazewnictwa i sposobu prezentacji stanu.

**Sweeps nie może ponownie deklarować tych informacji.** W szczególności zabronione są
centralne listy parametrów urządzeń, osobne formularze kopiujące zakładki ręczne oraz
warunki w rodzaju `if device == "Keithley"` w rdzeniu kreatora.

Druga zasada nadrzędna: **nie korzystamy z wbudowanych sweepów urządzeń**. Sweep jest
programową orkiestracją wykonywaną punkt po punkcie przez aplikację. Dla każdego punktu
runner ustawia zwykłym poleceniem nową wartość parametru, czeka na stabilizację, wykonuje
zwykły pojedynczy pomiar lub akwizycję i zapisuje wynik. Dopiero potem przechodzi do
następnej wartości.

Outputy pozostają włączone pomiędzy kolejnymi punktami, o ile plan i polityka bezpieczeństwa
nie wymagają zatrzymania. Nie wykonujemy cyklu `OUTPUT OFF → konfiguracja → OUTPUT ON` dla
każdego punktu. Rampa do zera i wyłączenie outputu są operacjami końca całego runu albo
reakcją na stop, błąd lub naruszenie bezpieczeństwa.

## 2. Interpretacja docelowego działania

Operator nie wybiera z biblioteki `Keithley / Sweep current`, `Keithley / Fixed voltage`
ani kilkunastu technicznych akcji tego samego urządzenia. Wybiera po prostu moduł
**Keithley 2600** i przeciąga go do drzewa.

Po dwukliku w węzeł Keithley otwiera się osobne, modeless okno dostarczone przez moduł
Keithley. Jego lewa część wygląda i zachowuje się niemal tak samo jak uproszczona sekcja
**Source and measurement configuration** z ręcznej zakładki Keithley:

- Channel A/B;
- Source mode: current, voltage albo measure only;
- Source value;
- compliance;
- NPLC;
- settling time;
- sense mode;
- source i measurement ranges wraz z autorange;
- jawna polityka outputu;
- te same minima, maksima, komunikaty i zależności pól.

Różnica nie polega na napisaniu drugiego formularza. Ten sam komponent konfiguracji oraz
ten sam model widoku są osadzane w dwóch hostach:

1. **Manual Device Host** — steruje podłączonym urządzeniem na żywo;
2. **Sweep Node Host** — edytuje snapshot planu i nie komunikuje się z VISA.

W Sweep Node Host operator może pozostawić parametr jako wartość stałą albo oznaczyć go
jako oś sweepu. Dla osi pojawia się po prawej wspólny edytor etapów i wykres dokładnych
punktów. Moduł Keithley dostarcza znaczenie parametru, jednostkę, ograniczenia i sposób
zastosowania wartości; moduł Sweeps dostarcza jedynie mechanikę etapów i zagnieżdżenia.

## 3. Przykład referencyjny operatora

### 3.1. Stałe ustawienie Keithley A

1. Operator przeciąga `Keithley 2600` z Node Library do `Measurement sequence`.
2. Dwuklik otwiera okno konfiguracji modułu Keithley.
3. Wybiera kanał A, tryb current, `0.5 mA`, compliance, ranges, sense i NPLC.
4. Nie oznacza `Source current` jako osi.
5. Zapisuje węzeł.
6. Drzewo pokazuje np. `Keithley A · I = 0.5 mA · fixed`.

Węzeł nie tworzy wymiaru danych i wykonuje konfigurację dokładnie w miejscu drzewa, w
którym został umieszczony.

### 3.2. Sweep Keithley B

1. Operator dodaje drugi węzeł `Keithley 2600`.
2. W edytorze wybiera kanał B i tryb current.
3. Przy polu `Source current` włącza `Sweep this parameter`.
4. Dodaje etapy:
   - `0.01 A → 0.10 A`, 100 punktów;
   - `0.10 A → 0.15 A`, 20 punktów.
5. Wykres pokazuje jedną oś logiczną z kolorami etapów i 119 unikalnymi punktami, jeśli
   wspólna granica jest deduplikowana.
6. Ten sam walidator Keithley natychmiast wskazuje punkty wykraczające poza zatwierdzony
   profil. Run pozostaje zablokowany.

### 3.3. Zagnieżdżony Rigol i akwizycja Anritsu

1. Operator przeciąga `Rigol DG1032Z` jako dziecko sweepu Keithley.
2. W oknie dostarczonym przez moduł Rigol konfiguruje CH1, sinus, poziomy i oznacza
   częstotliwość jako oś `100 MHz → 1 GHz`.
3. Jako dzieci Rigola dodaje `Wait` oraz `Anritsu MS2830A`.
4. W oknie modułu Anritsu wybiera konfigurację widma i operację pojedynczej akwizycji.
5. Dla każdego punktu Keithley i każdego punktu Rigola wykonywane jest jedno widmo.

Semantyka pojedynczego checkpointu jest następująca:

```text
ustaw następny prąd Keithley B
→ poczekaj na settle/readback
→ ustaw następną częstotliwość Rigol CH1
→ poczekaj na settle/readback
→ uruchom pojedynczą akwizycję widma Anritsu
→ pobierz ukończone widmo
→ zapisz setpointy, readback, status i widmo do HDF5
→ przejdź do następnej częstotliwości bez wyłączania outputów
→ po zakończeniu wewnętrznej pętli przejdź do następnego prądu
```

Nie uruchamiamy funkcji sweep Rigola, list sweep Keithley ani ciągłego sweepu sprzętowego,
który sam przechodzi po punktach. Anritsu może wykonać swój pojedynczy wewnętrzny przebieg
akwizycyjny potrzebny do uzyskania jednego widma, ale nie steruje osiami eksperymentu.

```text
Measurement sequence
├─ Keithley 2600 · CH A · I = 0.5 mA · fixed
└─ Keithley 2600 · CH B · source current · 119 pts
   └─ Rigol DG1032Z · CH1 · sine frequency · 101 pts
      ├─ Wait · 100 ms
      └─ Anritsu MS2830A · Spectrum · acquire once
Finally (generowane z polityk modułów)
├─ Keithley A/B · ramp to zero
├─ Rigol CH1/CH2 · output off
└─ Anritsu · abort/return to safe state
```

## 4. Granice odpowiedzialności

| Warstwa | Jest właścicielem | Nie może być właścicielem |
|---|---|---|
| Moduł urządzenia | konfiguracja, UI pól, parametry, limity, walidacja, snapshot, apply/readback/safe stop | struktura całego eksperymentu |
| Sweeps | drzewo, drag-and-drop, osie, etapy, nesting, undo/redo, snapshot dokumentu | parametry i limity konkretnego urządzenia |
| Compiler | rozwinięcie drzewa do deterministycznego planu | odczyt wartości bezpośrednio z widgetów |
| Runner | wykonanie planu, checkpointy, stop/pause/finally | interpretowanie formularzy GUI |
| Storage | zapis zatwierdzonego snapshotu, osi, danych i statusu | tworzenie planu z bieżącego UI |

Kierunek zależności:

```text
Manual Page ───────┐
                   ├─> Device Configuration Component
Sweep Node Window ─┘             │
                                 v
                   Device Snapshot + Validation
                                 │
              ┌──────────────────┴──────────────────┐
              v                                     v
        Device Executor                       Device Metadata
              │                                     │
              └────────> Compiler / Runner <────────┘
                                  │
                                  v
                           HDF5 / PyThat
```

Rdzeń Sweeps zna interfejs providera, lecz nie importuje klas formularzy Keithley, Rigol
czy Anritsu bezpośrednio. Moduły urządzeń rejestrują swoje providery podczas składania
aplikacji.

## 5. Refaktoryzacja modułu urządzenia

### 5.1. Wspólny komponent, nie import całej strony

Nie należy osadzać całej istniejącej `KeithleyPage` w oknie sweepu. Strona zawiera również
historię, kontroler live, sygnały urządzenia, pomiary cykliczne i przyciski wykonawcze.
Jej bezpośrednie skopiowanie albo dwukrotne utworzenie mogłoby przypadkowo wykonywać
komendy podczas projektowania planu.

Należy wyodrębnić z niej współdzielone elementy:

```text
KeithleyPage (manual host)
├─ KeithleyConfigurationPanel       <─ współdzielony widget
├─ KeithleyConfigurationViewModel   <─ współdzielone zależności pól
├─ live measurement/history
└─ manual execution controls

KeithleyNodeEditor (sweep host)
├─ KeithleyConfigurationPanel       <─ ten sam widget
├─ KeithleyConfigurationViewModel   <─ ten sam view model
├─ AxisBindingPanel                 <─ mechanizm ogólny Sweeps
└─ PlannedPointsPlot                <─ mechanizm ogólny Sweeps
```

Widget ma pracować na przekazanym modelu i polityce edycji. Nie może sam wywoływać
adaptera. Host ręczny wiąże `Apply/Output/Measure` z kontrolerem urządzenia. Host sweepu
wiąże `Apply` wyłącznie z kopią roboczą węzła.

### 5.2. Jedno źródło konfiguracji

Referencyjny model `KeithleyChannelConfiguration` obejmuje co najmniej:

- `channel`;
- `source_mode`;
- `source_level`;
- `compliance`;
- `nplc`;
- `settling_time`;
- `sense_mode`;
- `source_autorange` i `source_range`;
- `measure_v_autorange` i `measure_v_range`;
- `measure_i_autorange` i `measure_i_range`;
- politykę outputu i rampy;
- wersję schematu konfiguracji.

Ten model jest wejściem zarówno dla `KeithleyPage`, jak i dla węzła Sweeps. Istniejący
`KeithleySourceRequest` oraz `validate_keithley_source()` pozostają częścią tej samej
ścieżki walidacji. Formularz nie tworzy własnych minimów i maksimów.

### 5.3. Tryby hosta

`ConfigurationPanelMode` powinien jawnie rozróżniać:

- `MANUAL_CONNECTED` — przyciski wykonawcze i readback są dostępne zgodnie z uprawnieniami;
- `PLAN_EDIT` — brak komunikacji z urządzeniem, dostępne wiązanie parametrów do osi;
- `READ_ONLY_SNAPSHOT` — podgląd planu uruchomionego lub historycznego.

Tryb nie może być ustalany przez ukrywanie losowych kontrolek po ich utworzeniu. Jest
częścią kontraktu konstruktora i testów.

## 6. Kontrakt Device Sweep Provider

Nazwa robocza interfejsu: `DeviceSweepProvider`. Provider znajduje się wewnątrz modułu
urządzenia, nie w `app/recipes` ani w monolitycznym `main_window.py`.

```python
class DeviceSweepProvider(Protocol):
    @property
    def descriptor(self) -> DeviceDescriptor: ...

    def default_snapshot(self, context: ProfileContext) -> DeviceSnapshot: ...
    def migrate_snapshot(self, raw: Mapping[str, object]) -> DeviceSnapshot: ...
    def create_configuration_panel(
        self,
        snapshot: DeviceSnapshot,
        mode: ConfigurationPanelMode,
    ) -> DeviceConfigurationPanel: ...
    def sweepable_parameters(
        self,
        snapshot: DeviceSnapshot,
    ) -> Sequence[ParameterDescriptor]: ...
    def validate(
        self,
        snapshot: DeviceSnapshot,
        axes: Sequence[AxisBinding],
        context: ValidationContext,
    ) -> Sequence[ValidationIssue]: ...
    def summarize(self, snapshot: DeviceSnapshot, axes: Sequence[AxisBinding]) -> NodeSummary: ...
    def compile(self, node: DeviceNode, context: CompileContext) -> DevicePlanFragment: ...
    def safe_shutdown(self, snapshot: DeviceSnapshot) -> Sequence[SafeAction]: ...
```

Interfejs jest pojęciowy; dokładne sygnatury należy dopasować do istniejących modeli.
Niezmienne pozostają następujące wymagania:

- snapshot jest serializowalny i niezależny od QWidget;
- panel nie wykonuje VISA w `PLAN_EDIT`;
- provider zwraca deskryptory parametrów z tego samego źródła, z którego budowany jest
  formularz;
- walidacja każdego wygenerowanego punktu wykorzystuje moduł bezpieczeństwa urządzenia;
- kompilacja zwraca neutralny fragment planu, a nie wykonuje komend;
- bezpieczne zakończenie jest własnością modułu urządzenia.

### 6.1. ParameterDescriptor

Deskryptor parametru powinien zawierać:

- stabilne `parameter_id`, np. `source.level`;
- etykietę i opis;
- wymiar fizyczny i preferowaną jednostkę prezentacji;
- getter/setter działający na snapshotcie albo bezpieczny odpowiednik funkcyjny;
- informację `fixed`, `sweepable`, `read_only`;
- zależności od trybu i kanału;
- reguły generacji osi, np. linear/log/list;
- funkcję walidującą przez istniejącą domenę urządzenia;
- sposób prezentacji skrótu w drzewie.

Nie zawiera ręcznie przepisanych limitów. Limity wynikają z profilu stacji i capabilities.

### 6.2. DeviceDescriptor i rejestr

Rejestr przechowuje wyłącznie providery urządzeń i metadane wizualne:

- `device_type_id`;
- nazwa operatora;
- ikona SVG/QIcon;
- kolor akcentu;
- status dostępności;
- fabryka providera.

Dodanie nowego urządzenia ma wymagać rejestracji jednego providera, bez modyfikowania
`RecipePage`, biblioteki węzłów, dialogu generatora ani kompilatora ogólnego.

## 7. Node Library po przebudowie

Biblioteka ma być krótka, czytelna i zgodna z modelem mentalnym operatora.

```text
NODE LIBRARY
Search devices and actions…

DEVICES
  [K] Keithley 2600
  [R] Rigol DG1032Z
  [A] Anritsu MS2830A

FLOW
  Wait
  Sequence / Group
  Repeat
  Comment
  Conditional stop       (późniejszy etap)
```

Nie pokazujemy tu osobno `Sweep current`, `Set fixed current`, `Output`, `Ramp to zero`
itd. Są to możliwości i polityki modułu Keithley, prezentowane po otwarciu jego węzła.
Biblioteka może pokazywać status `offline`, ale urządzenie offline nadal może być użyte do
projektowania, jeżeli dostępny jest zatwierdzony profil.

### 7.1. Drag-and-drop

- przeciągnięcie urządzenia tworzy `DeviceNode` z domyślnym snapshotem providera;
- upuszczenie na sekwencję tworzy krok wykonywany raz;
- upuszczenie jako dziecko węzła z osią tworzy operację wykonywaną dla każdego punktu;
- UI pokazuje linię i poziom docelowego zagnieżdżenia;
- model domenowy ponownie waliduje relację niezależnie od widoku;
- utworzenie węzła jest pojedynczą operacją undo/redo;
- opcjonalnie edytor urządzenia otwiera się automatycznie po dropie.

## 8. Model węzła urządzenia

```yaml
kind: device
node_id: node-keithley-b
device_type: keithley.2600
device_instance: keithley-main
enabled: true
configuration:
  schema_version: 1
  channel: B
  source_mode: current
  source_level: 1 mA
  voltage_compliance: 67 mV
  nplc: 1
  settling_time: 100 ms
  sense_mode: 2wire
  source_range: AUTO
  measure_v_range: AUTO
  measure_i_range: AUTO
  output_policy: enable_for_node
axes:
  - parameter_id: source.level
    boundary_policy: deduplicate_adjacent
    stages:
      - {start: 0.01 A, stop: 0.10 A, points: 100, spacing: linear}
      - {start: 0.10 A, stop: 0.15 A, points: 20, spacing: linear}
children: []
```

`configuration` jest nieprzezroczystym dla Sweeps snapshotem modułu Keithley. Rdzeń może
go kopiować, wersjonować i serializować, ale nie interpretuje `voltage_compliance` ani
innych pól. `axes` wskazują parametry poprzez stabilne identyfikatory providera.

### 8.1. Stała wartość

Parametr nieoznaczony jako oś pozostaje częścią snapshotu. Dzięki temu pojedyncza wartość
nie jest sztucznym sweepem jednopunktowym:

- nie dodaje wymiaru;
- nie mnoży liczby checkpointów;
- obowiązuje zgodnie z położeniem węzła;
- korzysta z dokładnie tego samego pola, jednostki i walidatora.

### 8.2. Jedna czy wiele osi w urządzeniu

Pierwsza wersja UI pozwala na jedną aktywną oś na węzeł urządzenia. Kilka parametrów można
modelować przez zagnieżdżone węzły tego samego urządzenia. Model dokumentu może od początku
przechowywać listę `axes`, ale wiele osi w jednym węźle pozostaje zablokowane do czasu
jednoznacznego określenia iloczynu kartezjańskiego i osi sprzężonych.

## 9. Okno edycji węzła Keithley

Okno jest osobnym, lekkim oknem modeless. Dwa węzły Keithley mogą mieć dwa niezależne
okna, lecz oba używają tej samej klasy komponentu konfiguracyjnego.

```text
┌──────────────────────────────────────────────────────────────────────┐
│ Keithley 2600 · Channel B                         valid · 119 points │
├────────────────────────────────┬─────────────────────────────────────┤
│ SOURCE & MEASUREMENT           │ PLANNED POINTS                      │
│ Channel              [B]       │                                     │
│ Source mode          [current] │          scatter plot               │
│ Source current [1 mA] [SWEEP]  │       z kolorami etapów             │
│ Compliance       [67 mV]       │                                     │
│ NPLC                  [1]       │ selected point / stage / limits     │
│ Settling time     [100 ms]     │                                     │
│ Sense mode          [2wire]    │                                     │
│ Source range         [AUTO]    │                                     │
│ Measure V range      [AUTO]    │                                     │
│ Measure I range      [AUTO]    │                                     │
├────────────────────────────────┼─────────────────────────────────────┤
│ AXIS: source.level             │ validation and generated summary    │
│ Stage 1  .01 A  .10 A  100    │ 119 unique points                   │
│ Stage 2  .10 A  .15 A   20    │ exact first/last/min/max            │
│ [+ stage] [duplicate] [delete] │ profile limit overlays              │
├────────────────────────────────┴─────────────────────────────────────┤
│ Revert                         Apply   Apply and close   Cancel       │
└──────────────────────────────────────────────────────────────────────┘
```

### 9.1. Zachowanie pól

- przy polu sweepowalnym jest spójna akcja `Use as sweep axis`;
- aktywna oś otrzymuje kolor i ikonę, lecz pole nadal pokazuje bieżący punkt podglądu;
- zmiana channel/mode aktualizuje pola przez view model Keithley;
- niezgodne osie nie są po cichu konwertowane — operator musi potwierdzić usunięcie osi;
- MIN/MAX i komunikaty są dokładnie te same co w ręcznej zakładce;
- offline używa zatwierdzonego snapshotu capabilities i wyraźnie pokazuje jego datę;
- `Apply` aktualizuje jeden węzeł jako pojedynczą komendę undo.

### 9.2. Wykres etapów

Wspólny komponent Sweeps generuje dokładny wektor punktów i pokazuje:

- osobny kolor każdego etapu;
- wszystkie punkty dla małych osi i reprezentatywną próbkę dla bardzo dużych;
- granice profilu i punkty niedozwolone;
- indeks globalny, etap, indeks lokalny i wartość w tooltipie;
- liczbę punktów po deduplikacji;
- minimalny i maksymalny krok;
- ostrzeżenie o skoku na granicy etapów;
- linear, logarithmic oraz explicit list, jeśli provider dopuszcza te generatory.

## 10. Bezpieczeństwo i output

Projektowanie węzła nigdy nie włącza urządzenia. Uruchomienie planu ma być semantycznie
równoważne zatwierdzonej sekwencji ręcznych operacji, ale wykonuje ją runner po pełnym
preflighcie.

W szczególności:

1. operator jawnie wybiera politykę outputu w konfiguracji modułu;
2. preflight waliduje każdy punkt przez `validate_keithley_source()` i profil stacji;
3. uprawnienie do outputu i ARM są sprawdzane bezpośrednio przed wykonaniem;
4. konfiguracja początkowa, która tego wymaga, odbywa się przy OUTPUT OFF;
5. runner włącza wymagany output jeden raz przed pierwszym aktywnym punktem;
6. pomiędzy punktami output pozostaje włączony, a runner zmienia wyłącznie setpointy
   dopuszczone do zmiany przy aktywnym wyjściu;
7. przejście do kolejnego poziomu odbywa się zgodnie z zatwierdzoną polityką kroku/rampy,
   bez zejścia do zera pomiędzy punktami;
8. readback i compliance mogą zatrzymać plan;
9. sukces, stop, błąd, timeout, błąd storage i wyjątek zawsze prowadzą przez `finally`;
10. dopiero w `finally` moduł Keithley odpowiada za rampę do zera i OUTPUT OFF.

Sweeps składa polityki `safe_shutdown()` wszystkich użytych providerów. Operator może je
zobaczyć, ale nie może usunąć wymaganych akcji bezpieczeństwa.

### 10.1. Trzy fazy życia urządzenia

Każdy provider musi rozróżniać trzy fazy, aby kompilator nie generował wyłączeń pomiędzy
punktami:

1. **enter / prepare** — połączenie, konfiguracja bazowa, ARM, początkowy setpoint i jedno
   jawne włączenie outputu;
2. **point transition / acquire** — zmiana setpointów, settle, readback, pojedyncze pomiary
   i checkpoint; bez domyślnego wyłączania outputu;
3. **exit / finally** — rampa do bezpiecznej wartości, OUTPUT OFF, abort i finalny status.

Operacje `enter` i `exit` wykonują się raz na zakres życia węzła lub całego runu, zgodnie
ze skompilowanym planem. Operacja punktowa wykonuje się dla każdej współrzędnej osi.

### 10.2. Wyjątki od utrzymania outputu

Output wolno wyłączyć przed końcem tylko wtedy, gdy wystąpi jawny warunek bezpieczeństwa:

- operator nacisnął Stop lub E-STOP;
- compliance wymaga zatrzymania;
- utracono połączenie lub readback;
- adapter zgłosił, że danej zmiany nie wolno wykonać przy aktywnym wyjściu;
- zapis checkpointu nie może być kontynuowany;
- preflight zdefiniował zatwierdzoną granicę zakresu życia urządzenia wymagającą ponownej
  inicjalizacji.

Taki wyjątek przerywa albo jawnie rozdziela plan. Nie może być ukrytym zachowaniem każdej
iteracji.

## 11. Kompilacja bez wiedzy o konkretnym urządzeniu

Pipeline:

```text
SweepDocument
→ walidacja struktury drzewa
→ provider.validate(snapshot, axes)
→ rozwinięcie dokładnych wartości osi
→ provider.compile(device node)
→ neutralny ExecutionPlan
→ estymacja czasu i rozmiaru
→ zatwierdzony immutable RunSnapshot
→ RecipeRunner
→ HDF5 / thaTEC / PyThat
```

Provider może emitować typowane operacje wykonawcze obsługiwane przez executor swojego
modułu, np. `KeithleyConfigure`, `KeithleyRampToLevel`, `KeithleyMeasure`. Rdzeń planu zna
jedynie wspólny kontrakt operacji, zależności, timeouty, checkpointy i cleanup.

Kompilator musi podnieść konfigurację stałą oraz `enter/enable output` poza właściwą pętlę,
a w jej ciele pozostawić jedynie operacje punktowe. Dla zagnieżdżonych osi daje to model:

```text
prepare Keithley A/B and Rigol CH1
enable required outputs once
for each Keithley current:
    set Keithley current
    settle/readback
    for each Rigol frequency:
        set Rigol frequency
        settle/readback
        acquire one Anritsu spectrum
        save one checkpoint
finally:
    safe ramp / output off / abort
```

Wbudowany mechanizm sweep/list danego urządzenia nie może pojawić się w skompilowanym
planie v1. Jest to osobna, przyszła optymalizacja wymagająca dowodu identycznej synchronizacji
i osobnej kwalifikacji; nie jest częścią tego masterplanu.

Nieakceptowalne rozwiązania:

- mapowanie `keithley.A.current` w `RecipePage`;
- tworzenie `KeithleySourceRequest` z pól dialogu Sweeps;
- przepisywanie compliance w kompilatorze ogólnym;
- generowanie surowego SCPI przez GUI;
- odczytywanie wartości z aktywnego QWidget podczas runu;
- specjalne gałęzie dla nazwy urządzenia w drag-and-drop lub HDF5 mapperze.

## 12. HDF5, thaTEC i PyThat

Źródłem prawdy jest zatwierdzony `RunSnapshot`, nie stan kontrolek. Zapis obejmuje:

- wersję dokumentu Sweeps;
- snapshot konfiguracji każdego urządzenia wraz z wersją schematu;
- identyfikator i wersję providera;
- zatwierdzony profil, capabilities i IDN;
- dokładne wartości osi po deduplikacji;
- indeks globalny, indeks etapu i indeks lokalny;
- drzewo zagnieżdżeń i kolejność wykonania;
- setpointy żądane oraz readback;
- wyniki pomiarów i widma;
- status punktu, błędy, compliance i czas;
- hash skompilowanego planu;
- wynik cleanupu każdego urządzenia.

Przykład Keithley × Rigol × Anritsu tworzy logicznie wymiary:

```text
keithley_b_current × rigol_ch1_frequency × anritsu_frequency_bin
```

Stały kanał Keithley A jest kontrolką/metadanym setpointem, nie dodatkowym wymiarem.
Zgodność musi być potwierdzona testem round-trip z PyThat, a nie tylko odczytem przez
`h5py`.

## 13. Docelowa struktura kodu

Nazwy są propozycją i mogą zostać dopasowane po małym spike'u refaktoryzacyjnego.

```text
app/
  devices/
    base.py
    registry.py
    keithley/
      adapter.py
      models.py
      configuration_view_model.py
      configuration_panel.py
      sweep_provider.py
      executor.py
    rigol/
      ... analogiczny kontrakt, własna implementacja ...
    anritsu/
      ... analogiczny kontrakt, własna implementacja ...
  sweeps/
    models.py
    tree_model.py
    axis.py
    commands.py
    compiler.py
    validation.py
    serialization.py
  ui/
    sweeps/
      page.py
      node_library.py
      measurement_tree.py
      inspector.py
      device_node_window.py
      axis_editor.py
      points_plot.py
      window_manager.py
  engine/
    runner.py
  storage/
    ... istniejący HDF5 i thaTEC ...
```

Wspólne komponenty formularza pozostają wewnątrz modułu urządzenia. `device_node_window.py`
jest tylko hostem: pobiera panel od providera i dokłada ogólny edytor osi.

## 14. Migracja z obecnej implementacji

### 14.1. Elementy do zatrzymania

- `Recipe`, parser i bezpieczna deklaratywna serializacja jako warstwa kompatybilności;
- `RecipeCompiler` i `RecipeRunner`, po stopniowym wprowadzeniu neutralnych fragmentów planu;
- `Hdf5RunWriter`, `ThatecSchemaMapper`, `ThatecHdf5Writer`;
- generator wieloetapowych punktów i jego testy;
- działające adaptery i moduły safety;
- bezpieczny cleanup, checkpointy, pause/stop;
- ogólny model drzewa, undo/redo i drag-and-drop, jeśli nie zawierają logiki urządzeń.

### 14.2. Elementy do wycofania

Po zbudowaniu providera Keithley należy usunąć z centralnego UI:

- `_SWEEPABLE_PARAMETERS`;
- `DeviceParameterDialog` jako selektor pól wszystkich urządzeń;
- `KeithleySweepBuilderDialog` z powtórzonymi polami;
- `_fixed_node_from_dialog()` i `_sweep_node_from_generator()` interpretujące urządzenia;
- device-specific node library entries;
- centralne mapy ikon i parametrów, jeśli provider może dostarczyć metadane.

Wycofanie następuje dopiero po testach równoważności. Nie wolno jednocześnie utrzymywać
dwóch aktywnych źródeł konfiguracji przez długi okres.

### 14.3. Kompatybilność nazwy Recipe

- w UI: `Recipes` → `Sweeps`;
- w dokumentacji operatora: `sweep plan` lub `measurement plan`;
- klasy backendowe `Recipe*` mogą pozostać w pierwszej migracji;
- format pliku otrzymuje jawny `schema_version` i migrator;
- zmiana nazw klas backendowych jest osobnym, mechanicznym etapem po stabilizacji kontraktu.

## 15. Plan wdrożenia

### Etap 0 — zamrożenie błędnego kierunku

- nie rozwijać dalej osobnych dialogów urządzeń w Sweeps;
- oznaczyć istniejący `KeithleySweepBuilderDialog` jako tymczasowy;
- zatwierdzić odpowiedzialności z rozdziału 4;
- spisać testy charakteryzujące aktualny formularz Keithley i walidację.

**Wyjście:** zespół nie dodaje nowych parametrów do centralnych map.

### Etap 1 — wspólny model konfiguracji Keithley

- utworzyć typowany snapshot kanału;
- przenieść zależności pól z `KeithleyPage` do view modelu;
- podłączyć istniejący `KeithleySourceRequest` i safety envelope;
- zapewnić round-trip snapshot ↔ formularz;
- zachować działanie ręcznej zakładki bez zmiany zachowania sprzętu.

**Wyjście:** ręczna strona korzysta z jednego modelu, nie z luźnych wartości widgetów.

### Etap 2 — wydzielenie KeithleyConfigurationPanel

- wydzielić sekcję widoczną na referencyjnym zrzucie;
- sparametryzować tryb `MANUAL_CONNECTED`, `PLAN_EDIT`, `READ_ONLY_SNAPSHOT`;
- pozostawić historię i live controls wyłącznie w manual host;
- dodać testy, że `PLAN_EDIT` nie może wywołać kontrolera ani VISA.

**Wyjście:** ten sam panel działa w testowym manual host i sweep host.

### Etap 3 — provider Keithley

- zaimplementować descriptor, snapshot, panel factory, parameter descriptors;
- zaimplementować summary, walidację każdego punktu, compile fragment i safe shutdown;
- zarejestrować provider w rejestrze urządzeń;
- nie modyfikować rdzenia Sweeps dla kanału A/B ani current/voltage.

**Wyjście:** wszystkie informacje o Keithley pochodzą z jego modułu.

### Etap 4 — nowa biblioteka i DeviceNode

- ograniczyć bibliotekę do urządzeń i Flow;
- wdrożyć drag-and-drop urządzenia;
- dodać `DeviceNode` z nieprzezroczystym snapshotem;
- otwierać editor providera po dwukliku i z menu kontekstowego;
- obsłużyć kilka niezależnych okien przez stabilne `node_id`.

**Wyjście:** operator buduje drzewo urządzeniami, nie technicznymi akcjami.

### Etap 5 — wiązanie osi i wieloetapowy wykres

- pokazać `Use as sweep axis` przy parametrach dostarczonych przez provider;
- osadzić wspólną tabelę etapów i scatter plot;
- obsłużyć points/step, linear/log/list i politykę granic;
- walidować wszystkie punkty przez provider;
- prezentować dokładny count, limity i błędy.

**Wyjście:** Keithley obsługuje fixed i sweep bez drugiego formularza.

### Etap 6 — neutralna kompilacja i bezpieczny run

- kompilować `DeviceNode` przez provider;
- zachować zagnieżdżone pętle i atomowe checkpointy;
- dodać immutable `RunSnapshot`;
- składać wymagany `finally` z modułów urządzeń;
- kwalifikować stop, error, compliance, storage failure i utratę połączenia.

**Wyjście:** przykładowy plan Keithley działa na symulatorze bez device-specific logiki w UI.

### Etap 7 — Rigol i Anritsu

- powtórzyć ekstrakcję wspólnych paneli w ich własnych modułach;
- wdrożyć provider bez kopiowania kodu Keithley;
- potwierdzić, że rdzeń Sweeps nie wymaga nowych warunków;
- uruchomić scenariusz Keithley × Rigol × Anritsu i round-trip PyThat.

**Wyjście:** architektura dowodzi rozszerzalności na trzech różnych urządzeniach.

### Etap 8 — usunięcie ścieżki tymczasowej i polish UI

- usunąć klasy i mapy z rozdziału 14.2;
- przemianować stronę i teksty na Sweeps;
- dopracować lekkie karty, ikony, odstępy, statusy, menu kontekstowe i dostępność;
- dodać undo/redo, autosave i recovery dla okien urządzeń;
- wykonać testy operatorskie na rzeczywistym stanowisku.

**Wyjście:** istnieje jedna wspierana ścieżka konfiguracji każdego urządzenia.

## 16. Strategia testów

### 16.1. Test najważniejszy: równoważność manual ↔ sweep

Dla tej samej konfiguracji Keithley:

1. manual panel tworzy snapshot;
2. sweep panel otwiera ten snapshot bez utraty danych;
3. oba widoki pokazują te same wartości, jednostki, minima i maksima;
4. oba wywołują ten sam walidator domenowy;
5. provider kompiluje snapshot do tej samej semantyki operacji co zatwierdzona ścieżka
   ręczna;
6. round-trip nie zmienia niewidocznych pól ani precyzji.

### 16.2. Izolacja modułów

- test nie pozwala importować `KeithleyPage` przez `app/sweeps`;
- test architektury wykrywa `keithley`, `rigol`, `anritsu` w warunkach rdzenia Sweeps;
- brak limitów urządzeń w plikach ogólnych;
- dodanie fake providera umożliwia drop, edycję, walidację i compile bez zmiany strony.

### 16.3. UI i drzewo

- drag-and-drop na korzeń i pod oś;
- menu kontekstowe i dwuklik otwierają to samo okno węzła;
- osobne `node_id` mają osobny stan okna;
- `Apply`, `Revert`, `Cancel`, undo/redo;
- zmiana trybu unieważniająca oś wymaga potwierdzenia;
- offline nie powoduje wywołania hardware.

### 16.4. Oś

- rosnące i malejące etapy;
- deduplikacja i zachowanie wspólnej granicy;
- linear/log/list;
- punkty kontra przedziały;
- bardzo mały i bardzo duży krok;
- wszystkie punkty walidowane, nie tylko start/stop;
- stabilna reprezentacja wartości w HDF5.

### 16.5. Safety i wykonanie

- brak OUTPUT ON podczas edycji i preview;
- preflight blokuje niedozwolony punkt;
- ARM i uprawnienia sprawdzane przed energizacją;
- wymagany output jest włączany raz, a nie dla każdego punktu;
- przejścia punktowe nie zawierają domyślnego OUTPUT OFF ani rampy do zera;
- dwa kolejne checkpointy potwierdzają zmianę setpointu przy zachowanym stanie outputu;
- plan nie wywołuje sprzętowego/listowego sweepu Keithley ani Rigola;
- compliance zatrzymuje run;
- stop, wyjątek i błąd zapisu zawsze uruchamiają cleanup wszystkich providerów;
- brak dalszych punktów po rozpoczęciu shutdownu.

### 16.6. Storage

- dokładne osie Keithley i Rigol;
- stałe ustawienie A zapisane jako kontrolka, nie wymiar;
- widmo przypisane do pełnego indeksu zagnieżdżonego;
- częściowy plik po przerwaniu jest oznaczony i czytelny;
- PyThat round-trip dla sukcesu, stopu i błędu.

## 17. Kryteria akceptacji architektury

Architektura jest zaakceptowana dopiero, gdy wszystkie warunki są spełnione:

1. Node Library zawiera urządzenia i ogólne Flow, bez list parametrów urządzeń.
2. Przeciągnięcie `Keithley 2600` tworzy działający `DeviceNode`.
3. Dwuklik otwiera osobne okno oparte na współdzielonym `KeithleyConfigurationPanel`.
4. Manual page i sweep node używają tego samego modelu, view modelu, opisów pól i safety.
5. Stały `Keithley A = 0.5 mA` nie tworzy osi.
6. `Keithley B current` może zostać oznaczony jako oś wieloetapowa.
7. Wykres pokazuje dokładny, deduplikowany wektor punktów.
8. Niedozwolony punkt blokuje Run przez walidator modułu Keithley.
9. Projektowanie i preview nie komunikują się z urządzeniem.
10. Provider kompiluje węzeł bez interpretowania pól Keithley przez Sweeps.
11. Runner zmienia setpointy punkt po punkcie i pobiera pojedyncze widmo dla każdej
    współrzędnej, bez używania wbudowanych sweepów urządzeń.
12. Output pozostaje włączony pomiędzy poprawnymi punktami; nie występuje per-point OFF/ON.
13. Cleanup modułu wykonuje rampę i OUTPUT OFF dla każdej ścieżki zakończenia.
14. Nie istnieje druga aktywna lista parametrów lub limitów Keithley w module Sweeps.
15. Fake provider daje się dodać bez zmiany kodu głównej strony.
16. Plik HDF5 odtwarza dokładne osie i widma w PyThat.
17. Dopiero po spełnieniu powyższych warunków rozpoczyna się analogiczna integracja Rigola.

## 18. Kryteria akceptacji UX

- interfejs jest lekki, jasny i oparty na spójnych kartach oraz ikonach;
- kliknięcie urządzenia zawsze pokazuje zrozumiałe podsumowanie;
- dwuklik, Enter i `Open editor` prowadzą do tego samego edytora;
- menu kontekstowe zawiera Edit, Duplicate, Disable, Move, Delete;
- kluczowe parametry są widoczne bez otwierania YAML;
- nazwy i kolejność pól Keithley odpowiadają ręcznej zakładce;
- błędy są przypięte do pola, etapu i węzła;
- dolny pasek pokazuje blockers, warnings, liczbę punktów/widm, czas i rozmiar;
- okna zachowują pozycję i rozmiar per `node_id`;
- aplikacja nie używa ciemnych, ciężkich paneli wewnątrz jasnego motywu;
- ikony nie zastępują tekstu w krytycznych operacjach bezpieczeństwa.

## 19. Otwarte decyzje przed kodowaniem

Przed Etapem 1 należy zatwierdzić tylko decyzje wpływające na kontrakt:

1. Czy snapshot konfiguracji urządzenia ma być dataclass, Pydantic czy istniejący model
   ustawień z dedykowanym DTO? Rekomendacja: niezmienny dataclass/DTO niezależny od Qt.
2. Czy v1 dopuszcza więcej niż jedną oś w jednym węźle? Rekomendacja: jedna aktywna oś.
3. Czy edytor jest modeless? Rekomendacja: tak, z lokalną kopią roboczą i atomowym Apply.
4. Czy domyślna polityka wspólnej granicy etapów to deduplikacja? Rekomendacja: tak.
5. Czy urządzenie offline może być projektowane? Rekomendacja: tak, tylko z zatwierdzonym
   profilem i wyraźnym oznaczeniem snapshotu capabilities.
6. Czy output może być automatycznie włączony przez węzeł? Rekomendacja: tylko przez
   jawną politykę w snapshotcie, po preflighcie, ARM i kontroli uprawnień.

## 20. Pierwszy bezpieczny zakres implementacyjny

Najbliższa iteracja nie powinna obejmować Rigola, Anritsu ani dalszego polishu całej strony.
Powinna dostarczyć pionowy wycinek tylko dla Keithley:

1. typed `KeithleyChannelConfiguration`;
2. współdzielony `KeithleyConfigurationViewModel`;
3. wydzielony `KeithleyConfigurationPanel` użyty przez obecną ręczną stronę;
4. `DeviceSweepProvider` i provider Keithley;
5. prosty `DeviceNode` tworzony przez drag-and-drop;
6. okno planu z tym samym panelem w `PLAN_EDIT`;
7. fixed source level oraz jedna wieloetapowa oś source level;
8. preview bez VISA, walidacja wszystkich punktów i kompilacja na symulator;
9. bezpieczny cleanup;
10. test równoważności manual ↔ sweep.

Po demonstracji tego pionowego wycinka należy zatwierdzić zachowanie i dopiero wtedy
przenosić wzorzec na kolejne urządzenia. Dzięki temu Sweeps stanie się rzeczywistym
integratorem modułów, a nie drugim, równoległym systemem sterowania aparaturą.
