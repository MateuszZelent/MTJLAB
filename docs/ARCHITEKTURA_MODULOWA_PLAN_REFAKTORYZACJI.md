# Architektura modułowa aplikacji pomiarowej — plan refaktoryzacji

Status: propozycja do akceptacji  
Data: 2026-07-18  
Zakres: dokumentacja i plan; bez zmiany zachowania aplikacji

## 1. Streszczenie decyzji

`app/ui/main_window.py` należy rozbić na cienki shell aplikacji, niezależne strony
ogólne oraz pionowe moduły urządzeń. Każdy obsługiwany model lub rodzina urządzeń
otrzyma własny pakiet zawierający specyfikację, modele poleceń i wyników, adapter,
symulator, reguły bezpieczeństwa, UI oraz integrację z recepturami.

Docelowo dodanie urządzenia nie powinno wymagać modyfikowania `MainWindow`,
`RecipePage` ani centralnego zbioru symulatorów. Nowy moduł ma implementować jawny
kontrakt i zostać dopisany w jednym rejestrze kompozycji.

To jest refaktoryzacja ewolucyjna. Nie wykonujemy jednorazowego przepisywania ani
nie zmieniamy równocześnie protokołów VISA, formatu receptur, formatu danych i UI.

## 2. Stan obecny

### 2.1. Fakty z repozytorium

- `app/ui/main_window.py`: 13 076 linii;
- `RecipePage`: 4 271 linii;
- `AnritsuPage`: 1 549 linii;
- `KeithleyPage`: 1 040 linii;
- `RigolPage`: 973 linie;
- `MainWindow`: 1 045 linii;
- istnieją pakiety `app/devices/anritsu`, `keithley` i `rigol`, ale obejmują
  głównie adaptery i część modeli sprzętowych;
- UI urządzeń, edytory węzłów receptur i konfiguracje są nadal w jednym module;
- `app/devices/simulators.py` jest centralnym plikiem dla różnych urządzeń;
- testy importują wiele szczegółów bezpośrednio z `app.ui.main_window`, więc
  bieżąca lokalizacja klas stała się nieformalnym publicznym API;
- arkusze stylów są osadzone w `app/main.py`, poza systemem projektowym UI.

### 2.2. Odpowiedzialności sklejone w jednym pliku

`main_window.py` zawiera obecnie:

1. współdzielone widgety i formatowanie;
2. dashboard i wykrywanie VISA;
3. ręczne sterowanie Rigol, Keithley i Anritsu;
4. modele konfiguracji widoków urządzeń;
5. edytory urządzeń używane przez receptury;
6. pełny edytor drzewa receptury;
7. monitoring wykonania i stronę wyników;
8. składanie aplikacji, autoryzację, audyt, awaryjne wyłączenie i lifecycle.

Skutkiem jest wysoki koszt zmiany: modyfikacja jednego urządzenia dotyka wspólnego
modułu, zwiększa ryzyko konfliktów, utrudnia testy izolowane i zachęca do dalszych
zależności przekrojowych.

## 3. Cele i kryteria powodzenia

### Cele

- jeden pakiet na konkretną rodzinę urządzeń;
- brak importów PySide6 w adapterach, specyfikacjach i logice bezpieczeństwa;
- `MainWindow` odpowiada tylko za shell i orkiestrację poziomu aplikacji;
- edytor receptur korzysta z rozszerzeń urządzeń przez kontrakt, nie przez
  `if device == ...` rozrzucone po klasie;
- symulator leży przy adapterze, który symuluje;
- stabilne API modułu eksportowane przez `__init__.py` lub `public.py`;
- testy odzwierciedlają strukturę kodu produkcyjnego;
- każdy etap migracji jest mały, odwracalny i przechodzi pełny zestaw testów.

### Mierzalne kryteria końcowe

- `app/ui/main_window.py` nie istnieje albo jest fasadą kompatybilności poniżej
  100 linii;
- właściwa klasa `MainWindow` ma mniej niż 500 linii;
- żadna strona UI nie przekracza orientacyjnie 800 linii; większe widoki są
  dzielone na panele i presenter/view-model;
