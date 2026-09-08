# Charakterystyka MTJ przy różnych prądach field line

Data: 2026-09-08. Status: plan wdrożenia po przeglądzie kodu, bez zmian sterowania sprzętem.

## 1. Cel i zakres pierwszej wersji

Kanał A mierzy I–V MTJ. Kanał B zasila field line stałym prądem dla każdej krzywej. Operator wprowadza uporządkowaną listę prądów B; każda pozycja otrzymuje własny CSV i PDF, a cała seria raport porównawczy. Przykładowe wartości w UI nie mogą być traktowane jako bezpieczne domyślne prądy.

Lista zachowuje kolejność, znak, powtórzenia i zero. Powtórzenie tego samego prądu może oznaczać inną historię magnetyczną. Nie sortujemy ani nie deduplikujemy listy. Zero na B jest prawidłową nastawą referencyjną; dotychczasowe pomijanie zerowego punktu sweepu A pozostaje ograniczone do osi A. Zerowy prąd field line nie dowodzi zerowego pola ani rozmagnesowania MTJ.

Wersja pierwsza używa jednej wspólnej siatki prądów A, ograniczonej ręcznie przez operatora. Nie zwiększa automatycznie zakresu ani limitów na podstawie zmierzonej rezystancji. W trybie field line role A=MTJ i B=field line są jednoznaczne i nie mogą wskazywać tego samego kanału. Zwykła charakterystyka jednokanałowa pozostaje dostępna.

## 2. Fizyka i poprawna interpretacja

### Mierzymy prąd field line; pole wymaga kalibracji

Bez kalibracji wyniki nazywamy V(I_A; I_B), R(I_A; I_B), R0(I_B), a oś B podajemy w amperach. Prąd B jest mierzony przez SMU; lokalne pole przy MTJ nie jest przez to zmierzone. Opcjonalna kalibracja składowej indukcji B_field = k * I_B + B_offset wymaga zakresu ważności, orientacji, geometrii, daty i niepewności. Rozróżniamy H [A/m] i B_field [T]; symbol kanału B nie może być mylony z indukcją. Nie zgadujemy przelicznika z nazwy próbki.

### Co wyznaczamy

| Wynik | Interpretacja i warunki |
|---|---|
| V(I_A), R=V_A/I_A | Osobna krzywa dla każdego I_B, z rzeczywistych pomiarów A |
| R0(I_B), RA(I_B) | Nachylenie przy małym biasie, wspólne jawne okno dopasowania; RA wymaga znanej powierzchni |
| MR względem referencji | 100% * (R-R_ref)/R_ref przy tym samym biasie i wskazanym przebiegu referencyjnym |
| TMR | 100% * (R_AP-R_P)/R_P tylko po uzasadnionym oznaczeniu stanów P/AP; nie utożsamiamy automatycznie skrajnych R z P/AP |
| dV/dI, dI/dV | Nieliniowość z jawną metodą różniczkowania i oceną jakości |
| Osiągnięcie compliance | Osobno ostatni punkt bez compliance, pierwszy punkt z compliance, prąd zadany i zmierzony |
| Histereza | Porównanie gałęzi rosnącej i malejącej I_B, z zachowaniem historii; progi pola dopiero po kalibracji |
| Powtarzalność i dryft | Powtórzone punkty odniesienia oraz pomiary przed/po serii |

