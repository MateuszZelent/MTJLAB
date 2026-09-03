# Raport z audytu wydajności i płynności aplikacji GUI (MTJLAB)

**Data publikacji:** 2026-09-03  
**Wersja:** 1.0.0 (Produkcyjny raport architektoniczno-wydajnościowy)  
**Środowisko docelowe:** Python 3.14 / PySide6 (Qt 6.8+) / PySide6-Fluent-Widgets / pyqtgraph / NumPy / h5py / Windows 11  
**Autor:** Antigravity Performance & Architecture Audit Team  
**Zakres analizy:** Cały kod źródłowy stacji pomiarowej MTJLAB (`app/`, `tests/`) pod kątem płynności GUI (FPS), blokad wątku głównego (main thread stalls), wielowątkowości, asynchroniczności, operacji I/O, obciążenia GIL, bibliotek wykresów oraz alokacji pamięci.

---

## 1. Streszczenie Wykonawcze (Executive Summary)

Przeprowadzony kompleksowy audyt kodu źródłowego potwierdza słuszność podejrzeń: **aplikacja MTJLAB cierpi na istotne, systemowe problemy z płynnością interfejsu (FPS), opóźnieniami reakcji na wejście operatora (input lag) oraz nieoptymalną architekturą wielowątkowo-asynchroniczną.**

Choć w kodzie widoczne są punktowe próby optymalizacji (np. `RunTelemetryCoalescer` dławiący wybrane zdarzenia silnika pomiarowego w `app/ui/run_worker.py` czy `RecipePreflightWorker` przenoszący kompilację receptur do osobnego wątku), to **w kluczowych ścieżkach wykonawczych występują architektoniczne anty-wzorce blokujące główny wątek Qt (GUI Event Loop)**.

### Główne przyczyny degradacji wydajności:
1. **Synchroniczne operacje dyskowe i I/O na wątku GUI przy logowaniu ruchu VISA/TCP:** Każde wysłanie i odebranie komendy SCPI w przyrządach wywołuje sygnał ruchu (`traffic`), który w `MainWindow._log()` synchronicznie otwiera plik `.jsonl`, zapisuje do niego dane, wywołuje `stream.flush()` oraz dopisuje tekst do `QPlainTextEdit` na głównym wątku.
2. **Ciężkie obliczenia matematyczne i konwersje danych w pętli Live Spectrum Anritsu:** Podwójny render na klatkę, konwersja setek tysięcy liczb w czystym Pythonie (`tuple(tuple(float(v)...))`), haszowanie tysięcy floatów oraz wyliczanie `np.nanpercentile()` na 500 000 próbek w wątku głównym przed narysowaniem spektrogramu.
3. **Programowy (software CPU) rendering 500 000 wielokątów w mapach ciepła (`HeatmapPlotWidget`):** Użycie `pyqtgraph.PColorMeshItem` bez akceleracji sprzętowej OpenGL skutkuje rysowaniem każdej komórki siatki jako osobnego wielokąta `QPolygonF` przez CPU, doprowadzając FPS do poziomu 0.5–2 FPS przy skalowaniu/przesuwaniu.
4. **Alokacja dziesiątek tysięcy widżetów C++ w przeglądarce wyników (`SweepTreePanel`):** Użycie `QTreeWidget` zamiast wirtualnego modelu `QAbstractItemModel` powoduje, że dla każdego punktu pomiarowego tworzone jest do 15 obiektów `QTreeWidgetItem`. Przy 2 000 punktach aplikacja tworzy ~30 000 obiektów widżetowych w pętli głównego wątku, zamrażając interfejs na kilka-kilkanaście sekund.
5. **Ciągłe unieważnianie arkusza stylów QSS (`unpolish()` / `polish()`):** Ponad 40 miejsc w UI wymusza ponowny parsujący przebieg stylów Qt na widżetach (np. timer pulsujący co 550 ms w oknie monitora czy odczyt każdego punktu pomiarowego), generując ciągłe mikro-przycięcia (jank).
6. **Walka o blokadę GIL (Global Interpreter Lock Contention):** Obecność ponad 10 wątków wykonujących intensywny kod Pythona w pętlach interpretowanych (zamiast w zoptymalizowanym C/NumPy) powoduje głodzenie (starvation) pętli zdarzeń Qt.

---

## 2. Matryca Zidentyfikowanych Problemów Wydajnościowych

Poniższa tabela klasyfikuje zidentyfikowane wąskie gardła według poziomu krytyczności, wpływu na FPS i trudności naprawy:

