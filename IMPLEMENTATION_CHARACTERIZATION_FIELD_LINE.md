# Stan wdrożenia charakterystyki field line

Cel pozostaje aktywny: pełny plan `PLAN_CHARAKTERYZACJA_FIELD_LINE.md` plus najnowsze polecenie operatora: czasowy STOP obu kanałów, pomijanie pozycji field line na compliance oraz odrębne formularze i granice A/B.

## Obowiązująca zmiana kontraktu

Najnowsze polecenie zastępuje wcześniejszy zapis „compliance B zawsze kończy całą serię”. B na compliance kończy/pomija bieżącą pozycję, oba wyjścia muszą być potwierdzone OFF, a recovery B musi być potwierdzone przed przejściem dalej. Restart od zera rozpoczyna nowy segment historii magnetycznej. Timeout, utrata komunikacji, niepotwierdzone OFF/recovery i utrata STOP nadal kończą serię błędem. Punkt diagnostyczny pozostaje w zapisie, ale nie jest poprawnym pomiarem przy zadanym polu.

## Zrobione i sprawdzone w tej części pracy

- Formularze sweepu zapisują osobno start/stop/liczbę punktów dla kanału i trybu. Synchronizacja z główną kartą również przechodzi przez zmianę formularza. Limity i nastawy sprzętowe nadal pochodzą z właściwego kanału głównej karty.
- PDF automatycznie zawiera rozdział Rigol z tabelą DC High-Z/50 Ω, punktowym wykresem oraz instrukcją i ograniczeniami AC. Dodatkowy `rigol_equivalence.csv` zapisuje pełną mapę i powody wykluczenia. Konwersja korzysta ze wspólnej definicji LOAD warstwy bezpieczeństwa.
- Model punktu przechowuje odczyty field line przed/po i flagę ważności. Eksport zapisuje te wartości z jednostkami; checksum uwzględnia dodatkową telemetrię.
- `field_series.py`: niezależnie waliduje plan A/B, wymaga zweryfikowanych ręcznych konfiguracji i STOP obu kanałów; sekwencyjnie rampuje/stabilizuje B, mierzy A z kontrolą B, pomija pozycje B na compliance i wykonuje recovery przy OFF. Zera/powtórzenia listy B zachowane. Runner nie jest jeszcze wpięty w UI.
- `field_storage.py`: manifest, pełny dziennik odczytów z flush/fsync, CSV/snapshot datasetu dla każdej krzywej, wpisy pominięć i stan końcowy. Integracja z rzeczywistym katalogiem i przeglądarką serii pozostaje do wykonania.
- Analizator nie dopasowuje R0 z punktów compliance przy niedoborze danych; pojedynczy punkt nie jest uznawany za pomiar R0. Nieważne warunki wykluczane. Pochodne nie korzystają z compliance. Pełna segmentacja i wspólne okno porównawcze pozostają do wykonania.

## Dowody testów

- 5 testów UI: formularze kanałów/trybów, synchronizacja głównej karty, granice i renderowana geometria.
- 44 testy: analyzer, field-series runner, istniejący runner charakterystyki, przeliczenia Rigola i PDF/CSV. Potem dodatkowy test trwałego zapisu: 10 testów field-series/storage przeszło.
- QtPdf odczytuje tekst wygenerowanego PDF i renderuje strony; bez nowej biblioteki PDF. Testowe artefakty pod `scratch/pytest-rigol-equivalence-20260908-b/`.
- `python -m ruff check app tests`: sukces.
- Pierwszy run pytest z domyślnym tmpdir miał błędy dostępu do katalogu systemowego. Kolejne używają jawnego `--basetemp` w workspace. Nie jest to test na sprzęcie.

## Pozostała praca — nie uznawać całości za ukończoną

1. Rezerwacja kontrolera/sesji dla obu kanałów: mechanizm wyłączności jest już zaimplementowany i testowany na poziomie kontrolera oraz wątku instrumentu. Pozostaje wpięcie w UI, wstrzymywanie Live i blokowanie edycji podczas całej transakcji. OFF i E-STOP pozostają dostępne; przerywają rezerwację i uniemożliwiają ponowne ON. Zamknięcie kontrolera także przerywa rezerwację i odrzuca zakolejkowane ON.
2. UI Fluent serii: lista B, rampa, tolerancje, stabilizacja, czas maksymalny i postęp są wpięte; przed startem obowiązuje drzewo scenariusza. Pozostają wybór zapisanej krzywej i nakładki oraz trwałe drafty między uruchomieniami. B ma jawne własne granice; A korzysta z własnego draftu i konfiguracji karty.
3. Transakcja A/B jest wpięta: jeden modal, projekcja obu polityk, rezerwacja i retry po błędzie. `field_worker.py` ustawia oba STOP, sprawdza odczyt i wykonuje rollback częściowej zmiany po obu OFF. UI utrzymuje rezerwację do zakończenia/naprawienia transakcji. Potrzebne dalsze testy z rzeczywistym kontrolerem/adapterem, w tym zamknięcia całej aplikacji podczas recovery.
4. Worker serii, trwały store i automatyczny resolver próbki/komórki są połączone. PDF każdej krzywej oraz Rigol CSV powstają automatycznie po obu OFF/przywróceniu polityk i zwolnieniu rezerwacji, z odczytu zapisanych snapshotów. Dodano ponowne generowanie raportów oraz rejestrację każdej pozyskanej krzywej w Measurements bez duplikowania przy ponowieniu. Nadal brak raportu zbiorczego. Obecna pojedyncza ścieżka nadal zapisuje PDF przed `_begin_policy_restore()` — wymaga uporządkowania.
5. Raport zbiorczy, R0/MR po polu/gałęzi, historia, Rigol dla wielu pól, pełna analiza jakości i adnotacje anomalii. Nie odwracać wielowartościowych map ani nie ekstrapolować przez compliance.
6. Odtwarzanie datasetów ze snapshotu i ponowienie PDF bez sprzętu są zaimplementowane i testowane. Pozostają przeglądarka krzywych/UI otwierania historycznej serii, pełny przepływ odzyskiwania przerwanego zapisu oraz odbudowa z dziennika. Czytnik nie przedstawia stanu running jako zakończonego ani jako zgody na wznowienie sprzętowe.
7. Przegląd shutdown, anulowania, błędu zapisu i deadline na każdej granicy. Należy uwzględnić, że callback zapisu może trwać długo; potrzebny nadzór ekspozycji i kwalifikacja watchdog. Nowy runner ma limit czasu sprawdzany między operacjami, nie niezależny sprzętowy watchdog.
8. Testy pełnej integracji UI/controller/adapter, scenariusze dwóch polityk, testy awarii i wizualna inspekcja nowych ekranów. Zweryfikować poprawność renderowania wszystkich stron PDF na białym tle, nie tylko odczyt tekstu.
9. Przegląd zgodności ze wszystkimi punktami planu i aktualnym poleceniem użytkownika. Kwalifikacja fizycznego modelu/połączeń wymaga dowodów sprzętowych; nie przedstawiać symulacji jako gwarancji bezpieczeństwa laboratorium.

## Uwaga do dalszej implementacji

Wszystkie nowe pliki są w aktualnym worktree; nie resetować wcześniejszych zmian. Testy `tests/test_keithley_field_series.py` używają niezależnego fake dwóch kanałów; konfiguracja testowa kopiuje do A włączony profil B, ponieważ domyślny profil fixture ma A wyłączone. Nie kopiować tego zachowania do aplikacji.

## Dalsza weryfikacja transakcji A/B