- dodanie testowego modułu urządzenia wymaga rejestracji w jednym miejscu i nie
  wymaga zmian w shellu;
- `ruff check app tests` i pełny `pytest` przechodzą po każdym etapie;
- test importów potwierdza brak zależności `device domain/adapter -> app.ui`;
- istnieją testy kontraktowe uruchamiane dla każdego adaptera i symulatora.

## 4. Docelowy model architektury

Architektura łączy podział warstwowy na poziomie aplikacji z pionowymi modułami
urządzeń. Pakiet urządzenia jest jednostką rozwoju, ale wewnątrz zachowuje kierunek
zależności.

```text
app/main.py (composition root)
        |
        v
app/ui/shell/MainWindow + wspólne usługi aplikacyjne
        |
        v
DeviceModule registry -----> moduły urządzeń / strony ogólne
        |                              |
        v                              v
engine / recipes / safety <----- publiczne kontrakty modułów
        |
        v
adapter -> transport VISA
```

### Reguła zależności

Dozwolony kierunek to:

```text
UI -> application services -> domain/specification -> transport abstractions
UI -> adapter contracts
adapter -> domain/specification + common VISA
simulator -> domain/specification + session contracts
```

Niedozwolone zależności:

- adapter, specyfikacja lub safety importuje PySide6;
- moduł jednego urządzenia importuje prywatne UI drugiego urządzenia;
- engine importuje widgety;
- `MainWindow` zna konkretne klasy poleceń każdego urządzenia;
- `RecipePage` tworzy konkretne dialogi urządzeń bezpośrednio;
- kod urządzenia odczytuje prywatne pola innego modułu.

## 5. Proponowana struktura katalogów

Nazwy pakietów pozostają małymi literami zgodnie z PEP 8. W nazwie warto zawrzeć
rodzinę/model, gdy protokół i możliwości są modelowo specyficzne, np.
`keithley_2600`, a nie ogólne `keithley`.

```text
app/
  main.py                         # wyłącznie uruchomienie i composition root
  bootstrap.py                    # budowa usług, kontrolerów i rejestru
  contracts/
    device_module.py              # DeviceModule, RecipeExtension, UI factory
  devices/
    common/
      base.py                     # DeviceAdapter, sesja, interlock
      visa.py
      discovery.py
      controller.py               # generyczny worker/controller urządzenia
      contract_tests.py           # opcjonalne helpery testów kontraktowych
    keithley_2600/
      __init__.py                 # małe, stabilne API publiczne
      module.py                   # manifest/fabryki i rejestracja integracji
      specification.py            # IDN, modele, capability i metadane
      models.py                   # request/result/snapshot; bez Qt
      adapter.py                  # tylko operacje sprzętowe
      simulator.py                # symulowana sesja/urządzenie
      safety.py                   # walidacja i limity urządzenia
      recipe.py                   # definicje parametrów i kompilacja akcji
      ui/
        page.py                   # ręczne sterowanie
        panels.py
        dialogs.py                # edytory węzłów receptury
        presenter.py              # stan i mapowanie UI, jeśli potrzebne
    rigol_dg1000z/
      ...
    anritsu_ms2830a/
      ...
  ui/
    shell/
      main_window.py              # nawigacja, lifecycle, emergency stop
      navigation.py
      workspace.py
    dashboard/
      page.py
      device_card.py
    recipes/
      page.py                     # generyczny edytor drzewa
      tree.py
      library.py
      common_dialogs.py
      sweep_editor.py
    execution/
      page.py
    results/
      page.py
    widgets/
      limit_field.py
      notification_banner.py
      spectrum_plot.py
    design_system/
      theme.py
      dark.qss
      light.qss
  engine/
  domain/
  recipes/
  safety/                         # wyłącznie reguły całej stacji
  settings/
  storage/
```

Dokładna nazwa pakietu powinna odpowiadać zakresowi adaptera. Obecny adapter
Keithley deklaruje rodzinę 2600, dlatego rekomendowane jest `keithley_2600`.
Jeżeli wsparcie faktycznie jest ograniczone do 2602/2602B, nazwa powinna zostać
zawężona do `keithley_2602` po potwierdzeniu macierzy modeli.

