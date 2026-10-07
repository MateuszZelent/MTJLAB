# Zacięcia podczas Acquire reference / Acquire spectrum

## Znalezione przyczyny

1. `InstrumentWorker.invoke_for_run()` emitował `state_changed` po każdym wywołaniu, również po odczycie właściwości. W ścieżce korzystającej z kontrolerów urządzeń uruchamiało to aktualizację kart, ponowne nakładanie stylów, przebudowę listy urządzeń i ocenę gotowości. Poprzedni duży test używał adapterów utworzonych bezpośrednio przez runner, więc nie pokrywał tego problemu.
2. `MainWindow._refresh_safety_strip()` i `StationDashboardController._refresh_readiness()` wywoływały kontrolę katalogu wyników, tworząc i usuwając plik tymczasowy w wątku GUI. Czas tej operacji zależy od dysku, systemu plików i dostępności udziału sieciowego. Powtarzano ją również przy aktualizacjach wykonania.
3. W symulacji zwykłe komunikaty logu były dopisywane do widgetu pojedynczo, z pominięciem istniejącego buforowania prezentacji.

## Naprawa

- Worker publikuje stan po wywołaniu runnera tylko wtedy, gdy się zmienił. Błąd wymusza publikację. Zakończenia ręcznych operacji zachowują wymuszone powiadomienia, potrzebne do odblokowania przycisków.
- Kontrola zapisywalności używana do prezentacji działa w osobnym wątku Python. Qt pobiera wynik bez czekania; sprawdzenie odświeża się co 30 sekund i przy aktualizacji ustawień. Jednocześnie wykonuje się najwyżej jedno sprawdzenie. Wynik dotyczący starej ścieżki jest odrzucany.
- Bieżąca prezentacja gotowości i bezpieczeństwa nie wykonuje operacji dyskowych. Początkowy/brakujący wynik kontroli katalogu nie jest uznawany za sukces.
- Preflight nadal wykonuje świeżą kontrolę katalogu, niezależną od wyniku przeznaczonego do prezentacji. Zapis HDF5, obsługa jego błędów i bezpieczne zakończenie runnera pozostają bez zmian.
- Zwykłe komunikaty logu w aktywnej symulacji są prezentowane partiami; błędy i komunikaty krytyczne nadal omijają ten bufor. Trwały audyt nie jest pomijany.

Nie uruchamiamy równolegle komend na jednej sesji VISA ani nie przenosimy widgetów Qt do workera. Test potwierdza, że same wywołania akwizycji przez kontrolery już wykonują się poza GUI. Dodawanie kolejnych wątków akwizycji nie usuwałoby opisanych operacji dyskowych i aktualizacji na głównym wątku.

## Testy

- 16 testów: rezerwacja urządzeń, propagacja błędów, przerwanie rezerwacji przez OFF, kontroler wykonania, izolacja wolnego sprawdzenia katalogu i świeża kontrola preflight — zaliczone.
- 20 testów: gotowość stacji, cykl życia dashboardu, geometria paska bezpieczeństwa — zaliczone.
- Nowy test pełnego okna z kontrolerami Anritsu, Rigola i Keithleya: referencja oraz 4 widma, każde uśredniane z 32 surowych przebiegów po 10 001 próbek. Dodane opóźnienie transportu 20 ms. Wszystkie 160 wywołań akwizycji wykonano poza głównym wątkiem. Potwierdzono HDF5: 160 surowych przebiegów, 4 widma wynikowe i referencję.
- Qt offscreen: test powyżej zaliczony, maksymalna przerwa timera GUI 124,4 ms podczas referencji i 181,6 ms podczas widma (limit 350 ms).
- Natywny backend Qt Windows (`QT_QPA_PLATFORM=windows`): ten sam test zaliczony; referencja 88,2 ms, widmo 183,4 ms. Wyniki: `averaged-acquisition-windows.json`.
- Ruff zmienianych plików: zaliczony. Pełne `ruff check app tests`: pozostaje 10 wcześniejszych nieużywanych importów w `test_spectrum_correction_layout.py` i `test_sweep_release_contracts.py`.

## Granice wnioskowania

Potwierdzono konkretne zbędne operacje oraz reakcję GUI w symulacji. Nie jest to kwalifikacja sterownika VISA ani czasu odpowiedzi fizycznego Anritsu. Poprzedni wynik 375 ms przy inicjalizacji dużego planu dotyczył innego etapu i pozostaje historycznym wynikiem; ten test mierzy osobno fazy akwizycji. Zmiany wymagają ponownego uruchomienia aplikacji.