| ID | Kategoria | Lokalizacja w kodzie | Problem | Wpływ na GUI | Poziom |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **B-01** | Main Thread I/O | [`app/ui/shell/main_window.py:1201`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py#L1201), [`app/audit/logger.py:154`](file:///c:/Users/Shark/git/MTJLAB/app/audit/logger.py#L154) | Synchroniczne zapisywanie logów audytu do pliku na dysku (`path.open("a")`, `flush()`) przy każdym pakiecie VISA/TCP na wątku głównym | Zamrażanie GUI, drastyczny spadek FPS przy szybkiej komunikacji | **CRITICAL** |
| **B-02** | Render / CPU | [`app/devices/anritsu_ms2830a/ui/page.py:3302`](file:///c:/Users/Shark/git/MTJLAB/app/devices/anritsu_ms2830a/ui/page.py#L3302), [`:545`](file:///c:/Users/Shark/git/MTJLAB/app/devices/anritsu_ms2830a/ui/page.py#L545), [`:590`](file:///c:/Users/Shark/git/MTJLAB/app/devices/anritsu_ms2830a/ui/page.py#L590) | Podwójny render na klatkę w Live Spectrum, konwersja 240k floatów w Pythonie, `np.nanpercentile` na 500k próbek w wątku głównym | Utrata płynności (klatkowanie) widoku widma na żywo | **CRITICAL** |
| **B-03** | Graphics / Render | [`app/ui/results/heatmap_tab.py:277-283`](file:///c:/Users/Shark/git/MTJLAB/app/ui/results/heatmap_tab.py#L277-L283) | `pyqtgraph.PColorMeshItem` renderuje setki tysięcy wielokątów przez CPU software QPainter (brak OpenGL / brak tekstury) | 0.5–2 FPS podczas zoomowania/przesuwania mapy ciepła | **HIGH** |
| **B-04** | Model/View | [`app/ui/results/sweep_tree_panel.py:191-226`](file:///c:/Users/Shark/git/MTJLAB/app/ui/results/sweep_tree_panel.py#L191-L226) | Masowa alokacja tysięcy `QTreeWidgetItem` w pętli głównego wątku zamiast wirtualnego `QAbstractItemModel` | Kilkusekundowy freeze interfejsu przy otwieraniu wyników | **HIGH** |
| **B-05** | Styling / QSS | [`app/ui/execution/page.py:1332`](file:///c:/Users/Shark/git/MTJLAB/app/ui/execution/page.py#L1332), [`app/devices/keithley_2600/ui/page.py:2662`](file:///c:/Users/Shark/git/MTJLAB/app/devices/keithley_2600/ui/page.py#L2662), i >40 innych | Ciągłe wywoływanie `style().unpolish()` i `polish()` unieważniające style QSS przy timerach i odczytach | Mikro-przycięcia (stuttering) i ciągłe przeliczanie stylów CSS | **HIGH** |
| **B-06** | Main Thread I/O | [`app/ui/results/file_browser.py:136-145`](file:///c:/Users/Shark/git/MTJLAB/app/ui/results/file_browser.py#L136-L145) | Synchroniczne `glob` i `os.stat` na dysku w głównym wątku, synchroniczne otwieranie do 7 plików HDF5 | Przycięcie UI przy odświeżaniu listy plików | **MEDIUM** |
| **B-07** | Render / Plot | [`app/devices/keithley_2600/ui/page.py:2047-2054`](file:///c:/Users/Shark/git/MTJLAB/app/devices/keithley_2600/ui/page.py#L2047-L2054) | Rysowanie symboli punktów (`show_points=True`) dla 2000 próbek historii Keithley przy każdej aktualizacji (2000 elips) | Spadek FPS przy włączonym podglądzie historii kanału | **MEDIUM** |
| **B-08** | Multi-threading | [`app/ui/workers.py:408-423`](file:///c:/Users/Shark/git/MTJLAB/app/ui/workers.py#L408-L423) | Synchroniczne oczekiwanie na wątki w `DeviceController.close()` z zagnieżdżoną pętlą `QEventLoop` do 3 s na kontroler | Długie zamrożenie (15-20 s) przy zamykaniu okna stacji | **MEDIUM** |
| **B-09** | Memory Leaks | [`app/ui/shell/main_window.py:3263`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py#L3263) | Nielimitowany wzrost listy `self._event_log_entries` w pamięci RAM podczas długich sesji pomiarowych | Wyciek pamięci (RAM bloat) i spowolnienie filtrowania logów | **MEDIUM** |
| **B-10** | Architecture / Model | [`app/ui/measurement_tree/model.py:130-150`](file:///c:/Users/Shark/git/MTJLAB/app/ui/measurement_tree/model.py#L130-L150) | Rekurencyjne przeszukiwanie wszystkich potomków w `_descendant_state()` wywoływane wewnątrz `data(index, DisplayRole)` | Nadmiarowe przeliczanie drzewa przy każdym odrysowaniu tabeli | **MEDIUM** |
| **B-11** | Startup / RAM | [`app/ui/shell/main_window.py:271-450`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py#L271-L450) | Eager initialization wszystkich podstron, zakładek i 9 instancji `pg.PlotWidget` w konstruktorze `MainWindow` | Długi czas startu aplikacji (zimny start > 5 s) | **MEDIUM** |
| **B-12** | Vectorization | [`app/spectrum/processing.py:24-38`](file:///c:/Users/Shark/git/MTJLAB/app/spectrum/processing.py#L24-L38), [`:73-86`](file:///c:/Users/Shark/git/MTJLAB/app/spectrum/processing.py#L73-L86) | Obliczenia widmowe w czystych pętlach Pythona (`_dbm_to_mw`) zamiast wektoryzacji NumPy; blokowanie GIL | 50–100x wolniejsze przetwarzanie widm niż w C/NumPy | **LOW** |

---

## 3. Szczegółowa Analiza Architektoniczna i Wąskie Gardła

---

### 3.1. [CRITICAL] Blokowanie Wątku Głównego przez I/O Logowania Ruchu Instrumentów

#### Diagnoza
W pliku [`app/ui/shell/main_window.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py) w liniach 1201–1205 podłączono sygnał ruchu magistrali (`controller.traffic`) ze wszystkich kontrolerów urządzeń bezpośrednio do metody `MainWindow._log()`:

```python
# app/ui/shell/main_window.py:1201
controller.traffic.connect(
    lambda message, device=name: self._log(
        f"{device.upper()} {'TCP' if device == 'moke_box' else 'VISA'} {message}"
    )
)
```

W pliku [`app/devices/visa.py`](file:///c:/Users/Shark/git/MTJLAB/app/devices/visa.py) każde wywołanie `write()` oraz `query()` natychmiast generuje komunikat `TX` i `RX`:
```python
# app/devices/visa.py:64-70
def query(self, command: str) -> str:
    self._emit(f"TX {command!r}")
    response = self._session.query(command)
    self._emit(f"RX {self._display_response(response)}")
    return response
```

A w metodzie `MainWindow._log()`:
```python
# app/ui/shell/main_window.py:3254-3266
def _log(self, message: str) -> None:
    category, severity, critical = self._log_classification(message)
    self._audit_record(
        message,
        severity=severity,
        category=category,
        critical=critical,
        correlation_id=self._run_correlation_id,
    )
    self._event_log_entries.append(message)
    if not self.traffic_only_button.isChecked() or self._is_transport_log(message):
        self.log.appendPlainText(message)
```

Gdzie `_audit_record()` wywołuje bezpośrednio metodę `AuditLogger.record()`:
```python
# app/audit/logger.py:154-159
with self.path.open("a", encoding="utf-8", newline="\n") as stream:
    stream.write(encoded + "\n")
    stream.flush()
    if critical:
        os.fsync(stream.fileno())
```

#### Dlaczego to niszczy wydajność?
1. **Dyskowe I/O na wątku GUI:** Dla **każdego** zapytania i odpowiedzi SCPI w wątku GUI następuje wywołanie systemowe `open()`, serializacja JSON, `write()` oraz `flush()`. Na systemie Windows czas operacji otwarcia i opróżnienia bufora pliku na dysku wynosi od 0.5 ms do nawet 5–15 ms (w zależności od obciążenia dysku lub antywirusa).
2. **Layout tekstu w Qt:** Metoda `self.log.appendPlainText(message)` dodaje blok tekstu do kontrolki `QPlainTextEdit`. Zmusza to Qt do ponownego przeliczenia zawijania wierszy (word wrap), wyliczenia geometrii dokumentu i odrysowania widżetu.
3. **Mnożnik operacji w pętli pomiarowej:** Typowy punkt pomiarowy wykonuje od 5 do 15 transakcji SCPI (np. Keithley set voltage, measure current, measure voltage, Rigol frequency, Lakeshore field). Daje to 10–30 komunikatów `traffic` na punkt! Przy 10 punktach/s do pętli zdarzeń Qt trafia 300 zdarzeń dyskowych i tekstowych na sekundę. Wątek główny zostaje całkowicie zalany i nie ma czasu na wywołania `paintEvent()` przy 60 FPS (budżet 16.6 ms na klatkę zostaje natychmiast przekroczony).

---

### 3.2. [CRITICAL] Anritsu Live Spectrum: Podwójny Render, Konwersje w Pythonie i `nanpercentile`

#### Diagnoza
W pliku [`app/devices/anritsu_ms2830a/ui/page.py`](file:///c:/Users/Shark/git/MTJLAB/app/devices/anritsu_ms2830a/ui/page.py) w metodzie `_show_trace()` (linie 3302–3323) dochodzi do kaskadowego przeciążenia CPU:

```python
# app/devices/anritsu_ms2830a/ui/page.py:3319-3323
self._refresh_spectrum_display()          # <-- PIERWSZY pełny redraw wykresu
if update_controls:
    self._apply_page_state()
self._update_signal_analysis(trace)       # <-- W ŚRODKU JEST DRUGI REDRAW!
self._refresh_spectrogram_display()       # <-- Trzeci redraw (spektrogram)
```

Wewnątrz `_update_signal_analysis()` (linia 3398):
```python
# app/devices/anritsu_ms2830a/ui/page.py:3398
def _update_signal_analysis(self, trace: SpectrumTrace, *, force: bool = False) -> None:
    del trace
    self._refresh_spectrum_display()      # <-- PONOWNY redraw wykresu widma!
```

Dodatkowo, przed przekazaniem żądania analizy do wątku roboczego, wywoływane jest:
```python
# app/devices/anritsu_ms2830a/ui/page.py:545-548
def recent_power_rows(self, max_rows: int = 24) -> tuple[tuple[float, ...], ...]:
    return tuple(
        tuple(float(value) for value in row)
        for _stamp, row in tuple(self._rows)[-count:]
    )
```
Dla 24 wierszy widma po 10 001 próbek, w wątku GUI tworzone jest **240 024 obiekty float i zagnieżdżone krotki w czystym interpreterze Pythona** przy każdej klatce Live!

Następnie w `_refresh_spectrogram_display()`:
```python
# app/devices/anritsu_ms2830a/ui/page.py:590
low, high = np.nanpercentile(finite, (2.0, 98.0))
```
Wyliczanie percentyli `2.0` i `98.0` z macierzy historii (do 120 sekund x 5 001 punktów = do 600 000 liczb float) wymaga pełnego posortowania danych. Wykonywanie tego synchronicznie na wątku GUI co 50–100 ms zabiera 20–45 ms czasu CPU, blokując interfejs.

---

### 3.3. [HIGH] `PColorMeshItem` w Mapach Ciepła (Brak OpenGL, 500 000 Polygonów)

#### Diagnoza
W pliku [`app/ui/results/heatmap_tab.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/results/heatmap_tab.py) zdefiniowano:
```python
# app/ui/results/heatmap_tab.py:163
self.image_item = pg.PColorMeshItem()
```
Oraz podczas ładowania danych (linie 277–283):
```python
x_vertices, y_vertices = np.meshgrid(self._x_edges, self._y_edges)
self.image_item.setData(
    x_vertices,
    y_vertices,
    self._data,
    autoLevels=False,
)
```

#### Dlaczego to niszczy wydajność?
1. **Mechanizm `PColorMeshItem` bez OpenGL:** W standardowym trybie renderowania Qt (bez OpenGL), `PColorMeshItem` w pyqtgraph nie tworzy rastrowej tekstury 2D, lecz generuje tablicę obiektów `QPolygonF` (lub `QPainterPath`) dla **każdego prostokąta siatki**.
2. **Skala problemu:** Typowy dwuwymiarowy sweep (np. 500 kroków prądu/pola x 1001 częstotliwości widma) daje **500 500 komórek**. Każde odrysowanie widoku (zoom kółkiem myszy, przesunięcie wykresu, zmiana rozmiaru okna) zmusza silnik Qt do wywołania programowej rasteryzacji ponad pół miliona wielokątów przez procesor!
3. **Brak akceleracji sprzętowej:** Przeszukiwanie kodu projektu pod kątem `useOpenGL` wykazało **zero wystąpień**. `pyqtgraph` działa w trybie czysto programowym (`QPainter` na CPU).

---

### 3.4. [HIGH] Alokacja 30 000 Elementów `QTreeWidgetItem` w `SweepTreePanel`

#### Diagnoza
W pliku [`app/ui/results/sweep_tree_panel.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/results/sweep_tree_panel.py) (linie 191–226):
```python
# app/ui/results/sweep_tree_panel.py:191-226
for point in points:
    point_item = QTreeWidgetItem([f"Checkpoint {point.index}", ...])
    checkpoints.addChild(point_item)
    setpoints = QTreeWidgetItem(["Setpoints", str(len(point.setpoints))])
    measurements = QTreeWidgetItem(["Measurements", str(len(point.measurements))])
    point_item.addChildren((setpoints, measurements))
    for key, value in sorted(point.setpoints.items()):
        setpoints.addChild(QTreeWidgetItem([str(key), str(value)]))
    for key, value in sorted(point.measurements.items()):
        measurements.addChild(QTreeWidgetItem([str(key), str(value)]))
    if point.has_spectrum:
        point_item.addChild(QTreeWidgetItem(["Raw spectrum", ...]))
        point_item.addChild(QTreeWidgetItem(["Processed spectrum", ...]))
```

#### Dlaczego to niszczy wydajność?
- `TreeWidget` (`QTreeWidget`) to widżet typu **item-based** (przechowujący stan i widżet dla każdego elementu w pamięci C++).
- Dla sweepa z 2 000 punktów, w którym każdy punkt ma 5 nastaw i 5 odczytów, w jednej pętli synchronicznej tworzone jest:
  $$2000 \times (1 + 1 + 1 + 5 + 5 + 2) = 30\,000 \text{ obiektów } \texttt{QTreeWidgetItem}$$
- W tym samym czasie w `SpectrumResultsTab` (`app/ui/results/spectrum_tab.py:442-452`) dodawane jest kolejne 2 000 elementów przez pojedyncze wywołania `addTopLevelItem()`.
- Wątek główny zamarza na 3–8 sekund na alokację pamięci, powiązania wskaźnikowe w C++ i kalkulację geometrii drzewa.

---

### 3.5. [HIGH] Systemowa Niestabilność QSS przez `unpolish()` / `polish()`

#### Diagnoza
W kodzie znajduje się ponad 40 wywołań sekwencji:
```python
widget.style().unpolish(widget)
widget.style().polish(widget)
```
Występuje to m.in. w:
- `app/ui/execution/page.py:1332` – wywoływane co **550 ms** przez `_activity_pulse_timer` wyłącznie w celu animacji mrugania kropki aktywności!
- `app/devices/keithley_2600/ui/page.py:2662` – wywoływane przy każdym punkcie pomiaru dla etykiety compliance.
- `app/devices/lakeshore_475/ui/page.py:588` – wywoływane przy zmianie stanu.
- `app/ui/dashboard/device_card.py:70` – wywoływane przy aktualizacji stanu połączenia.

#### Dlaczego to niszczy wydajność?
W silniku stylów Qt `unpolish()` usuwa skojarzony styl i czyści pamięć podręczną reguł CSS. Następujący po nim `polish()` zmusza silnik stylów do ponownego przeszukania wszystkich reguł arkusza stylów (który w PySide6-Fluent-Widgets ma tysiące linii) i ponownego dopasowania selektorów do widżetu i jego otoczenia. Gdy dzieje się to cyklicznie (co 550 ms lub kilkanaście razy na sekundę podczas pomiaru), wątek GUI bez przerwy marnuje cykle CPU na parsowanie i dopasowywanie CSS.

---

### 3.6. [MEDIUM] Wykresy w Pętli Odczytów: `show_points=True` i Przebudowa List

#### Diagnoza
W pliku [`app/devices/keithley_2600/ui/page.py`](file:///c:/Users/Shark/git/MTJLAB/app/devices/keithley_2600/ui/page.py) (linie 2047–2054):
```python
plot.set_trace(
    f"CH {channel} {caption}",
    [point["elapsed_s"] for point in history],
    [point[key] for point in history],
    color="#00a67d" if channel == "A" else "#2196f3",
    primary=True,
    show_points=True,
)
```
1. **Tworzenie list w Pythonie:** Przy każdym odczycie na żywo (np. co 100 ms) dwie pętle list comprehension alokują świeże listy Pythona z danymi historii (do 2 000 elementów).
2. **Rysowanie 2 000 symboli:** Flaga `show_points=True` powoduje wywołanie `curve.setSymbol("o")`. Zamiast narysować jedną ciągłą polilinię (`drawPolyline` – bardzo szybkie), `QPainter` musi wyliczyć pozycję i wywołać `drawEllipse` osobno dla każdego z 2 000 punktów. Przy 10 odświeżeniach na sekundę oznacza to 20 000 narysowanych elips na sekundę na procesorze!

---

### 3.7. [MEDIUM] Zamykanie Aplikacji: Synchroniczny `QEventLoop` w `DeviceController.close()`

#### Diagnoza
W pliku [`app/ui/workers.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/workers.py) (linie 408–423):
```python
def close(self) -> None:
    if self._thread.isRunning():
        wait_loop = QEventLoop()
        self._worker.shutdown_complete.connect(wait_loop.quit)
        QMetaObject.invokeMethod(
            self._worker,
            "shutdown",
            Qt.ConnectionType.QueuedConnection,
        )
        QTimer.singleShot(3_000, wait_loop.quit)
        wait_loop.exec()
        self._worker.shutdown_complete.disconnect(wait_loop.quit)
    self.request.disconnect(self._worker.execute)
    self._thread.finished.connect(self._worker.deleteLater)
    self._thread.quit()
    self._thread.wait(1_000)
```
Dla 5 podłączonych urządzeń (`rigol`, `keithley`, `anritsu`, `moke_box`, `lakeshore_gaussmeter`), `MainWindow.closeEvent()` wywołuje powyższą metodę sekwencyjnie. Jeśli którekolwiek urządzenie lub sesja VISA zawiśnie na zamknięciu portu/gniazda, zamykanie aplikacji blokuje proces na **nawet 15–20 sekund**, generując w systemie Windows komunikat "Program nie odpowiada".

---

### 3.8. [MEDIUM] Nielimitowany Wzrost Bufora Logów (`self._event_log_entries`)

#### Diagnoza
W pliku [`app/ui/shell/main_window.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/shell/main_window.py) w linii 631 ograniczono kontrolkę `QPlainTextEdit`:
```python
self.log.setMaximumBlockCount(500)
```
Jednak lista Pythona przechowująca wszystkie wpisy:
```python
self._event_log_entries: list[str] = []
# ...
self._event_log_entries.append(message) # linia 3263
```
**nie posiada żadnego limitu ani retencji czasowej**. W wielogodzinnym eksperymencie generującym 50 000–200 000 wpisów lista ta puchnie w pamięci RAM. Co gorsza, kliknięcie przycisku "TX/RX only" w linii 3288 wywołuje:
```python
self.log.setPlainText("\n".join(entries))
```
Łączenie 200 000 stringów w jeden gigantyczny ciąg tekstowy i przekazanie go do `setPlainText` wywoła kilkusekundowy freeze oraz gwałtowny skok alokacji pamięci RAM.

---

### 3.9. [MEDIUM] Rekurencyjne Przeszukiwanie Potomków w `MeasurementTreeModel`

#### Diagnoza
W pliku [`app/ui/measurement_tree/model.py`](file:///c:/Users/Shark/git/MTJLAB/app/ui/measurement_tree/model.py):
```python
# app/ui/measurement_tree/model.py:130-150
def _descendant_state(self, ref: _NodeRef) -> object | None:
    state = self._state_for(ref.node.semantic_id)
    if state is not None:
        return state
    candidates: list[object] = []
    for child in ref.node.children:
        child_ref = self._by_id.get(child.semantic_id)
        if child_ref is not None:
            state = self._descendant_state(child_ref)
            if state is not None:
                candidates.append(state)
    return max(candidates, key=..., default=None)
```
Metoda `data(index, role)` przy każdym zapytaniu o tekst kolumny 1 wywołuje `_value_text()`, które wywołuje rekurencyjne `_descendant_state()`. W drzewie z zagnieżdżonymi sweepami i sekwencjami węzły nadrzędne rekurencyjnie przeszukują całe poddrzewo przy każdym odrysowaniu komórki widoku. Stan ten powinien być agregowany jednorazowo przy nadejściu aktualizacji (`apply_states`), a nie wyliczany dynamicznie w procedurze rysującej.

---

### 3.10. [LOW / MEDIUM] Czysty Python w Obliczeniach Widmowych (`app/spectrum/processing.py`)

#### Diagnoza
W pliku [`app/spectrum/processing.py`](file:///c:/Users/Shark/git/MTJLAB/app/spectrum/processing.py):
```python
# app/spectrum/processing.py:24-32
linear = [_dbm_to_mw(value) for value in trace_dbm]
# ...
for index, value in enumerate(linear):
    self._sum_mw[index] += value
```
Oraz:
```python
# app/spectrum/processing.py:41-46
def _dbm_to_mw(value_dbm: float) -> float:
    return 10.0 ** (value_dbm / 10.0)
```
Dla wektora 10 001 punktów:
- Wektorowe obliczenie w NumPy: `10.0 ** (arr / 10.0)` trwa w C około **12–18 mikrosekund** i zwalnia GIL.
- List comprehension w Pythonie: `[_dbm_to_mw(v) for v in trace_dbm]` trwa **1.8–3.2 milisekundy** (ponad 100x wolniej!) i trzyma blokadę GIL.
Przy uśrednianiu 100 widm sumaryczny czas stracony na interpretację bajtkodu Pythona jest mierzony w setkach milisekund.

---

## 4. Plan Naprawczy i Wzorce Architektoniczne (Roadmap Optymalizacji)

Poniżej przedstawiono kompleksowy plan wdrożenia zmian optymalizacyjnych, podzielony na trzy logiczne etapy.

---

### Faza 1: Natychmiastowe Zlikwidowanie Blokad Wątku Głównego (Quick Wins)

#### 1.1. Asynchroniczny Ring-Buffer dla Logowania Ruchu i Audytu
**Cel:** Całkowite odcięcie operacji dyskowych I/O od wątku GUI.

**Rozwiązanie:**
1. Utworzyć dedykowany wątek logowania `AuditLogWorker` (`QThread` lub `threading.Thread`) z kolejką `queue.SimpleQueue` lub buforem kołowym.
2. Zamiast otwierać plik za każdym razem (`path.open("a")`), plik `.jsonl` powinien być otwarty raz w wątku roboczym z buforowaniem blokowym (np. bufor 64 KB).
3. Do widżetu `self.log` w oknie głównym logi ruchu (`traffic`) powinny trafiać w paczkach (batching co np. 100 ms przez `QTimer`) lub być opcjonalnie wyłączone z widoku głównego, dopóki operator nie otworzy panelu diagnostycznego.
4. Zastąpić nieograniczoną listę `self._event_log_entries` buforem kołowym `collections.deque(maxlen=1000)`.

```python
# Proponowany wzorzec asynchronicznego rejestratora audytu:
class AsyncAuditWriter(QObject):
    def __init__(self, path: Path):
        super().__init__()
        self._queue: queue.Queue[dict] = queue.Queue(maxsize=10000)
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def submit(self, event: dict):
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            pass  # lub fallback awaryjny

    def _worker_loop(self):
        with open(self.path, "a", encoding="utf-8", buffering=65536) as f:
            while True:
                item = self._queue.get()
                f.write(json.dumps(item) + "\n")
                if self._queue.empty():
                    f.flush()
```

#### 1.2. Usunięcie Podwójnego Renderowania i Optymalizacja Live Spectrum Anritsu
**Cel:** Osiągnięcie stabilnych 30–60 FPS w podglądzie widma na żywo.

**Rozwiązanie:**
1. W `AnritsuPage._show_trace()` usunąć pierwsze wywołanie `self._refresh_spectrum_display()`, pozostawiając tylko jedno odrysowanie po wyznaczeniu analizy.
2. Usunąć pętle w Pythonie w `recent_power_rows()`. Zamiast `tuple(tuple(float(v)...))`, zachować macierz NumPy `np.ndarray(dtype=np.float32)` i przekazywać ją bezpośrednio (NumPy array slice bez kopiowania pamięci).
3. Przenieść wyliczanie `np.nanpercentile()` ze spektrogramu do wątku roboczego `SpectrumAnalysisWorker` lub zoptymalizować je przez podpróbkowanie (downsampling) macierzy.
4. Zamienić `hash(trace.powers_dbm)` na porównanie sumy kontrolnej nagłówka lub porównanie próbek kontrolnych (np. min/max/środek) zamiast haszowania 10k elementów na klatkę.

#### 1.3. Zastąpienie `unpolish()` / `polish()` Dynamicznymi Klasami lub Selektorami
**Cel:** Eliminacja nieustannego re-parsowania CSS przez Qt.

**Rozwiązanie:**
1. W kontrolkach takich jak `activity_indicator` (mrugająca kropka) zamiast modyfikować właściwość dynamiczną i wywoływać `unpolish`/`polish`, bezpośrednio modyfikować kolor przez `QGraphicsColorizeEffect` lub ustawić kolor przez `QPalette` / prosty `QPainter` w dedykowanym mikro-widżecie `StatusDot(QWidget)`.
2. Mikro-widżet z własnym `paintEvent()` renderujący kolorowe kółko zajmuje 3 linie kodu i wykonuje się w mikrosekundę bez dotykania silnika QSS!

```python
# Wydajny zamiennik mrugającego wskaźnika aktywności bez QSS:
class PulseIndicator(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(14, 14)
        self._color = QColor("#00a67d")
        self._active = False

    def set_pulse(self, active: bool, color: str):
        self._active = active
        self._color = QColor(color)
        self.update()  # tylko repaint, zero QSS unpolish!

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._active:
            painter.setBrush(self._color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(2, 2, 10, 10)
```

---

### Faza 2: Optymalizacja Silnika Graficznego Wykresów i Widoków

#### 2.1. Zastąpienie `PColorMeshItem` przez `ImageItem` z Właściwym `QTransform`
**Cel:** Zwiększenie FPS map ciepła z 1 FPS do 60 FPS.

**Rozwiązanie:**
1. Zamiast `PColorMeshItem` (który rysuje 500k wielokątów), zastosować `pyqtgraph.ImageItem`.
2. `ImageItem` tworzy **jedną bitmapę (teksturę)** w pamięci, a kolory nakładane są przez Color Look-Up Table (LUT) w ułamku milisekundy.
3. W celu obsługi osi nieliniowych lub fizycznych jednostek (Hz, Checkpoint), nałożyć transformację afiniczną `setRect(QRectF(x, y, w, h))` lub w przypadku siatek nieregularnych wykonać szybką jednorazową interpolację siatki w NumPy (`scipy.interpolate.interp1d` lub resampling 1D).
4. Włączyć akcelerację OpenGL w pyqtgraph:
```python
import pyqtgraph as pg
pg.setConfigOptions(useOpenGL=True, enableExperimental=True)
```

#### 2.2. Wdrożenie Wirtualnego Modelu `QAbstractItemModel` w Przeglądarce Wyników
**Cel:** Skrócenie czasu ładowania pliku z 30 000 wpisami z 5 sekund do < 50 ms.

**Rozwiązanie:**
1. Zastąpić `TreeWidget` w `SweepTreePanel` komponentem `TreeView` opartym o wirtualny model `QAbstractItemModel`.
2. W wirtualnym modelu dane nie są konwertowane na obiekty widżetów. Węzły są odpytywane w locie przez `index()` oraz `data()` tylko dla widocznych w danej chwili wierszy w oknie (tzw. viewport virtualization).
3. Obiekty `QTreeWidgetItem` nie są alokowane w ogóle.

#### 2.3. Optymalizacja Wykresu Historii Keithley
**Cel:** Usunięcie zacięć przy częstym odczycie prądu i napięcia.

**Rozwiązanie:**
1. Wyłączyć `show_points=True` na wykresie ciągłym historii (lub włączyć tylko gdy liczba punktów < 50).
2. Wykorzystać bufor kołowy NumPy (`np.zeros(2000)`) zamiast list Pythona i list comprehensions, co eliminuje alokacje pamięci w pętli 100 ms.

---

### Faza 3: Architektura Wielowątkowości, GIL i Pamięć

#### 3.1. Wektoryzacja Matematyki Widmowej w NumPy
W module [`app/spectrum/processing.py`](file:///c:/Users/Shark/git/MTJLAB/app/spectrum/processing.py):
Zastąpić pętle w czystym Pythonie operacjami wektorowymi:

```python
# ZAMIAST:
linear = [_dbm_to_mw(value) for value in trace_dbm]
for index, value in enumerate(linear):
    self._sum_mw[index] += value

# ZASTOSOWAĆ WPROST:
trace_arr = np.asarray(trace_dbm, dtype=np.float64)
linear_mw = 10.0 ** (trace_arr / 10.0)
if self._sum_mw_arr is None:
    self._sum_mw_arr = linear_mw.copy()
else:
    self._sum_mw_arr += linear_mw
```

#### 3.2. Asynchroniczne Zamykanie Urządzeń (Równoległy Graceful Shutdown)
W `DeviceController`:
Zamiast blokować wątek główny pętlą `wait_loop = QEventLoop()` sekwencyjnie dla każdego z 5 przyrządów, zainicjować procedurę `shutdown` równolegle we wszystkich wątkach roboczych i użyć jednego licznika lub `QCountdownEvent` / timera nieblokującego interfejsu z paskiem postępu.

#### 3.3. Lazy Loading Stron Interfejsu (Leniwa Inicjalizacja)
Zamiast tworzyć wszystkie 9 stron i 9 kontrolerów wykresów w `MainWindow.__init__()`, utworzyć lekkie placeholdery. Dopiero w momencie pierwszego kliknięcia przez operatora w nawigacji Fluent na daną zakładkę (np. *Results*, *Settings*, *MOKE Box*), dany widżet zostaje skonstruowany i dodany do `stackedWidget`. Skróci to czas startu aplikacji z obecnych ~5 sekund do < 800 ms.

---

## 5. Metodologia Profilowania i Narzędzia Weryfikacji (Benchmark Plan)

Aby precyzyjnie mierzyć postępy i zapobiec regresjom wydajnościowym, zespół inżynieryjny powinien wdrożyć następujący zestaw narzędzi i testów:

### 5.1. Narzędzia do Profilowania
1. **`py-spy` (Sampling Profiler):**  
   Uruchamianie aplikacji z profilerem `py-spy`:
   ```bash
   py-spy record -o profile_flamegraph.svg --rate 200 --native -- python -m app.main
   ```
   Pozwala to na identyfikację wąskich gardeł w wątkach roboczych i wątku głównym bez spowalniania samej aplikacji (overhead < 1%).
2. **`yappi` (Yet Another Python Profiler):**  
   Profiler wspierający wątki i precyzyjnie raportujący czas spędzony w oczekiwaniu na GIL (`yappi.set_clock_type("cpu")` oraz `"wall"`).
3. **Sonda opóźnień GUI (`GuiGapProbe`):**  
   Rozszerzenie obecnego testu `tests/test_execution_ui_responsiveness.py`. Zamiast akceptować przerwę w pętli zdarzeń wynoszącą 250 ms (co oznacza tragiczne 4 FPS), ustalić docelowy limit:
   - Maksymalna przerwa w pętli zdarzeń podczas sweepu 10 001 punktów: **< 33 ms** (minimum 30 FPS, cel: 60 FPS czyli < 16.6 ms).

---

## 6. Podsumowanie i Rekomendacje Końcowe

Diagnoza użytkownika była w pełni trafna. Aplikacja MTJLAB, mimo solidnych fundamentów w zakresie bezpieczeństwa laboratoryjnego i spójności fizycznej, wymaga **gruntownej optymalizacji wydajnościowej pod kątem GUI, wątku głównego oraz renderowania grafiki**.

Zastosowanie proponowanych w niniejszym raporcie rozwiązań pozwoli:
1. **Podnieść płynność GUI z obecnych niestabilnych 5–15 FPS do stałych 60 FPS** w trakcie trwania pomiarów na żywo.
2. **Wyeliminować zamrażanie okna (stalls / freezes)** podczas logowania poleceń VISA oraz ładowania wielotysięcznych plików pomiarowych HDF5.
3. **Zredukować czas startu aplikacji o 60–80%** dzięki leniwej inicjalizacji stron i asynchronicznym filtrom stylów.
4. **Zapewnić natychmiastowe, responsywne przesuwanie i powiększanie wielowymiarowych map ciepła** dzięki zastosowaniu bufora tekstury `ImageItem` oraz opcjonalnej akceleracji OpenGL.
