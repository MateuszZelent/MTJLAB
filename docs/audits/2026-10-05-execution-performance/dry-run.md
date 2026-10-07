# Dry run z fizycznymi urządzeniami — przegląd 2026-10-05

## Kontrakt

Tryb wykonania `dry_run` jest niezależny od ustawienia `simulation`. Dla fizycznej stacji (`simulation=False`) RunWorker tworzy zwykłe adaptery VISA albo korzysta z istniejących kontrolerów. Konfiguracje i nastawy Keithleya/Rigola/Anritsu są wykonywane, a referencja i widma pochodzą z normalnej akwizycji Anritsu. Zapis HDF5 zachowuje `enabled=False` w metadanych symulacji oraz `execution_mode=dry_run` i `outputs_forced_off=True`.

Przed akcjami runner wyłącza i sprawdza wyjścia urządzeń objętych planem. Każde żądanie OUTPUT ON dla Keithleya, Rigola i generatora RF Anritsu jest wykonywane jako OFF. Funkcja analizatora widma działa przy wyłączonym wyjściu generatora RF. Niezwiązane z planem urządzenia nie są automatycznie obejmowane tym sprawdzeniem.

**MOKE jest wyjątkiem:** SET_VOUT bezpośrednio zmienia fizyczne napięcie. Dry run przygotowuje plan i odczytuje VOUT/Hall, ale nie uzbraja go i nie zapisuje nastaw DAC. Nie potwierdza wyłączenia zewnętrznego Kepco i nie zeruje zastanego napięcia MOKE. Nie wolno interpretować dry run jako potwierdzenia wyłączenia całego stanowiska.

## Poprawki

1. Początkowa kontrola OFF odrzuca teraz stan FAULT, DISCONNECTED, OUTPUT_ON i inne stany bez potwierdzenia, a także jawny wynik `False` z `emergency_off()`. Wcześniej sprawdzano wyłącznie UNKNOWN.
2. Po każdej akcji OUTPUT w dry run wymagany jest dokładny logiczny odczyt `False`, również dla jawnego OUTPUT OFF w drzewie. Niepotwierdzony wynik nie może przejść jako sukces.
3. Pominięta nastawa MOKE ma `applied_si=None`; odczyt rzeczywisty i wartość żądana są zachowane oddzielnie. Wcześniej metadane mogły oznaczać niezastosowaną nastawę jako zastosowaną.
4. Opisy trybu w UI wyjaśniają wyjątek MOKE i niezależność od SIMULATION.

## Weryfikacja

- 17 testów dry run: blokowanie OUTPUT ON, niewiarygodne potwierdzenie OFF, odmowa przy błędzie początkowego wyłączenia, konfiguracje/setpointy, akwizycja i zapis, brak zapisów DAC MOKE — zaliczone.
- 3 testy połączenia UI/trybu/kontrolera — zaliczone.
- Nowy test uruchamia RunWorker z `simulation=False`, z rzeczywistymi klasami adapterów i kontrolowanymi zamiennikami transportu. Użycie `SimulatedVisaFactory` jest zabronione w tym teście. Początkowo załączone wyjścia zamienników Keithleya i Rigola zostają wyłączone. Sprawdzono wysyłanie nastaw i brak komend ON.
- Wartość widma dostarczana przez testowy transport (`-57.25 dBm`) trafia bez podmiany do referencji i wszystkich czterech widm w HDF5. Metadane zachowują tryb niesymulowany i dry run.
- Ruff dla zmienionych plików i kontrola whitespace — zaliczone.

Nie wykonywano tego przebiegu na fizycznej aparaturze. Test transportu potwierdza wybór ścieżki oraz komendy i zapis, nie zachowanie sprzętu. Do rzeczywistej akwizycji należy uruchomić aplikację poza SIMULATION i wybrać `Dry run — outputs forced OFF`; samo wybranie dry run w symulacji nadal daje symulowane dane. Obowiązują normalne ograniczenia stacji, w tym jawny zakres źródła Keithleya i wyłącznie pomiar 2-przewodowy.