- Dodano 9 testów rzeczywistego `FieldSeriesWorker` z niezależnym fake i rzeczywistym zapisem plików. Sprawdzają oba STOP przed pierwszym ON, przywrócenie różnych polityk A/B, częściową awarię po mutacji A lub B, anulowanie, zmianę polityki po dialogu, nieznany snapshot, awarię OFF i przywracania, a także compliance A na ostatniej pozycji listy.
- Poprawiono rollback: błąd preflight nie nadpisuje polityki, która zmieniła się od zatwierdzenia. Snapshoty polityk są kopiowane, aby późniejsza mutacja słownika nie zmieniła treści zatwierdzenia.
- Compliance A na ostatnim punkcie listy, przy wyłączonej kontynuacji, daje `stopped_on_compliance`, a nie `completed`.
- Dodano 2 testy zamykania kontrolera: sygnał przerwania rezerwacji oraz blokada zakolejkowanego ON. Wywołania proxy po zamknięciu są odrzucane od razu zamiast czekać na nieaktywny wątek.
- Wyniki: 25 testów rezerwacji/serii/workera przeszło przed dodaniem ostatniego przypadku statusu; następnie wszystkie 9 testów workera przeszło. Osobno 35 testów regresyjnych runnera, analizatora, raportu i przeliczeń Rigola przeszło. Ruff przeszedł przed ostatnim dodatkowym testem; końcowy wynik należy sprawdzić po kolejnych zmianach.
- Nadal bez testu laboratoryjnego. Pełna integracja UI, raport zbiorczy, czytnik/regenerator oraz pozostałe wymagania planu nie są ukończone.

## Modal scenariusza — wdrożona część UI

- `field_scenario.py` buduje niemutowalny opis ze zwalidowanej konfiguracji i tej samej funkcji siatki A co runner. Zachowuje powtórzenia/zera B, pokazuje pierwszą rzeczywistą niezerową nastawę A, rampę B od 0, oba STOP, odrębne parametry sprzętowe i warunkowe ścieżki compliance/recovery/OFF.
- `FieldScenarioDialog` jest modalem Fluent z drzewem, rozwijaniem szczegółów, anulowaniem i jawnym zatwierdzeniem. Formularz `FieldSeriesPanel` dodaje uporządkowaną listę B z jednostkami, rampę, stabilizację, tolerancje i czas maksymalny. Brak automatycznie wybranego prądu B lub kroku rampy.
- Start z karty wymaga zatwierdzenia drzewa. Worker otrzymuje dokładnie jego config i zapisuje cały przegląd w provenance `series.json`. Niezgodność przeglądu i konfiguracji jest odrzucana przed mutacją.
- Główna karta wstrzymuje Live (odznacza na czas serii, zachowuje poprzedni wybór), blokuje konfigurację i polityki, zachowuje OFF. Polityki wyświetla na podstawie potwierdzeń workera. Ogólne komunikaty VERIFIED/FAULT nie zastępują ich domyślnym warn_clamp podczas rezerwacji.
- Testy: 33 testy scenariusza, workera, runnera i rezerwacji przeszły. Ruff przeszedł. Obejrzano PNG modala w 960×740 oraz 660×520, m.in. `scratch/pytest-field-scenario-20260908-d/test_modal_geometry_cancel_and0/`.
- Pierwsza szeroka próba UI: 40 testów przeszło, 1 test QSettings nie odczytał zapisanej wartości. Trwa osobna próba z QSettings skierowanym do izolowanego INI w workspace zamiast systemowego rejestru. Nie ukrywać tego wyniku jako pełnego sukcesu suite.
- Ten etap nie kończy pełnego celu: PDF/raport zbiorczy serii, przeglądarka krzywych, rejestracja Measurements, odczyt snapshotów i dalsza kwalifikacja błędów pozostają otwarte.

### Wynik końcowej weryfikacji tej części

- Zestaw scenariusza/serii/workera/rezerwacji: **33 passed**. Dodatkowy test renderowania pełnej karty z formularzem serii i anulowaniem: **1 passed**; PNG obejrzany.
- Szeroki zestaw dotychczasowego UI: **31 passed, 1 failed**. Rozpoznanie przyczyny: konstruktor `QSettings(organization, application)` nadal używał NativeFormat mimo `setDefaultFormat(IniFormat)`; zapis zwracał `Status.AccessError` dla rejestru Windows. Powtórzenie dokładnie nieudanego testu z konstruktorami QSettings skierowanymi jawnie do izolowanego INI: **1 passed**. Nie zmieniano testowanego zachowania aplikacji ani asercji.
- `ruff check app tests` oraz `git diff --check`: sukces (Git ostrzega jedynie o konwersji LF/CRLF).
- Formularz serii i wymagany modal są połączone z workerem. Pozostałe elementy pełnego celu wymienione wyżej nadal wymagają implementacji i nie są uznane za gotowe.

## Odczyt zapisanej serii

Dodano `field_reader.py`: odczyt wersji 1 manifestu oraz snapshotów datasetów bez urządzenia i bez aktualnych ustawień. Zachowane są kolejność, powtórzenia, pominięcia, segment historii i telemetria B. Czytnik sprawdza zgodność indeksów/liczby punktów/prądu z manifestem, checksum datasetu, skończoność odczytów i lokalność ścieżek. Niedostępne pochodne R zapisane jako null wracają jako NaN, nie zero. Stan running pozostaje running; odczyt nie stanowi zgody na wznowienie sprzętowe.

8 testów odczytu przeszło (round-trip, nieznany schemat, ucieczka ścieżki, rozbieżność prądu/liczby punktów, zmieniony pomiar, brak snapshotu i zachowanie stanu interrupted/running). Czytnik jest podstawą dalszego raportowania i przeglądarki; nie jest jeszcze wpięty w ich pełny przepływ. Pełny cel nadal pozostaje otwarty.

## Automatyczne raporty poszczególnych krzywych

`field_reports.py` odczytuje zamkniętą serię z dysku, generuje oddzielny PDF i Rigol CSV dla każdej pozyskanej krzywej, a następnie atomowo aktualizuje statusy raportów. Nie modyfikuje statusu akwizycji, surowego CSV ani snapshotów. Błąd jednego renderowania zachowuje pozostałe dane i poprzedni PDF. Seria running jest odrzucana, żeby renderer nie konkurował z writerem.

Karta uruchamia osobny wątek raportów po zakończeniu wątku pomiarowego, obu OFF, przywróceniu polityk oraz zwolnieniu rezerwacji. Przycisk Regenerate series reports ponawia operację z zapisanych danych. Wspólna blokada RLock serializuje Matplotlib i rejestrację fontów również względem istniejących raportów jednokanałowych.

PDF zawiera identyfikację B: prąd zadany, indeks pozycji, segment historii i zakres zapisanych prądów B. Oględziny pierwszej strony wykryły dawny problem formatowania małych nastaw: 1–3 µA było przedstawiane jako 0.00–0.00 mA. Zakres sweepu, compliance i moc korzystają teraz ze wspólnego formatowania jednostek, bez zerowania małych wartości przez sztywne dwa miejsca dziesiętne.

Weryfikacja: 28 testów raportów/scenariusza i regresji przeszło przed poprawką formatowania. Kolejna grupa po poprawce: 20 przeszło, jedna asercja oczekiwała znaku µ zamiast stosowanego w projekcie aliasu u; poprawiono oczekiwanie na `1 uA to 3 uA`. W teście integracyjnym każda próba renderowania dodatkowo sprawdza, że rezerwacja została zwolniona, oba wyjścia są OFF, a polityki wróciły do poprzednich wartości. Przy testowaniu workera PDF należy oddawać GIL (`time.sleep` z małym krokiem w pętli testowej); ciasna pętla QTest.qWait wcześniej zagłodziła renderer i powodowała timeout testu, mimo później poprawnie zapisanych plików.