## 6. Kontrakt modułu urządzenia

Nie jest potrzebny zewnętrzny system pluginów ani dynamiczne ładowanie kodu.
Wystarczy typowany, jawny rejestr wewnętrzny. Dynamiczne entry points można dodać
dopiero wtedy, gdy urządzenia będą dystrybuowane poza głównym pakietem.

Przykładowy kształt kontraktu (projekt, nie gotowa implementacja):

```python
@dataclass(frozen=True, slots=True)
class DeviceModule:
    key: str
    display_name: str
    settings_key: str
    adapter_factory: AdapterFactory
    simulator_factory: SimulatorFactory
    page_factory: DevicePageFactory
    recipe_extension: RecipeExtension
    specification: DeviceSpecification
```

`RecipeExtension` powinno dostarczać co najmniej:

- opis dostępnych bloków i parametrów do biblioteki;
- fabrykę edytora konfiguracji węzła;
- mapowanie węzła na typowane akcje;
- prezentację węzła w drzewie;
- migrację wersji payloadu urządzenia, jeśli format się zmieni.

Rejestr jest tworzony w `bootstrap.py` i wstrzykiwany do dashboardu, edytora
receptur i shellu. Import modułu nie może samoczynnie modyfikować globalnego stanu.

## 7. Granice odpowiedzialności

### `MainWindow`

Pozostają: nawigacja, composition root UI, lifecycle okna, globalna blokada podczas
run, globalne uprawnienia, koordynacja emergency-off i routing komunikatów.

Wychodzą: budowanie stron urządzeń, konfiguracje sprzętu, parsowanie wartości
urządzeń, szczegóły receptur, wyświetlanie wyników oraz QSS.

### Moduł urządzenia

Zawiera wszystko, co zmienia się z powodu specyfiki danego sprzętu. Nie zawiera
logiki całej stacji, dostępu do globalnego okna ani bezpośredniego zapisu runów.

### Engine i receptury

Pozostają niezależne od Qt. Engine wykonuje typowane akcje przez adaptery.
Generyczne drzewo receptury zna kontrakt rozszerzenia, a nie konkretne urządzenia.

### Safety

Reguły specyficzne dla urządzenia mieszkają przy urządzeniu. Reguły obejmujące
więcej urządzeń, stan stanowiska, uprawnienia i globalny interlock pozostają w
centralnej warstwie safety/domain. Walidacja UI nie zastępuje walidacji adaptera
i engine — zabezpieczenie musi działać także bez GUI.

## 8. Strategia migracji

### Etap 0 — zabezpieczenie stanu bazowego

1. Zapisać wynik pełnego `pytest` i `ruff`.
2. Dodać test startu aplikacji w trybie offscreen/simulation.
3. Dodać test emergency-off oraz blokady output jako bramki regresji.
4. Ustalić publiczne importy używane przez testy i kod aplikacji.
5. Nie mieszać refaktoryzacji z nowymi funkcjami UI.

Kryterium wyjścia: zielona, powtarzalna linia bazowa.

### Etap 1 — wydzielenie kodu bez zależności od urządzeń

Przenieść mechanicznie, bez zmian zachowania:

- `LimitField` i `LimitEditDialog` do `ui/widgets/limit_field.py`;
- `DeviceCard` i `DashboardPage` do `ui/dashboard/`;
- `RunMonitorPage` do `ui/execution/page.py`;
- `ResultsPage` do `ui/results/page.py`;
- wspólne dialogi i widgety receptur do `ui/recipes/`;
- QSS z `main.py` do plików systemu projektowego.

Na czas migracji `app.ui.main_window` re-eksportuje stare symbole. Dzięki temu
testy i potencjalni konsumenci nie muszą zmienić się w tym samym commicie.

Kryterium wyjścia: zachowanie i publiczne importy bez zmian, `main_window.py`
istotnie mniejszy.

### Etap 2 — moduł pilotażowy Rigol

