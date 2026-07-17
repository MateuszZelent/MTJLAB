# Masterplan drzewa pomiarów i wieloetapowych sweepów

**Projekt:** Lab Control  
**Urządzenia:** Keithley 2600/2602A, Rigol DG1032Z, Anritsu MS2830A  
**Format danych:** thaTEC:OS / PyThat / HDF5  
**Data:** 2026-07-16  
**Status:** plan wdrożenia do zatwierdzenia

## 1. Podsumowanie wykonawcze

Celem jest zastąpienie obecnego edytora YAML profesjonalnym, wizualnym kreatorem drzewa
pomiarowego. Operator ma budować eksperyment przez dodawanie urządzeń, pojedynczych stałych
nastaw, sweepów, opóźnień i akwizycji do hierarchicznego drzewa. Każda nastawa i każdy węzeł
sweepu otwierają własne, niezależne okno edycji właściwe dla danego urządzenia. Po lewej
stronie okna znajdują się parametry i tabela etapów, a po prawej wykres punktów, które
zostaną rzeczywiście wykonane.

Docelowy przykład procesu:

1. operator może dodać `Keithley / kanał A / set fixed source current`, np. `0.5 mA`, aby
   ustawić i utrzymywać kanał A na jednej wartości przez cały eksperyment;
2. dodaje zewnętrzny sweep `Keithley / kanał B / source current`;
3. definiuje zakres, np. `0.01 A → 0.15 A`, i liczbę punktów;
4. jako dziecko dodaje sweep `Rigol / CH1 / sine frequency`;
5. definiuje zakres `100 MHz → 1 GHz` i liczbę punktów;
6. jako dziecko Rigola dodaje `Anritsu / Acquire spectrum`;
7. kompilator wylicza iloczyn punktów, sprawdza wartości stałe, limity i dokładną kolejność;
8. runner ustawia kanał A tylko raz, a następnie dla każdego prądu kanału B przechodzi po
   wszystkich częstotliwościach Rigola, czeka na
   stabilizację i pobiera pojedyncze, zsynchronizowane widmo Anritsu;
9. każdy checkpoint jest zapisywany do HDF5 w strukturze odtwarzanej przez PyThat.

Wielosegmentowy sweep jest jedną osią logiczną. Operator może zdefiniować na przykład:

- etap 1: `0.01 A → 0.10 A`, 100 punktów;
- etap 2: `0.10 A → 0.15 A`, 20 punktów.

Wykres pokazuje oba etapy jako jedną serię punktów, z osobnym kolorem etapów. Wspólna
granica `0.10 A` jest domyślnie zapisywana i wykonywana tylko raz, dlatego powstaje 119
unikalnych punktów. Interfejs zawsze pokazuje tę liczbę przed uruchomieniem.

Ten projekt powinien rozwijać istniejące: `Recipe`, `RecipeCompiler`, `RecipeRunner`,
`Hdf5RunWriter`, `ThatecSchemaMapper` i `ThatecHdf5Writer`. Nie należy tworzyć drugiego,
niezależnego języka eksperymentów ani osobnej ścieżki wykonawczej dla GUI.

## 2. Interpretacja intencji użytkownika

### 2.1. Drzewo opisuje kolejność i zagnieżdżenie

Położenie węzła w drzewie ma znaczenie wykonawcze:

```text
Measurement recipe
└─ Keithley B · Current sweep
   ├─ Configure/verify Keithley B
   └─ Rigol CH1 · Sine frequency sweep
      ├─ Configure/verify Rigol CH1
      ├─ Wait for settling
      ├─ Measure Keithley B
      └─ Anritsu · Acquire spectrum TRAC1
```

Keithley jest pętlą zewnętrzną, a Rigol pętlą wewnętrzną. Jeżeli Keithley ma `Nk` punktów,
a Rigol `Nr` punktów, Anritsu zapisze `Nk × Nr` widm. Węzły na tym samym poziomie są
wykonywane od góry do dołu. Dzieci sweepu są wykonywane dla każdego jego punktu.

### 2.2. Sweep urządzenia oznacza programową pętlę punktową

Sweep częstotliwości Rigola wymagany w tym scenariuszu nie może być utożsamiany z ciągłym,
wewnętrznym sweepem generatora. Aplikacja musi ustawić jedną konkretną częstotliwość,
potwierdzić ją odczytem zwrotnym, odczekać settle, zlecić pojedynczy sweep Anritsu i zapisać
checkpoint. Dopiero potem przechodzi do następnej częstotliwości.

Sprzętowy sweep Rigola może pozostać osobną funkcją sterowania ręcznego, ale nie zapewnia
jednoznacznego przypisania widma Anritsu do częstotliwości generatora bez dodatkowej,
zakwalifikowanej synchronizacji triggerów.

### 2.3. Okno sweepu jest edytorem węzła, nie osobnym eksperymentem

Każdy węzeł sweepu ma własne okno. Okno edytuje jeden obiekt domenowy identyfikowany przez
stabilne `node_id`. Zamknięcie okna nie usuwa węzła. Ponowne otwarcie tego samego węzła
przywraca jego stan, położenie i rozmiar. Dwa różne sweepy tego samego urządzenia otwierają
dwa różne okna.

### 2.4. HDF5 jest wynikiem skompilowanego planu

Plik HDF5 nie powinien być budowany bezpośrednio z kontrolek GUI. Źródłem prawdy jest
zatwierdzony snapshot receptury oraz jego skompilowany plan. Ten sam snapshot steruje
runnerem, metadanymi, osiami thaTEC, liczbą checkpointów i odtwarzaniem danych w PyThat.

### 2.5. Pojedyncza wartość nie jest sweepem jednopunktowym

Stała nastawa, np. `Keithley A current = 0.5 mA`, jest osobnym rodzajem operacji. Nie należy
modelować jej jako sweepu z jednym punktem, ponieważ sweep ma semantykę pętli i wymiaru
danych. Węzeł stały:

- wybiera urządzenie, kanał A/B, tryb i jedną wartość;
- nie tworzy osi i nie mnoży liczby widm;
- jest wykonywany w miejscu, w którym znajduje się w sekwencji;
- może obowiązywać przez wszystkie późniejsze, zagnieżdżone sweepy;
- jest zapisany w `scan_definition` jako stała kontrolka z polem `value`;
- nadal wymaga compliance, limitów, ARM i jawnego `OUTPUT ON`, jeżeli ma zasilać DUT.

## 3. Stan obecny i analiza luk

### 3.1. Elementy, które już istnieją

- `app/recipes/models.py` zawiera bezpieczny, deklaratywny model `RecipeNode` oraz parser
  receptur YAML bez wykonywania Pythona i surowych komend SCPI;
- istniejąca akcja `configure_keithley` już potrafi przyjąć literalny `level`, więc backend
  ma podstawę do pojedynczej nastawy kanału A/B; brakuje jej jawnego modelu i kreatora UI;
- `app/engine/compiler.py` rozwija `sequence` i zagnieżdżone `sweep`, podstawia setpointy,
  sprawdza część limitów oraz wylicza hash planu;
- `app/engine/runner.py` wykonuje akcje, zapisuje punkt przy widmie, obsługuje pause/stop,
  compliance i bezpieczne wyłączenie;
- `app/ui/main_window.py` ma stronę `RecipePage`, ale obecnie jest ona edytorem YAML i
  płaską listą skompilowanych akcji;
- `app/storage/hdf5_writer.py` zapisuje prywatny indeks aplikacji i deleguje publiczny
  kontrakt do `ThatecHdf5Writer`;
- `app/storage/thatec_schema_mapper.py` odtwarza osie z przodków węzła
  `acquire_spectrum`;
- `app/storage/thatec_writer.py` tworzy grupy `devices`, `labbook`, `measurement` i
  `scan_definition`, checkpointuje setpointy, pomiary oraz widma;
- `recipes/example_nested_sweep.yml` już reprezentuje zagnieżdżony eksperyment
  Keithley × Rigol × Anritsu.

### 3.2. Brakujące funkcje domenowe

1. Nie ma rejestru sweepowalnych parametrów urządzeń. Obsługiwane cele są zapisane ręcznie
   w kilku mapach i obecnie brakuje m.in. `rigol.1.frequency`.
2. `sweep` przyjmuje tylko `start`, `stop`, `points` i `spacing`; nie obsługuje etapów ani
   jawnej listy punktów.
3. `RecipeNode.data` jest luźnym słownikiem. GUI potrzebuje typowanych specyfikacji węzłów,
   opisów pól, jednostek, limitów i zależności.
4. Kompilator od razu spłaszcza całe drzewo do listy akcji. Duże plany mogą zajmować dużo
   pamięci i utrudniają szybki podgląd.
5. `MeasurementPoint` nie przechowuje jawnie indeksów każdej osi, identyfikatora etapu,
   lokalnego indeksu etapu ani współrzędnej punktu.
6. Mapper thaTEC zakłada jednorodny sweep liniowy lub logarytmiczny. Wieloetapowa,
   nieregularna oś wymaga zapisania dokładnego wektora wartości.

### 3.3. Brakujące funkcje UI/UX

1. Drzewo na stronie receptur jest płaskim podglądem akcji po kompilacji, a nie edytorem.
2. Nie ma wizualnego wyboru `Keithley → Channel A/B → Set fixed current/voltage` ani
   dedykowanego formularza pojedynczej wartości.
3. Nie ma biblioteki węzłów, drag-and-drop, menu kontekstowego ani walidacji relacji
   rodzic–dziecko.
4. Nie ma osobnych okien sweepów ani wykresu planowanych punktów.
5. Nie ma tabeli etapów, deduplikacji granic, odwracania etapu ani importu listy punktów.
6. Nie ma podglądu czasu, miejsca na dysku, liczby widm i pierwszych/ostatnich punktów.
7. Błędy są pokazywane głównie w modalnym `QMessageBox`; brak trwałej listy problemów
   przypiętej do konkretnych węzłów i pól.