Oględziny: `scratch/pytest-field-report-integration-20260908-c/test_confirmed_modal_executes_0/ui_series/field_report_review.png` (przed poprawką jednostek). Nadal otwarte: raport zbiorczy z fizycznie poprawnym porównaniem, rejestr Measurements, wybór/porównywanie krzywych i pozostałe elementy pełnego celu.
Końcowe powtórzenie wszystkich 3 testów field_reports po korekcie jednostek: 3 passed. Ruff: sukces.

## Powiązanie serii z Measurements

- Snapshot `inventory_target` (ID/nazwa próbki, wiersz, kolumna, opis komórki) jest pobierany przed modalem i zapisywany z serią. Rejestracja z plików nie korzysta z bieżącego wyboru próbki. Starsze lub ręczne serie bez tego snapshotu nie są automatycznie przypisywane do przypadkowej próbki.
- `field_catalogue.py` rejestruje każdą pozyskaną krzywą z własnym CSV, statusem, numerem pozycji B, prądem B i segmentem historii. Pominięte pozycje bez krzywej pozostają diagnostyką w manifeście, nie udają wykonanych pomiarów.
- `InventoryStore.register_run_artifacts` atomowo względem wątków rozpoznaje wcześniejszy wpis po próbce i ścieżce. Wymaga niezmienionej sumy kontrolnej oraz komórki; przy ponowieniu aktualizuje ścieżki artefaktów, zachowując ID, notatki operatora i metadane publikacji eLab. Nie wysyła nic do eLab.
- Karta rejestruje dane po bezpiecznym zakończeniu, a po PDF aktualizuje te same rekordy i emituje istniejący sygnał odświeżenia Measurements. Błąd katalogu jest zgłaszany bez usuwania danych.
- Nowe manifesty przechowują `csv_sha256`; czytnik sprawdza ją przed dalszą pracą. Starsze manifesty bez tej opcjonalnej wartości pozostają czytelne, bez twierdzenia o dodatkowej weryfikacji CSV.
- Dowody: 29 testów katalogu, czytnika, serii i InventoryStore przeszło. Osobny pełny test UI od zatwierdzenia modala przez rzeczywisty worker i PDF do 3 rekordów właściwej komórki: 1 passed. Ruff: sukces.

Następne otwarte części: przeglądarka zapisanych krzywych i nakładki, raport zbiorczy/wspólne okno analizy, trwałe drafty, porządkowanie starej ścieżki jednokanałowej oraz pełny audyt pozostałych wymagań planu. Cel pozostaje aktywny.

## Przeglądanie zapisanych krzywych

Dodano Open saved field series oraz selektor pozycji B. Czytnik zasila wykresy V–I i R z jednego snapshotu; przełączanie nie komunikuje się z urządzeniem i nie zmienia nastaw źródła. Po zakończeniu raportowania seria otwiera się automatycznie. CSV/PDF wskazują wybraną krzywą. Pozycje pominięte są widoczne ze statusem i bez fałszywych wyników lub przycisków nieistniejących plików.

Wykres pokazuje zapisany tryb i osobny opis historycznej konfiguracji, niezależnie od formularza następnego pomiaru. Nieważne punkty tworzą luki, nie zastępuje się ich poprzednią rezystancją ani zerem. Dla praktycznie stałej krzywej ustawiany jest minimalny względny zakres osi, aby szum zaokrąglenia nie wyglądał jak fizyczna zmiana R. To wyłącznie skala prezentacji.

Test offline wielokrotnie przełącza pozycje (w tym powtórzone B=0 i pominiętą pozycję) oraz rzeczywisty przełącznik Fluent V–I/R; sprawdza dane, dostępność plików, skalę płaskiej krzywej i brak wywołań kontrolera. Zapisano i obejrzano renderowaną kartę w `scratch/pytest-field-viewer-20260908-a/`; po oględzinach poprawiono zbyt wąską skalę płaskiej krzywej. Zestaw viewer/scenario przed ostatnim opisem konfiguracji: 8 passed; Ruff: sukces. Nakładki wielu krzywych i zbiorcza analiza nadal pozostają do wdrożenia.

## Nakładki krzywych field line

Dodano Overlay curves dla zapisanej serii. Czysty moduł `field_plot_data.py` przygotowuje projekcję V–I lub R ze snapshotów, zachowując numer pozycji, prąd B i segment historii. Powtórzone B=0 po recovery są osobnymi krzywymi. Punkty nieważne/compliance pozostają lukami; pozycje pominięte na B compliance nie wchodzą do porównania. Ich diagnostykę można nadal oglądać przez wybór pojedynczej pozycji.

UI rysuje osobne kolory i legendę; przełączanie V–I/R przebudowuje nakładki bez urządzenia. Wyłączenie nakładek przywraca wybraną pojedynczą krzywą. Rozpoczęcie nowego pomiaru usuwa historyczne nakładki i przywraca tryb live. Kolory mają ograniczoną jasność w jasnym motywie, a legenda korzysta z tokenów wykresu.

Weryfikacja: 9 testów viewer/scenario przeszło; po korekcie kontrastu i oczekiwaniu na animację przełącznika oba testy viewer przeszły ponownie. Ruff: sukces. Obejrzano render nakładek w `scratch/pytest-field-overlay-20260908-a/`; końcowy render po poprawce kontrastu: `scratch/pytest-field-overlay-20260908-b/`.

Nadal otwarte są raport zbiorczy i wspólne okno analizy, trwałe drafty, usprawnienie wcześniejszej ścieżki jednokanałowej oraz całościowy audyt wymagań planu. Nakładki nie stanowią automatycznej interpretacji TMR, pola ani dynamiki magnetycznej.

## Wspólne okno analizy serii — rdzeń obliczeń

Dodano `field_analysis.py` z jawnym oknem prądu w SI i opcjonalnym numerem pozycji referencyjnej. Każda krzywa jest dopasowywana do V = R_fit I_measured + V_offset (OLS ze skalowaniem osi prądu), z minimum 3 różnymi zmierzonymi prądami. Wynik zachowuje indeksy punktów, rzeczywisty zakres wybranych prądów, błąd standardowy nachylenia i R². R_fit jest nachyleniem w wybranym oknie, nie automatycznym dowodem rezystancji dokładnie przy zerowym biasie.

Odrzucane są punkty compliance/nieważne, niepełne pokrycie żądanego okna, zbyt mała liczba różnych prądów, przerwy i mieszane kierunki sweepu. Nie ma automatycznego poszerzania okna, dopisywania punktów ani zastępowania nieważnej referencji inną krzywą. Każdy brak ma jawny powód. Powtórzone prądy B i segmenty historii pozostają oddzielne. Procentowa zmiana dopasowanej rezystancji ma wskazaną referencję i nie jest nazywana TMR.

7 testów przeszło: znane R i offset, rozróżnienie prądu zmierzonego/zadanego, jawna referencja, brak zamiennika referencji, compliance, nieważne punkty, zmiana kierunku, ucięty zakres i brak ekstrapolacji. Ruff: sukces. Rdzeń wymaga jeszcze połączenia z edytorem okna analizy i raportem zbiorczym; nie oznacza ukończenia tych elementów ani całego celu.

## Weryfikacja modala i integracja raportu zbiorczego (2026-09-08)

