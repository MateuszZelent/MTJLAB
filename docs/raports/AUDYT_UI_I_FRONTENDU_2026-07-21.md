# Raport z Kompleksowego Audytu UI, Frontendu i Dostępności Aplikacji

**Data audytu:** 21 lipca 2026 r.  
**Zakres:** Powierzchnia interfejsu PySide6-Fluent-Widgets, architektura UI, wydajność renderowania, spójność z systemem tokenów designu, obsługa wielowątkowości, zgodność z kontraktem `AGENTS.md` oraz bezpieczeństwo stanowiska pomiarowego.

---

## 1. Podsumowanie Wykonawcze (Executive Summary)

Aplikacja przeszła głęboką migrację w kierunku natywnej architektury **PySide6-Fluent-Widgets**, wprowadzając spójny powłokowy układ głównego okna (`MainWindow` w `app/ui/shell/main_window.py`), pasek bezpieczeństwa (`StationSafetyStrip`), zintegrowany pasek nawigacji oraz scentralizowane motywy oparte na paletach Catppuccin Mocha (Dark) i Catppuccin Latte (Light) w `app/ui/design_system/tokens.py`.

Jakkolwiek ogólna struktura aplikacji jest wysokiej jakości, audyt ujawnił **kluczowe błędy krytyczne, wąskie gardła wydajnościowe oraz zaległości refaktoryzacyjne**:

1. **Błąd Krytyczny Eksportu Danych (Crash UX)**: Eksport widma do CSV w `app/ui/widgets/spectrum_plot.py` używa `open(path, "x")`, co powoduje zgłoszenie wyjątku `FileExistsError` i awarię akcji eksportu przy próbie nadpisania istniejącego pliku wybranego przez użytkownika w oknie dialogowym.
2. **Problem Wydajnościowy (Mega-pliki i Opóźnienie Startu)**:
   - `app/ui/recipes/page.py` osiągnął rozmiar **223 KB (~5 500 linii kodu!)**, łącząc logikę drzewa edycji, generatorów kroków sweep, walidacji parametrów i widoków pomocniczych.
   - `app/ui/shell/main_window.py` synchronicznie instancjonuje **wszystkie 5 stron urządzeń, edytor receptur, monitor wykonania, wynikową przeglądarkę HDF5 oraz okna szybkich kontroli** podczas inicjalizacji aplikacji na głównym wątku UI.
3. **Naruszenie Priorytetu Bezpieczeństwa (Visual Safety Affordance)**: Przycisk awaryjnego zatrzymania `E-STOP` w `app/ui/shell/main_window.py` ma przypisaną właściwość `visualPriority="low"` oraz wymuszony wąski rozmiar `96px`, co osłabia jego widoczność w sytuacjach zagrożenia sprzętowego.
4. **Błędy Stylistyczne i Wycieki Kolorów QSS (Design Tokens Leak)**:
   - Część komponentów (np. edytor receptur, pola limitów, panele przeglądarki wyników) używa sztywno zakodowanych ciągów QSS z kolorami hex (`#333`, `#2b2b2b`, `#1e1e1e`), ignorując dynamiczny motyw z `tokens_for(theme)`. W motywie jasnym (Catppuccin Latte) powoduje to powstawanie ciemnych ramek i tła wewnątrz jasnych powierzchni.
5. **Sierocujące Pliki Dziedziczone (Legacy Residue)**: Plik `gui.py` (42 KB) pozostaje w korzeniu repozytorium jako nieużywany artefakt ze starej wersji interfejsu.

---

## 2. Zgodność z Umową Migracji UI (AGENTS.md Contract Compliance)