8. Nie ma undo/redo ani ochrony przed utratą niezapisanych zmian.

### 3.4. Ważne ograniczenie aktualnego profilu

Zakresy `0.01 A → 0.15 A` oraz `100 MHz → 1 GHz` dobrze opisują oczekiwany UX, ale nie są
obecnie poprawnym planem dla profilu w repozytorium:

- limit roboczy Keithley B wynosi obecnie `0 A → 10 mA`;
- limit częstotliwości Rigol CH1 wynosi obecnie `1 Hz → 100 MHz`;
- Rigol CH2 ma jeszcze węższy profil;
- akwizycja Anritsu wymaga uzupełnionych i zatwierdzonych limitów RF.

Kreator ma pozwolić wpisać wartość i natychmiast pokazać problem, ale przycisk uruchomienia
musi pozostać zablokowany. Zwiększenie limitu w GUI nie jest wystarczające: wymaga
potwierdzenia możliwości konkretnego modelu, okablowania, DUT i profilu bezpieczeństwa.

## 4. Niezmienne zasady projektu

1. **Jedno źródło prawdy:** GUI, YAML, kompilator, runner i HDF5 korzystają z tego samego
   modelu receptury.
2. **Brak komend przy projektowaniu:** otwieranie okien, generowanie punktów i preflight nie
   dotykają VISA.
3. **Preflight całego planu:** wszystkie punkty i zależności są walidowane przed pierwszym
   `OUTPUT ON`.
4. **Jednostki obowiązkowe:** tekstowe wartości mają jawne jednostki, a domena przechowuje
   wartości SI wraz z wymiarem fizycznym.
5. **Punkt jest atomem checkpointu:** setpointy, odczyty, status i widmo należą do jednego
   identyfikowalnego punktu.
6. **Bezpieczne przerwanie:** stop, błąd zapisu, timeout, compliance i utrata połączenia
   prowadzą do rampy Keithley, wyłączenia źródeł i `ABORT` Anritsu.
7. **Dokładna odtwarzalność:** zapisujemy recepturę, wygenerowane wartości osi, profil,
   capabilities, IDN, wersje i hash planu.
8. **Brak cichego poprawiania planu:** clamp może służyć w sterowaniu ręcznym, ale kreator
   receptury nie może sam zmienić `0.15 A` na `10 mA`. Ma zablokować i wyjaśnić błąd.
9. **Przejrzysta liczba punktów:** UI rozróżnia punkty, przedziały i liczbę widm.
10. **PyThat jest testowanym kontraktem:** zgodność potwierdza test round-trip, nie samo
    otwarcie pliku przez `h5py`.

## 5. Docelowy proces operatora

### 5.1. Utworzenie receptury

1. Operator wybiera **Recipes → New measurement**.
2. Nadaje nazwę, wybiera katalog wynikowy i opcjonalnie uzupełnia labbook.
3. Na ekranie widzi pusty korzeń `Measurement sequence` i bibliotekę urządzeń.
4. Opcjonalnie przeciąga `Keithley → Channel A → Set fixed source current` na korzeń.
5. Wybiera kanał A, tryb current, pojedynczą wartość, compliance, sense, NPLC i settle.
6. Drzewo pokazuje `Keithley A · Current = 0.5 mA · fixed`; węzeł nie dodaje wymiaru.
7. Przeciąga `Keithley → Channel B → Sweep source current` za stałą nastawę kanału A.
8. Dwuklik otwiera okno sweepu Keithley B.
9. Ustawia tryb źródła, compliance, NPLC, sense, settle oraz etapy osi.
10. Zatwierdza zmiany; drzewo pokazuje skrót i liczbę wygenerowanych punktów.
11. Przeciąga `Rigol → CH1 → Sweep sine frequency` jako dziecko Keithleya B.
12. Otwiera osobne okno Rigola i ustawia waveform, poziomy, impedancję oraz oś.
13. Pod Rigolem dodaje `Wait`, opcjonalne `Measure Keithley A`, `Measure Keithley B` oraz
    `Anritsu → Acquire single spectrum`.
14. W oknie Anritsu ustawia zakres analizatora, RBW/VBW, reference level, trace, averaging
    i timeout.
15. Uruchamia **Validate & preview**.

### 5.2. Preflight i zatwierdzenie

Preflight pokazuje pięć kart:

- **Structure:** poprawność drzewa i relacji;
- **Safety:** limity urządzeń, DUT, compliance, RF i stan profilu;
- **Capabilities:** zgodność z wykrytym modelem, firmware i opcjami;
- **Execution:** liczba punktów, akcji, kolejność, settle i szacowany czas;
- **Storage:** oczekiwany rozmiar HDF5, wolne miejsce, docelowa ścieżka i zgodność PyThat.

Każdy problem ma poziom:

- `BLOCKER` — run niemożliwy;
- `WARNING` — wymaga świadomego potwierdzenia, ale nie zmienia limitów bezpieczeństwa;
- `INFO` — wskazówka optymalizacyjna.

Kliknięcie problemu zaznacza węzeł, otwiera właściwe okno i ustawia fokus na błędnym polu.

### 5.3. Podgląd planu

Przed uruchomieniem operator otrzymuje:

- czytelne drzewo logiczne, bez tysięcy powtarzających się akcji;
- osobną listę stałych nastaw, ich kolejność i liczbę rzeczywistych wykonań;
- macierz osi i ich kolejność;
- dokładną liczbę unikalnych punktów każdej osi;
- liczbę widm i liczbę próbek widma;
- pierwsze 10 i ostatnie 10 punktów;
- losową próbkę punktów środkowych;
- wykresy osi;
- estymację czasu minimalnego, typowego i pesymistycznego;
- estymację rozmiaru pliku przed i po kompresji;
- listę stanów wyjść przed startem i po `finally`.

### 5.4. Uruchomienie

Run wymaga niezmienionego hasha receptury i profilu od czasu preflightu. Jeżeli operator
zmieni choć jeden etap, compliance, ustawienia Anritsu albo profil, poprzedni preflight jest
unieważniany. Przycisk zmienia się na **Validate again**.

## 6. Architektura informacji strony Recipes

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Recipes  [New] [Open] [Save] [Undo] [Redo]     Profile: APPROVED / LOCKED   │
├─────────────────┬───────────────────────────────────────┬────────────────────┤
│ NODE LIBRARY    │ MEASUREMENT TREE                      │ INSPECTOR          │
│                 │                                       │                    │
│ Keithley        │ ▾ Measurement sequence                │ Selected node      │
│  CH A           │   • Keithley A · I = 0.5 mA · fixed   │ summary            │
│  CH B           │   ▾ Keithley B · Current · 200 pts    │ validation         │
│ Rigol           │     ▾ Rigol CH1 · SIN Freq · 101 pts  │ quick fields       │
│  CH1 / CH2      │       • Wait 100 ms                   │ [Open editor]      │
│ Anritsu         │       • Measure Keithley A + B        │                    │
│ Flow            │       • Acquire TRAC1                  │                    │
│  Wait/Sequence  │   ▾ Finally                           │                    │
│  Comment        │     • Ramp Keithley A + B to zero     │                    │
│                 │     • Outputs OFF                     │                    │
├─────────────────┴───────────────────────────────────────┴────────────────────┤
│ BLOCKERS 2 · WARNINGS 1  |  20 200 spectra  |  est. 7 h 42 min  |  8.4 GB  │
│ [Validate & preview]                                      [Run — blocked]    │
└──────────────────────────────────────────────────────────────────────────────┘
```

### 6.1. Biblioteka węzłów

Biblioteka jest filtrowana przez dostępne urządzenia i capabilities. Każda pozycja ma ikonę,
nazwę, krótki opis i badge `safe at OUTPUT OFF`, `requires ARM` albo `acquisition`.

Kategorie v1:

- **Structure:** Sequence, Group, Comment;
- **Keithley:** Set fixed current, Set fixed voltage, Measure only, Sweep current, Sweep
  voltage, Measure, Arm, Output, Ramp to zero — osobno dla kanałów A i B;
- **Rigol:** Set fixed waveform/frequency/levels, Sweep frequency, Sweep high level, Sweep
  low level, Arm, Output — osobno dla CH1 i CH2;
- **Anritsu:** Configure spectrum, Acquire single spectrum;
- **Timing:** Fixed wait, Wait for stability;
- **Safety/finally:** Ramp to safe value, Output off, Abort acquisition.

### 6.2. Drzewo

Drzewo powinno używać `QTreeView` z własnym `QAbstractItemModel`, a nie budować modelu
domenowego bezpośrednio w `QTreeWidgetItem`. Daje to kontrolę nad drag-and-drop, indeksami,
undo/redo, walidacją i testowaniem bez widoku.

Wiersz drzewa pokazuje:

- ikonę urządzenia lub typu;
- nazwę operatora;
- skrót parametru i zakresu albo pojedynczą wartość z badge `fixed`;
- liczbę punktów;
- status walidacji;
- włączony/wyłączony stan węzła;
- przewidywany czas lub liczbę checkpointów dla dużych poddrzew.

### 6.3. Dozwolone operacje

- dodanie przez drag-and-drop lub menu `+ Add`;
- duplikowanie węzła z nowymi identyfikatorami;
- przenoszenie z podglądem wpływu na liczbę punktów;
- wyłączenie bez usuwania;
- zmiana nazwy;
- kopiuj/wklej jako fragment receptury;
- zwijanie całego poddrzewa;
- usunięcie z możliwością undo;
- otwarcie dedykowanego edytora dwuklikiem lub Enterem.

Nie wolno przeciągnąć akcji do węzła liściowego. `Finally` akceptuje wyłącznie akcje
bezpiecznego zakończenia. UI odrzuca niedozwolony drop, a parser i kompilator ponownie
sprawdzają tę regułę niezależnie od GUI.

### 6.4. Inspector

Inspector służy do szybkiej orientacji, nie zastępuje pełnego okna. Pokazuje status,
urządzenie/kanał, parametr, zakres, liczbę punktów, settle, liczbę dzieci i ostatnie błędy.
Pola podstawowe mogą być edytowane inline. Złożony sweep ma przycisk **Open sweep editor**,
a pojedyncza nastawa **Open fixed-value editor**.

## 7. Osobne okna nastaw i sweepów dla każdego urządzenia

### 7.1. Wspólna rama

Każde okno jest modeless i nie blokuje pracy z drzewem. Rekomendowana implementacja to
`QDialog` z `Qt.Window`, zarządzany przez `SweepEditorWindowManager`.

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Keithley B · Source current sweep                         ● valid   119 pts  │
├────────────────────────────────────────┬─────────────────────────────────────┤
│ DEVICE & PARAMETER                     │ PLANNED POINTS                      │
│ Channel: B                             │  Current [A]                        │
│ Mode: Current source                   │        •••••••••••••••••••          │
│ Parameter: Source current              │  •••••                  • • •       │
│ Compliance: 67 mV                      │                                     │
│ NPLC: 1.0   Sense: 4-wire              │  stage 1 ●  stage 2 ●  invalid ×   │
│ Settle: 100 ms                         │  [Fit] [Linear/Log Y] [Export CSV]  │
├────────────────────────────────────────┤                                     │
│ STAGES                                 │ Hover: stage 2 · point 7/20         │
│ #  Start    Stop     Points Spacing    │        0.118421 A · global 106/119 │
│ 1  0.01 A   0.10 A   100    Linear     │                                     │
│ 2  0.10 A   0.15 A    20    Linear     │                                     │
│ [+ Add] [Duplicate] [↑] [↓] [Delete]   │                                     │
├────────────────────────────────────────┴─────────────────────────────────────┤
│ Shared boundary: Remove duplicate  | Generated: 119 | Δ min/max: …          │
│ [Revert] [Apply] [Apply and close]                              [Cancel]     │
└──────────────────────────────────────────────────────────────────────────────┘
```