Modal przedstawia drzewo przygotowania, każdej pozycji B, rampy, stabilizacji, sweepu A i zakończenia. Pokazuje tymczasowe STOP obu kanałów, faktyczną liczbę niezerowych punktów A, gałęzie compliance oraz odtworzenie poprzednich polityk po potwierdzonym OFF. Zatwierdzony niezmienny scenariusz trafia do workera i manifestu; rozbieżność konfiguracji blokuje wykonanie. Anulowanie nie rezerwuje urządzenia ani nie zmienia nastaw.

Weryfikacja: wszystkie 7 testów test_keithley_field_scenario.py przeszło w zestawie z testami raportu. Obejrzano render desktopowy modala; test sprawdza również geometrię wąskiego okna, Escape i jawne zatwierdzenie. Test integracyjny wykonuje serię na atrapie urządzenia, sprawdza przywrócenie polityk oraz zapis CSV/PDF i rejestr Measurements. Nie jest to test fizycznego Keithleya.

Połączono opcjonalne wspólne okno analizy i numer referencji z formularzem, scenariuszem i raportem zbiorczym. Powstają field_series_summary.csv, field_series_analysis.json oraz field_series_report.pdf, z osobnymi przyciskami otwierania. Raport obejmuje wszystkie pozycje, przyczyny brakujących dopasowań, porównania krzywych i nominalne mapy Rigola. Brak okna nie uruchamia automatycznie arbitralnego dopasowania. Wyniki R_fit i względna zmiana nie są automatycznie interpretowane jako R0 ani TMR.

3 testy raportu zbiorczego przeszły po dodaniu tekstowego objaśnienia LOAD High-Z/50 ohm obok rastrowych wykresów. Sprawdzają wartości w CSV/JSON/PDF, brak komend urządzenia i zachowanie poprzedniego PDF przy błędzie. Ruff app tests: sukces przed ostatnią zmianą tekstu raportu.

Pozostałe otwarte elementy pełnego celu wymienione wcześniej nadal wymagają wykonania; ta weryfikacja nie zamyka całego wdrożenia.

## Trwałe formularze A/B i serii field line

Formularz zapisuje wersjonowany operator_drafts_v1 w istniejącym QSettings: niezależne start/stop/liczba punktów dla A/current, A/voltage, B/current i B/voltage, ostatni tryb każdego kanału oraz parametry listy B i wspólnego okna analizy. Zapis następuje po edycji, zmianie kanału/trybu oraz przy zamknięciu karty. Odtworzenie nie wysyła komend do urządzenia. Compliance, sense, NPLC, zakresy i limity sprzętowe nie są duplikowane w tym zapisie; po odtworzeniu formularz korzysta z aktualnych limitów kanału.

Niepoprawny JSON, nieznany schemat i nieprawidłowe typy/identyfikatory odrzucają odtworzenie z komunikatem. Wartości tekstowe pozostają szkicem operatora, a wykonanie wymaga zwykłej walidacji i potwierdzenia scenariusza.

Weryfikacja: 11 testów drafts/scenario przeszło, w tym ponowne utworzenie karty z wszystkimi czterema szkicami, listą B zawierającą zero/powtórzenia oraz uszkodzone dane. Test zapisu używa rzeczywistego QSettings w jawnym pliku INI w katalogu testowym; nie potwierdza zapisu do rejestru Windows w środowisku produkcyjnym. Ruff app tests: sukces. Pełny cel nadal otwarty zgodnie z wcześniejszą listą braków.

## Zakończenie zwykłego pomiaru przed PDF

Usunięto różnicę w kolejności zwykłej charakteryzacji względem serii: surowy CSV i wpis Measurements powstają po zakończonym runnerze, natomiast PDF czeka na potwierdzone przywrócenie poprzedniej polityki compliance. Błąd przywrócenia zachowuje CSV i oczekujący raport, blokując następny pomiar. Po udanym retry PDF uzupełnia ten sam wpis katalogu przez register_run_artifacts. Błąd renderowania nie opóźnia już przywracania polityki. Renderowanie zwykłego raportu pozostaje synchroniczne; nie jest to jeszcze rozwiązanie całego audytu zamykania aplikacji.

Dwa testy przeszły: nowy test awarii/przywrócenia polityki z kontrolą momentu generowania PDF oraz istniejący test pełnego zapisu zwykłego pomiaru do InventoryStore. Nowy test używa atrapy urządzenia i jawnego QSettings INI. W teście celowo zastąpiono wyłącznie modal informacyjny o błędzie, aby nie czekał na ręczne zamknięcie. Ruff app tests: sukces.

## Diagnostyczne odzyskiwanie odczytów z dziennika

Dodano field_journal.py i automatyczny journal_readings.csv podczas raportowania zamkniętej serii. Eksport czyta surowe sample_raw, także jeżeli awaria nastąpiła przed utworzeniem dataset.json. Rozróżnia committed_dataset, uncommitted_full_point i raw_only_field_after_unconfirmed. Nie dopisuje brakujących pomiarów B, nie oblicza rezystancji dla niezatwierdzonych danych, nie zmienia statusu akwizycji i nie uruchamia wznowienia.

Sprawdza skończone liczby, typ compliance, tożsamość i ciągłość indeksów oraz zgodność surowych wartości z zatwierdzonym datasetem. Niekompletny ostatni rekord bez newline pozostaje pominięty z jawną flagą; uszkodzony kompletny rekord zgłasza błąd. Manifest raportowania otrzymuje ścieżkę, liczbę odczytów i SHA256 źródłowego dziennika. Eksport publikuje CSV atomowo, zachowując poprzedni plik przy błędzie.

6 testów journal/reports przeszło: zgodność z datasetami, przerwanie po surowym odczycie A przed kontrolą B, urwany rekord, uszkodzenie pełnego rekordu, zachowanie poprzedniego artefaktu oraz regeneracja raportów. Ruff app tests: sukces. Nadal nie jest to automatyczne wznowienie pomiaru ani dowód fizycznego OFF po awarii procesu.

## Deadline B obejmuje zapis i końcowe wyzerowanie

Usunięto reset deadline przed końcową rampą B do zera. Po zapisie krzywej oraz przed następną pozycją sprawdzany jest nadal limit poprzedniej aktywnej pozycji. Wolny zapis nie może w ten sposób ukryć przekroczenia limitu i dopuścić kolejnego celu B. Oczekiwanie stabilizacyjne jest ograniczone pozostałym czasem deadline; dodatkowe kontrole działają po zapisie obserwacji i przed pierwszym enable B.

22 testy serii/workera przeszły, w tym przekroczenie podczas zapisu ostatniej i pośredniej krzywej oraz ograniczenie czasu oczekiwania. Ruff: sukces. To nadal kontrola między operacjami, nie niezależny sprzętowy watchdog zdolny przerwać zablokowany sterownik lub fsync.

## Macierz przywracania indywidualnych polityk

Dodano 8 wariantów testu całego workera: A/B w kombinacjach stop/warn_clamp, każde przy zakończeniu i przy anulowaniu w trakcie pomiaru A. Każdy odczyt sprawdza STOP obu kanałów, a wynik potwierdza oba OFF i dokładne przywrócenie odrębnego początkowego trybu każdego kanału. Wszystkie 17 testów field_worker przeszło. Ruff app tests: sukces. Nadal są to testy programowe na atrapie urządzenia.
Zakończyła się także regresja karty UI: 31 passed, 1 deselected (persistence_of_last, wcześniej sprawdzany z jawnym backendem INI ze względu na ograniczenia rejestru Windows). Zakres obejmuje przełączanie kanałów, dziedziczenie konfiguracji oraz bramki potwierdzenia compliance i OFF.

## Skala rezystancji w raporcie zbiorczym

