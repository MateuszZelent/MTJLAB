# Przegląd konfiguracji sweepa i zakaz 4-wire — 2026-10-05

## Zmiany

- Edytor Keithleya pokazuje parametr, bieżącą wartość z karty urządzenia, wartość sweepa i efekt. Zielony oznacza zgodność, pomarańczowy zmianę / ROI, szary brak zapisu albo nieznaną wartość bieżącą. Wielkości fizyczne są porównywane w SI.
- To porównanie z formularzem karty urządzenia, nie nowy odczyt sprzętu ani prognoza stanu po wcześniejszych węzłach. Opis w oknie wskazuje to ograniczenie. Brak wartości bieżących nie jest oznaczany na zielono.
- W głównym przewijanym formularzu dostępne są także zakresy, autorange i sense. Nie wymagają dodatkowego modalu. Polityka OUTPUT ma osobny wiersz.
- Starszy configure_keithley zachowuje pominięte pola: samo Apply nie dopisuje NPLC, sense ani zakresów pomiarowych z domyślnych wartości. Edycja pominiętej wartości jawnie dodaje ją do konfiguracji. Zakres źródła pokazuje również powiązany zapis autorange.
- Węzeł wybranych parametrów pokazuje Unchanged jako brak programowania, a tryb źródła jako wymaganie zgodności. Usunięto błędny opis sugerujący pełną konfigurację niezależnie od wyboru.
- Poprawiono szerokości i przewijanie. Zwykły węzeł konfiguracji nie wyświetla zbędnej drugiej karty. Zabezpieczenie ponownego wejścia do exec zapobiega otwarciu dwóch edytorów tego samego typu dla tego samego rodzica.

## Twardy zakaz 4-wire

Dozwolony jest wyłącznie 2wire (local sense). Ustawienia kanału oraz defaults są walidowane niezależnie, także gdy są sprzeczne. Kompilator i walidacja źródła odrzucają zdalny sense. Adapter odrzuca żądanie przed komunikacją, również dla wybranych pól i measure_only. Generator komend nie generuje SENSE_REMOTE.

UI nie oferuje 4-wire ani potwierdzenia pozwalającego ominąć zakaz. Niedozwolona wartość starego węzła pozostaje widoczna jako błąd; Apply jest zablokowane do jawnego wyboru 2wire. Odczyt stanu urządzenia i historycznych metadanych nadal może reprezentować 4-wire — nie daje to zgody na pomiar.

## Weryfikacja

- Zakaz, ustawienia i kompilator: 100 passed + 29 subtests.
- Adapter, runner, zachowanie niewybranych parametrów i recovery: 134 passed + 5 subtests.
- Nowy przegląd UI, zakaz sense, dotychczasowa polityka zakresów i zakresy sprzężone: 85 passed.
- Końcowy przebieg nowych testów przeglądu i zakazu sense: 25 passed.
- Charakterystyka Keithleya oraz okno odczytu obu kanałów: 35 passed. Testy powłoki korzystają z izolowanych katalogów ustawień i katalogu próbek; dane stacji nie są celem zapisu testów.
- Renderowanie obu rodzajów węzłów: 1120×780 (jasny i ciemny motyw) oraz 760×640. Testy sprawdzają dostępność zaawansowanych pól przez przewijanie, geometrię i ponowne otwieranie modalu.
- Aktualny profil i przygotowany sweep kompilują się do 1812 akcji; wszystkie konfiguracje Keithleya mają 2wire. Profil nie został zmodyfikowany.
- Ruff zmienionych plików: OK. Globalny ruff zgłasza 10 wcześniejszych F401 w test_spectrum_correction_layout.py i test_sweep_release_contracts.py.

Testy używają symulatorów i Qt offscreen. Nie uruchamiano fizycznej aparatury. Zrzuty configuration-review-*.png znajdują się w tym katalogu.