Okno pracuje na kopii roboczej. **Apply** wykonuje walidację syntaktyczną i atomowo
aktualizuje węzeł. **Cancel** porzuca niezastosowane zmiany. Każde Apply jest pojedynczym
poleceniem undo.

### 7.2. Rejestr okien

- klucz: `recipe_document_id + node_id`;
- jedno żywe okno dla jednego węzła;
- aktywacja istniejącego okna zamiast tworzenia duplikatu;
- `QPointer` lub sygnał `destroyed`, aby nie przechowywać martwych referencji;
- zamknięcie dokumentu pyta o niezastosowane zmiany we wszystkich oknach;
- usunięcie węzła zamyka jego okno po potwierdzeniu;
- stan geometrii jest zapisywany per typ okna, a nie w recepturze pomiarowej.

### 7.3. Okno Keithley

Pola zależne od kanału i trybu:

- kanał A/B;
- tryb current/voltage;
- sweepowany parametr;
- compliance właściwego wymiaru;
- source range i autorange;
- measure voltage/current range;
- NPLC;
- 2-wire/4-wire;
- settle per punkt;
- strategia przejścia do pierwszego punktu: bezpośrednio lub bezpieczna rampa;
- maksymalny krok `ΔI`/`ΔV` i szybkość rampy;
- zachowanie po compliance: domyślnie stop całego runu;
- wartość bezpieczna i akcja `finally`.

Po prawej wykres pokazuje nie tylko wartości, ale także `Δ` pomiędzy punktami. Przekroczenie
maksymalnego kroku jest oznaczone czerwonym łącznikiem. Panel pod wykresem pokazuje
konserwatywną moc `|I × compliance V|` albo `|V × compliance I|` i porównuje ją z limitem
DUT.

### 7.4. Okno pojedynczej nastawy Keithley

Biblioteka węzłów udostępnia osobno:

- `Keithley → Channel A → Set fixed source current`;
- `Keithley → Channel A → Set fixed source voltage`;
- `Keithley → Channel B → Set fixed source current`;
- `Keithley → Channel B → Set fixed source voltage`;
- `Keithley → Channel A/B → Measure only`.

Dwuklik otwiera prostsze, dedykowane okno:

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ Keithley · Fixed source value                                      ● valid  │
├──────────────────────────────────────┬───────────────────────────────────────┤
│ Channel            [A ▼]             │ SETPOINT SUMMARY                      │
│ Mode               [Current ▼]       │                                       │
│ Source current     [0.5 mA        ]  │ Channel A · Current source            │
│ Voltage compliance [100 mV        ]  │ I = 0.5 mA                            │
│ Source range       [Auto ▼]           │ V compliance = 100 mV                 │
│ Sense              [4-wire ▼]         │ Worst-case power = 50 µW              │
│ NPLC               [1.0           ]  │                                       │
│ Settle             [100 ms        ]  │ Profile: within approved limits ✓     │
│ Apply behavior     [Ramp safely ▼]    │ Output: remains OFF until ARM + ON    │
├──────────────────────────────────────┴───────────────────────────────────────┤
│ [Revert] [Apply] [Apply and close]                                [Cancel]  │
└──────────────────────────────────────────────────────────────────────────────┘
```

Najważniejsze reguły:

1. wybór kanału A/B jest zawsze jawny i widoczny w tytule oraz wierszu drzewa;
2. zmiana current ↔ voltage zmienia jednostkę wartości i compliance;
3. `measure_only` ukrywa pola source level/compliance i nigdy nie włącza wyjścia;
4. `Apply` aktualizuje recepturę, ale nie wysyła komend do urządzenia;
5. konfiguracja źródła nie oznacza włączenia wyjścia — `Arm` i `Output ON` są osobnymi,
   audytowalnymi węzłami;
6. UI pokazuje konserwatywną moc `|source × compliance|` oraz limit DUT;
7. ustawienie przed sweepem wykonuje się raz i pozostaje aktywne przez cały sweep;
8. ustawienie umieszczone wewnątrz sweepu wykonuje się dla każdego punktu rodzica; UI
   pokazuje wtedy badge, np. `repeated 200×`, i ostrzega, jeżeli powtórzenie wygląda na
   niezamierzone;
9. zmiana stałej nastawy nie tworzy osi, ale unieważnia hash i wymaga nowego preflightu;
10. `finally` musi zawierać rampę i OFF dla każdego kanału, który mógł zostać włączony.

To samo podejście należy zastosować do stałych parametrów Rigola: kanał, waveform,
frequency, HighL/LowL, phase i load są pojedynczą konfiguracją, jeżeli operator nie wybiera
sweepu.

### 7.5. Okno Rigol

Pola:

- kanał CH1/CH2;
- waveform, w tym `SIN`;
- sweepowany parametr: frequency, high level lub low level;
- stałe parametry pozostałych kontrolek;
- output load `HIGHZ`/`50 Ω`;
- minimalna impedancja DUT;
- phase, amplitude/levels i offset;
- settle po zmianie częstotliwości;
- readback tolerance;
- opcjonalna kolejność forward/reverse/serpentine.

Dla scenariusza Anritsu UI nazywa funkcję **Stepped frequency sweep for synchronized
acquisition**. Opcja sprzętowego, ciągłego sweepu jest wyraźnie oddzielona i nie może mieć
`Acquire spectrum` jako zwykłego dziecka bez osobnego protokołu triggerów.

### 7.6. Okno Anritsu

`Acquire spectrum` nie jest osią sweepu w tym scenariuszu, ale również powinno mieć osobne
okno konfiguracji akwizycji:

- start/stop albo center/span;
- RBW, VBW;
- sweep points;
- reference level;
- attenuation/preamp zgodne z profilem;
- trace;
- detector;
- averaging i jednoznaczna liczba nowych sweepów;
- timeout `INIT:IMM`/`*OPC?`;
- nazwa wskaźnika w HDF5;
- zapis raw/processed/reference;
- przewidywany czas i rozmiar jednego trace.

Po prawej może być podgląd konfiguracji osi częstotliwości oraz ostatniego widma z symulatora,
ale nie wolno automatycznie kontaktować się z analizatorem podczas edycji receptury.

## 8. Model wieloetapowej osi

### 8.1. Model domenowy

```python
@dataclass(frozen=True, slots=True)
class SweepStage:
    id: str
    name: str
    start: Quantity
    stop: Quantity
    points: int
    spacing: Literal["linear", "log"]
    include_start: bool = True
    include_stop: bool = True

@dataclass(frozen=True, slots=True)
class SweepAxisSpec:
    target: str
    stages: tuple[SweepStage, ...]
    boundary_policy: Literal["deduplicate", "keep", "error"] = "deduplicate"
    traversal: Literal["forward", "reverse", "serpentine"] = "forward"
```

`Quantity` zachowuje wymiar, a wartości generowane są w SI. Tekst wpisany przez operatora
jest zachowany w snapshotcie dokumentu, ale wykonanie korzysta z wartości znormalizowanych.

### 8.2. Model pojedynczej wartości

```python
@dataclass(frozen=True, slots=True)
class FixedSetpointSpec:
    target: str
    value: Quantity
    apply_behavior: Literal["direct", "safe_ramp"] = "safe_ramp"
    settle_time_s: float = 0.0