Osie R i R_fit mają teraz minimalny względny zakres prezentacji 2% oraz wyłączony ukryty offset opisów. Chroni to przed przedstawianiem reszt zaokrągleń jako dużych przejść rezystancji, bez zmieniania danych i bez obcinania rzeczywistych zmian. Raport jawnie opisuje tę regułę. Zmieniono qualified fit na fit computed i wyjaśniono, że błąd standardowy nachylenia OLS nie jest pełną niepewnością pomiarową.

4 testy raportu zbiorczego przeszły, w tym stałe R z resztą zaokrągleń oraz rzeczywisty spadek 1000 do 700 ohm. Obejrzano render wykresów PDF: scratch/pytest-summary-scale-a/test_common_window_summary_has0/summary_page_2.png. Ruff app tests: sukces.

## Pochodne bez sortowania historii pomiaru

Usunięto sortowanie po V i usuwanie duplikatów z obliczania krzywych pochodnych. Dane zachowują kolejność akwizycji. Punkty nieważne/compliance, powtórzona współrzędna i zmiana kierunku rozdzielają odcinki; co najmniej trzy punkty są wymagane na odcinek. dV/dI jest obliczane bezpośrednio po I, nie przez odwracanie numerycznej dI/dV. W prezentacji NaN rozdziela linie; legenda PDF wskazuje brak wygładzania i odrębne gałęzie.

8 testów analizatora przeszło, w tym zmiana kierunku i brak mostkowania luk/duplikatów. Ruff app tests: sukces przed zmianą tekstu legendy. Detekcja skoków stanu/anomalii oraz kwalifikacja starego fitu BDR pozostają osobnymi otwartymi wymaganiami; rozdzielenie gałęzi samo nie rozpoznaje zmiany stanu magnetycznego.

## Kwalifikacja ciągłości danych do BDR

Dopasowanie BDR nie sortuje już gałęzi ani nie usuwa zduplikowanych napięć. Wymaga jednego ciągłego fragmentu bez wewnętrznej luki nieważnych/compliance/niefinitywnych danych i ściśle monotonicznych współrzędnych I oraz V. Analizator przekazuje maskę z oryginalnej kolejności datasetu, dzięki czemu wcześniejsze odfiltrowanie punktów nie ukrywa luki. Brak kwalifikacji daje parametry niewyznaczone, nie fit przez przerwę.

12 testów analizatora przeszło, w tym cztery rodzaje wad wejścia i dotychczasowy syntetyczny model bariery. Ruff app tests: sukces. Sam test monotoniczności nie dowodzi braku grzania ani zmiany stanu magnetycznego; model nadal wymaga ostrożnej interpretacji i rozszerzenia metadanych jakości według planu.

## Brak danych nie oznacza idealnej liniowości

R² pozostaje niewyznaczone (NaN w modelu, kreska/Not determined w UI i tabeli PDF), gdy brakuje minimum trzech punktów albo zmienności I/V. Pusty wynik również nie udaje R²=0. Usunięto z komentarza raportu deklaracje, że compliance dowodzi braku uszkodzenia bariery oraz że brak compliance oznacza wysoką liniowość. Raport podaje wynik obliczenia i stan detekcji.

26 testów analizatora i raportu Rigola przeszło. Nowe przypadki obejmują jeden/dwa punkty oraz stały prąd lub napięcie. Ruff app tests: sukces.

## Regresja zintegrowana i zamykanie aplikacji

Wspólny zestaw 15 plików testowych (serie, modal, viewer, analizy, zapis/czytnik/dziennik, raporty, drafty i rezerwacja urządzenia) zakończył się wynikiem 105 passed. Jest to dowód integracji przetestowanych ścieżek, nie zamknięcie całego planu.

Wykryto, że MainWindow.closeEvent nie pytał osadzonej karty charakteryzacji o zakończenie pracy. Dodano prepare_application_shutdown przed zamykaniem kontrolerów. Trwająca akwizycja dostaje żądanie stop; aktywna rezerwacja, recovery, odtwarzanie polityki i raportowanie blokują zniszczenie okna/kontrolerów. Po zakończeniu operator może ponowić zamknięcie. Nie wprowadzono nowej zgody na pomiar ani automatycznego wznowienia.

Dwa testy kolejności zamknięcia/raportu przeszły po zmianie, w tym wywołanie prawdziwej metody MainWindow.closeEvent na kontrolowanej fasadzie testowej i sprawdzenie bramki rezerwacji/odtwarzania/raportowania. Ruff app tests: sukces. Test nie zastępuje pełnego scenariusza zamykania rzeczywistego okna Fluent z aktywnym kontrolerem; ten szerszy test pozostaje do wykonania.

## Metadane wspólnego fitu i pozorne RA

FieldFit zapisuje bias_coverage (positive_only/negative_only/bipolar), residuals_v w kolejności point_indices oraz apparent_ra_ohm_um2 wyłącznie dla dodatniej znanej powierzchni. CSV i PDF pokazują jednostronność i pozorne RA, a JSON zachowuje pełne residuals. Raport wyjaśnia, że fit jednostronny nie jest bezpośrednim pomiarem przy zerze i że 2-wire obejmuje przewody/styki bez automatycznego odejmowania ich rezystancji. Nie zmieniono surowych danych ani komend urządzenia.

## Kwalifikacja warunków B w mapie Rigola i pozostałe prace

Mapa dla datasetu oznaczonego field_line_current_a wymaga obu obserwacji B, także przy B=0. Brak telemetryki otrzymuje field_conditions_undocumented, compliance B field_compliance, niefinitywne odczyty invalid_field_observation. Takie punkty zachowują indeks/przyczynę, ale nie otrzymują napięć równoważnych. Sam valid=True nie obchodzi tej kontroli.

Ponowny przegląd planu wskazuje następujące otwarte obszary, których nie należy utożsamiać z ukończonymi testami rdzenia:
- §16.5: ręczne adnotacje obszaru anomalii, pochodzenie/adnotujący/czas, odrębna hipoteza i opis w PDF. Automatyczny detektor jest w planie późniejszym etapem, nie warunkiem pierwszej wersji.
- §18: pełne metadane modelu równoważności, referencyjna płaszczyzna napięcia i warunkowość 4-wire; analiza pokrycia wszystkich treści instrukcji oraz statusów jakości.
- §4/14: pełny test zamknięcia pokazanego okna Fluent z aktywną serią, rzeczywistym kontrolerem i symulatorem; dalsza macierz timeoutów i E-stop podczas różnych faz.
- §6/13: czytelność UI/PDF dla dużej liczby pozycji i osobny dostęp do zbiorczych artefaktów z Measurements.
- §12: pełne jawne powody braku/modelu jakości w dotychczasowych wynikach jednokanałowych, w tym zakres stosowalności oszacowania R0 i modelu bariery.

Lista kieruje dalszym wdrożeniem; nie zastępuje końcowego audytu każdego wymagania całego planu.

## Kontekst mapy DC i ustalenie operatora: stanowisko 2-wire

CSV równoważności zawiera teraz Context_JSON: wersję modelu, nominalne 50 ohm, płaszczyznę napięcia, sense, checksum źródła, historię B i jawnie brakujące dane kalibracji/częstotliwości/niepewności. PDF korzysta z tego samego kontekstu. 14 testów istniejącego raportu przeszło po dodaniu kontekstu; Ruff: sukces.

Operator potwierdził, że stanowisko nie ma przewodów 4-wire. Bieżące pomiary i instrukcje planujemy dla 2-wire. Raport 2-wire pomija ogólną instrukcję 4-wire i wyjaśnia tylko wliczony spadek przewodów/styków. Warunek dla historycznego datasetu jawnie zapisanego jako 4-wire zachowuje prawdziwe pochodzenie danych; nie przepisuje go na 2-wire. Nie zmieniono nastaw sprzętu ani konfiguracji głównej karty w tej poprawce.