TMR zależy również od biasu, dlatego porównanie przy wspólnym napięciu wymaga interpolacji wyłącznie w wspólnym zmierzonym zakresie, osobno dla gałęzi. Nie ekstrapolujemy brakujących punktów za compliance. Źródło: [Kalitsov i in., bias dependence of TMR](https://arxiv.org/abs/1309.4357).

Przy w przybliżeniu omowej próbce I_compliance ≈ V_limit/R. Dla 670 mV i 3000 Ω daje to około 223 µA. Jest to oszacowanie miejsca ograniczenia napięciowego, nie dopuszczalny prąd próbki, próg przebicia ani próg przełączania magnetycznego. Przy mniejszej R prąd osiągnięcia compliance rośnie, ale operatorowy limit prądu A nadal obowiązuje. Jeśli sweep zakończył się na swoim końcu, raport mówi „compliance nie osiągnięto w badanym zakresie”, a nie podaje tego końca jako progu.

Field line grzeje układ: P_B = V_B * I_B, w przybliżeniu I_B²R_line. Zmiana R MTJ może pochodzić zarówno od pola, jak i temperatury, a gradient temperatury może wytwarzać offset napięciowy. Dlatego zapisujemy moc B, czas utrzymania i opóźnienia. Pomiary +I_B/-I_B oraz powtórzenia odniesienia pomagają diagnozować te efekty, lecz same nie rozdzielają ich jednoznacznie. Źródło: [Liebing i in., tunneling magneto thermopower](https://arxiv.org/abs/1104.0537).

Sam sweep A może zmieniać stan magnetyczny. Histereza uzyskana z pełnych I–V nie jest automatycznie czystą pętlą R(H) przy nieinwazyjnym odczycie. Taką pętlę warto dodać później jako osobny protokół małego biasu. Statyczna charakterystyka nie dowodzi także sprawności energy harvesting ani osiągów RF.

## 3. Stan obecny i miejsca rozszerzenia

- `app/devices/keithley_2600/characterization/models.py`: konfiguracja, punkt i dataset opisują obecnie pojedynczy kanał. Dodać typowane modele serii, pozycji field line i obserwacji B; nie zmieniać znaczenia istniejących kolumn A.
- `characterization/runner.py`: runner wymaga polityki stop, sprawdza zgodność z ręcznie zastosowaną konfiguracją i wyłącza kanał po sweepie. Dodać nadrzędny runner serii oraz jawny hook kontroli B przed i po punkcie A. Nie uruchamiać dwóch niezależnych workerów na jednej sesji VISA.
- `ui/characterization_card.py` i `ui/page.py`: rozszerzyć aktualną transakcję tymczasowej polityki o oba kanały, wspólną blokadę i potwierdzenie operatora.
- `characterization/analyzer.py`: rozszerzyć analizę o porównania serii. Przed tym poprawić brak kwalifikacji części wyników: obecnie R0 może korzystać z punktów compliance przy niedoborze danych, a różniczkowanie dostaje wszystkie punkty. Brak poprawnych danych ma dawać „niewyznaczono”, nie pozorny wynik fizyczny.
- `characterization/export.py`, `report_pdf.py`, `app/storage/characterization_csv_reader.py`: wersjonowane metadane serii, odczyt starszych plików, nowe pola z jednostkami.
- Wykorzystać istniejący resolver katalogu próbki i rejestr Measurements. Nie budować drugiego systemu ścieżek.

## 4. Kontrakt bezpieczeństwa

1. Przed startem: tożsamość konkretnego dwukanałowego modelu, dostępność obu kanałów, oba OUTPUT OFF potwierdzone odczytem, ręcznie zastosowane i zweryfikowane konfiguracje A/B, walidacja całej listy i wszystkich kroków rampy.
2. Zakresy source/measure, compliance liczbowe, sense, NPLC i pozostałe nastawy pochodzą z normalnych kart danego kanału. B musi być źródłem prądu. Nowe parametry protokołu: lista B, rampa B, czas stabilizacji, tolerancja stabilności i limit czasu utrzymania.
3. Oba kanały mają własne limity laboratoryjne i DUT/field line. Nie używać limitu MTJ dla przewodu field line. Sprawdzić także ograniczenia pracy dwóch kanałów i obciążenia indukcyjnego dla rozpoznanego modelu i połączeń. Punktem odniesienia jest [instrukcja Keithley 2600B](https://download.tek.com/manual/2600BS-901-01_C_Aug_2016_2.pdf); zgodność konkretnego urządzenia wymaga weryfikacji jego modelu.
4. Jeden modal opisuje role, kolejność, zakresy, tymczasowe stop dla obu kanałów i zachowanie po compliance. Potwierdzenie obejmuje całą serię; anulowanie nie włącza wyjść.
5. `stop`/`warn_clamp` to polityka reakcji aplikacji w adapterze, nie osobny tryb sprzętowy odczytywany z Keithleya. Jej potwierdzenie dotyczy stanu adaptera. Napięciowy/prądowy limit sprzętowy i OUTPUT wymagają rzeczywistego odczytu instrumentu.
6. Jeden właściciel obu kanałów na czas serii: blokada ręcznego sterowania, innych sweepów i zapisów konfiguracji. Live korzysta z pomiarów runnera lub jest jawnie wstrzymane z poprawnym stanem UI; E-STOP pozostaje dostępny.
7. Każda zmiana B odbywa się przy potwierdzonym A OFF. B zmienia wyłącznie poziom w zatwierdzonej rampie; inne nastawy konfigurowane tylko przy OFF. Rampa i oczekiwania są przerywalne, mają skończone kroki i deadline.
8. B mierzymy przed i po punkcie A; każdy odczyt ma własny timestamp. Nie deklarujemy jednoczesności odczytów A/B. Compliance B, dryft poza tolerancją, brak świeżego odczytu lub utrata OUTPUT B kończą serię i unieważniają zależny punkt A.
9. Domyślnie compliance A kończy całą serię i wyłącza oba kanały. Osobna, jawnie wybrana przed startem opcja „zakończ krzywą i przejdź do następnego pola” może pozwalać na dalszy przebieg tylko po potwierdzeniu A OFF, zapisaniu częściowych danych, wymaganym recovery compliance i ponownym preflight. Bez zweryfikowanej ścieżki recovery opcja niedostępna. Compliance B zawsze kończy serię.
10. Błąd komunikacji, zapisu danych, anulowanie i E-STOP: próbować wyłączyć oba kanały niezależnie, nawet jeżeli pierwsza próba zawiedzie. Nie czekać na PDF. Niepotwierdzone OFF oznacza FAULT/UNKNOWN, nigdy sukces. Ścieżka awaryjna nie może zależeć od zakończenia normalnej rampy.
11. Polityki sprzed serii przywracamy po potwierdzeniu obu OFF; nie przywracamy wcześniejszego stanu OUTPUT ON. Błąd przywracania blokuje nowy start i udostępnia kontrolowane ponowienie.
12. Watchdog/utrata procesu: określić rzeczywistą możliwość wyłączenia obu wyjść po zawieszeniu aplikacji. Sam `finally` nie zapewnia ochrony po zabiciu procesu; wynik kwalifikacji zapisać przed dopuszczeniem automatycznej pracy.

## 5. Sekwencja wykonania

Preflight → modal → rezerwacja A/B i snapshot → potwierdzenie polityk stop → konfiguracja przy OFF → ustawienie B rampą → stabilizacja i pomiary B → sweep A z kontrolą B → A OFF → utrwalenie krzywej → następna pozycja B lub zakończenie → oba OFF → przywrócenie polityk → raport zbiorczy.

Normalnie B pozostaje aktywny między krzywymi i przechodzi rampą bezpośrednio do kolejnej nastawy. Pozwala to zachować zadaną ścieżkę pola. Nie wstawiamy ukrytego powrotu B do zera pomiędzy pozycjami. Zerowanie, pauza chłodząca i restart tworzą jawne zdarzenia historii. Awaria ma pierwszeństwo nad zachowaniem tej historii.

PDF generujemy w tle z utrwalonych danych, z ograniczoną kolejką; nie utrzymujemy field line dłużej tylko z powodu renderowania. Niepowodzenie PDF jest oznaczonym błędem artefaktu, który można ponowić z danych. Niepowodzenie zapisu surowych danych zatrzymuje pomiar.

## 6. UI i zapis

Sekcja „Field line — Channel B”: lista wartości z jednostkami, generator start/stop/krok, ręczna kolejność, powtórzenia, rampa i stabilizacja, podgląd odziedziczonych nastaw B. Przed startem podsumowanie liczby krzywych i szacowanego czasu ekspozycji. Po starcie widoczne: numer krzywej, zadany i zmierzony I_B, V_B, moc B, stan obu wyjść i przyczyna zakończenia.

Wykresy V–I i R przełączają się na tym samym dataset. Selekcja krzywej, nakładanie krzywych, kolor według I_B, osobne oznaczenie gałęzi/powtórzeń i punktów compliance. Zbiorczo R0(I_B), MR(I_B), moc oraz zakres uzyskanych prądów A. Mapa R(V,I_B) tylko przy wystarczających danych, z widocznymi brakami.

Proponowana struktura pod istniejącym katalogiem konkretnej próbki i komórki:

```text
<sample>/measurements/Keithley_2600/characterization/<cell>/
  <UTC_timestamp>_<series_id>/
    series.json
    summary.csv
    series_report.pdf
    001_Ib_<signed_value>/
      characterization.csv
      characterization_report.pdf
    002_Ib_<signed_value>/
      characterization.csv
      characterization_report.pdf
```

Indeks pozycji zapewnia unikalność przy powtórzonym I_B. Folder jest etykietą; dokładna wartość SI pochodzi z metadanych. Resolver zachowuje ręcznie wybrany katalog próbki. Cała seria i krzywe pojawiają się automatycznie w Measurements.

`series.json`: wersja schematu, sample/cell ID, lista w kolejności, pełne snapshoty A/B, identyfikacja urządzenia, wersja aplikacji, polityki pierwotne/efektywne, kalibracja, czasy UTC, statusy i przyczyny zakończenia. Każdy punkt: dotychczasowe A oraz zadane B, zmierzone I_B/V_B/P_B, compliance B, timestampy odczytów i ważność punktu. Dane B nie mogą istnieć wyłącznie jako nagłówek CSV.

Zapis punktowy/checkpoint przed kolejnym krokiem, atomowe aktualizacje manifestu, zachowanie danych częściowych po błędzie. Statusy completed/cancelled/stopped_on_compliance/fault odrębne od statusu generowania PDF. Po restarcie odczyt częściowej serii; brak samoczynnego wznowienia wyjść. W pierwszej wersji ponowienie jako nowy segment historii, nie ciche kontynuowanie pętli histerezy.

## 7. Etapy i kryteria odbioru

1. **Modele i plan:** typowana seria, parser SI, walidacja, snapshoty, zerowy B i powtórzenia. Ustalenie modelu urządzenia, granic field line i kalibracji; brak kalibracji nie blokuje pomiaru w funkcji I_B.
2. **Runner i bezpieczeństwo:** wyłączna własność A/B, nadzór B, przerywalne rampy, transakcja polityk, shutdown obu kanałów. Testy wstrzykują awarie przed/po każdej mutacji, compliance A/B, anulowanie, timeout, błędny readback, zapis i nieskuteczne OFF.
3. **Dane:** częściowy zapis, manifest, CSV, zgodność starszego czytnika, rejestracja w Samples, odporność na kolizje nazw i ponowienie raportów bez sprzętu.
4. **Analiza i UI:** wspólne okno R0, odrzucenie compliance i nieważnych punktów z fitów, brak ekstrapolacji, poprawne etykiety MR/TMR. Testy syntetyczne obejmują znane R, offset, histerezę, różne długości krzywych i wszystkie punkty w compliance. Renderowanie normalnego i wąskiego okna po show()/obsłudze zdarzeń.
5. **Kwalifikacja:** regresje charakterystyki jednokanałowej, testy jednostek, adapterów, runnera, storage i UI oraz `ruff check app tests`. Następnie kontrolowany test dwóch kanałów na odpowiednich obciążeniach zastępczych, pomiar ramp/przejść, compliance i E-STOP. Test na MTJ dopiero po zaakceptowanym wyniku tej kwalifikacji.

Odbiór oznacza: N pozycji daje N rozróżnialnych datasetów i raportów (albo jawne przerwanie z zachowanymi danymi); kolejność pola jest wierna planowi; każdy punkt ma kontekst B; żaden limit nie jest automatycznie zwiększany; wszystkie ścieżki zakończenia próbują wyłączyć oba kanały; wyników symulacji nie przedstawiamy jako gwarancji zachowania rzeczywistego stanowiska.

## 8. Pełny scenariusz pracy operatora

### 8.1. Przygotowanie próbki i zwykłych kart Keithleya

Operator wybiera próbkę i komórkę w Samples. Aplikacja pokazuje ścieżkę docelową, identyfikator komórki i jej status. Historycznego statusu „burned” nie można ignorować ani automatycznie zmieniać na „good” po udanym pomiarze. Przed pomiarem operator rozstrzyga, czy wybrano właściwą komórkę; reguła dopuszczenia wynika z polityki próbki.

Operator potwierdza fizyczne połączenia: A mierzy MTJ, B zasila przewód field line. Sprawdzamy schemat połączeń i wspólne masy — dwie logiczne role nie dowodzą elektrycznej niezależności obwodów. W kartach A/B operator ustawia i ręcznie weryfikuje właściwe zakresy, wiring i compliance. Dla tego laboratorium nie wprowadzamy automatycznie 4-wire. Udany odczyt konfiguracji nie dowodzi poprawności fizycznego okablowania.

Próba ręczna potwierdza zachowanie dla rzeczywiście sprawdzonych nastaw, nie kwalifikuje automatycznie wszystkich większych prądów z przyszłej listy. Limity field line, jego moc i czas ekspozycji muszą mieć osobne uzasadnienie. Po próbie oba wyjścia zostają wyłączone i odczytane.

### 8.2. Zdefiniowanie serii

W charakterystyce operator wybiera „Seria z field line”. Wskazuje:

1. wspólny sweep A: start, stop, liczbę punktów i kierunek;
2. listę prądów B oraz etykiety gałęzi/powtórzeń;
3. parametry rampy i stabilizacji B;
4. zachowanie serii po compliance A;
5. opcjonalny przebieg referencyjny i kalibrację pola;
6. metadane: temperatura, orientacja pola, notatka o przygotowaniu magnetycznym.

UI rozwija generator do konkretnej tabeli wykonania. Pokazuje liczbę rzeczywistych punktów A po pominięciu zera. Lista B może zawierać zero wielokrotnie. Wspólny sweep A nie oznacza równej liczby uzyskanych punktów — część krzywych może skończyć się wcześniej na compliance.

W pierwszej wersji nie wprowadzamy „inteligentnego” zwiększania I_A do wyliczonego V_limit/R. Późniejszy algorytm zagęszczania punktów musiałby dostać odrębny kontrakt, zachować operatorowy limit i jednoznacznie zapisywać adaptacyjną siatkę.

### 8.3. Podgląd i zgoda na start

Podsumowanie zawiera tabelę pozycji B, pełne odziedziczone nastawy obu kanałów, granice ekspozycji i katalog wyników. Modal mówi, które polityki aplikacji zostaną czasowo zmienione na stop oraz czy compliance A kończy serię, czy tylko bieżącą krzywą. Nigdy nie sugeruje, że rosnący prąd do compliance jest automatycznie bezpieczny dla próbki.

Po zatwierdzeniu następuje ponowna kontrola aktualności snapshotów pod wyłączną rezerwacją A/B. Zmiana ustawień, urządzenia lub próbki od chwili podglądu unieważnia zatwierdzenie. Zapobiega to uruchomieniu planu innego niż pokazany w modalu. Tworzymy manifest serii ze statusem przygotowania przed pierwszym włączeniem.

### 8.4. Jedna pozycja field line

1. Potwierdzić A OFF. Przy pierwszej pozycji skonfigurować B przy B OFF i potwierdzić konfigurację; załączyć zgodnie z zatwierdzoną sekwencją.
2. Przejść rampą do I_B. Zapisać początek, koniec i istotne zdarzenia rampy. Przed zmianą znaku nie przełączać ukrycie zakresów lub trybu.
3. Sprawdzić stabilność B. Warunek powinien obejmować błąd względem nastawy i rozrzut kilku kolejnych odczytów, np. |I_meas-I_set| ≤ max(tolerancja_bezwzględna, tolerancja_względna*|I_set|), a także brak compliance. Liczba odczytów, tolerancje i timeout są częścią planu. Przy zerze nie dzielimy przez I_set.
4. Upływ minimalnego czasu stabilizacji i spełnienie kryterium elektrycznego pozwalają rozpocząć A. Nie nazywamy tego dowodem stabilizacji temperatury lub namagnesowania. Bez pomiaru temperatury znamy jedynie czas oczekiwania i stabilność elektryczną.
5. Skonfigurować pierwszy punkt A przy A OFF; potwierdzić zgodność wszystkich nastaw poza dozwoloną zmianą poziomu i polityki. Kontrola wspólnej konfiguracji nie może przypadkowo wyłączyć B.
6. Dla każdego punktu: kontrola B → ustawienie A → przerywalne oczekiwanie → pomiar A → kontrola B → ocena ważności → trwały zapis → aktualizacja wykresów.
7. Przy compliance A zapisać punkt ze znacznikiem, bez wysyłania następnej wyższej nastawy. Zatrzymać krzywą i potwierdzić A OFF. O dalszej serii decyduje uprzednio zatwierdzona polityka.
8. Zamknąć dataset i zgłosić go do Measurements; PDF korzysta z tego samego utrwalonego zestawu danych. Dopiero po trwałym zapisie wolno rozpocząć następną pozycję.

Jeśli odczyt B po punkcie A jest nieważny, zachowujemy surowe odczyty i oznaczamy punkt A jako „warunki pola niepotwierdzone”. Nie usuwamy go bez śladu, ale wykluczamy z dopasowań. Kontrola przed/po nie gwarantuje braku krótkiego zaburzenia między odczytami; szybsza kontrola wymaga odrębnej kwalifikacji sprzętowej.

### 8.5. Koniec, anulowanie i przegląd

Po ostatniej krzywej A jest OFF, B wraca zatwierdzoną rampą do zera i jest wyłączany; odczyt potwierdza oba OFF. Dopiero potem przywracamy poprzednie polityki aplikacji. Rampy końcowe również należą do historii magnetycznej, lecz nie dopisują fikcyjnych krzywych pomiarowych.

„Zatrzymaj serię” przerywa wykonanie bez czekania na koniec pełnej krzywej. E-STOP korzysta z istniejącej ścieżki awaryjnej. Długi zapis lub generowanie PDF nie może opóźniać wyłączenia. W przypadku błędu jednego kanału nadal próbujemy wyłączyć drugi. Jeśli shutdown zmienił stan magnetyczny, wznowiony pomiar jest nowym segmentem.

Po zakończeniu operator wybiera dowolną krzywą i płynnie przełącza V–I/R, nakłada przebiegi, otwiera raport indywidualny albo zbiorczy. Wpis częściowy lub zakończony compliance pozostaje widoczny i opisany właściwym statusem. „Pomiar zakończony” i „PDF wygenerowany” to dwa odrębne komunikaty.

## 9. Hipotetyczny przykład liczbowy — cztery charakterystyki

### 9.1. Założenia wyłącznie do demonstracji

Poniższe liczby są syntetyczne. Nie są rzeczywistymi wynikami tej próbki, rekomendacją nastaw ani dowodem bezpiecznej ekspozycji. Pomijamy szum, offset, nieliniowość i dynamikę ogranicznika, aby pokazać obliczenia. W rzeczywistym raporcie podstawą są odczyty instrumentu.

| Parametr | Założenie przykładu |
|---|---|
| Próbka/komórka | DemoMTJ / R1C1 |
| Sweep A | 1 µA → 293 µA, 101 punktów, krok 2,92 µA |
| Compliance napięciowe A | +670 mV dla dodatniego sweepu |
| Operatorowa górna granica I_A | 293 µA w tym przykładowym planie |
| Lista B | 0 mA, +5 mA, +10 mA, +15 mA |
| R_line w modelu | 20 Ω, stałe |
| Pole | Brak kalibracji; osie tylko I_B |
| Zachowanie po compliance A | Jawnie zatwierdzona kontynuacja do następnej pozycji po recovery |
| Zależność MTJ w każdej krzywej | Idealnie omowa, R z tabeli poniżej |

Przykład czterech wykonanych krzywych zakłada wdrożoną i przetestowaną opcję recovery z sekcji 4.9. Przy domyślnym „zatrzymaj całą serię” pomiar kończy się już na pierwszej krzywej i pozostałe trzy pozycje mają status „niewykonane”. Dokumentacja i UI muszą jasno rozróżniać te scenariusze.

### 9.2. Przewidywane wyniki modelu

| I_B | R0 MTJ | V_A przy 100 µA | Szacowane V_limit/R | Ostatni zadany punkt bez compliance | Pierwsza zadana nastawa z compliance | Zakończenie |
|---|---:|---:|---:|---:|---:|---|
| 0 mA | 3000 Ω | 300 mV | 223,33 µA | 222,92 µA | 225,84 µA | Compliance A |
| +5 mA | 2400 Ω | 240 mV | 279,17 µA | 278,40 µA | 281,32 µA | Compliance A |
| +10 mA | 1800 Ω | 180 mV | 372,22 µA | 293,00 µA | Nie osiągnięto | Koniec planu |
| +15 mA | 1200 Ω | 120 mV | 558,33 µA | 293,00 µA | Nie osiągnięto | Koniec planu |

Wartości przy 100 µA są oceną idealnego modelu/interpolacją, ponieważ 100 µA nie jest punktem tej siatki. Na prawdziwych danych raport oznacza interpolację.

W pierwszej krzywej aplikacja zadaje 225,84 µA, ale idealny ogranicznik przy 670 mV i 3000 Ω pozwala uzyskać około 223,33 µA. Zachowujemy obie liczby. Obliczenie R z 670 mV / 225,84 µA dawałoby około 2966,7 Ω, czyli pozorny spadek rezystancji. Obliczenie z rzeczywistego prądu daje 3000 Ω. Punkt compliance zachowujemy diagnostycznie, ale nie wykorzystujemy do dopasowania R0.

Liczba zapisanych punktów, licząc punkt compliance: odpowiednio 78, 97, 101 i 101. Tego nie należy traktować jako utraty danych w krótszych krzywych. W pierwszej następna nastawa 228,76 µA nie jest już wysyłana; w drugiej nie wysyłamy 284,24 µA.

Przy +10 mA napięcie końcowe wynosi 527,4 mV, a przy +15 mA — 351,6 mV. Osiągnięcie końca sweepu nie uprawnia do wysyłania wyższych prądów, chociaż obliczone V_limit/R jest większe niż 293 µA.

### 9.3. Co zobaczy operator podczas tej serii

- **Krzywa 1/4, I_B=0 mA:** V–I rośnie najbardziej stromo; R oscylowałoby wokół 3000 Ω przy obecności szumu. Po compliance A wykres zatrzymuje się, punkt graniczny dostaje znacznik, status mówi „zatrzymano krzywą na compliance”. B=0 mA nadal jest pełnoprawnym warunkiem pomiaru.
- **Przejście 1→2:** A OFF potwierdzone, zapis krzywej, kontrolowane recovery i rampa B do +5 mA. UI pokazuje, że field line jest aktywna mimo A OFF. Nie pokazuje globalnego „Outputs off”, gdy B pozostaje ON.
- **Krzywa 2/4:** mniejsze nachylenie, więcej punktów przed ograniczeniem napięciowym. Końcowy status nadal wskazuje compliance.
- **Krzywe 3/4 i 4/4:** dochodzą do 293 µA. Raport podaje „brak compliance w badanym zakresie”, bez ekstrapolowanego „zmierzonego progu”.
- **Koniec:** oba wyjścia OFF potwierdzone, cztery wpisy krzywych i jeden wpis serii, automatyczny raport zbiorczy.

### 9.4. Wnioski ilościowe z tego przykładu

**Wniosek 1: rezystancja zależy od warunku field line.** W modelu maleje z 3000 do 1200 Ω, czyli o 60% względem krzywej I_B=0. Jest to obserwacja zależności R od protokołu I_B. Bez kontroli temperatury i historii nie przypisujemy całych 60% wyłącznie efektowi magnetycznemu.

**Wniosek 2: zmienia się zakres prądów osiągalnych przed compliance napięciowym.** Ograniczenie występuje około 223,33 i 279,17 µA dla pierwszych dwóch krzywych. Dla pozostałych znamy tylko fakt braku ograniczenia do 293 µA. Nie zmierzyliśmy ich progów przy 372,22 i 558,33 µA — to przewidywania modelu omowego, niewykonywane przez algorytm.

**Wniosek 3: MR i TMR wymagają jasno wskazanego mianownika.** MR względem pierwszej krzywej to 0%, −20%, −40%, −60%. Jeżeli osobny protokół potwierdziłby R_AP=3000 Ω i R_P=1200 Ω przy tym samym biasie i porównywalnej temperaturze, TMR wynosiłoby 150%. Sam zestaw czterech liczb nie dowodzi stanów AP/P; w pierwszym raporcie pokazujemy MR względem referencji.

**Wniosek 4: pobór mocy zależy od sposobu porównania.** Przy wspólnym I_A=100 µA moc MTJ wynosi 30, 24, 18 i 12 µW. Przy wspólnym V_A=100 mV prądy modelu wynoszą 33,33; 41,67; 55,56; 83,33 µA, a moce 3,33; 4,17; 5,56; 8,33 µW. Zatem mniejsza R zmniejsza moc przy stałym prądzie, ale zwiększa ją przy stałym napięciu. Raport musi podawać, co było utrzymywane stałe.

**Wniosek 5: zasilanie field line może kosztować więcej energii niż bias MTJ.** Przy R_line=20 Ω moc B wynosi kolejno 0; 0,5; 2; 4,5 mW, a napięcie B 0; 0,1; 0,2; 0,3 V. Te liczby nie wyznaczają temperatury MTJ, ale uzasadniają zapis mocy i czasu oraz bilans energii. W raporcie energy harvesting trzeba oddzielić pobór B od ewentualnej energii uzyskanej na MTJ.

**Wniosek 6: te dane nie dowodzą nieliniowości ani prostowania.** Krzywe przykładu są omowe. Do określenia asymetrii potrzebne są porównywalne dodatnie i ujemne biasy. Do oceny rzeczywistego prostowania RF potrzebne byłyby m.in. pomiar sygnału wejściowego, częstotliwości, mocy dostarczonej i wyjściowego napięcia/mocy DC.

## 10. Drugi eksperyment: historia pola i histereza

Po serii rozpoznawczej można zaplanować gałąź rosnącą i malejącą, np. od −15 do +15 mA i z powrotem, wyłącznie we wcześniej zakwalifikowanym zakresie. Każda pozycja otrzymuje `branch_id`, `sequence_index` i `repeat_index`. „Powrót do 0 mA” nie jest resetem stanu.

Hipotetycznie przy I_B=+5 mA gałąź rosnąca daje R0=2400 Ω, a malejąca 1400 Ω. Raport wskazuje różnicę 1000 Ω przy tym samym zadanym I_B i różnej historii. To przesłanka histerezy; alternatywy obejmują dryft cieplny, wpływ poprzednich sweepów A i niestabilność próbki. Weryfikacja obejmuje ponowną pętlę, jednakowe czasy i odczyt małym biasem. Zależność rezystancji MTJ od konfiguracji magnetycznej oraz przełączanie prądem omawia [NIST: Magnetic Random Access Memory](https://www.nist.gov/programs-projects/magnetic-random-access-memory).

Jeżeli zmiana stanu nastąpiła pomiędzy 5 a 10 mA, podajemy przedział progu dla danej gałęzi. Nie raportujemy dokładnego progu 7,5 mA jako wartości zmierzonej. Zagęszczenie listy jest nowym planem; nie wprowadzamy po fakcie fikcyjnych punktów.

Przykładowa **fikcyjna** kalibracja k=0,2 mT/mA przekształciłaby przedział 5–10 mA w 1–2 mT przy zerowym offsecie. To przykład jednostek, nie kalibracja tego stanowiska. Bez kalibracji pozostaje przedział prądowy. Przy znanych obu progach i uzasadnionym modelu pętli można wyznaczyć jej szerokość i przesunięcie; nie każda wielostanowa/vortexowa krzywa ma jednoznaczne pole koercji.

Zmiana stanu podczas sweepu A przy stałym B jest osobnym kandydatem na zdarzenie przełączania zależne od biasu. Wymaga wykluczenia compliance, skoku zakresu, błędu styku i dryftu oraz sprawdzenia powtarzalności. Nie klasyfikujemy jej automatycznie jako spin-transfer torque; fizyka przełączania prądem jest opisana w [Ralph i Stiles, Spin Transfer Torques](https://tsapps.nist.gov/publication/get_pdf.cfm?pub_id=620024).

## 11. Jak oddzielać efekt pola, grzanie i zmianę próbki

| Obserwacja | Co wolno stwierdzić | Kontrola potrzebna do mocniejszego wniosku |
|---|---|---|
| R zmienia się wraz z I_B | Zależność od ustawienia field line w danym protokole | Powtórzenia, temperatura, historia, kalibracja |
| R różni się przy +I_B i −I_B | Asymetria względem znaku prądu | Jednakowa historia i ekspozycja; samo porównanie znaków nie usuwa histerezy |
| R dryfuje przy stałym I_B | Stan pomiaru nie jest czasowo stały | Rejestracja temperatury/czasu, mocy B, powtarzany mały bias |
| Napięcie ma offset przy małym I_A | V/I może być obciążone offsetem | Dopasowanie V=R0*I+V_offset; pomiary obu polaryzacji |
| R po serii nie wraca do początku | Zmiana stanu, dryft lub uszkodzenie są możliwe | Identyczne przygotowanie magnetyczne i temperatura, ponowny mały odczyt |
| R skokowo maleje i pozostaje niska | Trwała zmiana charakterystyki | Kontrola styku, leakage, powtarzalność; nie rozstrzygamy automatycznie przebicia |

Wnioski o ogrzewaniu muszą opierać się na dowodach termicznych, a nie samym I_B². Nawet część parzysta R(+I_B)+R(−I_B) nie jest automatycznie „czystym grzaniem”, ponieważ odpowiedź magnetyczna może również być parzysta. Zmiana offsetu cieplnego jest fizycznie możliwa w MTJ — patrz źródło Liebing i in. z sekcji 2.

Proponowane kolejne protokoły, poza minimalną pierwszą wersją: powtórzenia całej serii, jawne punkty referencyjne, mały odczyt przed i po każdym I–V, gałęzie dodatnie/ujemne A oraz sekwencje kontrolowanej ekspozycji/chłodzenia. Każdy dodatkowy odczyt jest fizyczną operacją zapisaną w planie, nie ukrytą czynnością analizy.

## 12. Dokładne zasady obliczeń i prezentacji jakości

1. **R statyczna:** V_meas/I_meas tylko dla wiarygodnego niezerowego prądu. Próg jakości zależy od zakresu, rozdzielczości i niepewności. Poniżej progu wartość niewyznaczona z przyczyną; nie zastępujemy I_meas prądem zadanym w kolumnie opisanej jako rezystancja zmierzona.
2. **R0:** fit V=R0*I+V_offset w jawnie ustalonym wspólnym oknie małego biasu. Raport podaje okno, liczbę punktów, błąd dopasowania i residuals. Co najmniej trzy poprawne różne punkty to minimum techniczne oceny fitu, a nie gwarancja jakości. Jeżeli tylko dodatni bias jest dostępny, oznaczamy wynik jako jednostronne oszacowanie w tym oknie.
3. **Brak zera A:** fit nie wymaga zmierzenia dokładnie I=0. Potrzebuje wystarczających danych blisko zera. Pominięcie zera nie uzasadnia ekstrapolacji z odległego zakresu ani ukrytego dodania zerowego punktu.
4. **Porównywalność:** krzywe o różnych długościach porównujemy tylko we wspólnym prawidłowym zakresie. Oddzielamy gałęzie, nie interpolujemy przez skok stanu lub lukę nieważnych danych. Zakres interpolacji i użyta metoda są zapisane.
5. **Pochodne:** dV/dI obliczamy osobno dla ciągłych gałęzi i stanów. Jawnie zapisujemy wygładzanie/okno; duży pik przy skoku lub końcu danych nie jest automatycznie rezonansem.
6. **Compliance:** punkt graniczny zostaje w CSV i na wykresie, lecz nie w fitach i porównaniach MR. „Nie osiągnięto” jest statusem, nie wartością zero. Szacowane V_limit/R ma odrębną kolumnę od zmierzonego zdarzenia.
7. **RA i 2-wire:** rezystancja obejmuje również wkład przewodów/styków, jeśli nie zastosowano uzasadnionej korekcji. Wtedy RA jest wartością pozorną układu pomiarowego, nie dowiedzionym RA samej bariery. Korekcja wymaga jawnego modelu i metadanych; nie włączamy 4-wire automatycznie.
8. **Parametry bariery:** dopasowanie modelu BDR w zmiennych stanach magnetycznych lub przy grzaniu może dawać parametry efektywne. Nie ogłaszamy zmiany fizycznej grubości bariery na podstawie samego fitu I–V. Pierwsza wersja raportu serii skupia się na obserwowalnych wielkościach i jakości dopasowań.
9. **Niepewność:** odróżniamy rozrzut powtórzeń, błąd dopasowania i niepewność systematyczną instrumentu/kalibracji. Nie pokazujemy arbitralnych ±0 lub nadmiernej liczby cyfr. Przy braku modelu niepewności podajemy to w metadanych wyniku.

## 13. Zawartość raportów i przykład komunikatów

### Raport pojedynczej krzywej

- Próbka, komórka, numer krzywej, gałąź, czas, identyfikator serii i dane urządzenia.
- Zadane I_B i zakres rzeczywistych odczytów B, napięcie/moc B, ekspozycja i kryterium stabilizacji; pole tylko przy kalibracji.
- Snapshot obu kanałów, jednostki, limity, sense i polityki pierwotne/efektywne.
- Wykres V–I oraz R z zaznaczeniem punktów odrzuconych/compliance; opcjonalne pochodne.
- R0, offset, jakość fitu, pozorne RA gdy właściwe, maksymalne zmierzone V/I/P oraz przyczyna zakończenia.
- Liczba punktów zaplanowanych, pominiętych zerowych A, poprawnych, nieważnych i granicznych.
- Link/relatywna ścieżka do CSV oraz status potwierdzenia OFF. PDF musi móc powstać później z utrwalonego snapshotu, bez aktualnych ustawień aplikacji.

### Raport całej serii

Tabela wszystkich pozycji, także niewykonanych; nakładki V–I/R; R0(I_B); MR względem jawnej referencji; gałęzie historii; wykres osiągniętego zakresu A z rozróżnieniem compliance/końca planu; moc B i czas. Kalibrowane pole może być dodatkową osią, ale prąd B pozostaje zachowany w danych.

Przykładowy automatyczny opis danych z sekcji 9:

> W czterech wykonanych krzywych oszacowano R0 od 3000 do 1200 Ω. Dla I_B=0 i +5 mA sweep zakończył się na compliance napięciowym. Dla +10 i +15 mA wykonano cały zaplanowany zakres do 293 µA; nie wyznaczono progu compliance. Zmiana R0 względem krzywej referencyjnej wyniosła do −60%. Brak kalibracji pola i danych temperatury ogranicza interpretację do zależności od prądu field line w zastosowanym protokole. Stanów P/AP nie przypisano.

Tekst powstaje z jawnych reguł i wartości jakościowych. Nie może dopisywać „próbka bezpieczna”, „bariera nieuszkodzona”, „wykazano TMR=150%” lub „osiągnięto próg przełączania”, jeżeli dane nie uzasadniają takiego stwierdzenia.

## 14. Szczegółowy podział wdrożenia i testów odbiorowych

| Pakiet | Konkretne zadanie | Dowód odbioru |
|---|---|---|
| Model serii | Niezmienny plan A/B, pozycje z ID, jednostki SI, statusy i kalibracja | Round-trip planu, znaki/zero/powtórzenia zachowane |
| Rezerwacja i snapshot | Jedna blokada obu kanałów, ponowna kontrola po modalu | Zmiana nastaw podczas modalu blokuje start; ręczne sterowanie nie konkuruje |
| Adapter | Zweryfikować niezależne konfiguracje A/B i zachowanie przy drugim kanale ON | Log komend dowodzi braku resetu/innej niezamówionej mutacji kanału B podczas konfiguracji A |
| Runner serii | Stany PREPARING, FIELD_RAMP, FIELD_SETTLING, SWEEP, CURVE_CLOSE, RECOVERY, STOPPING, terminalne | Test przejść dla każdej pozycji i obu polityk kontynuacji |
| Nadzór B | Dwa odczyty wokół punktu A, deadline, tolerancje i compliance | Awaria drugiego odczytu zapisuje nieważny punkt i wyłącza oba wyjścia |
| Shutdown | Niezależne próby OFF, potwierdzenie, przywrócenie polityk | Awaria OFF A nie pomija OFF B i odwrotnie; UNKNOWN pozostaje widoczne |
| Zapis | Trwałe punkty, atomowy manifest, częściowe serie, indeks Samples | Awaria/ponowne otwarcie zachowuje wszystkie zatwierdzone punkty; brak duplikacji |
| Analiza | Wspólne okno, statusy jakości, MR, interpolacja | Syntetyczny przykład z sekcji 9 reprodukuje liczby bez fikcyjnych progów |
| UI | Lista B, postęp, wybór krzywej, Live, dwa wyjścia, modal | Test kliknięć i geometrii po show(), normalna/wąska szerokość, stany błędu |
| Raporty | Indywidualne i zbiorcze, generowanie z danych | PDF/CSV zgodne; ponowienie PDF nie kontaktuje się ze sprzętem |

Dodatkowe przypadki obowiązkowe: B=0; duplikaty B; wartości ujemne i rampa przez zero; pusta lista; same zerowe punkty A po filtracji; NaN/inf; granice limitów; compliance na pierwszym punkcie A; brak stabilizacji B; anulowanie w czasie modal/rampa/settling/zapis/recovery; utrata połączenia po enable; zmiana aktywnej próbki; brak miejsca na dysku; niedostępny katalog; timeout PDF; próba restartu po niepotwierdzonym OFF.

Same zerowe punkty A muszą zostać odrzucone w preflight przed włączeniem B. Przekroczenie limitu ostatniej pozycji listy B również odrzuca całą serię przed pierwszą mutacją. Testy monitorują wszystkie komendy, aby wykryć ukryte ustawienia sense, autorange, limitów lub resetów.

W kalkulacji czasu uwzględniamy pomiary B przed/po każdym A, NPLC obu kanałów, autozero, opóźnienia komunikacji, rampy, zapis i potencjalne recovery. Iloczyn liczby punktów i dwell A jest tylko dolnym składnikiem czasu, nie pełną prognozą. Energia field line jest szacowana z całki zmierzonej mocy po czasie wraz z rampami; luki pomiarowe i metoda całkowania muszą być oznaczone.

## 15. Decyzje wymagające danych stanowiska przed implementacją sprzętową

- Dokładny model Keithleya, firmware i schemat A/B–MTJ–field line, w tym powroty prądowe.
- Operatorowe limity field line: prąd, napięcie, moc, czas ekspozycji, dopuszczalna rampa oraz warunki chłodzenia. Wartości przykładu nie uzupełniają tych pól.
- Czy dostępna jest kalibracja lokalnego pola i pomiar temperatury. Jeśli nie, pierwsza wersja uczciwie raportuje I_B i warunki elektryczne.
- Docelowy protokół magnetyczny: konkretna kolejność, przygotowanie stanu, kierunek pola i kryterium porównywalności powtórzeń. Początkowe zero field line nie oznacza znanego stanu MTJ.
- Kwalifikacja dalszej pracy po compliance A: czy zatwierdzona procedura recovery zachowuje potrzebną historię B. Jeśli wymaga wyłączenia B, dalszy przebieg oznaczamy nowym segmentem, a nie nieprzerwaną gałęzią histerezy.

Te dane są wymaganiami konfiguracji eksperymentu. Aplikacja nie może zgadywać ich na podstawie wcześniejszych screenshotów, nazw próbek ani hipotetycznych liczb w tym dokumencie.

## 16. Rzeczywista obserwacja operatora: anomalia R przy prądzie około 700–750 µA

### 16.1. Materiał i poziom dowodu

Operator udostępnił zrzut ekranu wykresu `characterization.csv`, z osią poziomą „Demanded Current (µA)” i pionową „True Resistance (kΩ)”, oraz informację o nieaktywnej field line. W chwili sporządzania tego uzupełnienia nie analizowano źródłowego CSV ani logu instrumentu. Wartości poniżej są przybliżonym odczytem obrazu, a nie wynikiem dopasowania do surowych danych. Nie znamy na tej podstawie dokładnej komórki, limitów tego przebiegu, stanu OUTPUT B ani jego rzeczywistego prądu.

Na przedstawionej dodatniej gałęzi rezystancja maleje w przybliżeniu z 1,09 kΩ do 0,77 kΩ. Na tle tego trendu w okolicy 720–750 µA widać bardziej stromy spadek z około 0,86 do 0,80 kΩ, rzędu 60 Ω (około 7% względem 0,86 kΩ). Mniejsze zmiany nachylenia występują też wcześniej; detekcja nie powinna z góry ograniczać się do jednego zdarzenia powyżej arbitralnego progu 700 µA.

Oś przedstawia prąd **zadany**. Dokładny przedział zdarzenia trzeba wyznaczyć również względem prądu **zmierzonego**. Etykieta „True Resistance” sama nie potwierdza sposobu obliczenia kolumny ani braku compliance. Trzeba sprawdzić V_A, I_A, I_demanded, znaczniki compliance, zakresy, timestampy i wzór eksportu.

### 16.2. Gotowy tekst do raportu

> **Obserwacja wymagająca dalszej weryfikacji.** Przy nieaktywnej field line według informacji operatora zaobserwowano lokalne zwiększenie stromości spadku rezystancji na tle jej ogólnego spadku z biasem. Na zrzucie ekranu zmiana obejmuje w przybliżeniu zakres zadanego prądu 720–750 µA i rezystancji 0,86–0,80 kΩ. Dokładnego progu i amplitudy zdarzenia nie wyznaczono z surowych danych. Możliwa jest zmiana konfiguracji magnetycznej lub wzbudzenie dynamiki magnetycznej, jednak statyczna charakterystyka R(I) nie rozstrzyga mechanizmu. Do wykluczenia pozostają m.in. efekty cieplne, nieliniowość transportu, ograniczenie compliance, zmiana zakresu pomiarowego i zmiana stanu elektrycznego próbki. Brak aktywnego zasilania field line nie jest dowodem zerowego pola przy MTJ.

Na wykresie raportu oznaczamy półprzezroczystym pasem „Obszar anomalii — ocena wizualna”. Nie umieszczamy pionowej linii podpisanej „próg dynamiki = 700 µA”. Po analizie danych przedział może zostać zmieniony i opisany metodą detekcji. Zachowujemy wcześniejszą adnotację oraz pochodzenie jej wartości.

### 16.3. Dlaczego hipoteza magnetyczna jest zasadna, ale niepotwierdzona

Prąd spinowo spolaryzowany może wzbudzać dynamikę magnetyczną i ruch rdzenia wiru w odpowiednio zbudowanych MTJ. Takie oscylacje potwierdzano pomiarem emisji mikrofalowej i porównaniem z modelami: [Dussaux i in., Large microwave generation from d.c. driven magnetic vortex oscillators in magnetic tunnel junctions](https://arxiv.org/abs/1001.4933). Praca ta uzasadnia hipotezę badawczą, ale nie identyfikuje mechanizmu w tej próbce ani nie przenosi na nią swoich progów prądowych.

Zmiana średniej rezystancji może towarzyszyć zarówno przejściu pomiędzy statycznymi konfiguracjami, jak i zmianie ruchu magnetyzacji. Odczyt DC uśrednia sygnał w swoim czasie integracji i nie mierzy bezpośrednio częstotliwości szybkich oscylacji. Należy rozdzielić „anomalię DC”, „przejście stanu” i „potwierdzoną dynamikę”. Prąd A może również wytwarzać własne pole; dochodzą pola rozproszone, remanencja i historia magnetyczna. Nieaktywna field line nie usuwa tych oddziaływań.

### 16.4. Kolejność dalszego badania

1. **Najpierw analiza istniejącego pliku, bez ponownego obciążania próbki.** Odtworzyć V(I_meas), R(I_meas), I_meas(I_demanded), P(I_meas), compliance i historię zakresów. Sprawdzić, czy anomalia pokrywa się ze zdarzeniem instrumentu. Brak odpowiedniej kolumny/logu oznacza brak możliwości wykluczenia tego mechanizmu.
2. **Wyznaczyć lokalny przedział.** Porównać trend przed i po zdarzeniu, obliczyć zmianę R i nachylenia, sprawdzić wrażliwość wyniku na okno i wygładzanie. Próg podawać jako przedział między punktami; nie dopisywać rozdzielczości lepszej niż dane.
3. **Ocenić możliwość bezpiecznego powtórzenia.** Obecność wcześniejszych punktów powyżej 700 µA nie dowodzi, że ponowna ekspozycja jest bezpieczna. Najpierw zweryfikować limity, maksymalne V/P, stan próbki po pomiarze oraz czas ekspozycji. Program nie podnosi limitów, aby odtworzyć anomalię. Jeżeli obowiązujące limity nie dopuszczają obszaru, protokół pozostaje analizą istniejących danych.
4. **Powtórzenie z zagęszczeniem w zatwierdzonym obszarze.** Jako przykład konstrukcji planu: gęstsza siatka obejmująca obie strony anomalii, np. 650–780 µA, wyłącznie jeśli cały ten zakres został niezależnie dopuszczony. To obszar analityczny do zatwierdzenia, nie zalecenie prądu. Podgląd podaje liczbę punktów, dwell i łączną ekspozycję; więcej punktów zwiększa czas grzania. Zmiana siatki jest jawnym nowym planem.
5. **Sprawdzić odwracalność.** Gałąź rosnąca i malejąca oraz powtórzenia z identycznym przygotowaniem pokażą, czy anomalia występuje ponownie i czy ma histerezę. Mały odczyt przed i po przebiegu pomaga wykryć zmianę stanu. Brak powrotu R nie rozstrzyga sam w sobie między remanencją a uszkodzeniem.
6. **Porównać czasy i polaryzacje.** Jawnie zaplanowane różne czasy ekspozycji oraz oba znaki A, jeśli dopuszczone, pomagają ocenić wpływ grzania i zależności od kierunku prądu. Zależność czasowa jest również możliwa dla termicznie aktywowanych procesów magnetycznych; nie jest samodzielnym dowodem grzania jako jedynej przyczyny.
7. **Powtórzyć dla listy I_B.** Dla każdej krzywej szukać zdarzenia tą samą metodą i z zachowaniem gałęzi. Powstaje mapa przedziału I_anomaly(I_B), amplitudy ΔR(I_B) i odwracalności. Zdarzenie może przesunąć się poza zagęszczony obszar — wtedy raport mówi „nie wykryto w zbadanym zakresie”, nie „dynamika zanikła”.
8. **Potwierdzenie dynamiki jako osobny etap pomiarowy.** Rozważyć pomiar widma RF lub odpowiednio szybkiego sygnału czasowego poniżej, w obrębie i powyżej anomalii, w dopuszczonych nastawach. Tor RF/bias-tee, blokada DC, obciążenie, pasmo, dopuszczalny poziom wejścia analizatora i jego wpływ na obwód MTJ wymagają osobnej kwalifikacji. Nie podłączać wyjścia SMU bezpośrednio do wejścia RF. Integracja analizatora Anritsu jest odrębnym rozszerzeniem planu, nie ukrytą czynnością charakterystyki DC.

W pomiarze widma rejestrujemy tło, częstotliwość, szerokość linii, moc po uwzględnieniu toru oraz zależność od A/B. Powtarzalny pik zależny od biasu i pola stanowi mocniejszą przesłankę dynamiki, lecz wymaga wykluczenia zakłóceń toru. Brak piku ogranicza wniosek do sprawdzonego pasma i czułości. Średnia R ani szum między wolnymi odczytami Keithleya nie zastępują takiego pomiaru. Kontekst metody: [NIST, Spin-Transfer Nano-Oscillators](https://www.nist.gov/publications/spin-transfer-nano-oscillators).

### 16.5. Rozszerzenie modelu raportowania o zdarzenia

Dodać opcjonalną kolekcję `observations` do datasetu/manifestu. Jedna obserwacja zawiera:

- identyfikator serii, krzywej i punktów, gałąź oraz historię I_B;
- źródło: `operator_annotation`, `screenshot_estimate` albo `data_analysis`;
- przedział prądu zadanego i zmierzonego w A, przedziały R i V, ΔR oraz względną zmianę z podaną bazą;
- klasę `dc_anomaly`, oddzielne hipotezy mechanizmu i status ich dowodów;
- metodę/wersję analizy, okno, progi, wygładzanie, jakość i czynniki zakłócające;
- stan weryfikacji compliance/zakresów, powtarzalność, histerezę oraz odnośniki do ewentualnego widma;
- autora i czas adnotacji. Wartości nieznane są jawnie brakujące, nigdy zerowe.

W pierwszym wdrożeniu wystarczy ręczna adnotacja obszaru i opis w PDF. Automatyczny detektor zmian nachylenia jest kolejnym etapem, z testami na gładkiej nieliniowości, szumie, zmianie zakresu, compliance, wielu skokach i różnych siatkach. Nie może automatycznie ustawiać etykiety „wzbudzenie wiru” ani uruchamiać dodatkowych punktów pomiarowych.

W raporcie zbiorczym tabela zdarzeń rozróżnia: „wykryto anomalię DC”, „nie wykryto w badanym zakresie”, „obszar niezmierzony z powodu compliance” i „dane niewystarczające”. Dopiero zweryfikowane pomiary dynamiczne pozwalają dodać osobną kategorię dowodową. Ten podział pozwala wykorzystać interesujący fragment wykresu jako konkretny cel kolejnej serii, bez przedstawiania hipotezy jako odkrycia.

## 17. Główny cel eksperymentu: mapa Keithley → Rigol dla wzbudzania vortexu

Operator doprecyzował cel: docelowe wzbudzenie MTJ przebiegiem prądu zmiennym w czasie (AC lub DC z modulacją) pochodzi z generatora Rigol, który zadaje napięcie i nie mierzy prądu MTJ. Charakterystyki Keithleya mają dostarczać danych do **przewidywania prądu przy danej nastawie generatora** dla określonego field line i historii magnetycznej. Wynik raportu ma zatem obejmować mapę przeliczeniową oraz zakres jej ważności, a nie tylko rodzinę R(I).

### 17.1. Trzy różne napięcia i znaczenie 50 Ω

Rozróżniamy:

1. `V_display`: napięcie wpisane/wyświetlane przez Rigola przy konkretnym ustawieniu LOAD;
2. `V_oc`: równoważne napięcie generatora bez obciążenia, przed jego szeregową rezystancją źródła;
3. `V_MTJ`: napięcie na próbce pod obciążeniem.

DG1000Z ma stałą szeregową impedancję wyjściową nominalnie 50 Ω. High-Z nie usuwa tej rezystancji. LOAD opisuje założone zewnętrzne obciążenie, według którego instrument prezentuje/programuje amplitudę i offset. Sama zmiana LOAD przeskalowuje wyświetlane liczby bez zmiany fizycznego sygnału; ponowne wpisanie tej samej liczby po zmianie LOAD oznacza już inny sygnał. Źródło: [Rigol DG1000Z User’s Guide, Output Impedance, str. 2-88](https://int.rigol.com/ind/Images/DG1000Z_UserGuide_EN_tcm13-2800.pdf).

W nominalnym modelu rezystancyjnym:

```text
V_oc = alpha * V_display
alpha = 1                         dla LOAD=High-Z
alpha = 1 + 50 Ω / R_configured    dla liczbowego LOAD
alpha = 2                         dla LOAD=50 Ω

Rigol: V_oc → szeregowe 50 Ω → zewnętrzny tor → MTJ
```

`R_configured` jest ustawieniem prezentacji generatora, nie zmierzoną rezystancją MTJ i nie dodatkowym fizycznym rezystorem. Przewód koncentryczny 50 Ω również nie jest po prostu rezystorem 50 Ω do dodania w szeregu. Terminator 50 Ω jest rzeczywistym obciążeniem i zmienia obwód; jego obecność wymaga osobnego modelu.

### 17.2. Proste obliczenie dla stałej rezystancji

Przy bezpośrednim połączeniu, małych częstotliwościach i stałej R_MTJ:

```text
I_MTJ = alpha * V_display / (50 Ω + R_MTJ)
V_MTJ = I_MTJ * R_MTJ
V_display wymagane dla I_target = I_target * (50 Ω + R_MTJ) / alpha
```

Jeżeli występuje dodatkowa rzeczywista rezystancja szeregowa, dodajemy ją do mianownika. Zakładamy tutaj brak równoległego toru pomiarowego, tłumika i bias-tee.

**Przykład: R_MTJ=3000 Ω i docelowy prąd DC 200 µA.** Keithley mierzyłby na idealnej próbce 600 mV. Generator potrzebuje dodatkowych 10 mV na swoich 50 Ω, więc V_oc=610 mV. Odpowiada to:

- 610 mV DC przy LOAD=High-Z;
- 305 mV DC przy LOAD=50 Ω.

Natomiast **wpisane 600 mV DC** oznacza około 196,7 µA przy High-Z albo 393,4 µA przy LOAD=50 Ω. Drugi wynik zakłada nadal stałe 3000 Ω; rzeczywista nieliniowość może go zmienić. Nie istnieje więc jedna relacja „600 mV Rigola = 200 µA” bez określenia LOAD, definicji napięcia, charakterystyki i połączeń.

| Hipotetyczna R_MTJ dla kolejnych warunków pola | I dla 600 mV DC High-Z | Nastawa High-Z dla 200 µA DC | Nastawa LOAD=50 Ω dla 200 µA DC |
|---|---:|---:|---:|
| 3000 Ω | 196,7 µA | 610 mV | 305 mV |
| 2400 Ω | 244,9 µA | 490 mV | 245 mV |
| 1800 Ω | 324,3 µA | 370 mV | 185 mV |
| 1200 Ω | 480,0 µA | 250 mV | 125 mV |

Tabela jest obliczeniem modelowym, nie listą dopuszczonych nastaw. Pokazuje, dlaczego przy zmieniającym się polu stałe napięcie generatora nie zapewnia stałego prądu.

### 17.3. Właściwe przeliczenie z nieliniowej charakterystyki

Docelowo wykorzystujemy każdy poprawny punkt pomiaru Keithleya, a nie jedno średnie R0. Dla danej gałęzi i I_B tworzymy punkty:

```text
V_oc_equivalent(I, I_B) = V_measured_Keithley(I, I_B) + I_measured * 50 Ω
V_display_equivalent = V_oc_equivalent / alpha
```

To bezpośrednie przekształcenie obowiązuje, jeżeli napięcie Keithleya obejmuje dokładnie ten sam zewnętrzny obwód, który widzi Rigol. W pomiarze 2-wire wkład przewodów/styków jest już zawarty w V_measured; nie dodajemy go drugi raz. Zmiana przewodów, rezystora, masy, toru RF lub płaszczyzny pomiarowej wymaga korekcji opartej na znanych elementach albo nowej kalibracji.

Dla pytania „jaki prąd uzyskam po wpisaniu napięcia?” odwracamy tę mapę wyłącznie w jej zmierzonym, prawidłowym i jednoznacznym zakresie. Nie sortujemy po napięciu punktów z różnych stanów, aby sztucznie uzyskać funkcję. Wielowartościowość/histereza oznacza kilka możliwych odpowiedzi. Pomiędzy stanami nie interpolujemy.

Jest to szczególnie istotne przy anomalii około 720–750 µA. Źródło prądowe Keithleya i napięciowe Rigola z rezystancją 50 Ω inaczej reagują na zmianę R: przy spadku R prąd z Rigola może wzrosnąć. Zgodność jednego punktu DC nie dowodzi zgodności ścieżki przełączania ani progu dynamiki. Obszar małego lub ujemnego nachylenia d(V_MTJ+50Ω*I)/dI oznaczamy jako potencjalnie niejednoznaczny; nie rozwiązujemy jego stabilności przez arbitralny wybór pierwiastka.

### 17.4. AC, Vpp, offset i modulowany DC

„600 mV” wymaga jednostki rodzaju napięcia: DC, Vpp, peak albo RMS. Dla sinusoidy bez dodatkowej modulacji:

```text
V_display(t) = V_offset + (Vpp/2)*sin(2*pi*f*t)
V_display_high = V_offset + Vpp/2
V_display_low  = V_offset - Vpp/2

Przy stałej R i modelu rezystancyjnym:
I_DC   = alpha*V_offset/(50 Ω + R)
I_peak_AC = alpha*(Vpp/2)/(50 Ω + R)
I_pp   = alpha*Vpp/(50 Ω + R)
I_rms_AC = I_peak_AC/sqrt(2)
I_rms_total = sqrt(I_DC² + I_rms_AC²)
```

Dla R=3000 Ω, High-Z, **600 mVpp i offset 0 V** oznacza około ±98,4 µA, nie 200 µA DC. Przy LOAD=50 Ω ten sam wpis daje około ±196,7 µA. Aby w idealnym modelu otrzymać **200 µA DC + 50 µA peak sinusoidalnie**, potrzeba High-Z: offset 610 mV i 305 mVpp; wartości skrajne 457,5 i 762,5 mV. Dla LOAD=50 Ω wartości wpisane są o połowę mniejsze. To wyłącznie przykład rachunkowy: górna wartość napięcia próbki wynosiłaby 750 mV, więc plan z limitem próbki 670 mV musiałby zostać odrzucony.

Dla nieliniowej próbki stosujemy quasi-statycznie I(t)=F_branch(V_oc(t)), o ile cały przebieg mieści się w jednej zweryfikowanej gałęzi. Sinusoidalne napięcie nie musi dawać sinusoidalnego prądu; offset napięciowy nie musi odpowiadać średniemu prądowi wyliczonemu z R0. Średni prąd, peak, RMS i moc obliczamy z przebiegu i oznaczamy jako przewidywane. Przekroczenie zakresu mapy lub przejście stanu unieważnia taką predykcję.

Puls 0→I_high ma inne offset/Vpp, RMS i moc średnią niż sinusoida ±I_peak. AM wymaga uwzględnienia pełnej obwiedni: samo Vpp nośnej nie opisuje maksymalnego napięcia. Współczynnik wypełnienia, burst, zbocza, overshoot i suma sygnałów muszą należeć do modelu przebiegu. Nie stosujemy wzoru sinusoidalnego do dowolnego trybu Rigola.

### 17.5. Granica między mapą DC a rzeczywistym wzbudzeniem dynamicznym

Mapa Keithleya daje punkt wyjścia dla biasu i przybliżenia quasi-statycznego. Przy AC liczą się impedancja zależna od częstotliwości, pojemności MTJ i przewodów, odbicia, pasmo toru, tłumik, bias-tee i obciążenie analizatora. Nie można wyznaczyć ich z jednej krzywej DC. Małosygnałowo, w określonym punkcie pracy, model ogólny ma postać I_AC(f)=V_oc(f)/(Z_source(f)+Z_external(f)+Z_MTJ(f)) dla rzeczywiście szeregowej topologii. Przy torach równoległych rozwiązujemy odpowiedni obwód.

W granicy quasi-statycznej małe zmiany prądu zależą od rezystancji różniczkowej dV/dI, a nie zawsze od V/I. Prąd na zaciskach przy AC może zawierać składnik pojemnościowy; nie każdy taki składnik jest prądem tunelowym napędzającym spin torque. Nie deklarujemy „tego samego wzbudzenia” wyłącznie na podstawie zgodności peak lub RMS.

Przed predykcją dynamiczną należy określić zakres częstotliwości, topologię połączeń i płaszczyznę napięcia, a potem zweryfikować tor na odpowiednim obciążeniu oraz zmierzyć dostępne napięcie/prąd przy próbce metodą niezmieniającą istotnie obwodu. Próg DC nie jest automatycznie progiem AC: znaczenie mają częstotliwość, czas, polaryzacja i stan magnetyczny. Raport dzieli wyniki na „DC-equivalent”, „quasi-static estimate” i „experimentally validated at frequency ...”.

### 17.6. Docelowy wykres i raport PyLab

Podstawowy nowy wykres: **X = nastawa Rigola [mV DC, LOAD jawnie podany], Y = przewidywany prąd MTJ [µA]**, oddzielna krzywa dla każdego I_B i gałęzi. Opcjonalnie odwrotna prezentacja: zadany docelowy prąd → potrzebna nastawa. Dodatkowe panele:

- napięcie rzeczywiste MTJ i moc względem nastawy;
- pasma zakresu ważności i niepewności, przerwy na compliance/zmianie stanu;
- obszar anomalii DC z adnotacją „odpowiednik napięciowy, nie potwierdzony próg Rigola”;
- dla zdefiniowanego przebiegu: I_min/I_max, I_mean, I_rms, I_pp oraz P_mean;
- nakładka ograniczeń operatorowych, bez automatycznego zwiększania granic.

Operator wybiera dataset, gałąź, I_B, LOAD, połączenia, typ przebiegu i częstotliwość. Kursor np. 200 µA wskazuje napięcie potrzebne osobno dla każdej krzywej, z informacją „pomiar/interpolacja/model”. Zmiana LOAD przelicza etykiety i liczby w raporcie, lecz nie wysyła komend do urządzenia. Brak wiedzy o częstotliwości/torze ogranicza wykres do jawnego oszacowania DC/quasi-static.

Do serii dodać `rigol_equivalence.csv` i odpowiedni rozdział PDF: identyfikatory danych źródłowych i punkty, I_B, I_A_measured, V_A_measured, V_oc_equivalent, V_display_equivalent, LOAD, model toru, jednostki, gałąź, jakość i ograniczenia. Predykcja z innej próbki/komórki lub starszego stanu nie staje się automatycznie aktualną kalibracją. Jeśli field line jest nieaktywna, raport wskazuje to jako warunek operatora/odczytu, bez podpisu „pole = 0 T”.

### 17.7. Integracja z kodem i warstwą bezpieczeństwa

W obecnym repo `app/safety/rigol_current.py` zawiera już przeliczenie LOAD → napięcie otwartego obwodu oraz konserwatywny model ograniczeń; adapter ustawia LOAD i VPP i weryfikuje je odczytem. Rozszerzenie ma współdzielić definicję napięć/jednostek z tym kodem. Nie tworzymy drugiej sprzecznej interpretacji 50 Ω w analizatorze.

Mapa naukowa szacuje prąd konkretnej zmierzonej próbki; nie zastępuje niezależnej walidacji bezpieczeństwa. Zmiana R lub uszkodzenie może unieważnić prognozę. Program musi nadal respektować High/Low, offset+amplitudę, aktualny LOAD i limity operatora. Wyświetlenie przewidywanych 200 µA nie jest pomiarem ani sprzętowym ograniczeniem do 200 µA. Nigdy nie podnosimy limitów, bo dopasowana R jest duża.

Etap pierwszy: przeliczenie offline i raport z istniejących datasetów, bez sterowania Rigolem. Etap drugi: podgląd parametrów przebiegu z walidacją i jawnym zastosowaniem standardową ścieżką Rigola. Etap trzeci: kwalifikacja częstotliwościowa i ewentualna integracja toru RF. Połączenie Keithleya A i Rigola z MTJ musi być rozwiązane fizycznie; OUTPUT OFF nie zawsze oznacza izolację zacisków. Nie zakładamy, że dwa źródła można pozostawić równolegle podłączone.

Testy obliczeń: 3000 Ω/200 µA → 610 mV High-Z i 305 mV przy LOAD=50 Ω; 600 mVpp → właściwy peak zamiast DC; offset i oba ekstrema; dodatkowy rezystor bez podwójnego liczenia przewodów; nieliniowa krzywa; gałęzie wielowartościowe; compliance; brak ujemnej gałęzi dla AC; zmiana LOAD; nieznany tor/frequency; brak ekstrapolacji. Odbiór modułu predykcji wymaga także kontroli na niezależnych danych, a nie tylko odtworzenia wzoru.

## 18. Instrukcja wdrożenia automatycznego rozdziału PDF dla Rigola

**Status: projekt do późniejszej implementacji.** Na polecenie operatora nie wdrażamy teraz tej funkcji. Poniższa specyfikacja opisuje docelowe zachowanie i kolejność zmian. Rozpoczęte w bieżącej turze dodatki kodu zostały wycofane; wcześniejsze zmiany projektu pozostają zachowane.

### 18.1. Efekt widoczny dla operatora

Uruchomienie zwykłej charakterystyki ma po zapisaniu wyników automatycznie dopisać do `characterization_report.pdf` rozdział **„Rigol — przeliczenie i instrukcja ustawienia”**. Nie wymaga osobnego eksportu ani wyboru katalogu. Funkcja działa także dla charakterystyki zakończonej compliance lub anulowanej, ale tylko w zakresie prawidłowo zebranych danych. Brak poprawnych punktów daje rozdział wyjaśniający brak możliwości przeliczenia, a nie fikcyjne nastawy.

Rozdział zawiera tabelę odpowiedników DC, wykres napięcie Rigola–prąd MTJ, jawne warunki połączeń oraz instrukcję wyboru LOAD i jednostek. Instrukcja AC jest obliczeniowa, dopóki nie znamy częstotliwości i toru. Nie wolno tytułować oszacowania DC „dokładną kalibracją prądu AC” ani przedstawiać go jako zatwierdzenia bezpiecznego włączenia.

Generowanie PDF nie wykonuje żadnego połączenia z instrumentem. Nie ustawia LOAD, nie odczytuje na potrzeby raportu bieżącej karty Rigola, nie zmienia limitów i nie włącza wyjść. Wszystkie dane pochodzą z utrwalonego pomiaru i wersjonowanego modelu przeliczenia. Ten sam raport można odtworzyć przy odłączonych instrumentach.

### 18.2. Dwa poziomy szczegółowości instrukcji

| Dostępne dane | Co generujemy | Czego nie uznajemy za potwierdzone |
|---|---|---|
| Tylko istniejący dataset Keithleya | Nominalna mapa DC dla bezpośredniego, równoważnego toru; obie kolumny LOAD; lista założeń | Rzeczywiste połączenia Rigola, aktywne limity, pole i zakres AC |
| Dataset i utrwalony profil toru | Przeliczenie dla udokumentowanych elementów i płaszczyzny napięcia | Kalibracja AC bez pomiaru charakterystyki częstotliwościowej |
| Dataset, profil toru i pomiar kalibracyjny | Wynik z zakresem częstotliwości, niepewnością i datą ważności | Zastosowanie do dowolnej innej próbki, historii lub konfiguracji |

W pierwszym wydaniu wdrażamy pierwszy wiersz. Profil toru jest rozszerzeniem, a nie zbiorem domyślnie zgadywanych parametrów. Określenie field line jako „nieaktywna według operatora” jest inne niż „I_B zmierzono jako ...”; oba są inne niż „B_field=0 T”.

### 18.3. Mapa plików i odpowiedzialności

| Plik / obszar | Planowana zmiana |
|---|---|
| `app/safety/rigol_current.py` | Udostępnić czystą, publiczną konwersję między napięciem otwartego obwodu i napięciem wyświetlanym dla LOAD, współdzieląc istniejącą definicję. Nie zmieniać zachowania walidatorów przy okazji raportu |
| Nowy `app/devices/keithley_2600/characterization/rigol_equivalence.py` | Typowany wynik i obliczenia offline na punktach pomiarowych, bez Qt, adapterów, sesji VISA i generatora PDF |
| `characterization/report_pdf.py` | Wyrenderować wyniki modelu, tabelę, wykres i instrukcję jako osobny rozdział PDF |
| `characterization/export.py` lub dedykowany exporter obok | Zapisać pełną tabelę przeliczenia do dodatkowego artefaktu z jednostkami; nie zmieniać znaczenia surowego CSV |
| `ui/characterization_card.py` | Wpiąć artefakt w istniejącą ścieżkę automatycznego zapisu; obsłużyć osobno błąd pomiaru i błąd raportu |
| Samples / Measurements | Pokazać dodatkowy artefakt jako pochodny tej samej charakterystyki, nie jako nowy pomiar sprzętowy |
| Testy modelu i `tests/test_keithley_characterization_report.py` | Zweryfikować liczby, selekcję punktów, treść rzeczywistego PDF i przypadki awaryjne |

Aktualny punkt integracji: `_on_sweep_finished()` wywołuje `_save_completed_measurement()`, które zapisuje CSV i wywołuje `KeithleyPdfReportGenerator.generate(dataset, params, pdf_path)`. Rozdział należy dodać do tej istniejącej funkcji generowania. Nie tworzyć osobnego przycisku uruchamiającego nowy pomiar w celu uzyskania instrukcji.

### 18.4. Kontrakt modelu obliczeniowego

Proponowane nazwy modeli są projektem, nie istniejącym API:

```text
RigolEquivalenceContext
  model_version
  topology_kind = nominal_direct_connection | qualified_profile
  voltage_reference_plane
  source_resistance_ohm
  optional_external_series_resistance_ohm
  calibration_id / frequency_validity / uncertainty (opcjonalne)
  dataset_identity / field_context / assumptions

RigolEquivalentPoint
  source_point_index
  sequence_index / branch_id
  measured_current_a
  measured_voltage_v
  open_circuit_voltage_v
  displayed_highz_voltage_v
  displayed_50ohm_voltage_v
  quality_status / exclusion_reason

RigolEquivalenceResult
  context
  eligible_points
  excluded_points_with_reasons
  warnings_and_missing_evidence
```

Wszystkie obliczenia na wartościach SI. Konwersja µA/mV odbywa się tylko przy wyświetleniu. Pole `source_point_index` zachowuje indeks surowego punktu — nie zmienia się po filtrowaniu. Nie wolno nadpisywać surowej charakterystyki nowymi „przeliczonymi pomiarami”.

Nominalne 50 Ω jest udokumentowaną właściwością generatora; zakres tolerancji sprzętu i rzeczywiste połączenia pozostają osobnymi źródłami niepewności. Wspólna funkcja konwersji LOAD nie może przyjmować przypadkowych tekstów jako High-Z. Odrzuca nieznane wartości, NaN/inf i nieprawidłową rezystancję. Odwrotność konwersji musi być testowana dla High-Z, 50 Ω i innego prawidłowego LOAD.

### 18.5. Algorytm punkt po punkcie

1. Odczytać dataset i jego metadane. Zachować kolejność akwizycji i wskazanie gałęzi. Dla starszych danych bez gałęzi nie zgadywać jej historii; pierwsza wersja rysuje punkty bez łączenia.
2. Odrzucić z mapy punkt compliance, punkt z niepotwierdzonym warunkiem field line i punkt z niefinitywnym V lub I. Zapisać przyczynę. Jeżeli starszy schemat nie rejestrował jakości B, wyświetlić „warunki B nieudokumentowane”, a nie „B stabilne”.
3. Punkt I=0 pozostaje niedostępny jako odpowiednik niezerowego biasu. Małych prądów nie klasyfikować jako dokładnych tylko dlatego, że są niezerowe. Stosować dostępne kryteria jakości pomiaru; przy braku modelu niepewności wyraźnie ograniczyć interpretację.
4. Nie wykorzystywać automatycznie punktów o przeciwnych znakach V/I jako rezystancyjnego modelu pasywnej próbki; oznaczyć możliwy offset/aktywny składnik i potrzebę analizy. Nie „naprawiać” znaków wartością bezwzględną.
5. Dla kwalifikowanego punktu obliczyć `V_oc = V_measured + 50 Ω * I_measured` w nominalnym torze. Dodatkowy spadek toru uwzględnić wyłącznie po udokumentowaniu; pomiar 2-wire może już go obejmować. Przy 4-wire bez znanych zewnętrznych spadków mapa jest warunkowa.
6. Obliczyć odpowiednie wartości LOAD przez wspólną konwersję. Do obliczeń nie używać `demanded_si`, `true_resistance_ohm`, `apparent_resistance_ohm` ani dopasowanego R0 jako substytutu zmierzonych V/I.
7. Odrzucić wynik niefinitywny. Zachować znak. Nie zaokrąglać na tym etapie do kroku SCPI.
8. Zwrócić pełną mapę i statystykę odrzuceń. Dla zbioru pustego zwrócić poprawny wynik „brak danych do przeliczenia”, nie wyjątek uniemożliwiający cały raport pomiarowy.
9. Pierwsza wersja nie odwraca automatycznie wielowartościowych krzywych ani nie proponuje niezmierzonego punktu 200 µA. Pokazuje istniejące punkty. Późniejszy kursor docelowego prądu może korzystać z interpolacji tylko na wybranej ciągłej gałęzi, oznaczając wynik jako interpolowany.

Jeżeli raport pokazuje mniej wierszy niż liczba punktów, wybór musi być deterministyczny i zachować kolejność. Należy podać „wybrane N z M punktów”, a pełną tabelę zachować w artefakcie. Nie łączyć linią punktów rozdzielonych odrzuceniem lub skokiem stanu. Brak metadanych anomalii nie oznacza potwierdzonego braku anomalii.

### 18.6. Dokładna struktura rozdziału PDF

**Strona / blok A: identyfikacja i warunki.** Nazwa próbki, komórka, czas, kanał Keithleya, identyfikator danych, tryb, sense, status zakończenia, dostępna informacja o field line. Następnie model bezpośredniego połączenia, fizyczne 50 Ω i dwie konwencje LOAD. Wyraźny podpis: „Oszacowanie DC na podstawie pomiaru; nie zmierzony prąd Rigola”. Nie przenosić dzisiejszych ustawień aplikacji do historycznego pomiaru.

**Blok B: tabela.** Kolumny: indeks punktu; prąd zmierzony [µA]; napięcie zmierzone [mV]; odpowiednik Rigol DC High-Z [mV]; odpowiednik Rigol DC LOAD=50 Ω [mV]; jakość/uwagi, jeżeli potrzebne. Wyraźnie oznaczyć, że kolumny LOAD są alternatywnymi nastawami opisującymi ten sam nominalny sygnał.

**Blok C: wykres.** Oś X: `Equivalent Rigol DC setting [mV]`; oś Y: `Measured Keithley current [µA] / predicted Rigol equivalent`. Dwa rozróżnialne symbole dla LOAD. Dla jednej charakterystyki nie udawać rodziny pól. Dla serii używać osobnych paneli LOAD oraz kolorów I_B, aby nie mieszać w jednej legendzie pola i konwencji napięcia. Opcjonalne pasma jakości tylko tam, gdzie rzeczywiście wyznaczono niepewność.

**Blok D: instrukcja operatora.** Sekwencja z punktu 18.7. Zawiera krok dotyczący wyboru kolumny LOAD i rozróżnienia DC/Vpp. Nie zawiera automatycznej rekomendacji maksymalnego napięcia z tabeli.

**Blok E: AC i ograniczenia.** Wzory sinusoidy, definicje offset/peak/Vpp/RMS, warunki modelu małosygnałowego i nieliniowego. Jeśli nieznane są częstotliwość i tor, brak gotowej rekomendacji AC. Przykłady ogólne muszą być wyraźnie oddzielone od danych próbki.

**Stopka:** wersja modelu, identyfikator/checksum danych, definicja jednostek i źródło dokumentacji producenta. Wartości wydrukowane są zaokrąglone do czytelności; format nie deklaruje dokładności metrologicznej ani nie służy bezpośrednio jako gotowy payload komendy.

### 18.7. Treść instrukcji dla operatora, którą ma generować PDF

1. **Sprawdź próbkę i stan.** Tabela dotyczy wskazanej komórki i historii pomiaru. Wybierz prawidłowy punkt bez compliance i niewyjaśnionej anomalii. Potwierdź, że aktualne limity dopuszczają docelowy eksperyment.
2. **Przy wyłączonych wyjściach sprawdź połączenia.** Odizoluj nieużywane źródło zgodnie ze schematem stanowiska. Nie traktuj OUTPUT OFF Keithleya jako automatycznej izolacji. Sprawdź właściwy kanał Rigola, powroty, terminację i zgodność toru z modelem raportu.
3. **Ustal LOAD.** Ustaw i sprawdź High-Z albo 50 Ω przed wpisaniem napięcia. Korzystaj tylko z odpowiadającej temu kolumny. Nie zmieniaj konwencji podczas aktywnego eksperymentu.
4. **Dla DC wybierz poziom DC/offset.** Liczby z tabeli oznaczają napięcie DC, nie amplitudę Vpp. Ustawianie odbywa się standardową kartą Rigola, z istniejącą walidacją i odczytem konfiguracji.
5. **Dla AC zdefiniuj pełny przebieg.** Podaj częstotliwość, kształt, offset, Vpp, duty/burst/modulację i model toru. Sprawdź obie wartości skrajne oraz obwiednię. Jeżeli raport nie zawiera kwalifikacji AC, najpierw potrzebny jest osobny pomiar/kalibracja; nie kopiuj bezpośrednio napięcia DC do pola amplitudy.
6. **Sprawdź ograniczenia.** Wyliczenie nie podnosi High/Low, limitu prądu, napięcia ani mocy. Wartość obliczona z dużej R nie zabezpiecza przed większym prądem po spadku R. Limity operatora pozostają nadrzędne.
7. **Zweryfikuj tor przy niezależnie dopuszczonym małym poziomie.** Raport nie jest poleceniem automatycznego włączenia wyjścia na wartości maksymalnej. Dalsza praca korzysta z istniejącej procedury stanowiska i aktualnie zatwierdzonego planu.

Raport powinien powiedzieć wprost: „Nie ma sprzętowego potwierdzenia, że Rigol wymusza prąd z tabeli”. W razie zmiany pola, stanu próbki lub połączeń należy ponownie ocenić ważność mapy.

### 18.8. Przykładowy wynik funkcji i kontrola jednostek

Dla surowego punktu `I_measured=0.0002 A`, `V_measured=0.6 V`, bez compliance, model zwraca:

```text
source current          200 µA
measured sample voltage 600 mV
nominal V_oc            610 mV
Rigol DC, High-Z        610 mV
Rigol DC, LOAD 50 Ω     305 mV
status                  nominal DC equivalent; circuit not independently qualified
```

Jeżeli prąd zadany wynosił 210 µA, tabela nadal wykorzystuje 200 µA zmierzone. Jeżeli punkt ma compliance, nie jest źródłem nastawy, nawet gdy jego V/I wygląda fizycznie wiarygodnie. Jeżeli w zbiorze nie ma takiego punktu, nie dopisujemy go jako pomiaru.

Dla tego samego stałego R=3000 Ω przykład AC 600 mVpp przy High-Z i zerowym offsecie daje około 98,36 µA peak, 196,72 µApp i 69,55 µA RMS AC. Użycie liczby 600 mV jako DC dawałoby około 196,72 µA DC. Ten test ma wykrywać pomylenie amplitudy i offsetu, a nie stanowić dowód sinusoidalnego prądu w rzeczywistej nieliniowej próbce.

### 18.9. Trwały zapis, błędy i generowanie ponowne

Surowy `characterization.csv` pozostaje źródłem pomiarów. Pełna tabela pochodna może mieć nazwę `rigol_equivalence.csv`; zawiera wersję modelu, identyfikator źródła, założenia i kolumny SI. PDF i CSV pochodny muszą powstawać z tego samego wyniku funkcji, aby uniknąć dwóch odmiennych obliczeń.

Zapis wykorzystuje istniejący katalog charakterystyki, plik tymczasowy i dopiero po powodzeniu zastąpienie docelowego artefaktu. Nie nadpisywać poprawnego raportu częściowym plikiem. Przy błędzie rozdziału zachować pomiar i czytelnie zgłosić niekompletny raport. Ponowienie generowania ma być możliwe z utrwalonych danych, bez uruchamiania charakterystyki i bez kontaktu ze sprzętem.

PDF nie powinien opóźniać potwierdzenia OFF ani przywracania polityk. Przy wdrożeniu sprawdzić istniejącą kolejność `_save_completed_measurement()` względem `_begin_policy_restore()`. Renderowanie należy odseparować od krytycznego kończenia pomiaru: najpierw trwałe dane oraz bezpieczne zakończenie, potem kosztowne grafiki/PDF. Jeśli wykonanie w tle wymaga zmiany UI, użyć istniejącej infrastruktury z poprawną obsługą zamknięcia aplikacji i pojedynczym szeregowanym rendererem Matplotlib.

Aktualny eksport CSV nie zapisuje wszystkich możliwych przyszłych metadanych toru. Nie deklarować odtwarzalności historycznej, dopóki czytnik nie potrafi odbudować wymaganego snapshotu. Dla starszych plików bez danych zwracać braki jawnie, a nie uzupełniać ich aktualnymi ustawieniami.

### 18.10. Testy wymagane przed uznaniem funkcji za wdrożoną

**Obliczenia:**

- Odpowiedniki 610/305 mV z powyższego punktu; oba znaki prądu i napięcia.
- LOAD High-Z/50 Ω/inny liczbowy oraz odrzucenie nieznanej jednostki, wartości ujemnej, zera, NaN i inf w ustawieniu LOAD.
- Pomylenie prądu zadanego ze zmierzonym, µA z mA, mV z V oraz Vpp z peak/RMS/DC.
- Nieomowa krzywa: wynik musi wynikać z każdego V/I, nie z jednego R0.
- Compliance na pierwszym/środkowym/ostatnim punkcie; wszystkie punkty odrzucone; dataset pusty i częściowy.
- Niezerowe bardzo małe prądy, offset, przeciwne znaki V/I, nieprawidłowe odczyty i przepełnienie obliczeń.
- Histereza, powtórzone prądy i zmiana kierunku: kolejność oraz identyfikatory pozostają zachowane; brak sztucznej monotoniczności.
- Brak ujemnej gałęzi nie daje automatycznej tabeli symetrycznego AC.

**Rzeczywisty PDF i artefakty:**

- Wygenerować PDF i odczytać jego tekst testowym parserem PDF: obecność tytułu, jednostek, właściwych liczb, warunków LOAD, statusu danych i ograniczeń AC. Sam rozmiar pliku nie sprawdza treści.
- Zweryfikować liczbę wierszy i źródłowe indeksy CSV pochodnego; wartości PDF zgodne po formatowaniu.
- Wyrenderować strony do obrazów i obejrzeć przy A4: tabela mieści się w szerokości, nagłówek powtarza się, podpisy/legendy są czytelne, długie metadane nie wychodzą poza marginesy. Sprawdzić znaki µ/Ω i font awaryjny.
- Sprawdzić brak łączenia wykresu przez compliance/luki i brak błędnej prezentacji „brak compliance = liniowy zakres omowy”. To dwie niezależne oceny.
- Wstrzyknąć błąd pliku, brak miejsca/uprawnień i błąd renderowania: poprawne dane oraz wcześniejszy PDF pozostają dostępne, UI zgłasza rzeczywisty stan.
- Instrumentowy fake/proxy, który zgłasza błąd przy każdej próbie komunikacji, nie może zostać wywołany podczas przeliczania i raportowania.

**Regresje i integracja:**

- Dotychczasowe raporty current/voltage, eksport CSV, czytnik Measurements, automatyczny katalog, zakończenie na compliance i anulowanie.
- Test potwierdzający, że dodanie raportu nie opóźnia wyłączania źródeł ani nie uruchamia ich przy ponowieniu PDF.
- Test zgodności nowej konwersji LOAD z istniejącą walidacją Rigola, bez zmiany dotychczasowych wyników bezpieczeństwa.
- `ruff check app tests` i właściwe testy pytest. Parser/renderująca biblioteka PDF, jeśli brak jej w projekcie, powinna być deklarowaną zależnością testową; nie wprowadzać ukrytej zależności uruchomieniowej tylko dla sprawdzenia dokumentu.

### 18.11. Kolejność wykonania przyszłego wdrożenia

1. Ustalić kontrakt nominalnego modelu, brakujące metadane i wersję artefaktu.
2. Dodać współdzieloną publiczną konwersję LOAD oraz testy jej zgodności z istniejącym kodem bezpieczeństwa.
3. Dodać czysty moduł równoważności i testy z niezależnie wyliczonymi przykładami.
4. Dodać pełny artefakt przeliczenia z metadanymi; zapewnić odczyt potrzebny do odtworzenia raportu.
5. Dodać rozdział PDF i wykres, korzystające wyłącznie z gotowego wyniku modelu.
6. Wpiąć generowanie w istniejący automatyczny zapis i rejestr Measurements. Sprawdzić bezpieczną kolejność zakończenia i obsługę ponowień.
7. Przeprowadzić testy treści, obrazu, awarii i regresji. Przygotować przykładowy PDF z danymi syntetycznymi, jednoznacznie oznaczony jako demonstracyjny.
8. Przejrzeć instrukcję na danych rzeczywistej charakterystyki offline. Dopiero osobne kwalifikowane pomiary toru mogą potwierdzić dokładność fizycznych odpowiedników Rigola.

**Definicja zakończenia:** każda nowa charakterystyka daje automatycznie rozdział instrukcji lub jednoznaczne wyjaśnienie braku danych; liczby pochodzą ze zmierzonych V/I, mają poprawną konwencję LOAD i jednostki; generowanie nie steruje urządzeniem; brak danych o torze lub AC nie jest ukrywany; operator ma instrukcję postępowania i nie otrzymuje pozornej gwarancji prądu ani bezpieczeństwa.