```

`FixedSetpointSpec` korzysta z tego samego `ParameterRegistry` i tych samych walidatorów co
sweep, ale generuje dokładnie jedną akcję konfiguracji, a nie oś. W przypadku Keithley
pozostałe pola — kanał, source mode, compliance, range, sense i NPLC — należą do typowanej
konfiguracji urządzenia otaczającej ten setpoint.

W implementacji v1 wizualny węzeł **Set fixed value** może serializować się do istniejącej
akcji `configure_keithley` albo `configure_rigol` z literalną wartością. Referencja
`${keithley.A.current}` oznacza wartość pochodzącą ze sweepu, natomiast literalne `"0.5 mA"`
oznacza stałą nastawę. Dzięki temu nie trzeba tworzyć równoległej ścieżki wykonawczej.

### 8.3. Znaczenie liczby punktów

Interfejs używa etykiety **Liczba punktów**, nie niejednoznacznego słowa „kroki”. Dla
sweepu liniowego z włączonymi końcami:

```text
step = (stop - start) / (points - 1)
```

`points = 100` oznacza dokładnie 100 wartości łącznie ze startem i stopem. Panel pomocy
może dodatkowo pokazać `99 intervals`.

### 8.4. Łączenie etapów

Algorytm:

1. wygeneruj wartości każdego etapu osobno;
2. zweryfikuj skończoność, monotoniczność i wymiar;
3. dołącz etapy w kolejności z tabeli;
4. jeżeli ostatni punkt poprzedniego etapu jest równy pierwszemu punktowi następnego w
   tolerancji numerycznej i polityka to `deduplicate`, zachowaj jeden punkt;
5. zapisz mapowanie każdego punktu do `stage_id`, `stage_local_index` i
   `axis_global_index`;
6. ponownie sprawdź limity dla wszystkich wygenerowanych wartości.

Porównanie granic nie może używać prostego `==` dla float. Tolerancja powinna zależeć od
rozdzielczości urządzenia i dodatkowo mieć mały składnik względny.

### 8.5. Polityki granic

- **Remove duplicate — domyślna:** jeden pomiar na wspólnej granicy;
- **Keep both:** dwa kolejne pomiary tej samej wartości, jawnie widoczne jako dwa punkty;
- **Treat duplicate as error:** wymusza poprawienie etapów.

Włączenie `Keep both` wymaga ostrzeżenia, ponieważ oś ma powtórzoną współrzędną i niektóre
operacje xarray/interpolacji mogą zachowywać się niejednoznacznie.

### 8.6. Sweep logarytmiczny i mieszany

Każdy etap ma własny spacing. Jedna oś może łączyć etap liniowy i logarytmiczny. Log wymaga
wartości dodatnich i nie może przekraczać zera. HDF5 musi zapisać dokładny wektor wartości,
więc nie wolno opisywać całej osi jednym nieprawdziwym `start/stop/step`.

### 8.7. Jawna lista punktów — P2

Po ustabilizowaniu etapów należy dodać tryb `Explicit values`, z importem CSV, wklejaniem
kolumny i sortowaniem opcjonalnym. Domyślnie kolejność użytkownika jest zachowana. Funkcja
jest przydatna dla kalibracji, punktów rezonansowych i powtarzania wybranych wartości.

## 9. Wykres planowanych punktów

Wykres korzysta z PyQtGraph i renderuje scatter bez łączenia albo z delikatną linią kolejności.

Wymagania:

- oś X: globalny indeks wykonania lub numer punktu;
- oś Y: wartość fizyczna z automatycznym prefiksem SI;
- osobny kolor i legenda dla każdego etapu;
- granice etapów jako pionowe znaczniki;
- czerwone punkty poza profilem i pomarańczowe warningi;
- hover: etap, indeks lokalny/globalny, dokładna wartość, delta do poprzedniego punktu;
- zaznaczenie wiersza tabeli podświetla etap na wykresie i odwrotnie;
- zoom/pan, Fit, reset, eksport CSV i kopiowanie wartości;
- redukcja wyłącznie renderowania dla bardzo dużej liczby punktów;
- podpis `Rendered 10 000 of 1 000 000 points`; obliczenia i kompilacja korzystają z pełnej
  osi;
- deterministyczny downsampling zachowujący pierwszy, ostatni, granice etapów oraz min/max.

Zmiana komórki tabeli powinna aktualizować wykres z debounce około 100–200 ms. Generowanie
miliona punktów odbywa się poza wątkiem GUI lub przez analityczny podgląd, ale wynik zmiany
jest stosowany tylko, jeśli nadal odpowiada aktualnej rewizji edytora.

## 10. Model dokumentu i serializacja

### 10.1. Wersja schematu

Rekomendowana jest `schema_version: 2`, ponieważ wieloetapowe osie, typowane metadane i
stabilne ID etapów są zmianą kontraktu. Parser v1 pozostaje dostępny do odczytu, a migrator
zamienia pojedynczy `start/stop/points` na jeden etap v2.

### 10.2. Przykładowa receptura v2

Poniższy YAML opisuje intencję użytkownika. Nie jest zgodą na przekroczenie profilu.

```yaml
schema_version: 2
name: "Keithley B current × Rigol CH1 frequency × Anritsu spectrum"

root:
  id: sequence-main
  type: sequence
  name: Measurement sequence
  children:
    - id: anritsu-setup
      type: configure_anritsu
      start_frequency: "10 MHz"
      stop_frequency: "4 GHz"
      reference_level: "-10 dBm"
      points: 1001

    - id: configure-keithley-a-fixed
      type: configure_keithley
      name: Keithley A fixed current
      channel: A
      mode: current
      level: "0.5 mA"
      compliance: "100 mV"
      nplc: 1.0
      sense_mode: 4-wire
      settle_time: "100 ms"

    - id: sweep-keithley-b-current
      type: sweep
      name: Keithley B current
      target: keithley.B.current
      boundary_policy: deduplicate
      stages:
        - id: current-low
          name: Fine region
          start: "0.01 A"
          stop: "0.10 A"
          points: 100
          spacing: linear
        - id: current-high
          name: Coarse region
          start: "0.10 A"
          stop: "0.15 A"
          points: 20
          spacing: linear
      children:
        - id: configure-keithley-b
          type: configure_keithley
          channel: B
          mode: current
          level: "${keithley.B.current}"
          compliance: "67 mV"
          nplc: 1.0
          settle_time: "100 ms"

        - id: sweep-rigol-ch1-frequency
          type: sweep
          name: Rigol CH1 sine frequency
          target: rigol.1.frequency
          boundary_policy: deduplicate
          stages:
            - id: frequency-main
              start: "100 MHz"
              stop: "1 GHz"
              points: 101
              spacing: linear
          children:
            - id: configure-rigol-ch1
              type: configure_rigol
              channel: 1
              waveform: SIN
              frequency: "${rigol.1.frequency}"
              high_level: "3 mV"
              low_level: "-3 mV"
              output_load: HIGHZ
              dut_min_impedance: "50 ohm"

            - id: settle-rigol
              type: wait
              duration: "100 ms"

            - id: measure-keithley-b
              type: measure_keithley
              channel: B

            - id: measure-keithley-a
              type: measure_keithley
              channel: A

            - id: acquire-anritsu-trac1
              type: acquire_spectrum
              trace: TRAC1

finally:
  - id: ramp-keithley-a-zero
    type: ramp_keithley_to_zero
    channel: A
    deadline: "10 s"
  - id: ramp-keithley-b-zero
    type: ramp_keithley_to_zero
    channel: B
    deadline: "10 s"
  - id: rigol-ch1-off
    type: set_rigol_output
    channel: 1
    enabled: false
  - id: keithley-b-off
    type: set_keithley_output
    channel: B
    enabled: false
  - id: keithley-a-off
    type: set_keithley_output
    channel: A
    enabled: false
```

Po deduplikacji Keithley ma 119 punktów. Przy 101 punktach Rigola plan zawiera 12 019
widm. Stałe `0.5 mA` na kanale A nie zmienia tej liczby i nie tworzy wymiaru. Wariant z
jednym etapem Keithley B o 200 punktach zawiera 20 200 widm.

### 10.3. Synchronizacja GUI i YAML

Model dokumentu jest źródłem prawdy. YAML jest serializowany deterministycznie:

- stabilna kolejność pól;
- stabilne identyfikatory UUID lub czytelne ID z bezpiecznym sufiksem;
- brak utraty nieznanych, przyszłych metadanych podczas round-trip;
- komentarze użytkownika zachowane tam, gdzie pozwala na to `ruamel.yaml`;
- wartości prezentacyjne z jednostką, wartości SI generowane dopiero w kompilatorze;
- canonical JSON/YAML używany do hasha semantycznego, niezależnego od whitespace.

Tryb zaawansowany może pokazywać YAML w zakładce read-only z akcją **Edit source**.
Przejście do ręcznej edycji wymaga ponownego sparsowania i atomowego zastąpienia modelu.
Błąd YAML nie może uszkodzić ostatniej poprawnej wersji drzewa.

## 11. Rejestr parametrów urządzeń

Należy utworzyć centralny `ParameterRegistry`, używany przez UI, parser, kompilator, mapper
HDF5 i testy.

Przykładowy descriptor:

```python
ParameterDescriptor(
    target="rigol.1.frequency",
    device="rigol",
    channel="1",
    label="Frequency",
    dimension=DIMENSION_FREQUENCY,
    unit="Hz",
    fixed_settable=True,
    sweepable=True,
    required_capability="basic_waveform",
    limit_path="rigol.safety.channels.1.lab_limits.frequency",
    apply_action="configure_rigol",
    readback=True,
)
```

Minimalny zakres v1:

- `keithley.A.current`, `keithley.B.current`;
- `keithley.A.voltage`, `keithley.B.voltage`;
- `rigol.1.frequency`, `rigol.2.frequency`;
- `rigol.1.high_level`, `rigol.2.high_level`;
- `rigol.1.low_level`, `rigol.2.low_level`.

Każdy descriptor jawnie określa, czy parametr dopuszcza wartość stałą, sweep albo oba tryby.
Biblioteka UI generuje z tego osobne akcje **Set fixed value** i **Sweep value**, zamiast
zakładać, że każdy parametr jest sweepem.

Rejestr nie zastępuje safety validatorów. Dostarcza opis i routing, natomiast ostateczne
sprawdzenie wykonują istniejące walidatory oraz adapter.

## 12. Kompilator i plan wykonania

### 12.1. Pipeline

```text
RecipeDocument
  → schema validation
  → semantic tree validation
  → generate exact axes
  → capability and safety preflight
  → topology/count analysis
  → immutable ExecutionPlan
  → plan hash + storage schema