## Adnotacje anomalii — zapis i raport zbiorczy

Dodano observations.py: osobne niezmienne pliki JSON z opisem operatora, hipotezą oznaczoną unverified, autorem/czasem UTC, zakresem oryginalnych indeksów, zakresem zmierzonego I/V/R, historią B i checksum datasetu. Zapis odrzuca zakres poza danymi; odczyt odrzuca powiązanie z innym datasetem. Nie modyfikuje surowych CSV/snapshotów ani nastaw urządzenia.

Raport zbiorczy drukuje adnotacje i osobno niepotwierdzone hipotezy, z informacją o compliance/nieważnych punktach obszaru. 5 testów zapisu/odczytu i 4 testy raportu przeszły; test PDF sprawdza rzeczywisty tekst dodanej adnotacji. Ruff: sukces przed ostatnią asercją testową. Do domknięcia tej części pozostaje formularz UI adnotacji i pełne metadane wymagane w §16.5; obecna funkcja zapisu jest rdzeniem, a nie gotowym workflow operatora.

## Formularz adnotacji zapisanej krzywej

Przycisk Annotate curve otwiera Fluent StationDialog dla wybranej zapisanej pozycji. Operator wybiera zakres indeksów źródłowych, autora, opis i opcjonalną niepotwierdzoną hipotezę. Pusty opis/autor lub błędny zakres nie zamyka formularza i pokazuje przyczynę. Zapis uruchamia ponowne raportowanie serii. Podczas aktywnego pomiaru, rezerwacji, zmiany polityki lub raportowania adnotowanie jest blokowane; operacja nie steruje urządzeniem.

6 testów observations przeszło, w tym pokazany dialog, walidacja, zapis i brak komend urządzenia. Ruff: sukces. Obejrzano render scratch/pytest-observation-ui-a/test_observation_dialog_valida0/observation_dialog.png. Pozostaje pełny test kliknięcia przycisku na karcie wraz z automatycznym odświeżeniem PDF oraz rozszerzenie metadanych §16.5.

## Adnotacja: test od przycisku do PDF

Nowy test otwiera zapisaną serię na rzeczywistej karcie, klika Annotate curve, zatwierdza rzeczywisty formularz i czeka na rzeczywisty worker raportowania. Następnie odczytuje tekst wygenerowanego PDF przez QtPdf i potwierdza obecność opisu operatora. Kontroler nie otrzymuje komend ani żądania rezerwacji. Cały plik observations: 7 passed; Ruff app tests: sukces. Domknięto brakujący test przepływu UI–zapis–raport; pozostałe braki całego planu pozostają otwarte.
Adnotacje rozszerzono o R pierwszego i ostatniego zaznaczonego punktu, delta R oraz zmianę procentową względem pierwszego. Nieważny endpoint daje brak wyniku bez zastąpienia sąsiednim. Raport drukuje te wartości i definicję bazy. Powtarzalność, histereza, potwierdzenie zmiany zakresu i widmo są jawnie nieznane. Dotychczasowe 7 testów observations przeszło; dwa nowe testy potwierdziły spadek 1000 do 800 ohm jako -200 ohm/-20% oraz brak wyniku dla nieważnego końca.
Adnotacja nie wylicza delta R przez wewnętrzną lukę nieważnych danych, compliance ani zmianę/powtórzenie kierunku nastaw. comparison_unavailable_reason jest zapisane i drukowane w PDF. Opis operatora nadal pozostaje zachowany jako obserwacja do weryfikacji. 12 testów observations przeszło, w tym pełne odświeżenie PDF oraz trzy nowe przypadki przerwanej porównywalności. Ruff: sukces.
Menu kontekstowe pomiaru w Samples/Measurements zawiera teraz Open series summary PDF/CSV, jeżeli rekord należy do zweryfikowanej zapisanej serii i artefakty istnieją. Resolver sprawdza członkostwo przez czytnik manifestu/datasetów, nie tylko nazwę katalogu; odrzuca ścieżki artefaktów wychodzące poza serię. Nie dodaje sztucznego pomiaru ani nie dubluje rekordów. 4 testy field_catalogue przeszły, w tym brak plików i obcy katalog; Ruff: sukces. Interakcja/render menu Measurements wymaga jeszcze osobnego testu UI.
Test pokazanego MeasurementTreeWidget (1000x700) potwierdził geometrię wiersza, utworzenie obu akcji summary w rzeczywistym menu i przekazanie właściwej ścieżki przez każdą akcję. Zastąpiono tylko blokujące exec menu i otwieranie zewnętrznej aplikacji. 5 testów field_catalogue przeszło; Ruff: sukces. Nie zmienia to danych ani liczby rekordów Measurements.
Test pokazanego pełnego MainWindow w symulacji potwierdził odrzucenie close i zachowanie kontrolera, gdy karta ma aktywnego workera/rezerwację (worker kontrolowany mockiem). Oględziny wykazały, że powód blokady był niewidoczny na innej stronie; close przechodzi teraz do keithley_characterization przed komunikatem. To test rzeczywistego okna, ale nadal nie pełna akwizycja przez rzeczywisty kontroler/symulator podczas close; ten element pozostaje otwarty.
Nowy test uruchomił rzeczywisty FieldSeriesWorker przez rezerwację RunDeviceAdapter i kolejkę DeviceController/InstrumentWorker. Sprzęt pozostaje atrapą, ale wszystkie odczyty wykonują się w jednym wątku kontrolera innym niż GUI; seria zakończyła się completed_with_skips, oba OFF i obie poprzednie polityki potwierdzone. Test: 1 passed; Ruff: sukces. Domknięto integrację workera z kolejką w ścieżce nominalnej; pełny adapter/symulator, przerwania i zamknięcie aktywnej serii nadal wymagają dalszej weryfikacji.
Test rzeczywistej rezerwacji i kolejki kontrolera rozszerzono o request_stop oraz emergency_off zgłoszone do kontrolera podczas odczytu A. Obie ścieżki kończą serię statusem cancelled, potwierdzają OFF i przywrócenie polityk; od chwili przerwania log nie zawiera żadnego output ON. Trzy warianty testu przeszły; Ruff: sukces. Adapter nadal jest kontrolowaną atrapą, co nie stanowi weryfikacji fizycznego czasu wyłączenia instrumentu.
Test kolejki kontrolera obejmuje teraz timeout B po odczycie A. Wynik: fault, oba wyjścia OFF, polityki przywrócone, sample_raw zachowany bez fałszywego sample_point. Czytnik i eksport dziennika odzyskują dokładnie jeden odczyt z oznaczeniem raw_only_field_after_unconfirmed. Cztery warianty integracji controller/worker przeszły; Ruff przed ostatnimi asercjami: sukces.
Dwa nowe warianty A/B potwierdziły pobieranie świeżej konfiguracji współdzielonej przy każdym build_config: zmiana compliance, NPLC, settling, source range i obu measure ranges trafia do żądania runnera. Test porównuje cały dataclass KeithleySourceRequest, zachowuje własne start/stop i sprawdza niezależność drugiego kanału. Wszystkie 6 testów drafts przeszło; Ruff: sukces. Test wykorzystuje kontrakt providera głównej karty bez sterowania instrumentem.
Poprawiono etykiety powtórzonych punktów R_fit: dokładnie pokrywające się pozycje mają wspólny czytelny podpis wszystkich indeksów, np. #1, #3, bez scalania danych ani historii. 5 testów summary przeszło; Ruff: sukces. Ogólny problem licznych krzywych/legend i paginacji raportu pozostaje otwarty; ta poprawka dotyczy nakładania etykiet przy identycznych współrzędnych.
Raport zbiorczy dzieli teraz wykresy V-I/R, dopasowań i map Rigola na grupy maksymalnie 12 oryginalnych pozycji listy. Nie usuwa pominiętych pozycji z numeracji ani nie scala powtórzeń; tabela zbiorcza nadal obejmuje wszystkie. Test 13 pozycji sprawdza legendy #1–#12 i #13 oraz oba opisy grup w rzeczywistym PDF. Wszystkie 6 testów summary przeszło; Ruff: sukces. Rozmiar/legenda nakładek w samej aplikacji to osobny pozostały element.
Nakładki w karcie mają wybór grup po maksymalnie 8 oryginalnych pozycji. Zachowują indeksy i historię, również dla pominiętych pozycji; wybór grupy nie komunikuje się z urządzeniem. Test 17 pozycji potwierdza grupy 8/8/1 i etykiety #1/#9/#17. Trzy testy viewer przeszły; Ruff przed ukryciem kontrolki przy nowym pomiarze: sukces. Wymagana jeszcze wizualna kontrola nowej kontrolki przy normalnej/wąskiej szerokości.
Oględziny nowego selektora wykazały ściskanie napisu Overlay curves. Kontrolki nakładek/grupy/adnotacji przeniesiono do osobnego wiersza; napis i akcje są widoczne na końcowym renderze scratch/pytest-overlay-render-b. Trzy testy viewer przeszły; Ruff: sukces. Żądane resize(1000,900) nadal podlega minimalnej szerokości całego istniejącego formularza (render jest szerszy), więc nie stanowi dowodu pełnego layoutu 1000 px.