| Wymóg z AGENTS.md | Stan Realizacji | Ocena | Uwagi i Deficyty |
| :--- | :--- | :---: | :--- |
| **Brak hybrydowej powłoki (Fluent-first shell)** | Zgodny w powłoce głównej | 🟢 OK | Układ bazuje na `FluentWindow`, zintegrowanym `NavigationInterface` oraz hostingu stron w `FluentPageHost`. |
| **Usunięcie legacy wrappers/QMainWindow** | Częściowo | 🟡 OSTRZEŻENIE | W korzeniu projektu wciąż znajduje się plik `gui.py` (42 KB). Należy go usunąć lub przenieść do archiwum. |
| **Stosowanie kontrolek PySide6-Fluent-Widgets** | Dobry, z wyjątkami | 🟡 OSTRZEŻENIE | Główne przyciski i karty używają `PrimaryPushButton`, `CardWidget`, `Pivot`, `SegmentedWidget`. Jednak wewnątrz głębokich dialogów i edytora receptur nadal występują surowe kontrolki Qt z własnym QSS. |
| **Spójność tokenów designu (Zero ad-hoc QSS)** | Deficyt w recepturach i limitach | 🔴 DO POPRAWY | Sztywno zakodowane wartości kolorystyczne QSS w `app/ui/recipes/page.py` oraz `app/ui/widgets/limit_field.py`. |
| **Przycisk i wskaźniki bezpieczeństwa** | Wymaga korekty estetycznej | 🔴 DO POPRAWY | E-STOP ustawiony na `visualPriority="low"` w `app/ui/shell/main_window.py` zamiast jaskrawego accent/danger. |

---

## 3. Audyt Wydajności UI (Performance & Responsiveness)

### 3.1. Zsynchronizowana Budowa Drzewa Widżetów na Wątku UI
- **Problem**: `MainWindow.__init__` uruchamia tworzenie obiektów dla wszystkich stron aparatury pomiarowej (`Rigol`, `Keithley`, `Anritsu`, `MOKE Box`, `Lake Shore`), edytora receptur, monitora wykonania oraz panelu wyników.
- **Wpływ**: Odczuwalne opóźnienie (stutter / splash delay) przy starcie aplikacji na słabszych stacjach roboczych.
- **Rekomendacja**: Wdrożenie **leniwego ładowania stron (Lazy Page Loading)** w `FluentPageHost`. Strony urządzeń oraz edytor receptur powinny być inicjalizowane dopiero przy pierwszym przejściu użytkownika do danego widoku.

### 3.2. Renderowanie Widm i Wykresów (`SpectrumPlotWidget`)
- **Stan**: `SpectrumPlotWidget` używa `pyqtgraph.PlotWidget` z włączonym automatycznym downsamplingiem (`setDownsampling(auto=True, method="peak")`) oraz `setClipToView(True)`.
- **Wąskie Gardło**: Podczas ładowania dużych zbiorów HDF5 w `app/ui/results/spectrum_tab.py` i `app/ui/results/heatmap_tab.py`, konwersje macierzy NumPy i filtrowanie punktów przebiegają synchronicznie w pętli zdarzeń Qt, co powoduje zamrożenie UI przy przełączaniu plików z ponad 100 000 punktów.
- **Zgłoszenie Ruchu Kurasora**: Proxy sygnałów myszy (`pg.SignalProxy` z `rateLimit=45`) wykonuje formatowanie prefiksów inżynieryjnych SI (`_format_x_value`) przy każdym ruchu celownika. Formatowanie tekstu powinno być wywoływane tylko wtedy, gdy wartość współrzędnej zmieni się o znaczącą deltę.

### 3.3. Koszt Przełączania Motywów (Theme Repolishing)
- **Problem**: Funkcja `_settle_fluent_background_animations` w `app/ui/design_system/fluent_theme.py` iteruje po wszystkich widżetach aplikacji (`application.allWidgets()`), aby zatrzymać i zresetować animacje tła.
- **Wpływ**: Przy rozbudowanym drzewie widżetów (ponad 3000 obiektów Qt) zmiana motywu Dark/Light powoduje zawieszenie interfejsu na kilkaset milisekund.

---

## 4. Audyt Jakości Kodowania i Architektury UI

### 4.1. Rozmiar i Złożoność Plików (Monolityczny Edytor Receptur)
- **Baza kodu**:
  - `app/ui/recipes/page.py`: **223,490 bajtów (~5 500 linii)**.
  - `app/ui/shell/main_window.py`: **93,903 bajtów (~2 140 linii)**.
  - `app/ui/settings_page.py`: **82,963 bajtów (~1 950 linii)**.
- **Wpływ**: Naruszenie zasady pojedynczej odpowiedzialności (Single Responsibility Principle). Prace nad drobną zmianą w generatorze kroków wymagają nawigowania po 5-tysięcznym pliku, co podnosi ryzyko regresji.
- **Rekomendacja**: Podział `app/ui/recipes/page.py` na dedykowany pakiet:
  - `app/ui/recipes/tree_view.py` (widok i kontrola drzewa kroków)
  - `app/ui/recipes/step_editors.py` (dialogi i formularze edycji parametrów)
  - `app/ui/recipes/preview_panel.py` (podgląd oszacowania czasu i planu)