Rigol jest dobrym pilotem: strona jest duża, ale ma mniej integracji przetwarzania
widma niż Anritsu i mniej złożony stan dwukanałowy niż Keithley.

1. Utworzyć `devices/rigol_dg1000z`.
2. Rozdzielić modele od adaptera.
3. Przenieść safety i symulator.
4. Przenieść `RigolPage`, snapshot i dialog węzła.
5. Zaimplementować pierwszy `DeviceModule` i `RecipeExtension`.
6. Zachować aliasy starych importów przez jedną wersję migracyjną.

Kryterium wyjścia: Rigol jest ładowany z rejestru, a generyczny shell i edytor
receptur nie importują jego konkretnych klas UI.

### Etap 3 — Keithley 2600

1. Przenieść modele request/result/snapshot i rampy.
2. Rozdzielić stronę na page, karty kanałów, historię i presenter.
3. Przenieść panel konfiguracji, dialog węzła i builder sweep.
4. Przenieść walidację i symulator.
5. Zastąpić keithley-specyficzne gałęzie `RecipePage` rozszerzeniem.

Szczególnie chronić: ramp-off, compliance, stan obu kanałów, interlock i zachowanie
przy utracie komunikacji.

### Etap 4 — Anritsu MS2830A

1. Rozdzielić spectrum analyzer i signal generator jako capability jednego modułu,
   a nie dwa niezależne urządzenia.
2. Przenieść modele konfiguracji, trace/reference i hardware options.
3. Rozdzielić stronę na acquisition, advanced spectrum, reference i SG.
4. Przenieść edytory receptur oraz integrację parametrów.
5. Zachować logikę averaging/live jako jawny model stanu lub presenter.

Szczególnie chronić: lifecycle live acquisition, anulowanie averaging, zgodność
siatki częstotliwości, reference store, opcje sprzętowe i limity firmware.

### Etap 5 — odchudzenie RecipePage

Po migracji wszystkich urządzeń:

- biblioteka bloków jest budowana z `RecipeExtension`;
- edycja węzła jest delegowana po `device_key`;
- prezentacja i ikony są metadanymi rozszerzenia;
- generyczne sweep/loop/comment/finally pozostają w `ui/recipes`;
- mapowanie YAML pozostaje w domenowej warstwie `app/recipes`.

Kryterium wyjścia: `RecipePage` nie importuje pakietów konkretnych urządzeń.

### Etap 6 — finalny shell i usunięcie fasady

1. Przenieść właściwy shell do `ui/shell/main_window.py`.
2. Przenieść budowę zależności do `bootstrap.py`.
3. Zastąpić prywatne połączenia między stronami jawnymi sygnałami/kontraktami.
4. Zaktualizować wszystkie importy na nowe publiczne ścieżki.
5. Usunąć fasadę `app/ui/main_window.py` dopiero po okresie migracji.
6. Dodać test architektury i instrukcję tworzenia kolejnego modułu.

## 9. Plan testów i bramki jakości

Każdy etap kończy się następującymi kontrolami:

1. testy jednostkowe modeli, safety, parserów i presenterów;
2. wspólne testy kontraktowe wszystkich adapterów:
   connect/identity, disconnect, emergency-off, timeout, błędy transportu;
3. testy kontraktowe adapter–symulator dla operacji wysokiego poziomu;
4. testy UI offscreen dla każdej strony i dialogu;
5. testy integracyjne receptura -> plan -> simulated run -> zapis/odczyt;
6. test aplikacji uruchamianej z pełnym rejestrem i z pojedynczym modułem;
7. `ruff check app tests` i pełny `pytest`;
8. ręczny smoke test na sprzęcie po migracji każdego adaptera.

Test HIL jest obowiązkową bramką przed wdrożeniem, ale nie przed każdym lokalnym
commitem. Refaktoryzacja nie może obniżyć istniejących ograniczeń bezpieczeństwa.

## 10. Zarządzanie ryzykiem