## Responsywna karta: zweryfikowana szerokość 1000 px

Splitter formularza i wyników przełącza się na układ pionowy poniżej 1360 px; przy większej szerokości pozostaje poziomy. Formularz zachowuje przewijanie. Test pokazanego widgetu sprawdza teraz rzeczywistą szerokość 1000 i 1400 px, orientację splittera oraz widoczną geometrię wykresu i selektora grup. Obejrzano oba rendery w scratch/pytest-responsive-card-b. Trzy testy field_viewer przeszły; ruff check app tests: sukces. To weryfikacja samodzielnej karty, nie pełnego okna z rozwiniętym panelem serii. Nie zmieniono sterowania sprzętem ani danych pomiarowych.

## Minimum danych dla dotychczasowego R0

Usunięto zastępowanie nachylenia medianą V/I przy stałym prądzie. Ekstrakcja wymaga co najmniej trzech różnych skończonych wartości prądu bez compliance; dwie wartości i powtórzone odczyty nie dają R0. Test sprawdza brak wyniku w tych przypadkach oraz nachylenie 1000 ohm mimo offsetu 1 mV. Wszystkie 17 testów analyzer przeszło. Pozostaje ujednolicenie jawnego okna i opisu jakości w dotychczasowym raporcie pojedynczej krzywej; ta poprawka nie dowodzi poprawności pełnej analizy naukowej.

## Regresja 135 testów i zawijanie akcji

Uruchomiono wspólnie 15 plików testów field series/worker/drafts/scenario/reader/reports/analysis/summary/catalogue/journal/viewer/observations/Rigol/lease/analyzer. Pierwszy przebieg: 134 passed, 1 failed (wymuszona szerokość karty 1416 zamiast 1400). Pasek akcji używa teraz Fluent FlowLayout z zawijaniem i pomijaniem ukrytych akcji. Test pokazuje również przyciski summary i recovery oraz sprawdza ich zawieranie w rodzicu. Drugi pełny przebieg: 135 passed w 79.31 s, scratch/pytest-field-integrated-final-b. Ruff: sukces. Osobno 3 testy viewer przeszły, scratch/pytest-field-actions-render-a; obejrzano render 1000 px.

Otwarty problem wizualny: po pełnej sekwencji testów tekst w renderze zastępują kwadraty, natomiast uruchomienie samego viewer wyświetla prawidłowy tekst. Przyczyna zależności od wcześniejszych testów/środowiska fontów wymaga ustalenia; pozytywny wynik pytest nie zamyka tej usterki. W wąskim renderze pełnego paska legenda ośmiu krzywych również wymaga dopracowania wysokości. Dodano CHARACTERIZATION_VERIFICATION.md z dowodami głównych wymagań i otwartymi punktami, bez deklaracji ukończenia planu.

## Legenda nakładek w wąskim oknie

Dla ponad czterech krzywych legenda ma dwie kolumny. Test rzeczywistego widgetu sprawdza zawieranie legendy w scenie wykresu przy 1000 i 1400 px oraz wszystkich akcjach widocznych. Render 1000 px w scratch/pytest-legend-columns-a pokazuje osiem czytelnych etykiet bez przycięcia. 3 testy viewer oraz 9 testów summary+viewer przeszło; Ruff: sukces. Sekwencja summary+viewer nie odtworzyła kwadratów zamiast tekstu (render scratch/pytest-font-isolation-a), zatem sam zestaw testów raportu zbiorczego nie wystarcza do reprodukcji wcześniejszej usterki fontów. Przyczyna pełnej sekwencji pozostaje otwarta.

## Zawężanie problemu fontów

Dwie dodatkowe sekwencje nie odtworzyły kwadratów: scenario+reports+catalogue+viewer (18 passed, scratch/pytest-font-isolation-b) oraz worker+drafts+viewer (30 passed, scratch/pytest-font-isolation-c). Obejrzano końcowe rendery 1000 px, tekst prawidłowy. Test viewer sprawdza teraz również dostępność podstawowych znaków przez QFontMetrics dla przycisku start, statusu i overlay. 3 testy z nową asercją przeszły; Ruff: sukces. Pokrycie znaków nie dowodzi poprawności całego rasteryzowania i nie zastępuje oględzin pełnej sekwencji; przyczyna wcześniejszej usterki pozostaje nieustalona.

## Rzeczywisty adapter i symulator VISA

Nowy test test_field_series_with_real_adapter_and_visa_simulator używa KeithleyAdapter, SimulatedVisaFactory (5000 ohm), DeviceController, rezerwacji RunDeviceAdapter i FieldSeriesWorker w osobnym wątku. Najpierw konfiguruje oba kanały przy OFF, następnie wykonuje listę B=0,10uA,0,5uA. Zweryfikowano completed/skipped_field_compliance/completed/completed, trzy punkty i R=5000 ohm każdej wykonanej krzywej, końcowe potwierdzenia OFF oraz przywrócenie oryginalnych polityk. Zgoda output enable dotyczy wyłącznie profilu testu w pamięci i fabryki symulatora; żaden plik ustawień operatora nie został zmieniony. Cały field_worker: 22 passed; Ruff: sukces. To domyka nominalną integrację adaptera z symulatorem i compliance B, ale jeszcze nie zamknięcie pełnego okna podczas tej akwizycji ani pełną macierz usterek VISA.

## E-stop i częściowa krzywa przez rzeczywisty adapter

Rozszerzenie testu rzeczywistego adaptera/symulatora o stop i emergency_off po pierwszym sample_point ujawniło brak zwykłej częściowej krzywej po E-stop: odczyt był trwały w dzienniku, lecz ogólny SafetyViolation z kolejki przerywał zwrot datasetu. Wprowadzono domenowy RunInterrupted (podklasa SafetyViolation) dla jawnego przerwania. Runner przechwytuje wyłącznie ten typ, ustawia cancelled i zwraca dotychczasowe punkty po wykonaniu dotychczasowego shutdown. Błędy urządzenia i brak potwierdzenia OFF nadal propagują. Lokalna anulacja field runnera dziedziczy ten sam typ, aby anulacja przy odczycie B zachowywała wcześniej ukończone punkty.

