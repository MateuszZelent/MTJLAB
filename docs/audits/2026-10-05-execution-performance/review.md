# Execution — ograniczenie obciążenia GUI, 2026-10-05

## Wynik

Usunięto narastanie kolejki zwykłej telemetrii przy zajętym GUI, ograniczono koszt rysowania drzewa, ukrytych formularzy i wykresu planu. Nie deklarujemy jeszcze pełnego spełnienia kryterium płynności: pełny test 1000 punktów wykazał maksymalną przerwę 374,8 ms przy starcie, wobec progu 350 ms. Próg pozostał niezmieniony. Pomiar obejmuje plan, jego pierwszy render i wykonanie.

## Zmiany

- Produkcyjny RunWorker buforuje najnowszą telemetrię; timer GUI pobiera ją co 100 ms. Zajęty wątek GUI nie powoduje generowania kolejnych zaległych klatek. Zduplikowane techniczne granice akcji z identyfikatorem semantycznym pomijamy w prezentacji, zachowując zdarzenia semantyczne i trwały zapis runnera.
- Zdarzenia błędu, watchdog i wyłączania omijają zwykłe buforowanie. Oczekujące normalne stany prezentacji są wtedy usuwane, aby nie odrysować starego RUNNING po błędzie.
- Model drzewa pamięta wynik formatowania komórki/roli. Zmiana stanu, drzewa, trybu edycji, polityki wyjść lub motywu unieważnia cache.
- Niewidoczne strony urządzeń nie przebudowują formularzy podczas wykonania. Potwierdzony stan i pasek bezpieczeństwa nadal są aktualizowane; po przejściu na stronę urządzenia prezentowany jest najnowszy odczyt.
- Wykres planu współdzieli symbole znaczników według kategorii, nie przelicza automatycznie zakresów po każdej dodanej krzywej i nie zmienia wysokości szczegółów przy każdym kroku. Powtórzony numer kroku nie uruchamia ponownej aktualizacji. Ponowne włączenie Follow przywraca podgląd bieżącej akcji.

Sterowanie przyrządami i zapis wyników pozostają w workerze. Zmiany dotyczą prezentacji; nie dodają komend aparatury ani nie zmieniają setpointów, ramp, compliance czy konfiguracji przewodów.

## Weryfikacja

- 32 testy regresyjne: model drzewa, buforowanie telemetrii, kontroler wykonania i wykres planu — zaliczone (15,20 s).
- Test przeciążenia: 10 000 zmian podczas zajęcia GUI nie emituje zwykłych klatek Qt; po pobraniu dostarcza najnowsze stany i zachowuje liczbę zmian. Watchdog dociera natychmiast.
- Krótki przebieg 20 punktów, 10 001 próbek widma, natywny Windows, bez profilera: maksymalna przerwa GUI 282,7 ms; najwolniejsza zmierzona obsługa pojedynczego zdarzenia 12,2 ms. Nie jest to kwalifikacja rzeczywistej aparatury.
- Pełny przebieg w symulacji: 1000 punktów, 5234 tyknięcia próbnika GUI; maksymalna przerwa 374,8 ms (0 zapisanych punktów, worker aktywny), aktualizacja modelu do 38,9 ms, podgląd widma do 25,1 ms. Test płynności **niezaliczony** na progu 350 ms.
- Niezależna kontrola HDF5 po teście: 1000 widm po 10 001 wartości oraz referencja 10 001 wartości — zaliczona. Import całego wyniku pozostaje odroczony; czytnik wyników nie był aktywny w końcowym pomiarze.
- Ruff dla zmienianych plików — zaliczony.

## Ograniczenia i dalsza diagnostyka

Pierwszy pełny pomiar tej iteracji wykazał 645,4 ms przy starcie; po zmianie budowy wykresu kolejny wykazał 374,8 ms. To dwa pomiary na tej samej stacji, nie statystyczna gwarancja przyspieszenia. Profil inicjalizacji wskazuje budowę wykresu i pierwszy render/layout jako pozostały koszt. Należy osobno kwalifikować ten etap po dalszym podziale inicjalizacji na krótkie zadania, bez przenoszenia widgetów Qt do workera.

Testy nie komunikowały się z fizycznymi urządzeniami. Nowe zachowanie wymaga ponownego uruchomienia aplikacji. Profilowanie i testy korzystają z odizolowanych ustawień, katalogów wyników i bazy testowej.
