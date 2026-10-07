# Naprawy sweepów — kwalifikacja oprogramowania, 5 października 2026

**Aktualizacja: problemy SA-R1–SA-R5 i znalezione błędy Rigola zostały naprawione.** [Końcowy raport Anritsu, Rigola i RAM](SWEEPS_PRODUCTION_FOLLOWUP_2026-10-05.md) opisuje kwalifikację SIM, automatyczny budżet pamięci oraz poprawny zapis i import serii 1309 × 10001. Nie wykonano kwalifikacji fizycznej aparatury. Poniżej pozostaje rozliczenie wcześniejszych napraw i ich zakresu testów; [ponowny audyt](AUDYT_SWEEPS_ANRITSU_2026-10-05.md) zachowuje historyczny stan sprzed tych dodatkowych napraw.

**Wynik wcześniejszej kwalifikacji: ustalenia SW-01–SW-20 zostały naprawione. Opisane testy wykonania, zapisu, odzyskiwania, renderowania i responsywności przeszły weryfikację. Zakres kwalifikacji obejmuje oprogramowanie pracujące z symulatorami; nie obejmuje fizycznej aparatury ani DUT.**

To raport realizacji [audytu z 4 października](AUDYT_SWEEPS_2026-10-04.md), a nie zmiana jego historycznych ustaleń. Punktem wyjścia napraw był czysty HEAD `acce560`. Nie zmieniono rzeczywistego `.config/settings.yml`, zatwierdzeń kanałów ani uprawnień OUTPUT. Wszystkie wykonane połączenia prowadziły do symulatorów.

## 1. MOKE0 × Keithley B × Keithley A

Zachowano kolejność: **MOKE VOUT0: 0–100 mV → B: −80–+80 mA → A: +0,2–+1,4 mA → Wait 3 s → nowe spektrum**. Początek A jest dodatni, zgodnie z założeniem audytu. Operator wybiera kroki; program nie narzuca tych przedziałów ani liczby punktów.

Produkcyjny kompilator, runner, writer i natywne adaptery wykonały scenariusz **11 × 17 × 7 = 1309 kombinacji**, z 1001 próbkami częstotliwości na widmo. To przykładowe kroki 10 mV, 10 mA i 0,2 mA. Powstało 1309 kompletnych checkpointów i widm; `_pending` pozostał pusty. Plik przeszedł rzeczywisty odczyt **PyThat 0.2.14**.

| Wynik końcowej próby | Wartość |
|---|---:|
| Kompilacja | 1,009 s |
| Wykonanie i zamknięcie archiwum w symulacji | 138,662 s |
| Archiwum HDF5 | 268 008 395 B |
| Górna estymacja archiwum | 2 132 559 695 B |
| Estymacja roboczych tablic importu | 171 074 880 B |
| Zarejestrowane jawne oczekiwania | 1309 × 3 s |
| Czas samej stabilizacji w eksperymencie | 1 h 5 min 27 s |

[Wyniki i SHA-256 archiwum](docs/audits/2026-10-05-sweep-fixes/scale-1309.json). Górna estymacja pokryła rzeczywisty plik; jest konserwatywnym budżetem, a nie prognozą dokładnego rozmiaru lub zużycia RAM.

W próbie skali oczekiwania zastąpiono rejestratorem; sprawdzono ich kolejność i wartości. Osobny test wykonuje rzeczywiste oczekiwanie 3 s oraz jego przerwanie. 139 s nie jest prognozą czasu eksperymentu. Model DUT **0,1 Ω** jest jawnie syntetyczny: przy 80 mA wymaga 8 mV i nie osiąga testowego compliance 20 mV. Nie jest to dobór compliance dla rzeczywistej próbki.

## 2. Konfiguracja bez ukrytych nastaw

Konfiguracja początkowa jest **jawnym blokiem drzewa**, oddzielonym od wybranych zmian parametrów. Zapamiętany snapshot edytora nie upoważnia do ponownego wysłania całej konfiguracji. **Add baseline** wstawia widoczny blok do przejrzenia przed uruchomieniem. Brak wymaganej konfiguracji początkowej kończy preflight błędem.