Test rzeczywistego adaptera+VISA+kontrolera sprawdza nominalną serię oraz stop/E-stop: oba OFF, poprzednie polityki przywrócone, dokładnie jeden punkt częściowej krzywej i brak datasetów kolejnych pozycji po przerwaniu. 44 testy field_series/worker/lease i 11 dotychczasowego runnera przeszły. Ruff: sukces. Nie jest to jeszcze test zamknięcia pełnego MainWindow podczas rzeczywistej akwizycji.

## Rozróżnienie anulowania od błędu

Nowy test runnera w trzech wariantach zgłasza RunInterrupted, DeviceError lub SafetyViolation po pierwszym punkcie, równocześnie ustawiając flagę cancel. Tylko RunInterrupted zwraca częściowy dataset cancelled. Pozostałe wyjątki propagują mimo ustawionej flagi; każdy wariant wykonuje OFF i nie zadaje kolejnego poziomu. Wszystkie 14 testów pojedynczego runnera przeszło; Ruff: sukces. Ten test chroni przed przyszłym zbyt szerokim przechwytywaniem błędów jako anulowania.

## Zmiana operatora: lista albo przedział prądów B, bez tolerancji

Wykonano osobne, uzgodnione uproszczenie: FieldSeriesPanel ma wybór Current list / Current interval. Przedział przyjmuje początek i koniec z jednostką prądu oraz liczbę punktów obejmującą oba końce. -10 mA do +10 mA, 5 punktów daje -10,-5,0,+5,+10 mA. Obsługuje kierunek malejący, pozostawia zero B, zachowuje nieaktywną listę i zapisuje oba zestawy wejść po restarcie. Modal i runner dostają tę samą rozwiniętą listę SI. Nie ma konwersji na pole magnetyczne. Pełna lista podlega istniejącej walidacji limitów B przed mutacjami.

Usunięto z nowego formularza Stable readings, B absolute tolerance oraz B relative tolerance. Nowe serie zapisują verify_current_stability=false: po czasie oczekiwania wykonywany jest odczyt B i sprawdzenie compliance/wyjścia oraz poprawności liczb; monitoring B przed/po punktach A pozostaje. Nie stosuje się dodatkowego porównania odczytu z tolerancją. Historyczny domyślny tryb konfiguracji pozostaje true dla zgodności starych serii, a zapisany stan starego formularza migruje do listy bez przywracania usuniętych kontrolek. Raport zbiorczy PDF jawnie opisuje brak dodatkowego kryterium stabilności. Parametry rampy i maksymalnego czasu nie zostały w tej zmianie usunięte.

Weryfikacja: 77 testów panel/series/worker/scenario/drafts/reader/reports/summary przeszło; dodatkowe testy restartu przedziału i tekstu rzeczywistego PDF również przeszły. Ruff app tests: sukces. Obejrzano render scratch/pytest-b-panel-a/test_list_and_interval_share_v0/b_interval.png. Brak sterowania fizycznymi urządzeniami. Ten zakres jest ukończony; nie stanowi zamknięcia wcześniejszego pełnego audytu.

## Automatyczne zastosowanie konfiguracji przed charakteryzacją

Na prośbę operatora usunięto wymóg wcześniejszego ręcznego Apply settings. Runner buduje kompletne żądanie z konfiguracji zwykłej karty, przekazuje je do configure_source (OFF i weryfikacja readbacku w adapterze), porównuje zwrócone parametry ze świeżym żądaniem i dopiero potem dopuszcza ON. Nie porównuje już ze starym last_source_request. W serii A/B obie konfiguracje są stosowane i sprawdzane przy obu wyjściach OFF przed pierwszym ON B; konfiguracja A wykorzystuje pierwszy niezerowy punkt. Preflight i modal nie wykonują konfiguracji sprzętu, a modal opisuje późniejsze automatyczne zastosowanie nastaw. Brak połączenia, ON w preflight, niezgodny readback i błędy adaptera nadal blokują start. Zgoda na tymczasową politykę STOP i przywracanie poprzednich polityk pozostają.

Test adaptera/symulatora potwierdza brak last_source_request na obu kanałach po świeżym połączeniu, a następnie skuteczny start i ścieżki stop/E-stop. Testy ujemne wstrzykują niezgodny sense B i NPLC A i potwierdzają brak output ON. 61 testów runner/field_series/worker/scenario oraz 5 testów UI wspólnej konfiguracji, zgody, OFF i przywrócenia polityki przeszło. Ruff app tests: sukces. Zmiana zweryfikowana offline, bez fizycznego sprzętu.

## Nazwy raportów rozróżniają kolejne pomiary

Dodano wspólny resolver report_paths.py. Nowe PDF-y pojedynczych charakterystyk zawierają nazwę katalogu pomiaru (datę/czas); PDF-y pozycji serii zawierają identyfikator serii oraz pozycji B. Raport zbiorczy również zawiera identyfikator serii. Otwieranie z karty i Samples używa wspólnego resolvera; stare stałe nazwy pozostają czytelne jako fallback. Każdy nowy pomiar już miał osobny katalog; dodano atomowy przydział kolejnego katalogu pojedynczego pomiaru z sufiksem _002, _003 przy kolizji. CSV pozostaje characterization.csv we własnym katalogu pomiaru, zachowując kontrakt czytników.

Nowy test wykonuje dwa rzeczywiste zapisy CSV/PDF dla tej samej próbki i wymuszonej identycznej proponowanej ścieżki: nowe katalogi i różne nazwy PDF, poprzednie bajty PDF/CSV niezmienione. Ponowne generowanie raportu tego samego zapisanego pomiaru aktualizuje jego własny raport; nie tworzy nowego pomiaru. Żadne sterowanie urządzeniem nie zostało zmienione.
Weryfikacja nazw raportów: 33 testy report_names/field_reports/field_catalogue/field_summary/observations/field_viewer przeszły w 51.92 s; Ruff app tests: sukces. Testy obejmują nowe linki summary, zgodność starych nazw i aktualizację PDF po adnotacji.

### Ponowne uruchomienie po compliance (2026-09-08)

Przyczyna: poprawne zatrzymanie i OUTPUT OFF pozostawiały zatrzaśnięty stan
compliance w adapterze. Nowy sweep konfigurował pierwszy punkt, lecz blokada
odrzucała późniejsze OUTPUT ON.

Przy przygotowaniu nowego sweepu program potwierdza OUTPUT OFF, wykonuje
istniejące recover_from_compliance(channel, "keep_off"), sprawdza potwierdzenie
odzyskania i ponownie potwierdza OFF. Dopiero potem stosuje i weryfikuje pełną
konfigurację pierwszego punktu. Nie odtwarza poprzedniego prądu ani nie usuwa
historycznego wyniku compliance. Seria przygotowuje w ten sposób oba kanały
przed włączeniem B. Brak potwierdzenia przerywa start; zabezpieczenie adaptera
przed bezpośrednim włączeniem zatrzaśniętego kanału pozostaje aktywne.

Walidacja: 56 testów runnera, serii i workera zakończonych powodzeniem, w tym
dwa kolejne sweepy rzeczywistego adaptera z symulatorem VISA, oba zatrzymane
na compliance i oba zaczynające od pierwszego punktu, oraz odrzucenie startu
przy niepotwierdzonym odzyskaniu. Ruff: bez błędów. Bez testu na sprzęcie.
