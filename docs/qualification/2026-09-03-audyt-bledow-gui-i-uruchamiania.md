# Audyt błędów startowych i problemów układu GUI (PyLab / MTJLAB)

**Data audytu:** 2026-09-03  
**Zakres audytu:** Analiza kodu powłoki Fluent (`app/ui/shell/main_window.py`, `app/ui/shell/safety_strip.py`), edytora receptur (`app/ui/recipes/page.py`), systemu motywów (`app/ui/design_system/`) oraz cyklu życia okna głównego i paneli roboczych.  
**Plik docelowy:** `docs/qualification/2026-09-03-audyt-bledow-gui-i-uruchamiania.md`  
**Status:** Zidentyfikowano przyczynę źródłową zgłoszonego problemu („GUI się psuje, trzeba otworzyć i zamknąć”) oraz 6 powiązanych usterek układu.

---

## 1. Podsumowanie wykonawcze (Executive Summary)

Użytkownik zgłosił objaw:
> *„Przy uruchamianiu aplikacji GUI się psuje, muszę je otworzyć i zamknąć wtedy się samo naprawia. Przeszukaj kod, sprawdź czy mamy gdzieś błędy które moglibyśmy naprawić.”*

Przeprowadzona analiza kodu źródłowego oraz testy diagnostyczne z replikacją zachowania Qt wykazały **bezpośrednią przyczynę źródłową** tego zjawiska:

