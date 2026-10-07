# Pełny przegląd dialogów i liczników zmian — 2026-10-05

## Zakres i wynik

Sprawdzono 56 konkretnych klas dialogów wykrytych automatycznie w kodzie oraz
8 okien tworzonych wewnątrz metod. Każde otwarto w natywnym Qt/Windows,
w jasnym i ciemnym motywie, przy rozmiarach docelowych 1120×800 i 800×700
(z zachowaniem minimum wymaganego przez dane okno). Łącznie 256 stanów.
Przechodzono strony QStackedWidget, rozwijano porównania, przewijano formularze
i wykonywano zrzuty Qt oraz własnego HWND. Kontrolowano brak natywnych
viewportów powodujących wcześniejszy „ekran w ekranie”, widoczną geometrię
i wyjątki obsługi zdarzeń. Przejrzano zrzuty zbiorcze obu motywów.

[Pełna lista okien i galeria](modal-catalogue/index.md).
[Manifest przebiegu](modal-catalogue/catalogue.json).

## Naprawy

1. **eLab Upload:** odkryto błąd otwarcia `unexpected keyword argument resizable`.
   FluentRecipeDialog przekazuje teraz jawne ustawienia okna i marginesów do
   StationDialog, zachowując domyślne skalowanie edytorów. Niczego nie wysyłano.
2. **Katalog próbek:** sześć starszych dialogów (dodawanie/zmiana nagłówków,
   ustawienia katalogu, programowanie próbki, zmiana siatki, podgląd obrazu)
   oraz pomocnicze przenumerowanie wierszy używają wspólnej powierzchni Fluent.
   Zachowano ich przyciski i istniejące operacje.
3. **Anritsu Spectrum:** przewijana lewa część chroni formularz przy wąskim
   oknie i rozwiniętym porównaniu. **Anritsu SG:** przewijana zawartość oraz
   większy rozmiar początkowy, z osobno dostępnymi przyciskami zatwierdzenia.
4. **Edytor akcji:** długie formularze są przewijane.
5. **Starszy kreator Keithley:** przegląd obrazu ujawnił nakładanie pól źródła
   po rozwinięciu porównania. Karta ustawień ma teraz przewijanie i minimalny
   rozmiar zawartości, a tabela ROI i wykres mogą zajmować mniej miejsca.
   Sprawdzono nieprzecinanie pól, karty/tabeli oraz wykresu/podpisu testem
   geometrycznym i powtórzono jego cztery natywne zrzuty po poprawce.

## Liczniki i znaczenie porównania

| Edytor | Punkt odniesienia i zasada liczenia |
|---|---|
| Keithley A/B | Zachowano wcześniejsze porównanie wszystkich programowanych pól. |
| Rigol | Osobne ustawienia kart kanałów, zamrożone przy otwarciu. Pełny carrier liczy również wartości przy selektorze „Unchanged”, zgodnie z semantyką tego edytora. Amplituda/offset są porównywane przez fizyczne high/low; nieaktywna częstotliwość/faza DC i NOIS nie są liczone. |
| Anritsu Spectrum | Podstawowe i zaawansowane ustawienia karty urządzenia. „Unchanged” oznacza zachowanie parametru; Set i Sweep są rozróżnione. |
| Anritsu SG | Aktualne frequency/power z karty. Oba pola pełnej konfiguracji są uwzględnione. |
| Fixed value / MOKE | Ustawienie napięcia z właściwego kanału karty MOKE; brak dostępnej wartości jest jawnie nieznany. |
| ROI | Osobna informacja o parametrze zmienianym przez ROI, bez udawania jednej stałej wartości. |
| Kreator Keithley | Oś plus tryb źródła, compliance, NPLC, stabilizacja i sense. |
| Edytor akcji | Porównanie do zapisanej akcji, wyraźnie opisane jako niebędące odczytem sprzętu. |

Licznik jest zawsze nad formularzem. Przycisk „Show parameter comparison”
rozwija tabelę bieżącej i planowanej wartości z efektem operacji. Zielony oznacza
zgodność, pomarańczowy zmianę lub ROI, szary brak programowania albo nieznaną
wartość. Porównanie uwzględnia jednostki; `1000 ms` i `1 s` nie tworzą zmiany.
Nie zastępujemy brakującego stanu domyślnymi wartościami. Dodatkowy dostawca
porównania Rigola nie przedstawia awaryjnej kopii kanału 1 jako znanego kanału 2.
Polityka OUTPUT pozostaje widoczna osobno; licznik parametrów nie jest licznikiem
komend VISA ani potwierdzeniem aktualnego fizycznego stanu przyrządu.

## Weryfikacja

- Natywny pełny katalog oraz poprzedni test wariantów urządzeń: **2 passed**.
- Inventory UI, wspólne dialogi, modal elevation, liczniki, recipe builder:
  **131 passed, 4 subtests passed**.
- Fluent settings (po izolacji katalogu testowego): **11 passed**.
- Porównania Keithley i zakaz 4-wire wraz z nowymi licznikami: **33 passed**.
- Po rozszerzeniu kreatora: liczniki i recipe builder:
  **94 passed, 4 subtests passed**; po ostatecznej korekcie wysokości:
  **6 passed** oraz ponowny natywny przegląd kreatora w czterech wariantach.
- Testy liczników i integracja głównego okna z kartami Rigola: **6 passed**.
- Ruff dla zmienianych plików: OK. Pełny Ruff nadal zgłasza 10 wcześniejszych
  F401 w `test_spectrum_correction_layout.py` i `test_sweep_release_contracts.py`.

Test katalogu wykrywa klasy z AST; dodanie nowego dialogu wymagającego fixture
powoduje błąd testu zamiast cichego pominięcia. Okna tworzone wewnątrz metod
są wyszczególnione osobno. Cykl zamykania testu respektuje właścicieli okien
i kończy działanie workerów CPU przed usunięciem widgetów.

## Granice sprawdzenia

To przegląd UI i regresji formularzy, nie ponowna kwalifikacja fizycznych
urządzeń ani wszystkich algorytmów analizy danych. Testy natywne używają
danych offline i obejmują stany początkowe/bez danych; nie uruchamiają
pomiarów, transmisji eLab ani zapisu wyników pomiarowych. Dwa hosty pływających
paneli sprawdzano z wstrzykniętą pustą treścią; ich host, motyw i geometria
są objęte testem. Systemowe okna wyboru pliku pozostają dialogami platformy.
Nie sprawdzano każdej kombinacji monitorów/DPI.

Profil `.config/settings.yml` zachował SHA256
`37ef6cb097aa1c3115f0036e18d3c06bcb847abb340540d34e7350d793203491`.
Zakaz pomiaru 4-przewodowego pozostaje aktywny.