| Ryzyko | Ograniczenie |
|---|---|
| Cykliczne importy po przeniesieniu klas | kontrakty w neutralnym `app/contracts`; UI zależy od kontraktów |
| Ukryta zmiana działania podczas move | małe commity: najpierw move + re-export, dopiero potem redesign |
| Zerwanie testów i zewnętrznych importów | czasowa fasada zgodności i ostrzeżenia deprecacji |
| Rozjechanie formatów receptur | bez zmian `device_key` i schema w fazie strukturalnej; testy golden/round-trip |
| Osłabienie interlocków | safety także w engine/adapterze; testy negatywne obowiązkowe |
| Nadmierny framework pluginowy | statyczny rejestr; entry points dopiero przy realnej potrzebie |
| Jeden ogromny plik w każdym module | limity odpowiedzialności i podział page/panels/dialogs/presenter |
| Konflikty z równoległymi funkcjami | krótkie fazy, zamrożenie zmian w przenoszonym obszarze |

## 11. Konwencje dla nowych modułów

- `key` urządzenia i klucze receptur są stabilnymi identyfikatorami, niezależnymi
  od nazwy wyświetlanej;
- modele danych są dataclass/Pydantic bez Qt;
- adapter udostępnia wyłącznie bezpieczne operacje wysokiego poziomu, nigdy surową
  konsolę SCPI;
- UI komunikuje się ze sprzętem przez controller/worker, nie bezpośrednio;
- importy między modułami idą tylko przez publiczne API;
- każdy capability ma opis, walidację, operację adaptera, symulację i test;
- każda zmiana schematu payloadu ma wersję i migrator;
- moduł dostarcza testy urządzenia w analogicznym katalogu `tests/devices/...`.

## 12. Proponowany podział prac i commitów

Commity powinny być małe i semantyczne, np.:

1. `test: establish modularization regression baseline`
2. `refactor(ui): extract shared widgets and dashboard`
3. `refactor(ui): extract execution and results pages`
4. `refactor(devices): introduce device module contracts and registry`
5. `refactor(rigol): create dg1000z vertical module`
6. `refactor(keithley): create 2600 vertical module`
7. `refactor(anritsu): create ms2830a vertical module`
8. `refactor(recipes): delegate device nodes to extensions`
9. `refactor(ui): reduce main window to application shell`
10. `docs: add new-device module guide and architecture checks`

Każdy commit ma przechodzić testy. Nie łączymy masowego przeniesienia plików z
poprawkami funkcjonalnymi, chyba że dana poprawka jest konieczna do zachowania
kontraktu i została osobno opisana.

## 13. Decyzje wymagające potwierdzenia przed implementacją

1. Czy Keithley ma być modułem całej rodziny 2600, czy wyłącznie 2602/2602B?
2. Czy stare ścieżki importu są używane poza tym repozytorium? Jeśli tak, okres
   działania fasady zgodności musi zostać określony wersją wydania.
3. Czy w najbliższym czasie moduły mają być instalowane niezależnie? Domyślna
   rekomendacja brzmi: nie — statyczny rejestr jest prostszy i bezpieczniejszy.
4. Jaki jest wymagany zestaw fizycznego sprzętu HIL dla bramki każdego urządzenia?

Brak odpowiedzi na punkty 1–4 nie blokuje etapów 0–1. Jest potrzebny przed zmianą
nazw pakietów i finalizacją kontraktu rejestru.

## 14. Definition of Done całej refaktoryzacji

- odpowiedzialności z rozdziału 7 znajdują się w docelowych modułach;
- trzy bieżące urządzenia są ładowane przez ten sam kontrakt;
- dodano szablon/checklistę nowego urządzenia;
- nie ma importów UI w domain/engine/adapter/safety;
- wszystkie testy automatyczne i kwalifikacja HIL są zielone;
- format istniejących ustawień, receptur i plików wynikowych pozostał kompatybilny
  albo ma udokumentowaną, automatyczną migrację;
- emergency-off, blokady output, audyt i recovery zachowują co najmniej obecny
  poziom ochrony;
- dokumentacja uruchomienia, architektury i dodawania urządzenia odpowiada kodowi.