```

### 12.2. Dwa poziomy reprezentacji

Obecna płaska lista `PlanAction` może zostać utrzymana w pierwszej iteracji dla planów
mieszczących się w limicie. Docelowo potrzebne są:

- `LogicalExecutionPlan` — pętle, osie i akcje bez ekspansji;
- `ExecutionCursor` — iterator generujący kolejny punkt i akcje;
- `PreviewSampler` — pierwsze/ostatnie/wybrane punkty bez materializacji całości;
- opcjonalny `ExpandedExecutionPlan` wyłącznie dla małych receptur i testów.

To pozwoli projektować duże eksperymenty bez tworzenia milionów obiektów akcji w pamięci.
Limit `max_expanded_points` nadal obowiązuje jako limit bezpieczeństwa, nawet gdy plan jest
leniwy.

### 12.3. Tożsamość punktu

Każdy punkt powinien mieć:

```python
PointCoordinate(
    ordinal=105,
    axis_indices={"sweep-keithley-b-current": 105,
                  "sweep-rigol-ch1-frequency": 0},
    stage_ids={"sweep-keithley-b-current": "current-high"},
    stage_local_indices={"sweep-keithley-b-current": 6},
    setpoints_si={"keithley.B.current": 0.1131578947,
                  "rigol.1.frequency": 100_000_000.0},
)
```

`ordinal` jest monotoniczny i nie zmienia się przy zapisie. Identyfikator checkpointu może
być zbudowany z `plan_hash + ordinal`. HDF5 przechowuje indeksy osi i stage provenance.
`setpoints_si` zawiera również obowiązujące stałe nastawy, np. `keithley.A.current = 0.0005`,
aby każdy checkpoint był samowystarczalny, mimo że wartość nie jest osią.

### 12.4. Kolejność pojedynczego punktu

Przed wejściem do pierwszej pętli runner wykonuje preamble: konfiguracje stałe umieszczone
przed sweepami, ich readback, wymagany settle oraz jawne akcje ARM/ON. Następnie dla każdego
punktu:

1. sprawdź stop/pause i heartbeat;
2. ustaw zmienione setpointy sweepów od pętli zewnętrznej do wewnętrznej;
3. odczytaj konfigurację i potwierdź tolerancję;
4. odczekaj settle urządzeń, których wartość się zmieniła;
5. zmierz Keithley;
6. sprawdź compliance, napięcie, prąd i moc;
7. zainicjuj nowy single sweep Anritsu;
8. czekaj na `*OPC?` z deadline;
9. pobierz trace oraz potwierdź oś i liczbę próbek;
10. utwórz kompletny `MeasurementPoint`;
11. zapisz atomowy checkpoint HDF5 i flush;
12. wyemituj zdarzenie postępu i przejdź do następnego punktu.

### 12.5. Optymalizacja bez zmiany semantyki

Runner nie powinien ponownie konfigurować Keithley przy każdym punkcie wewnętrznego sweepu,
jeżeli jego setpoint się nie zmienił. Plan oznacza akcje jako zależne od osi. Cache ostatnio
potwierdzonej konfiguracji może pominąć redundantne ustawienie, ale nie może pominąć
wymaganego odczytu bezpieczeństwa po błędzie lub reconnect.

Stała konfiguracja umieszczona przed sweepem jest wykonywana raz. Jeżeli operator świadomie
umieści ją wewnątrz pętli, jej pozycja jest semantyczna i kompilator nie może sam przenieść
jej poza pętlę. Preview pokazuje dokładną liczbę wykonań każdego takiego węzła.

### 12.6. Pause, stop i resume

- Pause następuje wyłącznie po kompletnym checkpointcie zapisanym i opróżnionym na dysk;
- Stop natychmiast przerywa oczekiwania, ale kończy aktualną operację tylko w sposób
  wspierany przez adapter;
- po Stop zawsze wykonywane są dopuszczone akcje `finally` oraz emergency off;
- resume po zamknięciu aplikacji nie może zaczynać w środku punktu;
- P2 może dodać wznowienie od następnej bezpiecznej współrzędnej po porównaniu hasha planu,
  profilu, urządzeń i kompletności poprzedniego checkpointu.

## 13. Preflight bezpieczeństwa i capabilities

### 13.1. Walidacja strukturalna

- unikalne ID węzłów i etapów;
- brak cykli;
- co najmniej jedno dziecko sekwencji/sweepu;
- akwizycja ma jednoznaczny ancestry sweepów;
- każda referencja `${target}` ma aktywną oś nadrzędną;
- literalna wartość stała ma zgodny target, urządzenie, kanał i source mode;
- dwa aktywne węzły ustawiające ten sam kanał przed pomiarem nie są niejednoznaczne;
- `finally` zawiera wyłącznie bezpieczne akcje;
- każdy włączony output ma ARM i odpowiadający OFF/rampę;
- węzeł nie odwołuje się do wyłączonego urządzenia/kanału.

### 13.2. Walidacja każdego etapu i punktu

- jednostka i wymiar;
- NaN/Inf;
- `points >= 2`;
- log tylko dla wartości dodatnich;
- zakres profilu;
- zakres capability sprzętu;
- rozdzielczość i maksymalna liczba punktów;
- maksymalny skok i slew rate;
- dla Keithley: compliance przed output, zakres I/V/P i limit DUT;
- dla Rigola: waveform, częstotliwość, HighL/LowL, Vpp, offset, obciążenie i model prądu;
- dla Anritsu: zakres częstotliwości, reference level, moc na wejściu, tłumienie, preamp,
  trace i protokół single sweep.

### 13.3. Walidacja krzyżowa urządzeń

Preflight powinien wykrywać także problemy, których nie widać w pojedynczym oknie:

- zakres Anritsu nie obejmuje częstotliwości Rigola;
- oczekiwany sygnał plus tłumienie może przekroczyć bezpieczną moc wejścia RF;
- settle jest krótszy niż oszacowany czas zmiany źródła;
- sweep Anritsu i timeout nie mieszczą się w założonym czasie punktu;
- częstotliwość/poziom Rigola wymaga capability niewykrytego na danym firmware;
- iloczyn punktów przekracza limit albo wolne miejsce na dysku;
- jeden z etapów ma powtórzenia lub zmianę kierunku, która może być niezamierzona.
- stała nastawa kanału A i sweep kanału B nie przekraczają łącznych ograniczeń DUT,
  fixture, zasilania i dopuszczalnej mocy;

### 13.4. Bramka uruchomienia

Run jest dostępny tylko, gdy:

- liczba blockerów wynosi zero;
- profil jest zatwierdzony i jego hash nie zmienił się;
- wymagane urządzenia mają oczekiwane IDN/capabilities;
- ręczne sesje urządzeń są rozłączone zgodnie z obecną architekturą runnera;
- ścieżka zapisu jest dostępna i ma wystarczający zapas;
- operator zobaczył końcowe podsumowanie;
- receptura ma bezpieczny `finally`;
- hash planu odpowiada aktualnej rewizji dokumentu.

## 14. Szacowanie czasu i rozmiaru

### 14.1. Liczba checkpointów

```text
spectra = product(point_count of every sweep ancestor)
          × acquisitions per leaf