Wybrana zmiana prądu wysyła zmianę poziomu właściwego kanału. Zachowuje NPLC, compliance, zakresy, sense i nastawy drugiego kanału. Wyjścia są sterowane według jawnych akcji/polityki drzewa oraz procedury bezpieczeństwa. Analogiczne maski zmian obowiązują dla Rigola oraz Spectrum/SG Anritsu. Niezaznaczone parametry nie są pobierane ze snapshotu jako nowe nastawy domyślne.

Akwizycja Anritsu wymaga trybu Spectrum i śladu Write. Zastany View/Blank jest odrzucany przed rejestracją. Pojedyncza akwizycja chwilowo uruchamia SINGLE; po pomyślnym pobraniu przywraca wcześniejszy tryb ciągły i potwierdza odczyt. Przejście SG → Spectrum wymaga jawnej konfiguracji oraz RF OFF w recepturze. Konfiguracja podstawowa sweepa nie zeruje RBW/VBW/detektora ani trybu zapisu śladu.

Polecenie `TRACe[n]:TYPE?` i znaczenie Write/View sprawdzono w lokalnym [manualu Anritsu](docs/MS2830A_40A_SpectrumAnalyzer_Remote_Manual_e_43_0.pdf), strony PDF 194–195, sekcje 2-184–2-185. To kontrola protokołu; odpowiedzi konkretnego fizycznego egzemplarza wymagają osobnej kwalifikacji.

## 3. Rozliczenie ustaleń audytu

| ID | Naprawa i zakres dowodu |
|---|---|
| SW-01 | Profil MOKE jest wybierany i zatwierdzany dla konkretnego kanału, niezależnie od profilu głównego. |
| SW-02 | Wybrane parametry wszystkich providerów mają maski zmian; konfiguracja początkowa jest jawna. Testy sprawdzają faktyczne polecenia natywnych adapterów. |
| SW-03 | Oś Anritsu używa właściwego wspólnego modelu konfiguracji i zachowuje pozostałe parametry. |
| SW-04 | Usuwane są wyłącznie techniczne aktualizacje dokładnie odpowiadające danej osi; inny kanał/parametr pozostaje w planie. |
| SW-05 | Błędy normalizacji i wymiarów przerywają preflight. |
| SW-06 | Oś fizyczna wymaga konfiguracji i rzeczywistej akcji aktualizacji. |
| SW-07 | Indeks punktu i etapu pochodzi z pozycji wystąpienia, również dla powrotów i powtarzających się wartości. |
| SW-08 | Wykonywany i zapisywany czas oczekiwania jest zgodny; uśrednianie ma jawny `inter_sweep_delay` w YAML i UI. |
| SW-09 | Iloczyn kartezjański, repeat i wektory osi są ograniczane przed alokacją. Nieaktywne poddrzewa nie rozwijają ROI. |
| SW-10 | Liczby punktów, powtórzeń i inne pola całkowite są dokładnymi integerami; boolean i ułamki są odrzucane. |
| SW-11 | Requested, applied i readback MOKE rozróżniają kod DAC; rampa dochodzi do dokładnego kodu docelowego. |
| SW-12 | RAW, checkpointy i osie publiczne mają spójną semantykę fizyczną oraz oddzielny dowód nastawy. |
| SW-13 | Błąd commit/flush cofa licznik i rekord. Rollback RAW usuwa opublikowaną tożsamość; nieudany rollback blokuje dalsze użycie writera. |
| SW-14 | Akwizycja zachowuje potwierdzenia osi; brak odczytu nie dostaje statusu readback. Zmiana konfiguracji unieważnia zależne potwierdzenia. |
| SW-15 | Recovery liczy faktyczne checkpointy, odtwarza pełną potwierdzoną konfigurację i właściwą referencję. Brak potwierdzonej bezpiecznej granicy uniemożliwia wznowienie. |
| SW-16 | Parser odrzuca nieznane pola akcji, konfiguracji i parameter_actions. Znane pola pomocnicze edytora są jawnie dopuszczone i walidowane. |
| SW-17 | Karty pokazują potwierdzone poziomy, compliance, NPLC, settling, sense, zakresy i OUTPUT. Ukryte trasy zachowują odczyty; rampa ma postęp i fazę RAMPING. |
| SW-18 | Rejestr wymiarów i jednostek obejmuje skalarne pomiary/setpointy; jednostki eksportu publicznego są weryfikowane. |
| SW-19 | Estymacja obejmuje RAW, potwierdzenia, zdarzenia, trajektorię MOKE i monitorowanie SMU. Brak miejsca lub pamięci jest wykrywany przed połączeniami. |
| SW-20 | Odłączony adapter nie stanowi potwierdzenia OFF. Niepotwierdzone wyłączenie oznacza błąd/UNKNOWN. Pasek bezpieczeństwa używa potwierdzeń runnera. |