### 4.2. Błąd Krytyczny w Eksporcie Wykresu (`SpectrumPlotWidget`)
- **Kod**: `app/ui/widgets/spectrum_plot.py:L343`
  ```python
  with path.open("x", newline="", encoding="utf-8") as handle:
  ```
- **Diagnoza**: Użycie trybu wyłącznego otwarcia pliku `"x"` wyzwala wyjątek `FileExistsError`, gdy plik docelowy już istnieje. Standardowe okno zapisywania pliku `QFileDialog.getSaveFileName` pyta użytkownika „Plik istnieje, czy chcesz go nadpisać?”. Gdy użytkownik wybierze „Tak”, funkcja `open("x")` rzuca nieobsłużonym błędem i przerywa zapis.
- **Rozwiązanie**: Zmiana trybu na `"w"`.

---

## 5. Audyt Estetyki, UX i Dostępności (Visual Quality & Micro-details)

### 5.1. Dostępność i Widoczność Przycisku E-STOP
- **Plik**: `app/ui/shell/main_window.py:L198`
  ```python
  self.safety_strip.estop.setProperty("visualPriority", "low")
  self.safety_strip.estop.setMaximumWidth(96)
  ```
- **Ocena UX**: W stacji pomiarowej akcja **E-STOP** musi natychmiast przyciągać wzrok operatora i stanowić najwyższy priorytet wizualny na pasku stanu. Ustawienie priorytetu na `low` oraz ograniczenie szerokości do 96px sprawia, że przycisk staje się niepozorny.
- **Zalecenie**: Przypisanie priorytetu `visualPriority="high"` lub wyrazistego czerwonego stylu ostrzegawczego z tokenów `danger`.

### 5.2. Obsługa Stanów Pustych i Ładowania (Empty & Loading States)
- **Stan Przeglądarki Wyników**: Gdy katalog wyników jest pusty, w oknie podglądu pojawia się zwykłe pole tekstowe z napisem `"No HDF5 files in the results directory."`.
- **Rekomendacja**: Zastąpienie surowego pola tekstowego elegancką kartą informacyjną Fluent (`SimpleCardWidget` / `StateToolTip`) z odpowiednią ikoną i przyciskiem do zmiany katalogu docelowego.

### 5.3. Zachowanie Interfejsu przy Wąskich Oknach (Narrow-Window Responsiveness)
- **Szerokość paska nawigacji**:
  ```python
  self.navigationInterface.setMinimumExpandWidth(820)
  ```
- **Problem**: Przy szerokości okna aplikacji równej 820px pasek nawigacji nadal pozostaje rozłożony (`248px`), co pozostawia jedynie `572px` na zawartość strony docelowej. Powoduje to zawijanie etykiet i ściskanie kart akcji w widoku Wyników i Receptur.

---

## 6. Plan Działań Naprawczych (Actionable Roadmap)

### Faza 1: Poprawki Krytyczne i Bezpieczeństwo (Pilne)
1. **Fix Eksportu CSV**: Zmiana `open(..., "x")` na `open(..., "w")` w `SpectrumPlotWidget._export_csv`.
2. **Promocja Przycisku E-STOP**: Usunięcie `visualPriority="low"` z paska bezpieczeństwa, nadanie wyrazistego akcentu awaryjnego (`danger`).
3. **Czyszczenie Dziedziczonego Kodu**: Usunięcie nieużywanego pliku `gui.py`.

### Faza 2: Stylistyka i Spójność Tokenów (Jakość Frontend)
1. **Eliminacja Sztywnych Kolorów QSS**: Wymiana wpisów `#333`, `#2b2b2b`, `#1e1e1e` w edytorze receptur na zmienne pobierane z `tokens_for(theme)`.
2. **Poprawa Widoków Stanu Pustego**: Zastosowanie komponentów Fluent w widokach `ResultsPage` i `Discovery`.

### Faza 3: Architektura i Wydajność (Refaktoryzacja)
1. **Podział Monolitu `recipes/page.py`**: Rozbicie pliku 223 KB na moduły wewnątrz `app/ui/recipes/`.
2. **Leniwa Inicjalizacja Stron (Lazy Loading)**: Ładowanie ciężkich stron urządzeń i wynikowych przy pierwszym przełączeniu karty.
