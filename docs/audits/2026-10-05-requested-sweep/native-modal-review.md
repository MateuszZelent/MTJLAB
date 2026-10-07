# Dialogi — poprawka renderowania Windows, 2026-10-05

## Przyczyna i poprawka

Odtworzono zgłoszony „ekran w ekranie” w prawdziwym oknie Windows. Wcześniejsza
blokada ponownego otwierania edytora nie rozwiązywała tej przyczyny. Testy Qt
`offscreen` nie pokazywały uszkodzenia.

`WindowsFramelessDialog` tworzy HWND przez `winId()` już w konstruktorze. Qt
promował również kontrolki potomne do natywnych okien. Przewijanie i kopiowanie
backing store powodowało przesuniętą kopię całego dialogu w jego wnętrzu.
Przed poprawką próbnik widział około 60 potomnych HWND; po poprawce nie widział
ich, a viewporty pozostały nienatywnymi kontrolkami Qt.

`AA_DontCreateNativeWidgetSiblings` jest ustawiane przed utworzeniem QApplication
oraz przed konstruktorem bazowym StationDialog dla niezależnych hostów dialogów.
Zachowano rodziców okien, obsługę zamykania i istniejącą obsługę nativeEvent.
Naprawa jest wspólna dla modalnych okien stacji. Wymagany restart aplikacji.

## Interfejs

- Keithley A/B: stale widoczne podsumowanie liczby zmian nad porównaniem.
  Aktualizuje się przy edycji; równoważne jednostki nie tworzą fałszywej zmiany.
  Osobno liczy wartości zgodne, zachowane, nieznane, osie ROI i konflikty.
  Wymaganie trybu źródła nie jest liczone jako zapis. Polityka OUTPUT jest
  pokazana w tabeli oddzielnie od liczby zmienianych parametrów.
- Punktem odniesienia są ustawienia karty urządzenia przy otwarciu, nie nowy
  odczyt sprzętu. Nieznanych wartości nie przedstawiamy jako zgodnych.
- Edytory receptur obsługują zmianę rozmiaru i maksymalizację.
- Rigol: obie części formularza przewijane, bez zwijania panelu do zera;
  przy szerokości poniżej 980 px układ pionowy zapewnia miejsce kontrolkom.
- Zakaz pomiaru czteroprzewodowego pozostaje aktywny.

## Weryfikacja

`tests/test_native_modal_rendering.py` uruchamia osobny proces z pluginem Qt
`windows`, nawet gdy główny pytest działa offscreen. Sprawdza 12 wariantów:
Keithley A/B w pełnej konfiguracji i edycji wybranych parametrów, Rigol,
Anritsu Spectrum, Anritsu SG, akwizycja, referencja, OUTPUT, Repeat i komentarz.
Każdy przechodzi rozmiary 1120×780 oraz 760×640, motyw jasny i ciemny:
48 stanów renderowania, przewijanie, QWidget.grab i zrzut własnego HWND.
Kontrolowane są nienatywne viewporty, geometria powierzchni i widoczność licznika.
Zrzuty są w [native-modals](native-modals/).

Dodatkowe testy funkcjonalne i geometryczne obejmują wspólne dialogi, alerty,
gotowość urządzeń, limity, zapis ręczny, narzędzia widma i edytory receptur.
Nie jest to ręczny przegląd każdego możliwego okna i każdej konfiguracji DPI.
Nie wykonano żadnej operacji na fizycznych urządzeniach.

Wyniki końcowe:

- `test_fluent_dialogs.py`, `test_modal_elevation.py`, `test_recipe_builder.py`:
  110 passed, 4 subtests passed.
- `test_sweep_configuration_review.py`, `test_keithley_local_sense_only.py`
  oraz test renderowania edytorów z `test_recipe_builder.py`: 29 passed.
- `test_native_modal_rendering.py`: 1 passed, wszystkie 48 stanów.

Pełny Ruff nadal zgłasza 10 istniejących wcześniej F401 w
`test_spectrum_correction_layout.py` i `test_sweep_release_contracts.py`.
Ruff dla plików tej poprawki przechodzi.