Pierwotne kontrakty audytowe przechodzą bez xfail. Starsze pozytywne fixtures uzupełniono o **widoczne konfiguracje** i jawne zakresy. Negatywne przypadki braku konfiguracji oraz przekroczenia limitów nadal są odrzucane. Testy nie rozszerzają rzeczywistych zezwoleń.

## 4. Monitorowanie i bezpieczne zatrzymanie

Przed **każdą surową akwizycją**, także w uśrednianiu i rejestracji referencji, runner odczytuje I/V wszystkich aktywnych kanałów SMU. Sprawdza trips, moc i compliance według zatwierdzonej polityki stanowiska. Nie trzeba dodawać ręcznego Measure pomiędzy wszystkimi widmami.

STOP zatrzymuje pomiar przed następną akwizycją i zapisuje checkpoint compliance. Warn/clamp zachowuje informację o ograniczeniu. Zadany prąd oraz zmierzony prąd są odrębnymi wielkościami. To programowe próbkowanie przed ramką: sprzętowe compliance nadal jest konieczne, a krótki impuls podczas akwizycji może nie zostać wykryty programowo.

Zmiany prądu zwykłej osi są bezpośrednimi nastawami. Rampa MOKE i jawna rampa Keithleya do zera mają oddzielną politykę przejścia, zapisaną w zdarzeniach i widoczną jako RAMPING. Nie dodano ukrytej rampy pomiędzy punktami.

E-STOP wyłącza niezależne urządzenia równolegle. Test blokuje odpowiedź MOKE i potwierdza, że OFF Rigola, SMU i RF nie czeka na tę operację. Zero DAC nie dowodzi fizycznego odłączenia wzmacniacza Kepco. Stan pozostaje UNKNOWN; recovery odmawia wznowienia bez wymaganej bezpiecznej granicy.

Górny pasek bezpieczeństwa korzysta z potwierdzeń runnera. Nie przedstawia UNKNOWN jako OFF. Późniejsza zmiana stanu ręcznej sesji unieważnia jej poprzednie potwierdzenia. Odblokowanie kart pomija kontrolki, które Qt zdążył usunąć, dzięki czemu nie przerywa obsługi zakończenia pomiaru.

Historyczne `dut_limits` w recepturach są metadanymi zgodności. Ich polityka jest utrwalona w HDF5; aktywne limity pochodzą z zatwierdzonych ustawień stanowiska.

## 5. Dane i metadane

HDF5 jest archiwum naukowym. CSV pozostaje podsumowaniem checkpointów. Zapis obejmuje źródło receptury i ustawień oraz ich hashe, hash planu, identyfikacje i capabilities urządzeń, tryb/symulację, kontekst operatora i wybranej próbki, status zamknięcia, zdarzenia i UTC, RAW, wyniki przetwarzania, referencje i ich pochodzenie.

Dla nastaw utrwalane są requested/applied/readback, metoda i czas potwierdzenia, indeksy osi/etapów oraz snapshoty rzeczywistej konfiguracji. RAW zawiera czas rozpoczęcia/zakończenia, `safety_measurements_si` i `safety_sampled_at_s`. `safety_sampling_policy` checkpointu identyfikuje kontrolę aktywnych SMU przed każdą ramką.

Testy wymuszają błędy flush przed i po publikacji RAW, błędy checkpointów, nieudany rollback, niekompletny ogon po przerwaniu oraz błąd podsumowania CSV. Zachowywany jest pierwotny błąd; niezatwierdzony punkt nie zwiększa licznika. To nie jest dowód odporności na wszystkie awarie zasilania systemu plików.