```

Dla 119 punktów Keithley, 101 punktów Rigola i jednej akwizycji: `119 × 101 = 12 019`.

### 14.2. Czas

Estymator powinien sumować:

- czas konfiguracji zmienionych urządzeń;
- readback;
- settle;
- pomiar Keithley zależny od NPLC;
- czas sweepu Anritsu;
- transfer trace;
- zapis i flush HDF5;
- margines percentylowy z poprzednich runów/symulacji.

Pokazujemy zakres, nie fałszywie dokładną jedną wartość. Po rozpoczęciu runu estymacja ETA
jest aktualizowana medianą i percentylem ostatnich punktów, osobno dla etapów.

### 14.3. Rozmiar

Minimalny model:

```text
raw_spectrum_bytes = spectra × anritsu_points × 8
coordinates_and_scalars = spectra × scalar_count × 8
metadata_and_hdf5_overhead = measured fixed + per-checkpoint overhead
required_free_space = pessimistic_estimate × safety_factor
```

Do bramki należy użyć oszacowania bez zakładanej skuteczności gzip albo bardzo
konserwatywnego współczynnika. Run zatrzymuje się bezpiecznie przed całkowitym zapełnieniem
dysku, zachowując czytelny plik częściowy.

## 15. Mapowanie do thaTEC:OS, PyThat i HDF5

### 15.1. Zasada najważniejsza

Wieloetapowy sweep jednego parametru jest jedną osią współrzędnych. Nie wolno zapisać
każdego etapu jako osobnego zagnieżdżonego wymiaru, ponieważ PyThat otrzymałby iloczyn
etapów zamiast ich konkatenacji.

Pojedyncza nastawa nie jest osią. `Keithley A current = 0.5 mA` nie może tworzyć wymiaru o
długości 1 ani zmieniać iloczynu checkpointów. Jest stałą kontrolką obowiązującą dla danych
zapisanych pod osiami sweepów.

### 15.2. Docelowe wymiary danych

Dla Keithley × Rigol × Anritsu logiczny wynik to:

```text
Power[keithley_current, rigol_frequency, anritsu_frequency]
KeithleyVoltage[keithley_current, rigol_frequency]
KeithleyCurrent[keithley_current, rigol_frequency]
KeithleyPower[keithley_current, rigol_frequency]
KeithleyAVoltage[keithley_B_current, rigol_frequency]  # A ma stały setpoint, ale jest mierzone
```

Wektor `keithley_current` zawiera dokładne, połączone wartości etapów. Wektor
`rigol_frequency` zawiera dokładne wartości generatora. `anritsu_frequency` pochodzi z osi
każdego trace i w jednym runie musi mieć stabilną długość; zmiana długości jest błędem lub
wymaga osobnej serii danych.

Stała wartość kanału A jest współrzędną pomocniczą lub metadaną nastawy, nie głównym
wymiarem. Jeżeli `Measure Keithley A` znajduje się wewnątrz sweepu Rigola, jego odczyty mają
te same wymiary checkpointów co pozostałe skalarne wskaźniki.

### 15.3. `scan_definition`

Każda logiczna oś ma jeden `row_XX` typu control z:

- stabilną nazwą urządzenia i kontrolki;
- `tree indent level` wynikającym z zagnieżdżenia;
- liczbą rzeczywistych punktów po deduplikacji;
- start/stop jako informacją opisową;
- dokładnym datasetem wartości w `measurement/row_XX/data`;
- metadanymi etapów jako rozszerzeniem aplikacji lub tabelą labbook;
- informacją, że oś jest `linear`, `log`, `piecewise` albo `explicit`.

Nie wolno udawać, że oś piecewise ma stały krok. Przed ustaleniem wartości pola `equation`
dla nieregularnej osi trzeba wykonać test na PyThat 0.2.14. Jeżeli PyThat akceptuje wyłącznie
znany zestaw funkcji, pole publiczne musi otrzymać bezpieczną, wspieraną reprezentację, a
dokładna definicja etapów trafia do prywatnej przestrzeni nazw aplikacji. Dataset osi
pozostaje autorytatywny.

Stała nastawa otrzymuje osobny wiersz control w `scan_definition` z co najmniej:

- `device name`, np. `Keithley 2602A`;
- `control name`, np. `Keithley A current (A)`;
- `function` zgodnym z kontraktem stałej kontrolki thaTEC;
- `value`, np. `5.000000000000E-04`;
- `tree indent level` zgodnym z miejscem w recepturze;
- jednostką i source mode w metadanych.

Nie dostaje `start`, `stop`, `steps` ani osobnej osi w xarray. Dokładne zachowanie wymagane
przez PyThat dla constant control należy zamrozić golden testem. W prywatnym indeksie
aplikacji wartość stała jest dodatkowo powtarzana w `setpoints` każdego checkpointu.

### 15.4. Pochodzenie punktu

Oprócz danych wymaganych przez thaTEC zapisujemy:

- `plan_hash`;
- `recipe_revision`;
- `point_ordinal`;
- indeksy osi;
- ID etapów i indeksy lokalne;
- zadane wartości SI;
- odczytane wartości;
- status `ok/compliance/aborted/faulted`;
- timestamps UTC i monotoniczne;
- ID węzła akwizycji;
- numer/odcisk ramki Anritsu;
- czas startu, zakończenia i transferu sweepu.

Jeżeli publiczny kontrakt thaTEC nie ma miejsca na te pola, trafiają do prywatnej grupy
aplikacji, ale muszą być skorelowane przez `point_ordinal`.

### 15.5. Checkpoint i awaria

Transakcja punktu:

1. zweryfikuj cały obiekt w pamięci;
2. dopisz publiczne dane thaTEC;
3. dopisz prywatny indeks punktu i widmo;
4. dopisz event;
5. flush HDF5;
6. opcjonalnie flush CSV;
7. dopiero wtedy zgłoś punkt jako ukończony.

Jeżeli etap prywatny zawiedzie po zapisie publicznym, writer cofa ostatni append albo oznacza
go jako niezatwierdzony. Awaria zapisu natychmiast zatrzymuje pomiar i uruchamia safe shutdown.
`measurement running` jest `1` podczas aktywnego lub przerwanego awarią runu i przechodzi na
`0` dopiero przy kontrolowanym zamknięciu z zapisanym statusem końcowym.

### 15.6. Test PyThat

Test akceptacyjny ma:

1. wygenerować recepturę z dwoma etapami Keithley i sweepem Rigola;
2. wykonać ją na symulatorach;
3. otworzyć plik przez `PyThat.MeasurementTree`;
4. odtworzyć drzewo;
5. przekonwertować wskaźniki do xarray;
6. porównać `dims`, `coords`, kolejność i wszystkie wartości z modelem planu;
7. sprawdzić punkt wspólnej granicy etapów;
8. powtórzyć dla runu ukończonego, zatrzymanego i faulted;
9. porównać manifest HDF5 z golden file thaTEC.

## 16. Monitor wykonania

Obecny Run Monitor należy rozszerzyć z paska akcji na widok punktowy:

```text
RUNNING · point 4 812 / 12 019 · ETA 01:43:20

Keithley current: 0.07364 A  · stage Fine region 71/100
Rigol frequency:  451 MHz    · point 40/101
Anritsu: acquiring TRAC1     · sweep 1/1

[progress całkowity]
[progress Keithley stage]
[progress Rigol sweep]

Current spectrum | trend | event log | device states
[Pause after point] [Stop safely] [E-STOP]
```

Wymagania:

- breadcrumb aktualnego miejsca w drzewie;
- osobny postęp dla każdej osi;
- nazwa i postęp etapu;
- aktualne setpointy i readback;
- compliance i moc;
- ostatnie widmo bez blokowania runnera;
- prędkość checkpointów i ETA;
- rozmiar pliku, czas ostatniego flushu i wolne miejsce;
- przy Pause informacja „pause pending until checkpoint”;
- przy Stop widoczny postęp bezpiecznego shutdownu per urządzenie.

## 17. Proponowana architektura kodu

```text
app/
  recipes/
    models.py                 # model i parser v1/v2
    migration.py              # v1 → v2
    serializer.py             # deterministyczny YAML
    parameters.py             # ParameterRegistry
    axes.py                   # SweepStage, SweepAxisSpec, generator
    validation.py             # problemy przypięte do node/field
  engine/
    compiler.py               # kompilacja logiczna i preflight
    execution_plan.py         # plan, cursor, point coordinate
    runner.py                 # wykonanie punktowe
    estimation.py             # czas i rozmiar
  storage/
    thatec_schema_mapper.py   # dokładne osie i topology
    thatec_writer.py          # publiczny kontrakt
    hdf5_writer.py            # transakcja i prywatny indeks
    thatec_validator.py       # manifest + PyThat round-trip
  ui/
    pages/
      recipe_builder_page.py
      run_monitor_page.py
    recipe/
      tree_model.py
      tree_commands.py        # undo/redo
      node_library.py
      inspector.py
      validation_panel.py
      preview_dialog.py
      window_manager.py
    sweep_editors/
      base.py
      stage_table_model.py
      sweep_plot.py
      keithley_editor.py
      rigol_editor.py
      anritsu_acquisition_editor.py
    viewmodels/
      recipe_document_vm.py
      sweep_editor_vm.py
      run_monitor_vm.py
tests/
  unit/
    test_sweep_axes.py
    test_recipe_v2.py
    test_recipe_tree_model.py
    test_preflight.py
  integration/
    test_multistage_execution.py
    test_multistage_hdf5_pythat.py
  gui/
    test_recipe_builder.py
    test_sweep_editor.py