### Główny błąd: Desynchronizacja stanu przycisków paska poleceń przez blokowanie sygnałów w `RecipePage`
W pliku [`app/ui/recipes/page.py:978-982`](file:///c:/Users/Shark/git/MTJLAB/app/ui/recipes/page.py#L978-L982) podczas startu wywoływana jest funkcja `_update_workspace_layout(force=True)`. W celu dostosowania widoczności paneli *Library* i *Inspector* do szerokości okna kod wykonuje:
```python
action.blockSignals(True)
action.setChecked(visible)
action.blockSignals(False)
```
1. Przyciski paska `CommandBar` (biblioteka QFluentWidgets) subskrybują sygnały akcji Qt (`toggled`, `changed`).
2. W konstruktorze `RecipePage` przyciski są inicjalizowane jako zaznaczone (`setChecked(True)` — stan niebieski / aktywny).
3. Gdy aplikacja startuje i komponent nie ma jeszcze docelowej geometrii ekranowej (szerokość w trakcie `__init__` wynosi < 760 px lub < 1200 px), logika ukrywa panele i wywołuje `action.setChecked(False)` przy **zablokowanych sygnałach** (`blockSignals(True)`).
4. **Skutek:** `QAction` zmienia stan wewnętrzny na `False`, ale **widżet przycisku w pasku narzędzi nigdy nie otrzymuje zdarzenia zmiany**! Przycisk na ekranie pozostaje podświetlony na niebiesko (jako włączony), podczas gdy odpowiadający mu panel jest całkowicie ukryty (`sizes = [0, available, 0]`).
5. Ponadto klasa `RecipePage` **nie posiada metody `showEvent`** — po wyświetleniu okna z pełną rozdzielczością układ nie jest przeliczany automatycznie.
6. **Dlaczego operator musi „otworzyć i zamknąć”:**  
   Operator widzi niebieski przycisk „Inspector”, ale panelu nie ma. Kliknięcie przycisku powoduje, że Qt (uważając przycisk za dotąd włączony) przełącza go na stan wyłączony (nadal brak panelu). Dopiero drugie kliknięcie włącza przycisk z pełną emisją sygnałów Qt, co wymusza `_set_workspace_panel_visible("inspector", True)` i otwiera brakujący panel.

---

## 2. Zestawienie zidentyfikowanych błędów i usterek

| Lp. | Komponent / Plik | Objaw w interfejsie | Przyczyna techniczna | Poziom istotności |
| :--- | :--- | :--- | :--- | :--- |
| **1** | `app/ui/recipes/page.py` | Przyciski *Library* / *Inspector* świecą na niebiesko, lecz panele są schowane; konieczność dwukrotnego kliknięcia. | Użycie `action.blockSignals(True)` przed `setChecked()`, co gubi synchronizację widżetów `CommandBar` w QFluentWidgets. | **Wysoki (Błąd zgłoszony)** |
| **2** | `app/ui/recipes/page.py` | Brak adaptacji układu po pierwszym przejściu na zakładkę *Sweeps*. | Brak implementacji `showEvent()` w `RecipePage`; układ rekalkulowany jest wyłącznie w `resizeEvent` przy zmianie o $\ge 24\text{ px}$. | **Średni** |
| **3** | `app/ui/shell/main_window.py` | Znikające ikony 5 przyrządów w lewym pasku nawigacji na ekranach o wysokości $< 720\text{ px}$. | `_sync_apparatus_navigation_height` sztywno zwija gałąź `apparatus_navigation_item`, gdy `panel.height() < _apparatus_required_height`. | **Średni** |
| **4** | `app/ui/shell/main_window.py` | Resetowanie wysokości dziennika zdarzeń (*Event log*) po każdej zmianie rozmiaru okna. | `_sync_shell_splitter_layout` na sztywno narzuca `shell_splitter.setSizes([total - 125, 125])`, ignorując pozycję ustawioną przez operatora. | **Średni** |
| **5** | `app/ui/shell/main_window.py` | Utrata proporcji paneli w *Sweeps* po restarcie aplikacji. | `_save_workspace()` i `_restore_workspace()` zapisują geometrię okna i splitter Anritsu, ale pomijają `recipe_page.workspace_splitter`. | **Niski / Uciążliwy** |
| **6** | `app/ui/shell/safety_strip.py` | Ucinanie etykiet i ryzyko wypchnięcia przycisków `E-STOP` / `SAVE SETTINGS` na ekranach 900–1100 px. | Układ `wide` (aktywny już od 900 px) upycha wszystkie 6 elementów w jednym wierszu; długa nazwa roli w symulacji powoduje obcięcie tekstu (np. `SIMULATION · engin...`). | **Średni (Safety UI)** |
| **7** | `app/main.py` & `main_window.py` | Trzykrotne polerowanie arkuszy stylów QSS podczas startu, wywołujące migotanie. | Wywołanie `apply_application_theme()` w `MainWindow.__init__`, powtórzenie w `main.py:32`, a następnie trzecie przejście w `showEvent:3279`. | **Niski (Wydajność)** |

---

## 3. Szczegółowa analiza techniczna i dowody diagnostyczne

### 3.1. Błąd #1 i #2: Desynchronizacja akcji paska narzędzi w `RecipePage`

#### Dowód diagnostyczny (wynik testu w izolacji):
Wykonano test weryfikacyjny zachowania komponentu `CommandBar` z PySide6 i QFluentWidgets:
```python
# Stan początkowy
act.setChecked(True)
# Po wywołaniu z blockSignals(True):
act.blockSignals(True)
act.setChecked(False)
act.blockSignals(False)
# Wynik:
# act.isChecked() -> False
# btn.isChecked() -> True  <-- ROZBIEŻNOŚĆ WIZUALNA!
```
Gdy `blockSignals(True)` jest aktywne, `QAction` blokuje wewnętrzne powiadomienia Qt. W efekcie przycisk w `CommandBar` renderuje się jako aktywny (niebieski akcent Fluent), podczas gdy panel w splitterze ma zerowy rozmiar (`[0, 630, 0]`).

#### Dlaczego programista użył `blockSignals`?
Programista chciał zapobiec wywołaniu slotu:
```python
self.inspector_visibility_action.toggled.connect(
    lambda visible: self._set_workspace_panel_visible("inspector", visible)
)
```
Slot ten ustawia `self._workspace_visibility_override["inspector"] = visible`, co nadpisuje automatyczną responsywność. Zamiast blokować sygnały na poziomie obiektu Qt, należy zastosować flagę blokującą (ang. *re-entrancy guard*).

#### Rozwiązanie techniczne:
Wprowadzenie flagi `_syncing_workspace_actions`:
```python
# W _update_workspace_layout:
self._syncing_workspace_actions = True
try:
    for action, widget, visible in actions_to_sync:
        action.setChecked(visible)
        widget.setVisible(visible)
finally:
    self._syncing_workspace_actions = False

# W _set_workspace_panel_visible:
def _set_workspace_panel_visible(self, panel: str, visible: bool) -> None:
    if getattr(self, "_syncing_workspace_actions", False):
        return
    self._workspace_visibility_override[panel] = visible
    ...
```
Oraz dodanie `showEvent` do `RecipePage`:
```python
def showEvent(self, event: QShowEvent) -> None:
    super().showEvent(event)
    self._update_workspace_layout(force=True)
```

---

### 3.2. Błąd #3: Zwijanie menu urządzeń w `MainWindow`

W `app/ui/shell/main_window.py:3300-3329`:
```python
def _sync_apparatus_navigation_height(self) -> None:
    short = self.navigationInterface.panel.height() < self._apparatus_required_height
    if short and self.apparatus_navigation_item.isExpanded:
        self.apparatus_navigation_item.setExpanded(False, ani=False)
        self._apparatus_auto_collapsed = True
```
Wysokość `_apparatus_required_height` jest obliczana jako suma wysokości wszystkich elementów bocznego menu (~720 px).  
Jeśli okno uruchomi się na laptopie (np. ekran 1366×768 z paskiem zadań Windows o wysokości 40 px, gdzie dostępne okno ma wysokość ~700 px) lub okno nie jest zmaksymalizowane:
- `panel.height()` wynosi np. 690 px,
- warunek `short` staje się prawdziwy,
- **drzewo urządzeń zostaje bezpowrotnie zwinięte** i schowane za ikoną nadrzędną `Devices`,
- operator nie widzi zakładek Rigol, Keithley, Anritsu, MOKE ani Lakeshore, dopóki nie kliknie menu nawigacji lub nie zmieni rozmiaru okna.

#### Rozwiązanie techniczne:
Zapewnienie przewijalności panelu nawigacji w QFluentWidgets (`QScrollArea` wewnątrz `NavigationPanel`) lub obniżenie progu auto-kolapsu, aby nie ukrywać kluczowych urządzeń pomiarowych przy typowych wysokościach roboczych (650–750 px).

---

### 3.3. Błąd #4 i #5: Obsługa splitterów (`QSplitter`) i utrata konfiguracji

1. **Nadpisywanie pozycji dziennika zdarzeń:**  
   W `app/ui/shell/main_window.py:796-802`:
   ```python
   if show_log:
       total = max(0, self.shell_splitter.height())
       self.shell_splitter.setSizes([max(0, total - 125), 125])
   ```
   Każde zdarzenie `resizeEvent` (nawet o 1 piksel) oraz przełączenie zakładki `_navigate_to()` kasuje rozmiar splittera ustawiony ręcznie przez operatora i przywraca sztywne 125 px. Należy respektować poprzednie proporcje splittera.

2. **Brak zapisu `recipe_page.workspace_splitter`:**  
   W `_save_workspace()` zapisywany jest tylko stan `anritsu_page.workspace_splitter`. Proporcje paneli *Library* / *Tree* / *Inspector* w edytorze receptur są tracone po każdym zamknięciu programu.

---

### 3.4. Błąd #6: Pasek bezpieczeństwa `StationSafetyStrip` na ekranach średniej szerokości

W pliku `app/ui/shell/safety_strip.py:93`:
```python
self._reflow(mode="narrow" if width < 520 else "compact" if width < 900 else "wide")
```
Przejście na tryb `wide` następuje przy `width >= 900 px`. W trybie `wide` w jednym rzędzie umieszczane są:
1. Stan stacji (`readiness`)
2. Aktywne wyjścia (`outputs`)
3. Tryb pracy (`mode`)
4. Aktor i role (`actor` — np. `SIMULATION · engineer, operator, service`)
5. Przycisk `SAVE SETTINGS` (min. 100 px)
6. Przycisk `E-STOP` (min. 184 px)

Na zrzucie ekranu użytkownika (szerokość okna 1024 px) etykieta aktora została ucięta do postaci `SIMULATION · engin...`. Na węższych monitorach (900–980 px) zachodzi ryzyko wypchnięcia przycisku `E-STOP` poza prawy margines okna lub braku widoczności przycisku zapisu.

#### Rozwiązanie techniczne:
Zwiększenie progu trybu jednowierszowego (`wide`) do co najmniej `1100 px`. W przedziale 520–1100 px pasek powinien pracować w trybie dwuwierszowym (`compact`), który gwarantuje pełną czytelność tożsamości operatora i stałą, bezpieczną dostępność przycisku `E-STOP`.

---

### 3.5. Błąd #7: Wielokrotne przeładowywanie motywu podczas startu

1. `MainWindow.__init__` (linia 146): `apply_application_theme(application, ...)`
2. `app/main.py` (linia 32): `window._set_theme_mode(..., persist=False)`
3. `MainWindow.showEvent` (linia 3279): `apply_application_theme(application, self._configured_theme_mode)`

Każde wywołanie `apply_application_theme` powoduje pełne przejście po drzewie widżetów Qt i ponowne parsowanie reguł stylów QSS. Wyeliminowanie nadmiarowych wywołań skróci czas inicjalizacji aplikacji na Windows i zapobiegnie mikro-przesunięciom kontrolek podczas pierwszego renderowania.

---

## 4. Rekomendowany plan naprawczy

### Krok 1: Naprawa desynchronizacji przycisków w `RecipePage` (Priorytet krytyczny)
1. W [`app/ui/recipes/page.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/recipes/page.py):
   - Dodać atrybut `self._syncing_workspace_actions = False` w `__init__`.
   - Zastąpić `action.blockSignals(True)` / `action.blockSignals(False)` flagą `self._syncing_workspace_actions`.
   - W metodzie `_set_workspace_panel_visible()` sprawdzać tę flagę i wychodzić wcześnie.
   - Dodać implementację `showEvent(self, event: QShowEvent)` wywołującą `self._update_workspace_layout(force=True)`.

### Krok 2: Uodpornienie paska bezpieczeństwa `StationSafetyStrip`
1. W [`app/ui/shell/safety_strip.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/safety_strip.py):
   - Zmienić progi responsywności: `mode = "narrow" if width < 540 else "compact" if width < 1120 else "wide"`.
   - Zagwarantować, że przycisk `E-STOP` ma zawsze zachowany priorytet i nie podlega ściśnięciu poniżej 184 px.

### Krok 3: Stabilizacja proporcji splitterów w `MainWindow`
1. W [`app/ui/shell/main_window.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py):
   - W `_sync_shell_splitter_layout()` zapamiętywać dotychczasowe proporcje splittera zamiast twardego resetu do `125 px`.
   - W `_save_workspace()` i `_restore_workspace()` dodać zapis i odczyt stanu `recipe_page.workspace_splitter`.

### Krok 4: Uporządkowanie inicjalizacji powłoki
1. Usunąć nadmiarowe wywołanie `apply_application_theme()` w `main.py` (okno samo zarządza motywem na podstawie ustawień).
2. Dostosować próg zwijania gałęzi `apparatus_navigation_item` tak, by na standardowych ekranach desktopowych 768p menu nie ukrywało urządzeń stacji.

---

## 5. Podsumowanie dla Użytkownika

Zgłoszona usterka została dokładnie zdiagnozowana. Nie jest to błąd przypadkowy ani uszkodzenie konfiguracji, lecz konkretny błąd synchronizacji sygnałów Qt w edytorze receptur: program ukrywał panel przy zablokowanych sygnałach, przez co przycisk pozostawał włączony na pasku narzędzi. Kliknięcie „zamknij i otwórz” zmuszało Qt do ponownego wysłania sygnału i wymuszało naprawę geometrii.

Powyższy raport dokumentuje pełen mechanizm błędu oraz dodatkowe usterki układu wykryte podczas przeglądu kodu. Poprawki można wdrożyć w ramach ukierunkowanej refaktoryzacji GUI.