Walidacja eager PyThat ma jawny limit pamięci oraz ograniczenie wynikające z dostępnego RAM. Recovery sprawdza również limit zapisany w istniejącym pliku **przed połączeniem z aparaturą**. Większy plan wymaga świadomego wyboru budżetu; brak pamięci nie powoduje pominięcia walidacji.

Do inspekcji zachowano mały, aktualny [HDF5 z 18 kombinacjami](docs/audits/2026-10-05-sweep-fixes/cartesian-simulation.h5) i odpowiadający mu [CSV](docs/audits/2026-10-05-sweep-fixes/cartesian-simulation.csv). Duże archiwa prób skali są generowane w katalogach testowych; zachowane JSON-y utrwalają wyniki i hash pliku.

## 6. Wyniki końcowe

| Pakiet | Zakończony wynik |
|---|---|
| Kompilacja, wykonanie, recovery, zapis, estymacja i Wait | 466 passed, 4 skipped, 18 subtests; 195,04 s |
| Edytor receptur i przewijanie Fluent | 95 passed, 4 subtests; 131,69 s |
| Natywne adaptery i kontrakty wydania | 135 passed, 5 subtests; 40,44 s |
| Rozszerzone kontrakty konfiguracji i preflight | 93 passed, 6 subtests; 53,42 s |
| Końcowe renderowanie po show() i zakończeniu natywnej animacji, jasny/ciemny/wąski widok | 9 passed; 219,09 s |
| Pasek bezpieczeństwa i unieważnienie starej sesji | 1 passed; 26,99 s |
| Pełne napisy bezpieczeństwa i dostępność E-STOP, 240–1500 px, oba motywy | 11 passed; 43,71 s |
| Fluent, interakcje drzewa i responsywność, w tym 1000 × 10001 | 56 passed; 589,14 s |
| Powtórzony stres GUI na aktualnym kodzie | 1 passed; 185,73 s |
| eLab i integracja drzewa wyników | 14 passed; 20,15 s |
| Sweep 1309 × 1001, estymacja i PyThat | 1 passed; 145,25 s |
| Ruff, kontrola F w zmienionych plikach | All checks passed |

Pakiety częściowo się nakładają; nie należy sumować tych liczb jako unikalnych testów. Kolekcja zakresu audytu objęła **649 przypadków**; poza nią wykonano **11 testów geometrii paska bezpieczeństwa**. Dodatkowe przypadki wprowadzone po większej próbie sprawdzono osobno na aktualnym kodzie. Weryfikacja odbywała się pakietami, nie jednym przebiegiem wszystkich testów całego repozytorium.

Cztery pominięcia dotyczą nieobecnych licencjonowanych golden files thaTEC. Zwykły odczyt nowych archiwów przez PyThat został wykonany. [Logi, zrzuty i wyniki](docs/audits/2026-10-05-sweep-fixes/README.md) zawierają zakres dowodu.

W powtórzonej próbie GUI zarejestrowano **5369 ticków timera**. Maksymalna przerwa wyniosła **243,5 ms**, aktualizacja modelu drzewa **34,1 ms**, a podglądu widma **13,0 ms**. Progi regresji to 350/250/250 ms. Test obejmuje cały pomiar i zamknięcie pliku. Wyniki zależą od środowiska. Końcowy rezultat czeka na późniejszy odczyt; interfejs nie rozpoczyna automatycznie kosztownego importu eager.

Próba GUI 1000 × 10001 używa jawnego budżetu **2 GiB wyłącznie w tymczasowej konfiguracji SIM**. Estymacja i odmowa planu przekraczającego zwykły budżet pozostają aktywne.

Na fizycznym stanowisku trzeba oddzielnie zakwalifikować firmware, odpowiedzi OFF, obciążenie/compliance i czas stabilizacji. Symulacja nie dowodzi, że zadany ±80 mA będzie osiągalny w dowolnym DUT. Nie uruchomiono rzeczywistego eksperymentu na podstawie tej kwalifikacji oprogramowania.