```

Rozbicie `RecipePage` i `RunMonitorPage` z monolitycznego `main_window.py` powinno być częścią
wdrożenia. Nowe modele domenowe nie mogą importować PySide6; widoki Qt zależą od domeny, nie
odwrotnie.

## 18. Kontrakty pomiędzy warstwami

### 18.1. Problem walidacji

```python
ValidationIssue(
    severity="blocker",
    code="SWEEP_POINT_OUT_OF_RANGE",
    node_id="sweep-keithley-b-current",
    stage_id="current-high",
    field="stop",
    message="0.15 A exceeds approved maximum 0.01 A",
    details={"value_si": 0.15, "limit_si": 0.01},
)
```

Kody są stabilne i testowalne. UI nie parsuje tekstu wyjątku, aby odnaleźć pole.

### 18.2. Zdarzenia runnera

Minimalny zestaw:

- `run_started`;
- `axis_entered`, `axis_point_started`, `stage_entered`;
- `setpoint_applied`, `readback_verified`;
- `settling_started`, `settling_finished`;
- `measurement_completed`;
- `spectrum_started`, `spectrum_completed`;
- `checkpoint_started`, `checkpoint_committed`;
- `pause_pending`, `paused`, `resumed`;
- `compliance_detected`;
- `run_stopping`, `device_safe`, `run_completed`, `run_aborted`, `run_fault`.

Event zawiera `point_ordinal`, `axis_indices`, `node_id`, UTC i dane specyficzne. Monitor nie
powinien wyliczać stanu na podstawie liczby płaskich `action_finished`.

### 18.3. Snapshot uruchomienia

`RunSnapshot` powinien zawierać:

- canonical recipe v2;
- plan hash i recipe hash;
- exact generated axes;
- settings YAML i settings hash;
- device IDN/capabilities;
- wersje aplikacji, Pythona, h5py, numpy i PyThat;
- wynik preflightu;
- estymację czasu i rozmiaru;
- dane labbook;
- polityki boundary/traversal.

## 19. Undo/redo, autosave i wersjonowanie

- każda zmiana drzewa i Apply z okna to `QUndoCommand`;
- przeciągnięcie poddrzewa jest jednym poleceniem;
- walidacja i wykres reagują na rewizję dokumentu;
- autosave trafia do osobnego pliku recovery, nigdy nie nadpisuje świadomie zapisanego YAML;
- po crashu aplikacja proponuje odzyskanie, pokazując różnicę czasu i nazwę receptury;
- zapis używa pliku tymczasowego i atomowej zamiany;
- otwarcie v1 tworzy w pamięci v2 i proponuje `Save As`, bez cichego nadpisania;
- receptura zapisuje `schema_version`, ale nie zapisuje geometrii okien ani stanu zaznaczenia.

## 20. Dostępność i jakość wizualna

„Piękny interfejs” powinien oznaczać przede wszystkim szybkie rozumienie planu:

- jeden spójny design system z istniejącym motywem;
- status nie jest komunikowany wyłącznie kolorem — ikona i tekst są obowiązkowe;
- kontrast zgodny co najmniej z WCAG AA dla tekstu;
- pełna obsługa klawiatury i logiczny focus order;
- skróty: `Ctrl+N/O/S`, `Ctrl+Z/Y`, Delete, Enter, `Ctrl+Shift+V` validate;
- tooltipy z jednostkami, limitem profilu i źródłem limitu;
- błędy inline pod polem zamiast serii modalnych dialogów;
- wartości wyrównane dziesiętnie w tabeli etapów;
- prefiksy SI zmieniają prezentację, ale nie dokładność;
- układ od 1280×720, komfortowy przy 1920×1080 i skalowaniu 125–200%;
- splittery i geometria okien są zapamiętywane;
- animacje ograniczone do subtelnego podświetlenia zmian, bez ruchu utrudniającego pracę.

## 21. Testy

### 21.1. Generator osi

- stała wartość nie generuje osi ani nie zwiększa point count;
- pojedynczy etap rosnący i malejący;
- 2, 3 i duża liczba punktów;
- dwa etapy ze wspólną granicą dla wszystkich polityk;
- etapy rozłączne;
- etapy zachodzące na siebie;
- mieszany linear/log;
- log z zerem i wartością ujemną;
- jednostki `mA/A`, `MHz/GHz` i zapis wykładniczy;
- granice float i rozdzielczości urządzenia;
- NaN/Inf i bardzo duże liczby;
- deterministyczne wartości i hash;
- provenance stage/local/global dla każdego punktu.

### 21.2. Parser i migracja

- literalna stała wartość Keithley A/B w trybach current i voltage;
- odróżnienie literalnej stałej od referencji `${target}`;
- round-trip v2 bez zmiany semantyki;
- migracja każdego wspieranego sweepu v1 do jednego etapu v2;
- nieznane typy, targety i pola;
- duplikaty node/stage ID;
- brak referencji i referencja do nienadrzędnego sweepu;
- zachowanie komentarzy i jednostek;
- canonical hash niezależny od whitespace.

### 21.3. Tree model i GUI

- osobne pozycje `Set fixed value` i `Sweep value` dla kanałów A/B;
- zmiana kanału A ↔ B i current ↔ voltage w edytorze pojedynczej nastawy;
- stały węzeł pokazuje wartość i badge `fixed`, bez licznika punktów;
- dozwolony i niedozwolony drag-and-drop;
- undo/redo dodania, usunięcia, ruchu i Apply;
- osobne okna dla dwóch sweepów jednego urządzenia;
- brak duplikatu okna dla jednego node ID;
- ostrzeżenie o niezastosowanych zmianach;
- aktualizacja wykresu i licznika po edycji;
- klik błędu otwiera właściwe pole;
- run zablokowany po zmianie receptury;
- skalowanie i minimalne rozmiary przy 125/150/200%;
- nawigacja klawiaturą i widoczny focus.

### 21.4. Kompilator i preflight

- stała konfiguracja przed sweepem jest wykonywana raz i nie zmienia liczby widm;
- stała konfiguracja wewnątrz sweepu ma właściwy execution count;
- niezależne kanały: Keithley A fixed + Keithley B sweep;
- limity, compliance i moc stałej wartości są sprawdzane tak samo jak punkty sweepu;
- `rigol.1.frequency` i `rigol.2.frequency`;
- dokładny iloczyn osi po deduplikacji;
- brak materializacji ogromnego planu w podglądzie;
- limity Keithley/Rigol/Anritsu dla wszystkich punktów;
- wykrycie przykładu `0.15 A` i `1 GHz` jako blockerów przy obecnym profilu;
- cross-device coverage Anritsu;
- wymagane ARM/OFF/finally;
- estymacja dysku i limit maksymalnej liczby punktów;
- niezmienność hasha od preview do run.

### 21.5. Runner

- konfiguracja Keithley A fixed przed wejściem w pętle i readback tej wartości;
- brak zbędnego ponawiania stałej konfiguracji w każdym punkcie;
- pomiar kanałów A i B przy każdym widmie, jeżeli oba węzły Measure są obecne;
- dokładna kolejność outer/inner loop;
- configure Keithley tylko po zmianie jego osi;
- configure Rigol na każdy punkt częstotliwości;
- nowe widmo Anritsu dla każdego checkpointu;
- readback mismatch;
- pause na granicy checkpointu;
- stop podczas wait i `*OPC?`;
- compliance w pierwszym, środkowym i ostatnim punkcie;
- odłączenie każdego urządzenia;
- błąd zapisu przed i po publicznym append;
- wykonanie wszystkich bezpiecznych akcji mimo błędu jednej z nich.

### 21.6. HDF5/PyThat

- stały Keithley A jest control z `value`, lecz nie jest wymiarem;
- stały setpoint A występuje w provenance każdego checkpointu;
- oś piecewise jest jedną osią;
- dokładne 119 wartości w przykładzie 100 + 20 z deduplikacją;
- kształt `119 × Nr × Nf` widma;
- coords i units w xarray;
- provenance etapów;
- kompletność publicznego i prywatnego widoku;
- flush po każdym punkcie;
- odczyt po kontrolowanym abort i zasymulowanym crashu;
- brak cichej zmiany długości trace;
- golden manifest i system inwentaryzacji laboratorium.

### 21.7. Hardware-in-the-loop

1. read-only capability probe;
2. wszystkie outputs OFF;
3. 2 × 2 na sztucznym obciążeniu;
4. dwa etapy po 3 punkty ze wspólną granicą;
5. kontrolowany compliance;
6. stop w każdym rodzaju operacji;
7. odłączenie USB/TCP/IP;
8. kwalifikacja readback częstotliwości Rigola;
9. potwierdzenie nowej ramki Anritsu per punkt;
10. soak test dopiero w granicach zatwierdzonego profilu.

## 22. Etapy wdrożenia

### Etap 0 — kontrakty i decyzje bezpieczeństwa

**Cel:** zamrozić semantykę przed budową UI.

- potwierdzić nazwy parametrów i jednostki;
- zatwierdzić `points` jako liczbę wartości wraz z końcami;
- zatwierdzić domyślną deduplikację wspólnej granicy;
- zdecydować, czy descending i serpentine są w v1;
- zakwalifikować reprezentację nieregularnej osi w PyThat;
- potwierdzić limity realnego sprzętu i DUT;
- zmierzyć typowy czas single sweep Anritsu i flush HDF5.

**Wyjście:** ADR-y, golden test dla osi piecewise i zatwierdzony kontrakt receptury v2.

### Etap 1 — domena osi i rejestr parametrów

**Cel:** funkcje bez GUI.

- `SweepStage`, `SweepAxisSpec`, generator i provenance;
- `FixedSetpointSpec` oraz wspólna walidacja stałej i sweepowanej wartości;
- `ParameterRegistry`, w tym `rigol.*.frequency`;
- walidacja i stabilne `ValidationIssue`;
- parser/serializer v2 i migracja v1;
- rozszerzenie kompilatora;
- testy jednostkowe/property-based dla osi.

**Kryterium:** przykład wieloetapowy generuje deterministycznie 119 punktów i poprawne
blockery aktualnego profilu.

### Etap 2 — model drzewa i podstawowy Recipe Builder

**Cel:** wizualne budowanie bez uruchamiania.

- wydzielenie `RecipePage` z `main_window.py`;
- `RecipeTreeModel` i biblioteka węzłów;
- dodawanie, usuwanie, ruch, duplikowanie i włączanie;
- węzły `Set fixed value` dla Keithley A/B i Rigol CH1/CH2;
- inspector;
- undo/redo, dirty state, open/save/autosave;
- panel problemów;
- read-only YAML preview.

**Kryterium:** pełny przykład można zbudować bez ręcznego YAML i zapisać/otworzyć bez
zmiany semantyki.

### Etap 3 — okna sweepów i wykres

**Cel:** docelowy UX parametrów.

- wspólna baza okna;
- `StageTableModel`;
- scatter plot i hover;
- edytor Keithley;
- edytor pojedynczej nastawy z wyborem kanału A/B;
- edytor Rigol;
- edytor akwizycji Anritsu;
- manager okien i obsługa niezastosowanych zmian;
- pełna walidacja inline.

**Kryterium:** każdy sweep ma osobne okno, tabela i wykres pokazują dokładne wartości, a
Apply jest undoable.

### Etap 4 — preview, estymacja i bramka runu

**Cel:** operator rozumie koszt i ryzyko.

- topology summary;
- próbki punktów;
- estymator czasu i dysku;
- cross-device validation;
- hash receptury/planu/profilu;
- final review dialog;
- unieważnianie preflightu po każdej zmianie.

**Kryterium:** przycisk Run nie może zostać aktywowany dla `0.15 A`/`1 GHz` z obecnym
profilem i jednoznacznie wskazuje oba pola.

### Etap 5 — punktowy plan i runner

**Cel:** poprawne wykonanie zagnieżdżonych osi.

- `PointCoordinate` i provenance;
- logiczny plan + cursor;
- optymalizacja konfiguracji po zmianie osi;
- bogate zdarzenia punktowe;
- pause/stop na checkpointach;
- rozbudowany Run Monitor;
- fault injection.

**Kryterium:** symulator wykonuje dokładnie jeden nowy trace na każdą współrzędną i raportuje
poprawny etap oraz indeks.

### Etap 6 — thaTEC/PyThat

**Cel:** kompletna zgodność danych.

- nieregularne wektory osi;
- jedna logiczna oś dla wielu etapów;
- właściwe wymiary widma i scalarów;
- provenance w prywatnym indeksie;
- manifest, golden file i PyThat/xarray round-trip;
- testy completed/aborted/faulted/crash.

**Kryterium:** plik z dwoma etapami odtwarza dokładne coords i kształty przez przypiętą
wersję PyThat oraz system inwentaryzacji laboratorium.

### Etap 7 — kwalifikacja i dopracowanie

**Cel:** gotowość produkcyjna.

- testy na sztucznym obciążeniu;
- responsywność, DPI, klawiatura, accessibility;
- soak test w zatwierdzonych granicach;
- procedura operatora i awarii;
- telemetria czasu operacji do lepszego ETA;
- szkolenie operatorów i lista ograniczeń.

## 23. Priorytety backlogu

### P0 — wymagane przed realnym sweepem

- pojedyncza nastawa Keithley A/B w domenie, compilerze, runnerze i HDF5;
- jawne rozdzielenie konfiguracji, ARM i OUTPUT ON dla stałej wartości;
- generator etapów i jednoznaczna semantyka granic;
- `rigol.*.frequency` w całym pipeline;
- preflight wszystkich wygenerowanych punktów;
- dokładny `PointCoordinate`;
- zsynchronizowany single sweep Anritsu per punkt;
- atomowy checkpoint i safe shutdown po błędzie storage;
- jedna oś piecewise w PyThat;
- bramka limitów i capabilities;
- finally z rampą/OFF/ABORT.

### P1 — wymagane dla dobrego procesu operatora

- wizualne drzewo;
- dedykowany edytor stałej wartości z wyborem urządzenia i kanału;
- osobne okna sweepów;
- tabela etapów i scatter plot;
- inspector i panel błędów;
- undo/redo i autosave;
- preview liczby punktów, czasu i dysku;
- punktowy Run Monitor.

### P2 — funkcje zaawansowane

- jawna lista punktów/import CSV;
- serpentine;
- warunkowe wait for stability;
- wznowienie po restarcie;
- porównanie receptur;
- szablony poddrzew;
- wiele trace Anritsu i przetwarzanie referencyjne;
- optymalizacja kolejności punktów z zachowaniem jawnej semantyki.

## 24. Ryzyka i sposoby ograniczenia

| Ryzyko | Skutek | Ograniczenie |
|---|---|---|
| UI i YAML rozjeżdżają się | wykonywany jest inny plan niż widoczny | jeden model dokumentu, deterministic serializer, hash |
| Stage zapisany jako osobny wymiar | błędny kształt PyThat/xarray | konkatenowana oś, golden round-trip |
| Wspólna granica wykonana dwa razy | dodatkowe widmo i niejednoznaczna coord | jawna boundary policy i licznik |
| Ciągły sweep Rigola użyty z Anritsu | brak pewnego setpointu widma | programowy stepped sweep |
| Plan przekracza możliwości urządzenia | błąd lub uszkodzenie DUT | profil + capability preflight + adapter validation |
| Miliony płaskich akcji | pamięć i zamrożenie GUI | plan logiczny, cursor, sampler |
| Zapis HDF5 nie nadąża | długa ETA lub utrata danych | pomiary wydajności, flush metrics, backpressure, safe stop |
| Zmiana trace length | niepoprawna macierz widm | stały kontrakt runu i natychmiastowy fault |
| Okno edytuje usunięty węzeł | utrata lub błędna zmiana | window manager, revision/node lifecycle |
| Clamp ukrywa zły plan | fałszywe poczucie bezpieczeństwa | brak automatycznego clamp w recepturach |
| PyThat zmienia zachowanie | utrata kompatybilności | przypięta wersja i macierz golden-file |

## 25. Kryteria akceptacji końcowej

Funkcja jest gotowa, gdy:

1. operator tworzy cały przykład Keithley → Rigol → Anritsu bez ręcznego YAML;
2. operator wybiera Keithley A albo B i ustawia pojedynczy prąd lub napięcie bez tworzenia
   sweepu;
3. stała nastawa przed sweepem wykonuje się raz, nie mnoży widm i pozostaje widoczna w
   każdym checkpointcie;
4. konfiguracja stałej wartości nie włącza wyjścia bez osobnych ARM i OUTPUT ON;
5. każdy sweep otwiera własne okno z właściwymi parametrami, tabelą etapów i wykresem;
6. dwa różne sweepy tego samego urządzenia mają niezależne okna i stan;
7. przykład 100 + 20 punktów ze wspólną granicą generuje i pokazuje 119 wartości;
8. wykres i tabela pokazują dokładnie te wartości, które otrzyma runner;
9. `rigol.1.frequency` działa jako programowy sweep punktowy;
10. dla każdego punktu częstotliwości wykonywane jest nowe, potwierdzone widmo Anritsu;
11. liczba zapisanych widm jest dokładnym iloczynem osi i liczby akwizycji;
12. aktualny profil blokuje `0.15 A` i `1 GHz` z komunikatami przypiętymi do pól;
13. zmiana receptury po preflight blokuje Run;
14. pause i stop działają na bezpiecznej granicy checkpointu;
15. compliance, timeout, disconnect i błąd storage kończą się bezpiecznym shutdownem;
16. plik częściowy pozostaje czytelny;
17. PyThat odtwarza stałą nastawę jako control bez wymiaru, jedną wieloetapową oś Keithley,
   jedną oś Rigola i oś widma;
18. xarray ma poprawne dims, coords, units, wartości i kolejność;
19. snapshot zawiera recepturę, stałe nastawy, dokładne osie, profile, IDN, capabilities i
   hashe;
20. GUI pozostaje responsywne przy co najmniej 100 000 punktów planu;
21. zapis/odczyt, undo/redo i recovery nie tracą danych;
22. testy symulacyjne, fault injection i kwalifikacja na sztucznym obciążeniu są zaliczone;
23. procedura operatora opisuje przygotowanie, review, run, pause, stop i awarię.

## 26. Decyzje wymagające zatwierdzenia przed implementacją

1. Czy domyślna deduplikacja wspólnej granicy etapów jest pożądana?
2. Czy użytkownik potrzebuje dwóch identycznych punktów granicznych jako świadomej opcji?
3. Czy descending sweep ma wejść do v1?
4. Czy serpentine ma być widoczny w v1, czy dopiero po kwalifikacji wpływu na DUT?
5. Czy wszystkie etapy jednej osi muszą być monotoniczne w tym samym kierunku?
6. Jaka reprezentacja nieregularnej osi jest poprawnie odczytywana przez obecny system
   thaTEC/PyThat?
7. Czy widmo raw, averaged, processed i reference ma być osobnymi indicatorami?
8. Ile punktów ma mieć przykładowy sweep częstotliwości Rigola?
9. Jakie są rzeczywiste, zatwierdzone limity prądu Keithley B dla DUT, przewodów i fixture?
10. Jaki model generatora ma rzeczywiście realizować `100 MHz → 1 GHz`, skoro obecny profil
    Rigol DG1032Z kończy się na `100 MHz` dla CH1?
11. Czy Anritsu ma dostawać trigger sprzętowy, czy pozostajemy przy sekwencji SCPI
    `INIT:IMM` → `*OPC?` → trace?
12. Jaki zapas wolnego miejsca ma być wymagany: stały, procentowy czy oba?
13. Czy wznowienie po awarii jest wymaganiem v1?

Do czasu kwalifikacji punktów 9–11 plan z wartościami demonstracyjnymi pozostaje poprawnym
projektem UI, ale nie jest planem dopuszczonym do wykonania na realnym stanowisku.

## 27. Rekomendowana pierwsza iteracja implementacyjna

Najbezpieczniejszy pierwszy pionowy fragment obejmuje:

1. `FixedSetpointSpec` i węzeł `Keithley A/B → Set fixed value`;
2. test: Keithley A fixed jest wykonany raz i nie tworzy wymiaru HDF5;
3. `SweepStage` i generator z deduplikacją;
4. `rigol.1.frequency` w rejestrze, compilerze i mapperze;
5. recepturę v2 i migrację pojedynczego sweepu v1;
6. test 100 + 20 = 119;
7. edytor stałej nastawy oraz minimalny `StageTableModel` i scatter plot;
8. jedno okno sweepu Keithley i jedno okno Rigola otwierane z istniejącego drzewa;
9. kompilację do obecnego runnera dla małej receptury symulacyjnej;
10. zapis stałej kontrolki, osi piecewise i test PyThat;
11. dopiero po przejściu round-trip — pełny drag-and-drop Recipe Builder.

Taki pionowy fragment wcześnie sprawdzi najtrudniejszy kontrakt: czy dokładnie te same punkty
są widoczne na wykresie, wykonywane przez runner i odtwarzane z HDF5 przez PyThat. Dopiero
po jego zamknięciu warto inwestować w pełne dopracowanie wizualne kreatora.
