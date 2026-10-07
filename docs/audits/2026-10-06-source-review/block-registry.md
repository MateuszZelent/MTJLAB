# Wspólny rejestr bloczków UI i YAML

Wdrożono `app/recipes/block_registry.py` jako wspólne źródło identyfikatorów typów oraz dozwolonych pól poleceń. `id` oznacza konkretne wystąpienie bloczka, a `block_type` jego stabilny typ, np. `device.moke_box` lub `recipe.wait`. Przesunięcie bloczka nie zmienia jego typu. Pełna tabela znajduje się w `block-type-catalogue.md`.

Biblioteka UI korzysta z tych identyfikatorów; są dostępne w tooltipach i inspektorze. Edycja oraz zapis poprawnego YAML dodają jawne `block_type`. Parser odrzuca nieznany identyfikator, niezgodność typu urządzenia i nieprawidłowe pola. Kompilator sprawdza również węzły skonstruowane bez parsera. Rejestr schematów jest niemutowalny.

Starsze receptury bez identyfikatorów pozostają czytelne: identyfikator jest jednoznacznie wyprowadzany z istniejącego typu i modułu urządzenia. Zapis tworzy wersję z jawnymi identyfikatorami, zachowując komentarze, wartości, kolejność i gałęzie. Historyczne pliki nie są przepisywane; zmiana tekstu tworzy nowy hash wersji. Niepoprawny zapis nie zastępuje poprawnej receptury. Autosave nadal zachowuje niedokończone szkice.

Zaktualizowano `anritsu_background_reference_smoke_test.yml`. Dodanie identyfikatorów nie zmienia napięć, jednostek, kolejności pomiarów ani poleceń wykonawczych. Automatyczne przygotowanie urządzeń pozostaje odpowiedzialnością kompilatora; identyfikator nie zastępuje walidacji limitów stanowiska.

## Weryfikacja

- Rejestr, migracja, zapis i edycja, w tym stare ROI kanałów Keithley A/B: 43 testy przeszły razem po końcowych poprawkach.
- Kompilator, punkt odniesienia MOKE, limity i zgodność akcji: 83 testy oraz 6 subtestów przeszło podczas implementacji.
- Test symulacyjny pełnej receptury: zapis 10 widm RAW, tła i referencji do HDF5/PyThat zakończony poprawnie.
- Pełny zestaw edytora: 87 testów przeszło; jedyną porażką było stare oczekiwanie 27 zamiast 28 pozycji biblioteki. Po aktualizacji ten test przeszedł osobno.
- Ruff: bez błędów. Widok po `show()` i przetworzeniu zdarzeń obejrzano w `block-registry-ui.png`.

Podczas weryfikacji wykryto błąd `CommentedMap.insert()` po przebudowie węzła ROI: biblioteka YAML zachowywała stare informacje o kolejności kluczy. Zastąpiono wstawianie pozycyjne bezpiecznym przypisaniem pola; regresja obejmuje zaakceptowaną edycję obu kanałów Keithley. Test nieoczekiwanego komunikatu błędu teraz kończy się porażką zamiast zatrzymania w modalu. Nowy test UI zapisuje autosave w katalogu tymczasowym i usuwa okno po zakończeniu. Nie wykonano komunikacji z fizycznymi urządzeniami; test symulacyjny nie potwierdza kwalifikacji sprzętowej.
