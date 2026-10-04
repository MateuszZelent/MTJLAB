# Stan wdrożenia korekcji widma

Aktualizacja: 2026-10-04. Dokument wykonania dla
[planu](PLAN_KOREKCJI_SZUMU_WIDMA_REALTIME.md).

Najnowsza zmiana: naprzemienne bloki REF/SIGNAL z potwierdzeniem operatora,
jednym archiwum raw i wyborem bloków do interpolacji. Instrukcja i
ograniczenia z realnych nagrań znajdują się w końcowej aktualizacji tego
dokumentu. Pełny plan pozostaje częściowo niezakwalifikowany.

## Stan funkcji

### Trwała kalibracja modelu zakłóceń (2026-10-04)

`SpectrumInterferenceCalibration` zachowuje niezmienne tablice baseline/basis
w W, sigma w W, pełną oś Hz, boolean maski obszarów kontrolnych i ochronnych,
bezwymiarowe granice współczynników, limit uwarunkowania, wersję algorytmu
oraz IDs i SHA-256 profili źródłowych. Osobna kwalifikacja kontroli w SIGNAL
wymaga zapisanego opisu dowodu; domyślnie jest false. Sam opis dowodu nie
stanowi laboratoryjnej kwalifikacji ani automatycznego uczenia modelu z REF.
Checksum obejmuje również maski, wagi, konfigurację i kwalifikację.

Writer zapisuje `/spectrum_processing_v1/interference_models/<model_id>`
przez pending, flush, move i flush. Wcześniej sprawdza zatwierdzenie, hash
i kontekst każdego źródłowego REF oraz identyfikowalność QR. Powtórzenie
identycznego ID jest idempotentne; inne dane pod tym ID są odrzucane.
Czytnik sprawdza wersję, complete, jednostki, dtype, checksum i zależności.
Po odczycie operator QR powstaje ponownie z danych kalibracji, bez zapisu
zależnej od platformy faktoryzacji. Nie zmieniono publicznych wierszy thaTEC.

72 testy modelu, model-store, correction-store i writera przeszły; raport
`artifacts/spectrum-interference/model-storage-regression.xml`. Obejmują
odtworzenie dodatniego sygnału pokrywającego się z linią w obszarze ochronnym,
odmowę dopasowania SIGNAL bez kwalifikacji, brak nadpisania, brakujące REF,
modyfikacje bazy/masek/jednostek, nieukończony rekord i nieznaną wersję,
usunięcie częściowego pending po błędzie zapisu/flush oraz skuteczną ponowną
próbę. Plik z rzeczywistym publicznym widmem i prywatnym modelem przeszedł
`require_pythat=True`. Ruff i `git diff --check` przeszły.

### Model w rdzeniu, workerze i replay (2026-10-04)

`CorrectionSessionRequest.interference_calibration` pozwala jawnie wybrać
kalibrację przy rozpoczynaniu sesji. Domyślnie jest None. Kalibracja musi
odpowiadać kontekstowi, ID/hash i średniej aktywnego REF, mieć kwalifikowane
obszary kontrolne SIGNAL i mieścić się w budżecie pamięci. Zmiana modelu
zamyka blok uśredniania; odświeżenie REF usuwa model. Kontrola pamięci
poprzedza alokację operatora QR.

Model jest dopasowywany na niechronionych kontrolach każdego zaakceptowanego
SIGNAL, a reszta w W trafia do tej samej średniej BLOCK/WINDOW/EMA. Parametry
spoza kalibracji dają INVALID_MODEL: bez udziału w średniej, z zachowanym raw.
Wynik `signed-interference-v1` wskazuje model ID/hash, współczynniki oraz
RMS kontroli w W dla ostatniej zaakceptowanej ramki. To nie są średnie
współczynników całego bloku.

Writer wymaga zatwierdzonego modelu przed wynikiem, sprawdza tożsamość i
granice parametrów; cache nie przelicza QR przy każdym append. Czytnik
wyniku sprawdza model i jego REF. Replay ponownie dopasowuje raw i odtwarza
decyzje, średnie oraz diagnostykę. Test wykrywa zmianę współczynnika nawet
wewnątrz dopuszczalnego zakresu. Stare wyniki bez nowych pól zachowują None.

141 testów pipeline/model-store/controller/realtime/correction-store/
finalization/finalized-store przeszło; raport:
`artifacts/spectrum-interference/pipeline-regression.xml`. Test pipeline
obejmuje BLOCK/WINDOW/EMA, dodatni i ujemny sygnał pokrywający się z linią
w obszarze ochronnym, raw odrzuconego fitu, brak nieuprawnionego CI,
zmianę profilu/modelu, pamięć i odmowę zmienionych zależności.

Wybór kalibracji w GUI opisano poniżej. Uczenie lokalnej bazy z zamkniętego
raw REF działa offline przez opisane niżej narzędzie CLI.
Wynik modelu pozostaje UNQUALIFIED bez standard_uncertainty_w: wariancja
statycznego REF nie opisuje parametrów dopasowanego tła. Finalizacja między
REF realizuje osobny algorytm statycznych profili; kontrakt odrzuca final=True
dla tego modelu. Crash subprocess modelu, rzeczywisty REF/SIGNAL, maski jakości
binów i modelowa niepewność pozostają otwarte.

Działa ręczna akwizycja referencji i sygnału z oddzielnym archiwum każdego
przebiegu, przyczynowe odejmowanie mocy w W oraz podgląd na stronie Anritsu.
Nowa zakładka `Background correction` wymaga połączenia oraz wcześniej
kwalifikowanego protokołu pojedynczego sweepu. Nie kwalifikuje sprzętu na
podstawie samego ustawienia ani opisu operatora. W produkcyjnym podglądzie
wynik jest `unqualified`, a przedziały ufności nie są publikowane.

Wdrożenie całego planu pozostaje w toku. Poniższe elementy nie stanowią
deklaracji kwalifikacji laboratoryjnej ani zamknięcia etapów E0–E8.

## Elementy zaimplementowane

### Telemetria długiego benchmarku i budżet dysku (2026-10-04)

Benchmark asynchroniczny obsługuje teraz `--duration "30 min"` albo
`--duration "2 h"`, rozłącznie z --frames. Długość określa liczbę ramek
przy zadanej częstotliwości; faktyczny czas i opóźnienie harmonogramu są
raportowane osobno. Producent nadal czeka na commit raw przed następną
ramką, a wolniejszy podgląd współdzieli mailbox bez odrzucania raw.

`tools.spectrum_benchmark_resources` mierzy uchwyty bieżącego procesu przez
GetProcessHandleCount i jego wątki przez Toolhelp32. Każdy uchwyt snapshotu
jest zamykany. Raport zawiera próbki RSS, wątków, uchwytów, kolejek,
wielkości archiwum i wolnego dysku co sekundę, telemetrię przed workerem
i po jego shutdown, CPU identifier/logical count, czas CPU akwizycji oraz
zmienne środowiska BLAS. Nie jest to pomiar liczby wątków konkretnej puli
BLAS. Nieznana platforma zachowuje None, bez zastępowania wartości zerem.
Zasoby po shutdown nadal obejmują aplikację Qt i jej cache; różnica od
stanu początkowego sama nie dowodzi wycieku.

Osobny `.resources.jsonl` zapisuje i flushuje każdą próbkę. Nagłówek oznacza
niekompletny dziennik; dopiero gotowy raport opisuje ukończony benchmark.
Nie deklaruje się trwałości każdej próbki po awarii systemu bez fsync.
Gotowy raport powstaje po shutdown workera. Istniejący dziennik blokuje
ponowne użycie nazw, tak jak archiwum, screenshot i raport.

Preflight uwzględnia archiwum (64 B/bin/frame + 16 KiB/frame), osobny
tymczasowy netCDF pełnego importu PyThat (32 B/bin/frame) i 1 GiB rezerwy.
To konserwatywne oszacowania, nie gwarancja wielkości ani budżet RAM importu.
Podczas przebiegu wolne miejsce jest kontrolowane także względem rezerwy
na finalną konwersję. Brak miejsca powoduje zatrzymanie testu z błędem,
bez usuwania raw lub zmiany wymogu końcowego round-trip PyThat.

13 testów benchmark/resources przeszło; raport
`artifacts/spectrum-pipeline-benchmark/resources-regression.xml`.
Obejmują brak miejsca przed utworzeniem katalogów, jawny budżet konwersji,
30 powtórnych snapshotów OS bez wzrostu liczby uchwytów, zamknięcie workera,
kompletny zapis z wolniejszym podglądem, dziennik i odmowę nadpisania.
Ruff i git diff --check przeszły.

Krótki rzeczywisty przebieg 10001 punktów, 40 ramek + 10 warmup:
`artifacts/spectrum-pipeline-benchmark/resources-short-v2.json`.
50 raw commitów, zero strat, 20.01 Hz; p95 publikacji 49.22 ms i paintEvent
13.46 ms. Test odbył się równolegle z Monte Carlo i nie kwalifikuje
nominalnej wydajności bez konkurencyjnego obciążenia. Przy jednym Stop
nie powstaje p95 Stop. Końcowa próbka RSS około 301 MB, 19 wątków i
302 uchwyty; po shutdown 18 wątków i 286 uchwytów. To pojedynczy krótki
przebieg, nie bramka braku wzrostu zasobów przez 30 minut.

Aktualny preflight dla 36000 ramek + 20 warmup, 10001 binów, wymaga
36,246,551,424 B, przy około 24.7 GB wolnego miejsca. Test 30 min i
dwugodzinny soak pozostają niewykonane z powodu budżetu dysku; nie
zastąpiono ich krótszą kampanią, mniejszą liczbą binów ani kasowaniem raw.

### Bootstrap parametrów z zamkniętych bloków (2026-10-04)

`bootstrap_resonance_blocks` oraz `bootstrap_resonance_archives` realizują
offline bootstrap całych widm dla statycznego odejmowania REF. Każda replika
losuje jeden zestaw bloków REF i osobny zestaw SIGNAL. Jedna wylosowana średnia
REF jest wspólna dla całego SIGNAL; biny częstotliwości nie są losowane osobno.
Macierze wag multinomial zastępują kopiowanie wylosowanych widm. Dane wejściowe
pozostają niezmienne, a obliczenia dopasowania nie trafiają na ścieżkę Live.

Czytnik wymaga osobnych, zamkniętych raw REF/SIGNAL, jednego identycznego
profilu i kontekstu, zatwierdzonych checkpointów i jawnych decyzji accepted.
Odtwarza profil REF z raw, sprawdza kolejność, dowód kompletności i licznik
sweepów oraz brak nakładania REF/SIGNAL w czasie. Buduje równe, niepokrywające
się bloki po `block_sweeps` zaakceptowanych sweepów. Bloki zachowują wspólną
korelację częstotliwościową. Niepełny końcowy blok domyślnie powoduje odmowę;
`--discard-partial-tail` pozwala go pominąć z jawnym zapisem liczby sweepów.
Manifest zachowuje zakresy frame ID i czasu każdego bloku, SHA-256 źródeł,
symulację, IDN, hash ustawień, wersje NumPy/SciPy i seed.

Domyślny raport podaje punktowe dopasowanie, status unqualified i CI=None.
Przedziały wymagają wszystkich trzech jawnych kwalifikacji: niezależności
bloków, równoważności tła REF/SIGNAL i stacjonarności SIGNAL, wraz z dowodem.
Minimum wynosi 20 bloków z każdego źródła. Dobór długości bloku i kwalifikacja
nie są automatyczne; sąsiednie bloki mogą nadal być skorelowane. Po spełnieniu
warunków raport publikuje warunkowe marginalne przedziały percentylowe oraz
macierz kowariancji amplitudy W, centrum Hz, FWHM Hz i pola W*Hz.
Choćby jeden nieudany fit bootstrapowy wstrzymuje wszystkie CI — nie publikuje
się przedziałów warunkowanych odrzuceniem trudnych replik. Status
conditional_interval nadal ma coverage_qualified=false. Nie jest to detektor
pików, jednoczesny przedział ani próg fałszywych detekcji. Nie uwzględnia
niepewności dopasowanego modelu EMI ani kowariancji dwóch REF.

Limit 256 bloków i domyślny budżet 64 MiB są sprawdzane przed alokacją macierzy
archiwów. Oszacowanie dotyczy jawnych buforów NumPy, nie twardego limitu RSS
procesu ani wewnętrznych alokacji SciPy/BLAS. Raport jest publikowany jako
nowy JSON przez tempfile, flush/fsync i wyłączny hard-link; anulowanie oraz
wyścig utworzenia pliku nie nadpisują raportu i usuwają pending. Źródła są
otwierane tylko do odczytu, a SHA-256 jest sprawdzany przed i po analizie.

```powershell
python -m tools.bootstrap_spectrum_resonance reference.h5 signal.h5 --output resonance.json --block-sweeps 10 --center "1.5 MHz" --fwhm "100 kHz"
```

To polecenie celowo nie deklaruje kwalifikacji. Flagi
`--independent-blocks-qualified --reference-equivalence-qualified
--stationary-signal-qualified --qualification-evidence "..."` stosować tylko
z odpowiednim dowodem. Demo `artifacts/spectrum-bootstrap/demo-v1/` zawiera
dwa raw archiwa syntetyczne, unqualified-report.json oraz conditional-report.json
z 1000 replik i jawnie syntetyczną kwalifikacją. Nie są to dane laboratoryjne.

25 testów bootstrap-core/store/scientific-transfer przeszło; raport
`artifacts/spectrum-bootstrap/combined-regression.xml`. Weryfikują współdzielony
błąd REF przy zwiększaniu liczby SIGNAL, korelacje binów, ujemną amplitudę,
deterministyczny seed, kwalifikacje, limity pamięci, tail, uszkodzone źródła,
odmowę modelu EMI, anulowanie po fsync, wyścig publikacji i CLI z jednostkami.
Oba wyprodukowane raw archiwa przeszły validator z require_pythat=True, a test
potwierdza ich niezmienność bajtową. Ruff i git diff --check przeszły.
GUI bootstrapu i Monte Carlo pokrycia opisano poniżej.

### Niezależna kwalifikacja pokrycia bootstrapu (2026-10-04)

`tools.qualify_spectrum_bootstrap_coverage` generuje osobne, niezależne
REF/SIGNAL dla każdej repetycji. Generator gamma opisuje dodatnią moc,
korelację sąsiednich binów z jądrem [0.25,0.5,0.25] i wspólny przestrzenny
tryb o losowej amplitudzie. Oczekiwane tło obu źródeł jest identyczne,
a parametry rezonansu są znane. SeedSequence rozdziela strumienie danych
i bootstrapu per trial. Można jawnie wybrać ujemny rezonans. To test
niezależnych syntetycznych średnich blokowych, nie kwalifikacja długości
bloków rzeczywistego pomiaru ani statystyk detektora.

Raport podaje osobne pokrycie amplitudy, centrum, FWHM i pola oraz dokładne
95% przedziały dwumianowe Cloppera–Pearsona. Użyto
[proportion_ci(method="exact") z SciPy](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats._result_classes.BinomTestResult.proportion_ci.html).
Nieudany fit punktowy lub bootstrapowy liczy się jako niepokryta repetycja;
mianownik obejmuje wszystkie próby. Bramka wymaga co najmniej 1000
niezależnych prób, dostępności wszystkich CI i obecności nominalnego 0.95
w każdym marginalnym przedziale dwumianowym. Jest to kryterium dla
zadeklarowanego generatora, nie dowód jednoczesnego pokrycia parametrów,
fałszywych detekcji ani poprawności laboratoryjnej. Raport zawsze zachowuje
laboratory_qualified=false i false_detection_rate=None.

```powershell
python -m tools.qualify_spectrum_bootstrap_coverage --repetitions 1000 --resamples 200 --output coverage.json
```

CLI zapisuje wynik każdej ukończonej próby do osobnego `.trials.jsonl`
i flushuje go przed następną próbą. Nagłówek oznacza niekompletny dziennik;
sam dziennik nie jest raportem zaliczonych bramek. Gotowy JSON powstaje
dopiero po ukończeniu wszystkich prób. Istniejący raport lub dziennik
blokuje ponowne użycie ścieżki. Dziennik nie zapewnia fsync każdej próby
ani automatycznego wznowienia po awarii systemu.

Pięć testów przeszło: skrajne i nominalne liczby pokryć, jawne liczenie
brakujących CI, błędy fitu, signed truth, rozdzielone seedy, odtwarzalność,
serializacja JSON oraz CLI z dziennikiem i odmową nadpisania. Raport:
`artifacts/spectrum-bootstrap/coverage-regression.xml`. Ruff przeszedł.
Przebieg 1000 dodatnich repetycji ukończony:
`artifacts/spectrum-bootstrap/coverage-1000-positive.json`. Sprawdzono
kompletność 1000 kolejnych trial ID i zgodność dziennika z raportem.
40 bloków każdego źródła, 101 binów, 200 bootstrapów na próbę, seed
20261005. Zero nieudanych dopasowań, wszystkie CI dostępne, lecz bramka
pokrycia jest false dla wszystkich parametrów:

| Parametr | Pokrycie | Dokładny 95% przedział dwumianowy |
| --- | --- | --- |
| Amplituda | 934/1000 = 93.4% | 91.68–94.86% |
| Centrum | 925/1000 = 92.5% | 90.69–94.06% |
| FWHM | 928/1000 = 92.8% | 91.02–94.32% |
| Pole W*Hz | 925/1000 = 92.5% | 90.69–94.06% |

Żaden przedział dwumianowy nie obejmuje nominalnego 95%. Nie powiększono
ad hoc przedziałów, nie zmieniono seedu i nie usunięto niepokrytych prób,
żeby zaliczyć bramkę. Ten artefakt dokumentuje niedostateczne pokrycie
obecnego percentylowego bootstrapu w zadeklarowanym przypadku.
Publikowane warunkowe CI pozostają coverage_qualified=false. Metoda wymaga
dalszej pracy i niezależnej rewalidacji; laboratoryjna kwalifikacja,
ujemne/pozostałe scenariusze oraz false-alarm testy nadal pozostają otwarte.

### Studentyzowany wariant bootstrapu (2026-10-04)

Dodano jawne `interval_method="studentized"` i `--interval-method studentized`
w analizie archiwów oraz kwalifikacji pokrycia. Domyślny percentile nadal
pozostaje dostępny, a jego niezaliczony raport nie został zmieniony.
Dobór studentyzowanego pivotu ma podstawę w porównaniach metod bootstrapu,
np. [Hall, Theoretical Comparison of Bootstrap Confidence Intervals](https://doi.org/10.1214/aos/1176350933).
Ta literatura nie kwalifikuje poniższej implementacji ani danych laboratoryjnych.

`resonance_studentization` wyznacza lokalny Jacobian znanej hipotezy
Gaussian/Lorentzian w znormalizowanych współrzędnych amplitudy, centrum,
log szerokości i stałego baseline. Baseline pozostaje parametrem nuisance
w pseudoodwrotności. Analityczna transformacja zachowuje SI i pochodne pola
w skończonym oknie. Kowariancja pochodzi z projekcji całych bloków REF
i SIGNAL, z osobnym składnikiem wariancji średniej każdego źródła.
Nie pochodzi z reszt optimizera ani sumowania niezależnych wariancji binów.
REF jest uwzględniony raz, bez dzielenia jego błędu przez liczbę SIGNAL.

Dla każdej repliki ponownie dopasowuje się rezonans i oblicza jej SE na
podstawie tych samych wylosowanych multiplicities bloków. Pivot wynosi
`t* = (theta* - theta_hat) / SE*`; końce CI to
`theta_hat - quantile(t*, 0.975) * SE_hat` oraz
`theta_hat - quantile(t*, 0.025) * SE_hat`. Nie stosuje się mnożnika
dobranego do wyniku poprzedniego testu. Małe macierze kowariancji mają
4×4; nie tworzono macierzy F×F. Budżet buforów został ujednolicony w rdzeniu
i czytniku archiwów oraz obejmuje dodatkową macierz centrowanych bloków.

To lokalne przybliżenie sandwich przy założeniu niezależnych bloków
i poprawnej pojedynczej hipotezy rezonansu. Nie kwalifikuje obciążenia
modelu, nieznanych rezonansów, zmiennego SIGNAL, modelu EMI ani temporalnej
korelacji bloków. Nierozwiązywalna wariancja, także na poziomie precyzji
numerycznej, wstrzymuje studentyzację. Jedna nieudana studentyzacja repliki
wstrzymuje wszystkie CI; liczona jest osobno od błędów fitu. Nadal wymagane
są trzy kwalifikacje wejściowe i coverage_qualified pozostaje false.

32 testy bootstrap/studentization/coverage/archive przeszły; raport
`artifacts/spectrum-bootstrap/studentized-regression.xml`. Obejmują
pochodne sprawdzone przez niezależne ponowne fit Gaussian/Lorentzian,
dodatnią i ujemną amplitudę na osi GHz, porównanie kowariancji z pełnym
delete-one jackknife obu źródeł, wspólny błąd REF, zgodność wag z fizycznie
powielonymi blokami, zerową wariancję, jedno nieudane SE, kwalifikacje,
odtwarzalność i budżet archiwum przed alokacją. Ruff i diff --check przeszły.

Przebieg rozwojowy 50 prób, seed 20261005, zapisano do
`artifacts/spectrum-bootstrap/studentized-development-50.json`.
Nie jest wystarczającą kwalifikacją: minimum_1000_independent_trials=false.
Osobny seed walidacyjny 20261006 i 1000 prób ukończono:
`artifacts/spectrum-bootstrap/studentized-validation-1000-positive.json`.
Sprawdzono ciągłość 1000 trial ID i identyczność dziennika z raportem.
Wszystkie CI są dostępne; zero nieudanych fit/studentyzacji. Łączna bramka
pokrycia pozostaje false:

| Parametr | Pokrycie | Dokładny 95% przedział dwumianowy | Obejmuje 95% |
| --- | --- | --- | --- |
| Amplituda | 941/1000 = 94.1% | 92.46–95.48% | tak |
| Centrum | 933/1000 = 93.3% | 91.57–94.77% | nie |
| FWHM | 930/1000 = 93.0% | 91.24–94.50% | nie |
| Pole W*Hz | 943/1000 = 94.3% | 92.68–95.65% | tak |

Nie utożsamia się zaliczenia amplitudy/pola z kwalifikacją pozostałych
parametrów. Wariant studentyzowany nadal ma coverage_qualified=false.
Różne seedy wcześniejszego i obecnego przebiegu nie stanowią sparowanego
porównania. Oba raporty zachowano. Przy 200 replikach na próbę kwantyle
ogonowe mają niewielką liczbę obserwacji; dalsze rozróżnienie błędu Monte
Carlo i przybliżenia studentyzacji wymaga osobnego eksperymentu, a nie
rozszerzenia CI dobranym mnożnikiem lub zmiany bramki.

### Sparowana diagnoza liczby replik CI (2026-10-04)

`tools.compare_spectrum_bootstrap_trials` porównuje dokładnie pasujący
prefiks trial ID z dwóch zamkniętych raportów. Wymaga zgodności algorytmu,
generatora, truth, jednostek, seedów, wersji bibliotek, siatki/liczby bloków
i konfiguracji poza liczbą replik. Sprawdza ciągłość ID, unsigned seedy,
finite/ordered bounds i zgodność zapisanych covered z truth. Zachowuje
SHA-256 źródeł i sprawdza ich niezmienność przed i po odczycie. Limit pliku
JSON to 64 MiB na źródło; nie jest to twardy limit RSS dekodera.

Raport podaje cztery liczby par: pokryte przez oba warianty, tylko pierwszy,
tylko drugi i przez żaden. Brak CI pozostaje niepokrytą próbą w mianowniku.
Mediana ilorazu szerokości ma osobno podaną liczbę par z dostępnymi CI
i dodatnią szerokością pierwszego przedziału. Niesparowane rekordy są
wykazane liczbowo. To diagnostyka, zawsze coverage_qualified=false;
nie jest nową niezależną kwalifikacją i nie dowodzi braku niezapisanych
zmian kodu tylko na podstawie zgodności deklarowanych wersji.

12 testów porównania przeszło; raport
`artifacts/spectrum-bootstrap/comparison-regression.xml`. Obejmują pełną
tablicę par, krótszy prefiks, brak CI, fałszywe coverage, uszkodzone granice,
zmieniony generator/metodę/seedy/ID/metadata replik, niezmienność źródeł
i odmowę nadpisania. Ruff i diff --check przeszły.

Przebieg 100 prób, 1000 replik, seed 20261006, ukończono w
`artifacts/spectrum-bootstrap/studentized-paired-100-resamples1000.json`.
Porównano go z tymi samymi pierwszymi 100 próbami z raportu o 200 replikach;
wynik `studentized-paired-comparison.json`. Wszystkie CI dostępne.

| Parametr | Pokrycie przy 200 replikach | Pokrycie przy 1000 | Mediana ilorazu szerokości 1000/200 |
| --- | --- | --- | --- |
| Amplituda | 97/100 | 98/100 | 1.0190 |
| Centrum | 93/100 | 92/100 | 1.0352 |
| FWHM | 93/100 | 94/100 | 1.0154 |
| Pole | 96/100 | 97/100 | 1.0110 |

Minimum 1000 niezależnych prób nie zostało spełnione przez ten eksperyment.
Szerokie przedziały dwumianowe ze 100 prób nie zastępują niezaliczonej
wcześniejszej walidacji 1000. W tym sparowanym przypadku większa liczba
replik nie poprawiła pokrycia centrum; nie uznano jej za rozwiązanie ani
nie promowano CI do kwalifikowanych. Dalsza diagnoza obejmuje kalibrację
lokalnego estymatora błędu względem niezależnego rozrzutu parametrów.

### Analiza archiwów w workerze aplikacji (2026-10-04)

`SpectrumResonanceBootstrapRequest` przenosi niezmienne parametry i ścieżki
zamkniętych REF/SIGNAL do `SpectrumCorrectionController.bootstrap_archives`.
Worker uruchamia sprawdzony czytnik/bootstrap poza GUI, przekazuje kontrolę
anulowania i zwraca ścieżkę wraz z raportem. Postęp bootstrapu jest emitowany
co najwyżej około 10 Hz oraz na końcu; nie generuje zdarzenia GUI dla każdej
repliki. Czytanie/hash źródeł pozostaje fazą bez pozornej procentowej oceny.
Odmowa CI z braku kwalifikacji może zakończyć się bez losowania replik.

Kontroler odrzuca drugi równoległy offline request oraz start sesji podczas
offline processing. Odrzuca także offline request przy sesji pending/active,
zanim trafi on do workera — nie zamyka wtedy działającego writera jako faulted.
Anulowanie/ukończenie/błąd zwalniają blokadę offline; zdarzenie błędu writera
zwalnia stan sesji dopiero po obsłudze zamknięcia w workerze. Zamknięcie
kontrolera ustawia token cancel i kolejkuje shutdown, bez wymuszonego kill
wątku. Anulowany bootstrap nie publikuje raportu; można zlecić następny.

24 testy bootstrap-controller, correction-controller i workflow przeszły;
`artifacts/spectrum-bootstrap/controller-regression.xml`. Obejmują rzeczywiste
typed raw archiwa, wynik i postęp, heartbeat GUI podczas dopasowania,
odmowę duplikatu/startu akwizycji, anulowanie i ponowne zlecenie oraz
odmowę offline podczas akwizycji z późniejszym raw commit i poprawnym
completed close tego samego archiwum. Dotychczasowa REF/SIGNAL/finalization
regresja pozostała zielona. Ruff przeszedł.
Akcja GUI korzystająca z tej ścieżki została opisana poniżej.

### Okno analizy zapisanych rezonansów (2026-10-04)

W Background correction przycisk `Analyze recorded resonance…` otwiera
Fluent StationDialog. Akcja jest w przewijanym panelu ustawień, pozostaje
dostępna bez połączenia z urządzeniem i jest blokowana podczas nagrywania
lub operacji profilu. Okno jest otwierane asynchronicznie jako WindowModal,
bez zagnieżdżonego exec. Ponowne otwarcie podnosi istniejące okno.

Użytkownik wybiera osobne zamknięte raw REF/SIGNAL i nowy raport JSON,
centrum/FWHM z jednostkami, hipotezę Gaussian/Lorentzian, liczbę zaakceptowanych
sweepów na blok, liczbę replik oraz metodę studentized/percentile.
Pominięcie niepełnego ogona jest domyślnie wyłączone. Trzy kwalifikacje
założeń są domyślnie false; zaznaczenie wymaga dowodu. Bez wszystkich
założeń powstaje tylko punktowy fit. Okno wyraźnie podaje, że obecne CI
pozostają eksperymentalne po niezaliczonej pełnej bramce pokrycia 95%.

Oddzielny controller uruchamia analizę poza GUI. Status jest ustawiany
bezpośrednio po kliknięciu, a faza czytania ma indeterminate progress.
Postęp replik jest liczbowy, z informacją, że raport nie został jeszcze
opublikowany. Ukończenie pokazuje amplitudę W z prefiksem inżynierskim,
centrum/FWHM Hz z prefiksem, signed pole W*Hz, liczbę bloków i stan CI.
Raport zawiera pełne wartości i niekwalifikowane przedziały, jeśli jawnie
zlecono taki wariant. Błędne jednostki, brak dowodu lub konflikt raportu
są widoczne i nie nadpisują istniejącego pliku.

Cancel i zamknięcie okna ustawiają token anulowania. Zamknięcie czeka
asynchronicznie na shutdown przez timer; nie zabija wątku ani nie blokuje
GUI wielosekundowym wait. Przy shutdown strony niedomknięty controller
przechodzi do właściciela aplikacji do zakończenia wątku. Dialog nie wysyła
komend do instrumentu. Analiza wymaga zamkniętych źródeł i nie zmienia raw.

19 testów dialog/layout/interference-UI przeszło;
`artifacts/spectrum-resonance-ui/combined-regression.xml`.
Obejmują rzeczywiste raw i worker, domyślny brak CI, jednostki, dowód,
konflikt istniejącego raportu, zamknięcie podczas pending analizy bez nowego
raportu, niezmienność źródeł i otwarcie z workspace bez device requests.
Pokazane okna 1000×900 light oraz 750×700 dark mają dodatnią geometrię
akcji i wyniku; renderingi `artifacts/spectrum-resonance-ui/result-*.png`
sprawdzono wizualnie. Naprawiono kolizje nazw pól z QWidget.width i
QDialog.result; tytuł i geometria ponownie działają. Ruff i diff --check
przeszły. To nadal testy syntetyczne, nie kwalifikacja CI laboratoryjnego.

### Ilościowe zachowanie rezonansu (2026-10-04)

`fit_linear_resonance` dopasowuje pojedynczą hipotezę Gaussian/Lorentzian
do signed W z lokalnym stałym baseline. Zwraca amplitudę W, centrum Hz,
FWHM Hz, signed całkę w skończonym oknie W*Hz, RMS W i uwarunkowanie.
W*Hz nie jest całkowitą mocą W ani wynikiem całkowania skalibrowanej PSD.
Oś i moc są skalowane numerycznie przed optymalizacją; wynik zachowuje SI.
Limit iteracji, kontrola uwarunkowania i aktywnych granic odrzucają złe fit.
Nie powstaje CI z reszt optimizera. To analiza offline, bez zmian Live.
Opcjonalne zależności: `pip install ".[qualification]"` (SciPy).
[Oficjalna dokumentacja least_squares](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.least_squares.html)
opisuje użyte ograniczenia i skalowanie parametrów.

`tools.qualify_spectrum_signal_preservation` wykorzystuje rzeczywisty
RealtimeSpectrumProcessor, profile REF i trenowany tylko z REF model EMI.
Generator zapisuje seed, wersje NumPy/SciPy, liczby sweepów i średnich mocy.
Dodatni szum mocy pochodzi z gamma z 16 średnimi, ze znanym dodatnim
trzypunktowym jądrem korelacji binów. Nie jest to kwalifikacja rzeczywistego
detektora ani jego RBW. W każdej repetycji jest nowy blok 40 REF i 16 SIGNAL;
niezależne repetycje nie są fizycznymi binami jednego widma.

Scenariusze: dodatni Gaussian, dodatni Lorentzian, ujemny Gaussian,
Gaussian pod linią EMI o fluktuującej amplitudzie oraz koherentne złożenie
pól zespolonych. Dla modelu EMI baza pochodzi wyłącznie z REF, a maska
kontrolna wyklucza znany synthetic sygnał i jego ogony. Metryki statycznego
REF i wybranej korekcji dotyczą tego samego raw. Raport liczy także odrzucone
ramki i nieudane dopasowania; nie są pomijane przy kwalifikacji bramek.

```powershell
python -m tools.qualify_spectrum_signal_preservation --repetitions 1000 --output signal-transfer.json
```

Przebieg 100 repetycji na 1001 punktach (16 punktów/FWHM) ukończony:
`artifacts/spectrum-signal-preservation/100-trials.json`. Cztery przypadki
addytywne spełniły bramki średniego biasu amplitudy/pola 1%, FWHM 2% i
centrum 0.1 kroku. W overlap EMI średni RMSE zmniejszył się z 8.21e-11 W
(statyczna REF) do 9.83e-15 W (model), przy zachowaniu parametrów sygnału.
Koherentny przypadek pozostaje poza modelem addytywnej mocy: bias amplitudy
około +578%, FWHM +35.6%; brak deklaracji skuteczności korekcji.

Przebieg 1000 repetycji `1000-trials.json` zachowano również jako dowód
niezaliczonej bramki overlap_model: trzy ramki w dwóch blokach przekroczyły
granice wyznaczone z losowego REF. Pozostałe 998 bloków miało mały bias,
ale bramka pozostaje false, ponieważ odrzucenia nie zostały ukryte.
Nie rozszerzono granic modelu ani nie wybrano innego seedu, by ukryć wynik.

Osobny scenariusz overlap_calibrated_range mierzy w REF oba krańce (-0.5,+0.5)
i losowy środek zakresu przed SIGNAL (0.3±0.03). Generator v2 zapisuje tę
politykę. `1000-calibrated-range.json`: 1000/1000 pełnych bloków, zero
odrzuconych ramek, wszystkie cztery bramki biasu true. To test działania
wewnątrz udokumentowanego zakresu; nie zastępuje niezaliczonego testu
losowej kalibracji. Scenariusz overlap_insufficient_range celowo mierzy
węższy REF, odrzuca wszystkie SIGNAL i raportuje None zamiast zerowego błędu.
Nowy argument --scenarios pozwala uruchomić wybrane jawne przypadki.

66 testów resonance/scientific-transfer/realtime/training/pipeline przeszło;
raport `artifacts/spectrum-signal-preservation/combined-regression.xml`.
Testy zachowują znak, SI przy osi GHz, pełne dane wejściowe oraz deterministyczny
seed i ujawniają ograniczenie koherentne. Po dodaniu scenariusza zbyt wąskiej
kalibracji osiem ukierunkowanych testów przeszło; regression.xml.
Ruff i git diff --check przeszły.
Raport jawnie ma coverage_95=None, false_detection_rate=None i
laboratory_qualified=false. Parametry fit są hipotezami znanych syntetycznych
rezonansów, nie automatyczną detekcją; laboratoryjne injection/recovery,
pokrycie CI, fałszywe detekcje i pozostała macierz §19 nie są zaliczone.

### Walidacja na rozłącznym REF (2026-10-04)

`validate_interference_references` oraz `validate_interference_archive`
sprawdzają model na osobnym completed raw REF. Odmawiają użycia tego samego
źródła/hash/profilu, nakładających się przedziałów czasu, zmienionej osi,
SIGNAL oraz binów walidacyjnych należących do dopasowania współczynników.
Odtwarzają profil walidacyjny z raw, korzystając z jego własnej kwalifikacji
statystyk, bez przypisywania tej kwalifikacji modelowi.

Raport podaje zaakceptowane i odrzucone dopasowania wraz z przyczynami,
bias/RMS błędu per bin w W, RMS regionu niewykorzystywanego do dopasowania,
uwarunkowanie oraz porównanie ze statycznym baseline na dokładnie tych samych
zaakceptowanych ramkach. Gdy nie ma udanych dopasowań, błąd jest None, nie
zero. Statystyki są jawnie warunkowe względem zaakceptowanych fitów.
Temporalna rozłączność nie dowodzi niezależności ani zachowania nieznanego
SIGNAL; raport nie kwalifikuje kontroli, CI, TTL ani modelu.

Trening zapisuje teraz swój przedział czasowy. Starsze kalibracje bez tego
pochodzenia pozostają odczytywalne, ale wymagają ponownego treningu przed
walidacją. CLI wybiera jeden model, kontroluje budżet wektorów i sprawdza
hash obu źródeł przed i po obliczeniach. JSON jest publikowany przez
tymczasowy plik, fsync i os.link bez możliwości nadpisania celu.

```powershell
python -m tools.validate_spectrum_interference model.h5 separate-reference.h5 --output validation.json
```

Domyślne biny oceny to maska ochronna kalibracji. API pozwala wskazać inne
niepuste biny nieuczestniczące w fit. Przy wielu modelach podać --model-id.
Przykład CLI wykonano w `artifacts/spectrum-interference-training-demo/`:
model-for-validation.h5, validation-reference.h5 i validation-report.json.
12/12 osobnych syntetycznych REF dopasowano; błędy w chronionym regionie
nie stanowią kwalifikacji laboratoryjnej. Oryginalny model.h5 zachowano.
Testy obejmują także odrzucenie całego bloku poza zakresem, overlap czasu,
niezmienność źródeł, anulowanie po fsync i wyścig utworzenia celu.
47 testów validation/training/store/pipeline/UI przeszło. Ruff i
git diff --check bez błędów.
Raport `artifacts/spectrum-interference/validation-regression.xml`.
Modelowa niepewność, injection/recovery na rzeczywistym SIGNAL i kwalifikacja
laboratoryjna pozostają otwarte.

### Uczenie lokalnych szablonów wyłącznie z REF (2026-10-04)

`train_interference_basis` i `train_interference_archive` korzystają tylko
z zaakceptowanych raw REFERENCE w completed sesji. Odtwarzają średnią oraz
wariancję Welforda i sprawdzają hash profilu w obu przebiegach. SIGNAL jest
odrzucany nawet przy quantitative_accepted=false.

Domyślnie najwyżej 64 widma (limit 256) wybrane w równych odstępach całej REF
trenują zredukowane SVD reszt w jawnie wskazanych oknach zakłóceń. Baza ma
1–8 składowych i jest dokładnie zerowa poza tymi oknami. Cała oś jako okno
jest niedozwolona. Rząd oraz identyfikowalność na kontrolach są sprawdzane.
Nie powstaje macierz F×F. Implementacja korzysta z full_matrices=False:
[dokumentacja NumPy SVD](https://numpy.org/doc/stable/reference/generated/numpy.linalg.svd.html).

Drugi przebieg obejmuje wszystkie zaakceptowane REF. Wyznacza obserwowane
granice parametrów z marginesem numerycznym, nie CI ani ekstrapolację.
RMS jest diagnostyką na treningu, nie niezależną walidacją. Kontrola budżetu
buforów poprzedza odczyt wektorów i SVD; szacunek nie gwarantuje RSS LAPACK.
Anulowanie SVD czeka na zakończenie tej pojedynczej operacji.

Nowy artefakt zawiera profile/model, okna, wybór REF, wersje treningu/NumPy,
diagnostykę, hash źródła i jego settings, device IDN oraz simulation metadata.
Publiczny wiersz REFERENCE_MEAN jest jawnie pochodną średnią. Ponowne
uczenie wymaga oryginalnego raw; samo stosowanie modelu jest samodzielne.
Źródło pozostaje bez zmian, hash jest sprawdzany przed i po treningu.
Przerwana publikacja zostawia aborted artifact, odrzucany podczas importu.

```powershell
python -m tools.train_spectrum_interference reference.h5 --specification model-specification.json --output model.h5
```

JSON określa model_id, nuisance_regions, control_regions, protected_regions
(pary częstotliwości z jednostkami), dodatnią control_sigma z jednostką mocy,
components i opcjonalny maximum_training_frames. Kwalifikacja kontroli SIGNAL
domyślnie false wymaga niezależnego qualification_evidence; trening jej nie
wyznacza. Maskę ochronną należy ustalić dla ogonów, przesunięć i marginesu RBW.

Wykonano przykład CLI `artifacts/spectrum-interference-training-demo/`:
syntetyczne 40 REF, 201 binów, 12 próbek treningowych, dwie składowe,
specification.json i model.h5. To nie jest kalibracja laboratoryjna.
39 testów treningu/store/pipeline/UI przeszło, w tym signed injection w
chronionym obszarze po kwalifikacji syntetycznej, jednostki, pamięć, rząd,
zmieniony profil, obecność SIGNAL, anulowanie, niezmienność źródła i PyThat.
Raport `artifacts/spectrum-interference/training-regression.xml`; Ruff oraz
git diff --check bez błędów. Walidacja na osobnym REF opisana jest wyżej.
Modelowa niepewność,
trening przez GUI i rzeczywisty REF/SIGNAL pozostają do wykonania.

### Wybór modelu w zakładce korekcji (2026-10-04)

Nowe kontrolki `Background model`, `Load model calibration…` i `Use mean
background` pozwalają wczytać kalibracje z archiwum i jawnie wybrać model
następnej rejestracji. Import działa na workerze bez połączenia z analizatorem.
Sprawdza źródłowe REF, kontekst, baseline, kwalifikację kontroli i pamięć.
Niepasujące modele są wykluczane; jeżeli żaden nie pasuje, komunikat wyjaśnia
odmowę, a poprzedni wybór pozostaje. Wczytanie samo nie aktywuje modelu.

Selektor i import są zablokowane podczas akwizycji/importu. Odświeżenie REF,
import innego profilu lub finalizacja usuwa stare kalibracje. Wybrany model
jest przekazywany do rzeczywistego CorrectionSessionRequest dopiero przy
SIGNAL, po sprawdzeniu aktualnych ustawień urządzenia. Podgląd opisuje
model zapisany w wyświetlanym wyniku, a kontrolki wybór następnej sesji.
Zmiana wyboru nie zmienia zamrożonego wyniku ani jego opisu.

Testy obejmują rzeczywisty async import, odmowę niekwalifikowanej kontroli,
offline UI, powrót do średniego REF, blokady podczas nagrywania, reset po
odświeżeniu oraz symulowany adapter REF → SIGNAL z wybranym modelem,
HDF5 i replay. Zrzuty light 1200×900 i dark 800×700:
`artifacts/spectrum-interference-ui/selected-*.png`; oba obejrzano.
Minimalne wysokości obu wykresów zachowują miejsce na osie przy zwężeniu,
a wybór modelu pozostaje dostępny w przewijanym panelu. Stop jest poza nim.
30 testów UI/layout/workflow/pipeline przeszło. Ruff i git diff --check
bez błędów. Raporty testów:
`artifacts/spectrum-interference-ui/ui-regression.xml` i
`artifacts/spectrum-interference-ui/model-workflow-regression.xml`.
To nadal testy symulowane; naukowa kwalifikacja modelu pozostaje otwarta.

- Kontrakty niezmiennych tablic, kontekstu ustawień, ról REF/SIGNAL/TRANSITION,
  identyfikatorów ramki i segmentu, dowodu świeżego zakończonego sweepu.
- Adapter przekazuje generację konfiguracji i czasy początku/końca dla
  istniejącej ścieżki `acquire_single_sweep`. Zwykły odczyt bieżącego trace
  nie otrzymuje automatycznie dowodu świeżości.
- Jednorazowe przeliczenie dBm → W, Welford dla referencji, signed subtraction,
  średnia bloku, ograniczone okno i EMA zależna od rzeczywistego odstępu czasu.
  Nie ma wycinania linii, interpolacji rezonansów ani zerowania ujemnych reszt.
- Kontrola zgodności osi i konfiguracji, odrzucanie powtórek i ramek bez dowodu
  kompletności; reset po zmianie stanu/profilu/segmentu oraz nadmiernej przerwie.
- Jawny TTL i status starzenia referencji, także bez nowej ramki. Brak
  kwalifikowanego TTL/modelu dryfu nie oznacza ważnej referencji ilościowej.
- Niepewność bloku w jawnie kwalifikowanym modelu niezależnych sweepów.
  Wspólny błąd referencji jest dodawany raz; nie maleje z liczbą SIGNAL.
  EMA i okno nie otrzymują fikcyjnych przedziałów ufności.
- Ograniczona FIFO, osobny wątek CPU/HDF5, potwierdzenie checkpointu przed
  następnym sweepem oraz koalescencja żądań podglądu. Pełna kolejka sygnalizuje
  backpressure. Zamknięcie GUI ma ograniczony czas oczekiwania.
- Raw, decyzja o udziale ramki, envelope i opcjonalna korekcja są zapisywane
  razem w checkpointcie. Powtórzona ramka pozostaje raw i nie dostaje wyniku
  poprzedniej ramki. Profile muszą być zatwierdzone przed zależnym wynikiem.
- Wersjonowany kodek prywatnego HDF5, checksum profilu, jednostki Hz/W/W²,
  export/import profilu i odczyt wyników. Pozostaje publiczny widok thaTEC/PyThat.
- Replay ręcznej sesji używa surowych sweepów, faktycznej konfiguracji procesora
  i tego samego rdzenia co podgląd. Weryfikuje zgodność wartości, decyzji i
  pochodzenia. Nie zapisuje do źródła; nieobsługiwana historia jest odrzucana.
- Fluent workspace z raw/referencją i podpisaną resztą, opisem stanu REF,
  czasem rejestracji, wyborem średniej, stopem, profilem i ścieżką archiwum.
  Stop i informacja o stanie pozostają poza przewijanymi kontrolkami.
  Freeze zatrzymuje wykres oraz informacje o wyświetlanej ramce.
- Rdzeń opóźnionej finalizacji pomiędzy dwoma rozłącznymi REF. Uśrednia
  współczynniki obu referencji przed propagacją wspólnej niepewności i zwraca
  osobny wynik `final=True` z listą ramek źródłowych.
- Pełna ścieżka finalizacji archiwalnego SIGNAL z REF przed i po nim: osobny
  plik tworzony wyłącznie jako nowy, embedded raw i oba profile, wersjonowany
  checkpoint wyniku, jawny wiersz pochodnej średniej dla thaTEC/PyThat.
  Zachowane są ustawienia i kontekst SIGNAL, metadane symulacji, identyfikacja
  urządzeń oraz SHA-256 oryginalnych źródeł. Replay nie wymaga ich starych ścieżek.
  Akcja `Finalize between two references…` działa na workerze bez połączenia
  ze sprzętem i pokazuje FINAL osobno od PROVISIONAL. Po finalizacji najnowszy
  REF pozostaje profilem do kolejnej akwizycji, a interpolacja zachowuje oba REF.
- Osobny model małej bazy zakłóceń: wcześniejsza faktoryzacja QR, maksymalnie
  osiem składowych, regiony ochronne, kontrola rzędu/uwarunkowania i granic
  współczynników. Wersjonowany zapis kalibracji i zależności od REF jest gotowy;
  model można jawnie podać w session request, a raw replay odtwarza wynik.
  GUI importuje kalibracje i wybiera model następnej sesji. Kwalifikacja
  niepewności pozostaje do wykonania. Uczenie lokalnej bazy z raw REF działa
  offline z jawnym plikiem specyfikacji.
- Settings zachowują jednostki jako jawne ciągi, wartości procesora jako SI,
  korekcja jest domyślnie wyłączona. Nowe jednostki pW/fW, W/Hz i W*Hz mają
  odrębne wymiary. Ręczny start zakładki jest jawną akcją operatora.

## Weryfikacja wykonana

Zestaw regresji obejmujący rdzeń, model zakłóceń, settings, storage, worker,
finalizację, workflow, rendering i istniejące testy Anritsu/HDF5/referencji:
**162 testy i 23 podtesty przeszły**. Po kolejnych poprawkach wykonano dodatkowe
skierowane uruchomienia: storage 15/15, rdzeń z Monte Carlo 31/31,
controller/workflow 3/3. Ruff dla `app`, `tests` i narzędzia benchmarkowego przechodzi.

Kolejny zakres finalizacji: **69 testów przeszło** (final artifact, workflow,
finalizacja matematyczna, layout, storage, HDF5 i reference store). Po dodaniu
kontroli pochodzenia i dokładnego kontekstu SIGNAL ponowna regresja objęła
50 testów, wszystkie przeszły; osobny layout/workflow 7/7. Kontrole obejmują
brak nadpisania źródeł, finalizację offline z GUI, PyThat round-trip, odtworzenie
po przeniesieniu źródeł oraz odrzucanie zmienionych raw, wyniku, profilu,
checkpointu, listy ramek, polityki lub pochodzenia. Zrzut wyniku FINAL:
`artifacts/spectrum-correction-layout/finalized-block.png`.

Po zgłoszeniu niejasnego startu nagrania poprawiono flow wyboru pliku:
opis stanu i parametry są sprawdzane przed otwarciem dialogu, a aktywny
przycisk zmienia napis na `Recording background…`. Wybór pliku nadal
uruchamia zapis bez drugiego kliknięcia Start. Nieaktywny przycisk SIGNAL
wyjaśnia wymaganie gotowego profilu. Pierwsza regresja workflow i layout:
8 testów przeszło.

Po doprecyzowaniu zgłoszenia (brak widocznej reakcji po Save) dodano stały
panel Fluent nad przewijanymi ustawieniami. Natychmiast pokazuje
`Starting background recording…`, wybraną ścieżkę i animowany wskaźnik;
nie czeka na odpowiedź urządzenia. Rozróżnia odczyt konfiguracji, oczekiwanie
na pierwszy sweep, otwarcie archiwum, zapis, zatrzymywanie, zakończenie
oraz błąd. Licznik opisuje zatwierdzone surowe widma. Błąd pozostaje
widoczny także po asynchronicznym zamknięciu archiwum. Anulowanie dialogu
nie zgłasza startu, a metadane poprzedniego wyświetlanego widma są resetowane
przy nowym nagraniu. Status nie jest zamrażany razem z wykresem.

Regresja tej poprawki: 11 testów workflow/layout/controller przeszło,
Ruff i `git diff --check` przeszły. Testy pokazują stronę w jasnym i ciemnym
motywie, przy normalnym i małym oknie, po przewinięciu ustawień; sprawdzają
widoczność panelu zanim przyjdzie odpowiedź analizatora. Zrzuty startu/błędu:
`artifacts/spectrum-correction-layout/recording-{start,error}-{light,dark}-{1500,800}.png`.
Oceniono wizualnie start w małym jasnym i normalnym ciemnym oknie oraz
błąd w małym jasnym oknie. To potwierdza reakcję GUI; nie stanowi dowodu
zakończonej akwizycji na rzeczywistym urządzeniu.

Istotne sprawdzenia:

- Batch/stream, dBm → W, ujemne reszty, wąskie rezonanse i nieregularny czas EMA.
- Wspólna niepewność referencji i jawne wyłączenie CI bez kwalifikacji.
- Monte Carlo: 2000 niezależnych syntetycznych powtórzeń, niezależne REF/SIGNAL,
  bez dryfu; sprawdzenie pokrycia nominalnego 95% i braku clippingu. Wynik
  dotyczy tego modelu testowego, nie rzeczywistego toru RF.
- Liniowy dryf pomiędzy REF i zachowanie sygnału przez finalizację; brak
  kwalifikowanego modelu interpolacji blokuje publikację niepewności.
- Pełna kolejka, kolejność, koalescencja podglądu, ograniczone zamknięcie.
- Błąd po zapisie publicznego wiersza wycofuje raw/envelope/korekcję razem.
- Profil z innego kontekstu jest odrzucany również przy wcześniej cached osi.
- Test przez rzeczywisty adapter z symulowanym VISA: REF, SIGNAL, stop, trwały
  zapis wszystkich sweepów i replay. Naprawiony wyścig timer/stop.
- Pokazane strony w jasnym/ciemnym motywie, desktop/narrow; zrzuty znajdują się
  w `artifacts/spectrum-correction-layout/`. Windows offscreen Qt wymaga
  jawnego załadowania Segoe UI w teście, bo sam nie enumeruje fontów.

Pełny zestaw `tests` został uruchomiony, ale zatrzymał się po 8 błędach:
180 testów i 5 podtestów przeszło. Błędy obejmują BOM w
`tests/test_keithley_field_panel.py` odczytywany przez `ast.parse` oraz stare
przepisy testowe Keithley bez jawnego stałego zakresu źródła. Obie przyczyny
odtworzono na oddzielnej kopii `git archive HEAD` sprzed wdrożenia korekcji.
Nie osłabiono walidacji bezpieczeństwa. Nie ma podstaw do deklarowania, że
cała istniejąca baza testów jest zielona. Kopia diagnostyczna jest w
`artifacts/spectrum-correction-baseline/`.

## Wstępny benchmark CPU

Uruchomienie:

```powershell
python -m tools.benchmark_spectrum_correction --frames 2000 --output artifacts/spectrum-correction-benchmark/cpu-baseline.json
```

Windows, Python 3.14.6, NumPy 2.5.1. Po 100 ramkach rozgrzewki: 2000 pomiarów
na kombinację F/tryb. Surowe czasy i środowisko zapisano w JSON. To pomiar
rdzenia i publikacji niezmiennych tablic, bez VISA, zapisu HDF5 i rysowania GUI;
środowisko było współdzielone z regresją testów, więc nie jest dedykowanym
benchmarkiem stanowiska.

| Punkty | Mediana ingest: block / window / EMA | p95 ingest: block / window / EMA |
|---:|---:|---:|
| 1001 | 0,103 / 0,099 / 0,093 ms | 0,140 / 0,162 / 0,154 ms |
| 10001 | 0,561 / 0,547 / 0,509 ms | 0,877 / 1,108 / 0,663 ms |
| 100001 | 5,436 / 4,920 / 4,775 ms | 6,823 / 5,681 / 5,620 ms |

Dla 10001 punktów mediana publikacji wynosiła 0,056–0,068 ms. Pojedyncze
opóźnienia schedulerowe sięgały około 35 ms. Ten wynik nie gwarantuje deadline
całego pipeline ani możliwości sweepów analizatora przy 100001 punktach.

## Pozostałe prace i warunki odbioru

1. E0/E1: realne raw i kwalifikacja ustawień/detektora/trace mode, stanu REF,
   niezależności sweepów i wpływu zmian konfiguracji poza aplikacją.
2. E3/E5: dalsza historia odświeżeń i finalizacja wielu segmentów jednym
   workflow. Implementowany artefakt obejmuje jeden jawnie wybrany ciągły
   blok SIGNAL i dokładnie jeden profil w każdym pliku REF; bardziej złożona
   historia jest odrzucana, nie interpretowana automatycznie.
3. E3: pełne testy procesu przerwanego/crash/recovery nowych sesji; obecne testy
   sprawdzają rollback wyjątków, nie zastępują kampanii nagłego kill/power loss.
4. E4/E8: benchmark zapisu, rysowania, czasu do podglądu i Stop w pełnym
   pipeline; 30 min obciążenia i dłuższy soak z RSS/uchwytami/kolejkami/rozmiarem.
5. Diagnostyka korelacji/Allana jest zaimplementowana jako narzędzie offline
   opisane poniżej. Pozostaje kwalifikacja TTL/tau na dłuższym realnym REF;
   nie wolno nadać liczbowego TTL na podstawie krótkiego testu syntetycznego.
6. E6: role bloków w recipe/compiler/runner, bezpieczne przejścia i stabilizacja,
   deklaracja DUT oraz `finally`; ustawienie `interleaved` nie uruchamia jeszcze
   automatycznego przełączania. Brak automatycznych zmian pola lub biasu.
7. E7: integracja modelu zakłóceń, maski/protected regions i zapis decyzji/
   współczynników; walidacja injection/recovery na niezależnych realnych danych.
8. E8: amplituda/szerokość/pozycja/obszar rezonansu, pokrycie niepewności i
   ograniczenia przy interferencji koherentnej. Odejmowanie mocy samo nie
   rozdzieli koherentnego EMI od sygnału o tej samej częstotliwości.

Kolejny zakres programistyczny: kwalifikowane metadane/protected regions,
historia wielu bloków oraz pełne benchmarki i recovery.
Etapy wymagające danych laboratoryjnych pozostają otwarte do dostarczenia
takich danych; nie zastępuje ich wpis `settings_verified=True` w kodzie testu.

## Diagnostyka stabilności surowej referencji

`python -m tools.diagnose_spectrum_reference` czyta zakończone archiwum REF
strumieniowo i eksportuje osobny raport JSON. Nie modyfikuje źródła ani nie
otwiera połączenia z analizatorem. Wymaga surowych sweepów i dokładnie jednego
zatwierdzonego profilu; eksport samej średniej nie jest historią czasową.

Rdzeń utrzymuje do 4096 bloków czasowych dla domyślnie najwyżej 16 sond
częstotliwości. Wybiera rozłożone równomiernie biny i maksimum referencji;
operator może podać indeksy jawnie. To sondy diagnostyczne, nie maska filtra.
Domyślny blok trwa 1 s, a do Allana/ACF potrzeba co najmniej 16 ukończonych
bloków. Ostatni blok jest wyłączony, bo bez następnej granicy nie ma dowodu
jego zakończenia. Raport zachowuje jego licznik, liczniki pozostałych bloków,
czas średni oraz zakresy identyfikatorów źródłowych ramek.

Statystyka Allana używa wszystkich przesunięć sąsiednich średnich m-blokowych
i ma jednostkę W². Konstrukcja wynika z
[NIST SP1065](https://www.nist.gov/publications/handbook-frequency-stability-analysis),
ale tutaj dotyczy próbek mocy. Nie jest stabilnością względnej częstotliwości.
ACF dotyczy średnich blokowych, a stałe biny mają jawnie nieważną ACF.
Braki bloków i nieregularna efektywna kadencja wstrzymują obie analizy;
nie ma interpolacji brakujących danych. Czasy Unix są obsługiwane z kontrolą
rozdzielczości zmiennoprzecinkowej, także dla bloków 100 ms.

Źródło jest hashowane przed i po analizie. Eksport publikuje kompletny,
zsynchronizowany raport atomowo z pliku tymczasowego i nigdy nie nadpisuje
istniejącego celu. Anulowanie sprawdzane jest przy hashach, ramkach,
poszczególnych skalach Allana, lagach ACF i przed publikacją raportu.
Raport ma `qualification=diagnostic_only`, `qualified_ttl_s=null` oraz
`sweep_independence_inferred=false`. Nie nadaje ważności tłu ani niepewności
pomiaru; ścieżka akwizycji czasu rzeczywistego nie wykonuje tej analizy.

Weryfikacja: 29 testów diagnostyki, w tym niezależna konstrukcja Allana,
biały szum, liniowy dryf, stała moc, kadencja, brakujące/reordered sweepy,
granice czasu, niemodyfikowalność tablic, limity pamięci/bloków, uszkodzenia
archiwum, anulowanie, błąd publikacji, wyścig o plik docelowy oraz CLI z
kontrolą jednostek. Zestaw z finalizacją i workflow: 59 testów przeszło.
Ruff oraz `git diff --check` przeszły. Komenda operatora została wykonana
na syntetycznym archiwum: `artifacts/spectrum-reference-diagnostics/reference.h5`;
raport `diagnostic.json` w tym samym katalogu obejmuje 32 bloki / 132 sweepy,
4 sweepy w odrzuconym końcowym bloku, prawidłową kadencję oraz pustą listę
problemów. To artefakt syntetyczny, nie kwalifikacja stanowiska.

## Pochodzenie danych: tryb symulacji

Poprawiono błąd ścieżki ręcznego nagrywania: brak metadanych powodował
domyślne `enabled=false`, również przy symulowanej akwizycji. Okno aplikacji
przekazuje teraz własny tryb uruchomienia do workspace, który zamraża go
w żądaniu sesji. Worker zapisuje `run/simulation_json` z jawnymi
`enabled`, `mode`, `mode_source` i `manual_spectrum`. Przy braku informacji
tryb jest `unknown`, a `enabled=null`; nie zgaduje się go z IDN, nazwy pliku
ani adresu VISA. Niejednoznaczne flagi, np. string `false` lub liczba 1,
są odrzucane. Zmiana trybu w trakcie nagrywania jest zabroniona.

Eksport przenośnej średniej profilu również ma `mode=unknown`, ponieważ
obecny schemat eksportu nie zawiera pochodzenia z archiwum źródłowego.
Sam eksport nie jest dowodem akwizycji sprzętowej. Finalizacja zachowuje
metadane źródłowego SIGNAL. Nadal pozostaje pełne przenoszenie pochodzenia
profili importowanych/eksportowanych, w tym źródłowych archiwów i metadanych
modelu symulacji; nie dodano fikcyjnego ziarna ani wersji modelu.

Weryfikacja tej zmiany: 46 testów store, finalized store, controller,
workflow i layout przeszło. Obejmuje wszystkie trzy stany trybu, rzeczywisty
przebieg REF/SIGNAL przez adapter symulowany, PyThat oraz pokazane okno
aplikacji z izolowanym katalogiem. Ruff i `git diff --check` przeszły.
Szersze uruchomienie istniejącego `tests/test_fluent_shell.py` dało 12 błędów
przed utworzeniem stron: próba zapisu bazy/katalogu
`C:/Users/Shark/Documents/PyLab` poza workspace (`readonly database` /
`PermissionError`). Nie zmieniono uprawnień ani katalogu użytkownika;
nowy test integracji używa własnego katalogu tymczasowego i przechodzi.

## Anulowanie finalizacji offline

Finalizacja archiwów ma teraz token anulowania przekazywany przez worker
do hashów plików (sprawdzenie co 1 MiB), rdzenia obliczeń (między ramkami),
kopiowania raw (między checkpointami) i końcowej granicy zamknięcia.
`Cancel processing` jest dostępny w stałym pasku akcji podczas finalizacji.
Pokazuje `Canceling finalization…`, a potwierdzenie pojawia się dopiero po
powrocie workera. Źródła pozostają otwarte tylko do odczytu.

Anulowanie przed utworzeniem celu nie tworzy pliku. Po rozpoczęciu zapisu
plik zamykany jest ze statusem `aborted`, a weryfikator kompletnego artefaktu
odrzuca go. Plik zachowuje zatwierdzone checkpointy; nie jest automatycznie
kasowany. Powtórzenie finalizacji wymaga nowej nazwy. Jeśli zamknięcie lub
walidacja po anulowaniu zawiedzie, UI otrzymuje błąd, zamiast potwierdzenia
udanego anulowania. Anulowanie po ostatniej sprawdzonej granicy może zbiec
się z poprawnym zakończeniem; wtedy publikowany jest rzeczywisty wynik.

Zamknięcie kontrolera ustawia token bez czekania na kolejkę Qt. Workery
nadal domykają przyjęte checkpointy akwizycji; token nie anuluje ich zapisu.
Drugie żądanie finalizacji jest odrzucane, dopóki pierwsze nie zakończy się,
aby nie wyczyścić jego tokena. `close(wait_ms=500)` nadal ma ograniczony
czas oczekiwania GUI, a wcześniejsza ścieżka zachowania żywego workera działa
przy przekroczeniu tego czasu. Trwająca pojedyncza operacja I/O nie jest
przerywana w środku; nie jest to gwarancja deadline na dowolnym nośniku.

Testy: rdzeń/finalized store/controller 41/41, następnie
controller/workflow/layout 22/22; po dodaniu błędu zamknięcia
finalized store + akcja GUI 17/17. Obejmują anulowanie przed plikiem,
po jednym raw, przy końcowym hash oraz zamknięcie w trakcie obliczeń,
hashy niezmienionych źródeł, status `aborted`, puste `_pending`, zgodność
PyThat i odrzucenie przez replay. Test GUI pokazuje stronę i przycisk,
klika anulowanie oraz sprawdza jego potwierdzenie. Ocenione zrzuty:
`artifacts/spectrum-correction-layout/finalization-processing.png` oraz
`finalization-canceled.png`. Ruff i `git diff --check` przeszły.

To nie kończy pełnej kampanii recovery/kill ani pozostałych etapów planu.

## Raw − background w Current spectrum

Po zgłoszeniu operatora dodano w głównej zakładce selektor
`View → Raw − background [signed W]` oraz przejście `Background…` do
nagrywania/profili. Pokazuje dokładnie wynik z procesora korekcji,
z jednostką W i ujemnymi resztami. Oddzielny widget wykresu pozwala
zachować ustawienia Raw/reference i bezpiecznie wrócić do tego widoku.
Stare filtry oraz ręczny zapis legacy Raw są schowane przy signed W.
Nie ma dodatkowych zapytań VISA ani przeliczania starej referencji.

Publikacja jest powiązana z niezmiennym kontekstem i wynikiem; ścieżka
archiwum jest przypisywana do wyniku przy odbiorze snapshot/finalizacji,
a nie pobierana z aktualnego komunikatu o dowolnej operacji IO. Nowe
nagranie i import bez wyniku unieważniają główny podgląd. Osobne
odczyty Raw nie podmieniają go. Zamrożenie wyświetlanego wyniku
w workspace zatrzymuje także publikację do głównego wykresu.
Eksport CSV tej krzywej ma jawne `frequency_Hz` i `signed_power_w`;
ogólny widget otrzymał opcjonalną nazwę kolumny bez zmiany domyślnego
formatu pozostałych eksportów.

Layout sprawdzono po show/processEvents w obu motywach i przy obu
rozmiarach: widoczny główny wykres, znak ujemny, jednostka W, status,
przełączanie obu widoków, brak nowych poleceń urządzenia i CSV.
Wykonano oraz oceniono zrzuty
`artifacts/spectrum-correction-layout/current-background-{light,dark}-{1500,800}.png`.
Końcowe layout: 4/4. Workflow sprawdza publikację rzeczywistych snapshotów
REF/SIGNAL przez adapter symulowany i ścieżkę artefaktu FINAL.
Po dodaniu unieważnienia importem mimo Freeze: workflow/layout 11/11;
model/plot/workflow w osobnym uruchomieniu 18/18.
Ruff i `git diff --check` przeszły.

Szerszy zestaw modelu/plot/workflow/layout/floating: 40 testów przeszło,
1 nie przeszedł (`test_analysis_completed_applied_across_live_frames`:
cleanup był None po obsłudze zdarzeń). Ten test przeszedł następnie
w izolowanym uruchomieniu. Nie zmieniono jego asercji ani walidacji
wyników; zależność od kolejności asynchronicznych callbacków pozostaje
do zbadania. Nie deklaruje się zielonego pełnego zestawu.

## Świeżość sweepów przy mieszanych dowodach kompletności

Poprawiono wspólną kontrolę liczników w budowaniu REF, procesorze live,
finalizacji i diagnostyce. Wcześniej builder mógł próbować przeliczyć
tekstowy token SINGLE na liczbę, a pozostałe ścieżki porównywały liczniki
tylko w kolejnych ramkach z `instrument_counter`. Przeplot
`counter=40 → qualified SINGLE → counter=40` mógł więc ominąć porównanie.

`SweepCounterGuard` pamięta ostatni licznik urządzenia w aktualnym segmencie
niezależnie od pośrednich tokenów hosta. Powtórzenie i cofnięcie są
odrzucane, a poprawne zwiększenie jest przyjmowane. Tokenów hosta, także
wyglądających liczbowo, nie traktuje się jako liczników instrumentu.
Jawna zmiana segmentu pozwala rozpocząć nową sekwencję liczników;
kontrole kontekstu, generacji, czasu i identyfikatorów ramek pozostają
niezależne. Kontrola ma stałą pamięć, bez historii surowych ramek.

Odrzucony licznik nie zmienia średniej ani liczby użytych sweepów. Worker
nadal archiwizuje jego raw z decyzją odrzucenia; replay stosuje tę samą
kontrolę. Dane historyczne błędnie zaakceptowane przez wcześniejszą kontrolę
mogą teraz zostać odrzucone przy replay — nie są automatycznie uznawane
za dowód świeżości na podstawie wcześniejszego zapisu `accepted=true`.
Nie zmieniono komend urządzenia ani kwalifikacji niezależności sweepów.

Weryfikacja: 127 testów rdzenia, finalizacji, diagnostyki, controller,
workflow i finalized store przeszło. Nowe przypadki obejmują tokeny hosta
opaque/liczbowe/brak tokena, wzrost/powtórzenie/cofnięcie licznika,
zachowanie średniej po odrzuceniu, nowy segment, niezużywanie licznika
przez nieprawidłową próbkę REF oraz rzeczywisty worker/HDF5/replay
z zachowaniem odrzuconego raw. Ruff i `git diff --check` przeszły.
# Rzeczywiste przerwanie procesu i odzyskanie potwierdzonego prefiksu (2026-10-03)

## Benchmark przepływu worker → HDF5 → publikacja → wykres

### Niezależne harmonogramy akwizycji i podglądu (2026-10-04)

`tools/benchmark_spectrum_async_pipeline.py` rozdziela timer producenta,
worker zapisu i timer GUI. Producent czeka na potwierdzenie checkpointu,
podgląd odbiera najnowszy snapshot i może pomijać starsze obrazy. Archiwum
zachowuje każdą surową ramkę. Rzeczywisty `paintEvent` jest mierzony przez
wstrzyknięty podtyp PlotWidget; domyślny widget aplikacji pozostaje ten sam.

Polecenie: `python -m tools.benchmark_spectrum_async_pipeline --frames 200
--warmup 20 --rate "20 Hz" --output
artifacts/spectrum-pipeline-benchmark/async-noisy.json`.
Parametry: 10 001 binów, ten sam szum i seed co poniżej, GUI co 50 ms,
wyświetlony widget 1100×650, Qt offscreen, bez VISA.

Wynik: 220/220 checkpointów, bez utraty ramek, ostatnia pokazana ramka 219,
214 snapshotów, osiągnięte 20.024 Hz według liczby ramek/czasu akwizycji.
Commit p95 38.46 ms, publikacja p95 38.57 ms, setData p95 10.86 ms,
paintEvent p95 13.67 ms. Opóźnienie harmonogramu p95 4.12 ms, max 22.79 ms.
Aktualizacja danych wykresu po odbiorze raw p95 63.24 ms: czas publikacji
nie jest tym samym co czas pojawienia się nowego obrazu.
Stop submit 0.030 ms, zakończenie archiwum 3179 ms. Końcowa średnia została
porównana z niezależną sumą surowych mocy; dodatnia i ujemna cecha zachowane.
Obejrzano `async-noisy.png`.

To krótki syntetyczny pomiar, nie kwalifikacja 30 minut/2 godzin ani
rzeczywistego analizatora. Kwantyle publikacji dotyczą otrzymanych snapshotów,
a nie wszystkich raw; paintEvent nie mierzy fizycznego wyświetlacza.
Mała kolejka przy oczekiwaniu na ACK nie dowodzi odporności na dowolny napływ.
Regresja ze spowolnionym GUI do 5 Hz potwierdza mniej snapshotów przy
zachowaniu wszystkich 14 raw oraz ostatniego wyniku. 20 testów pipeline
i layout passed; `artifacts/spectrum-pipeline-benchmark/async-regression.xml`.
Ruff bez błędów. Zgłoszony przez użytkownika pusty wykres w działającej
aplikacji nadal wymaga odtworzenia; benchmark nie potwierdza jego naprawy.

Aktualizacja optymalizacji (nowy wynik nie zastępuje jeszcze kwalifikacji 20 Hz):
walidacja raw/processed została zwektoryzowana. Odrzuca NaN/Inf, dane zespolone,
tekstowe i zagnieżdżone przed zmianą checkpointów; sprawdza ścisły wzrost osi
po normalizacji do faktycznie zapisywanego f8. Nowy negatywny przypadek
2**60, 2**60+1, 2**60+2 odrzuca oś, której odrębne liczby całkowite stałyby się
tym samym f8. Nie osłabiono żadnej kontroli ani nie usunięto flush.

Dense trace >2000 points używa jednopikselowej linii; pozostałe zachowują 1.6.
Zmiana dotyczy wyłącznie QPen. Nie zmniejsza tablic, nie wygładza sygnału i
nie zmienia CSV, peak search, źródła HDF5 ani istniejącego peak downsampling.
Ustawienie jest zachowane przy zmianie motywu. Test zachowuje pełne 10 001
binów, pojedynczy dodatni i ujemny pik oraz wszystkie wartości eksportu.
Przesłanka Qt/pyqtgraph: [oficjalne wskazówki PlotDataItem](https://pyqtgraph.readthedocs.io/en/latest/api_reference/graphicsItems/plotdataitem.html)
opisują koszt linii szerszych niż 1 px; artefakt optimized-noisy.png obejrzany.

`optimized-noisy.json`: 220/220 committed, lost=0, 18.28 Hz, publication
p95 39.03 ms, commit p95 37.42 ms, setData+processEvents p95 18.70 ms.
Wcześniej: 13.09 Hz, publication 55.45 ms, GUI 28.79 ms. Cel publikacji
≤50 ms w tym krótkim przebiegu spełniony, render ≤16 ms i 20 Hz niespełnione.
Stop/archive-close 2844 ms pozostaje oddzielny od szybkiego submit Stop.
Benchmark serializuje producenta, snapshot i render; nie zastępuje pomiaru
aplikacji z niezależnym harmonogramem GUI i producenta. Wniosek dotyczy tego
przebiegu, nie gwarantowanej szybkości fizycznej akwizycji.

61 tests passed: correction-store, correction-layout, hdf5-writer,
pipeline-benchmark; JUnit optimization-regression.xml; Ruff bez błędów.
Narzędzie `tools/profile_spectrum_pipeline.py --scope worker|gui` zapisuje
pstats w odrębnych przebiegach. Python 3.14 nie pozwolił na dwie jednoczesne
sesje cProfile; pierwsza próba profile-noisy nie jest poprawnym pomiarem.
Profile worker/gui są wskazówką lokalizacji kosztów: zakres GUI zarejestrował
także nakładające się callbacki workera, więc nie przypisujemy wszystkich
czasów wyłącznie jednemu wątkowi ani nie traktujemy profili jako benchmarku
bez narzutu.

Nowy `tools/benchmark_spectrum_pipeline.py` korzysta z rzeczywistego
SpectrumCorrectionController, zatwierdza każdą ramkę, publikuje snapshot do
pokazanego SpectrumPlotWidget oraz zapisuje surowe i skorygowane widma.
Wejście: 10 001 binów, 200 mierzonych + 20 warmup, nominalnie 20 Hz, dodatnia
moc z multiplikatywnym szumem lognormal sigma 0.03, seed 20261003 i dwoma
znanymi cechami dodatnią/ujemną. Weryfikuje końcową korekcję wobec niezależnie
zsumowanych surowych mocy. Żadne polecenia VISA nie są wykonywane.

Wynik `artifacts/spectrum-pipeline-benchmark/nominal-noisy.json`:
220/220 committed, lost=0; osiągnięte 13.09 Hz; commit p95 54.19 ms;
publikacja p95 55.45 ms; setData+processEvents p95 28.79 ms; narastające
opóźnienie harmonogramu do 5.77 s; queue max 1 przy ack-paced producer.
Stop submission 0.042 ms, zakończenie archiwum/walidacji 3090 ms.
Working set: około 164 MB na początku, 187 MB pod koniec akwizycji,
338 MB po Stop/PyThat; peak po Stop około 343 MB. Plik około 108 MB.
To nie spełnia bramki 20 Hz / publication p95 ≤50 ms ani render ≤16 ms.
Mała FIFO bez utraconych ramek przy backpressure nie oznacza dotrzymania
nominalnej szybkości. Wymagana optymalizacja i ponowny pomiar.

Wcześniejszy przebieg na stałej mocy (`nominal-v2.json`) miał publication
p95 30.94 ms i 20.01 Hz; nie wolno nim zastępować trudniejszego noisy case.
Pierwsza próba `nominal.h5` pozostała jako faulted po błędzie w narzędziu;
nie jest poprawnym wynikiem benchmarku i nie została nadpisana.

Pięć testów narzędzia passed: rzeczywisty worker i GUI, liczba checkpointów,
ujemna cecha signed W, status aborted, wyłączność artefaktów i odmowa
nieprawidłowej szybkości przed utworzeniem plików. Ruff bez błędów.
Pamięć pobierana według [Windows PROCESS_MEMORY_COUNTERS](https://learn.microsoft.com/en-us/windows/win32/api/psapi/ns-psapi-process_memory_counters),
oddzielnie working set i peak. Raport deklaruje brak pomiaru OS threads/handles,
tylko jedną próbkę Stop i brak kwalifikacji długiego soak.

## Poprawka pustego Raw − background w głównym widoku

Druga poprawka po ponownym zgłoszeniu pustego wykresu: znaleziono niezależny
błąd zakresu osi. `SpectrumPlotWidget._stable_data_range` uznawał wszystkie
rozstępy ≤1e-15 za stałe i stosował minimalny margines 1.0 także dla SI W.
Reset słabego signed W mógł więc ustawić ±1 W i ukryć kształt sygnału.
Obecnie niezerowy rozstęp jest porównywany dokładnie i zachowuje swoją skalę;
stałe niezerowe W mają względny margines, a dokładne zero W używa wyłącznie
wyświetleniowego zakresu ±1 fW. Dane nie są skalowane, zerowane ani filtrowane.
Pozostałe jednostki zachowują wcześniejszy minimalny margines stałej wartości.

Główny i workspace wykres signed W dopasowują osie po pierwszej ramce nowego
pomiaru/resetu kontekstu. Kolejne aktualizacje nie kasują powiększenia użytkownika.
Regresja odtwarza stare osie X/Y=(0,1), wprowadza wynik ±1e-18 W przy MHz,
sprawdza widoczny zakres obu wykresów oraz zachowanie późniejszego ręcznego
zoomu. Osobne przypadki obejmują wartości stałe dodatnie/ujemne i dokładne zero.
9 testów layout passed; 20 testów weak-frame + plot/floating passed.
Artefakt `artifacts/spectrum-correction-layout/current-background-weak-signal.png`
obejrzany: krzywa ze znakiem widoczna i oś poprawnie opisuje aW.
Rozpoznanie tego błędu w kodzie nie dowodzi, że jest jedyną przyczyną w
konkretnej działającej instancji użytkownika; pytanie o komunikat/status i restart
pozostaje otwarte do czasu odpowiedzi.

Przyczyna: wybór tego widoku wcześniej wyłącznie pokazywał opublikowany wynik
osobnego nagrywania korekcji. Zwykły Start Live odczytywał legacy trace, który
nie dostarczał danych do tego wyniku. Sam wybór profilu lub nagranie REF nie
tworzy SIGNAL i nie powinno być wyświetlane jako odjęty sygnał.

Widok ma teraz własne Record background / Record corrected spectra / Stop
recording, korzystające z istniejącego kontrolera akwizycji i archiwizacji.
Przycisk Live przy wybranym Raw − background uruchamia ten sam dialog SIGNAL;
w trakcie tej sesji służy do Stop. Uruchomiony wcześniej legacy Live nadal
trzeba jawnie zatrzymać przed przejęciem analizatora. Brak wyniku pokazuje
widoczną kartę instrukcji zamiast pustego wykresu, a start i błędy są
odzwierciedlane w głównym widoku. Brak opisu stanu REF kieruje do jego wpisania
bez wysyłania zapytania do instrumentu. Zamrożony, pusty podgląd ma przycisk
Resume preview; nie znosimy zamrożenia automatycznie.

Weryfikacja: 24 testy layout/workflow/controller passed; dodatkowo ponowny
test całego głównego przepływu po dodaniu Resume preview passed. Realny
simulator adaptera: REF 3 sweeps, uruchomienie SIGNAL przez główny przycisk,
4 sweeps, zatrzymanie z głównego widoku, obecna krzywa i ścieżka HDF5,
brak wywołania legacy start_live. Renderowanie light/dark, desktop/narrow;
wizualnie sprawdzono pusty stan narrow-light i wynik desktop-dark.
JUnit: `artifacts/spectrum-correction-layout/main-background-regression.xml`.
To weryfikacja symulacyjna, nie kwalifikacja fizycznego analizatora.

Aktualizacja izolacji PyThat (zastępuje wcześniejszą odmowę przy istniejącym .nc):
plik konwersji powstaje w prywatnym TemporaryDirectory dla każdego importu.
W zakwalifikowanym PyThat 0.2.14 `save_netcdf` wyznacza miejsce z `self.path`;
lokalna podklasa zmienia tę ścieżkę wyłącznie na czas wywołania oryginalnego
save_netcdf i natychmiast przywraca ścieżkę źródła. Konstrukcja zbioru pozostaje
oryginalna, silnik h5netcdf pozostaje wymuszony. HDF5 nie jest kopiowany.
Istniejący sąsiedni .nc nie jest czytany, nadpisywany ani usuwany i nie blokuje
importu. RLock szereguje importer w obrębie procesu, chroniąc globalne opcje
xarray; różne procesy używają odrębnych katalogów tymczasowych.

Uchwyt częściowo skonstruowanego MeasurementTree jest zachowany także po
błędzie __init__: finally zamyka dataset i źródło przed usunięciem katalogu.
Test wstrzykniętego błędu save_netcdf potwierdza zamknięcie źródła i usunięcie
wyłącznie danych tymczasowych. Dwa rzeczywiste procesy importują ten sam
HDF5, zachowując SHA-256 oryginału i zawartość istniejącego .nc. Test dwóch
wątków sprawdza również przywrócenie opcji xarray.

Weryfikacja tej wersji: 52 passed (process-crash, hdf5-writer,
correction-store), Ruff bez błędów. JUnit:
`artifacts/spectrum-process-crash/isolated-pythat-regression.xml`.
Kontrola zamkniętych, spójnych checkpointów przed pełnym importem nadal
obowiązuje. Ta kwalifikacja importu nie zastępuje testu długotrwałej akwizycji
ani pomiarów laboratoryjnych z rzeczywistym sygnałem.

Uzupełnienie importu PyThat: `open_measurement_tree` przed uruchomieniem PyThat
sprawdza nasze archiwa run+points. Pełny import wymaga terminalnego statusu
completed/aborted/faulted, pustego pending oraz kompletnego, ciągłego zestawu
checkpointów. Przerwane archiwum jest dostępne przez czytniki zatwierdzonego
prefiksu; pełny import wymaga uprzedniego odzyskania kopii. Zewnętrzne pliki
thaTEC bez naszego kontraktu nie podlegają tej aplikacyjnej kontroli.
To jawna odmowa pełnego importu, nie konwersja ani automatyczne naprawianie
oryginału. Testy wszystkich faz awarii sprawdzają odmowę przed utworzeniem .nc,
a odzyskany plik nadal przechodzi rzeczywisty round-trip PyThat.

Istniejący plik .nc jest teraz konfliktem: import go nie nadpisuje ani nie usuwa.
Test sprawdza zawartość .nc i SHA-256 HDF5 po odmowie. Nie deklarujemy jeszcze
kwalifikacji równoczesnych importów tej samej ścieżki (sprawdzenie obecności
pliku nie stanowi międzyprocesowej rezerwacji).

Weryfikacja: crash, writer, correction-store oraz heatmap_coordinates —
55 passed; Ruff i diff-check bez błędów (wyłącznie ostrzeżenia LF/CRLF).
Raport JUnit: `artifacts/spectrum-process-crash/pythat-regression.xml`.
Test niekompletnego close potwierdza zgłoszenie błędu końcowej walidacji i
status faulted, zamiast raportowania udanego round-trip.

Uzupełnienie publicznego czytnika: `ThatecRunReader` ogranicza kształty,
timestamps, scalar_series i row_slice naszych oznaczonych wierszy do liczby
zatwierdzonych checkpointów. Zakres obejmuje role setpoint/measurement/spectrum/
spectrum_processed i dynamiczną oś checkpointów. Osi częstotliwości ani
zewnętrznych plików bez naszego kontraktu run+points nie ograniczamy.
Przy opisie wielu wierszy prefiks obliczany jest raz, bez ponownego skanowania
wszystkich checkpointów dla każdego wiersza.

Nowa faza subprocess kończy proces po rzeczywistym append publicznych wierszy,
przed complete. Test wykazuje ukrycie drugiego wiersza, odrzucenie jego odczytu,
odzyskanie pierwszego na kopii i poprawność PyThat po odzyskaniu. Sześć
syntetycznych przypadków sprawdza zewnętrzne pliki oraz niezależne osie.
Testy crash/correction-store/schema-mapper/validator: 41 passed, 1 skipped
(brak laboratoryjnego golden HDF5), 3 subtests passed. Pierwotne trzy testy
publicznego czytnika z licencjonowaną fixture także pozostają skipped.
Raport: `artifacts/spectrum-process-crash/public-regression.xml`.
Bezpośredni import całego zbioru przez PyThat i użycie go w heatmapach pozostają
odrębną ścieżką do sprawdzenia; ta zmiana nie deklaruje ich kwalifikacji.

Uzupełnienie odczytu: prywatny `Hdf5RunReader.summary/detail/points` liczy i
pokazuje wyłącznie ciągły prefiks z `complete=True`. Metody `spectrum` i
`spectrum_point_count` odrzucają surowe widmo bez zatwierdzonego checkpointu,
tak jak wcześniej robiły odczyty envelope/correction. Dodano rzeczywistą
awarię po utworzeniu linku `/spectra`, przed zapisem publicznego wiersza.
Pięć faz crash/clean-close oraz ukierunkowana regresja zapisu i przeglądarki:
22 testy przeszły; Ruff bez błędów. Raport JUnit:
`artifacts/spectrum-process-crash/reader-regression.xml`.

Szersza próba obu modułów przeglądarki nie przeszła: odnotowano błędy i awarię
natywną Qt/QFluent w `style_sheet.register` podczas tworzenia ResultsPage.
Izolowany test osadzonego widoku przy 1280×720 nie przechodzi z powodu
`sqlite3.OperationalError: attempt to write a readonly database` podczas
otwierania katalogu przez MainWindow. Nie traktujemy tej szerszej próby jako
zaliczonej ani nie rozszerzamy uprawnień do rzeczywistego katalogu użytkownika.
Ochrona publicznego czytnika thaTEC po awarii podczas dopisywania publicznych
wierszy wymaga osobnej kwalifikacji; powyższy wynik dotyczy prywatnego odczytu.

`tests/test_spectrum_process_crash.py` uruchamia osobny proces i kończy go
przez `os._exit(73)`, bez destructorów i bez `writer.close()`: po zatwierdzonym
checkpointcie, po flush pending oraz po przeniesieniu pending do points przed
oznaczeniem complete. Kontrola obejmuje także zwykłe zamknięcie aborted.
W lokalnym HDF5 2.0.0 wszystkie cztery pliki były czytelne; to wynik tego
środowiska, nie gwarancja dla innych bibliotek/systemów ani utraty zasilania.

Resume sprawdza kompletność całego zachowywanego, zewnętrznie potwierdzonego
prefiksu. Niekompletny ogon poza tym prefiksem usuwa wraz z pending i skraca
reprezentację publiczną. Niekompletny wpis wewnątrz prefiksu pozostaje błędem.
Test odzyskuje wyłącznie kopię, porównuje SHA-256 oryginału, czyta signed W
oraz waliduje odzyskany plik z `require_pythat=True`.

Weryfikacja: 19 testów process-crash i correction-store przeszło; Ruff dla
zmienionego writera i nowego testu bez błędów. Obserwacje faz i wersja HDF5:
`artifacts/spectrum-process-crash/*.json`.

Jeśli inna biblioteka blokuje otwarcie po awarii, test odnotowuje rzeczywisty
OSError i odmowę resume bez zmiany oryginału. Nie usuwa blokad ani flag ręcznie.
[Oficjalna dokumentacja h5clear](https://support.hdfgroup.org/documentation/hdf5/latest/_h5_t_o_o_l__c_r__u_g.html)
opisuje usuwanie pozostawionej flagi superblock; nie jest to narzędzie ogólnej
naprawy uszkodzeń. Automatyczna naprawa i kwalifikacja zaniku zasilania pozostają
niezaimplementowane; nie wolno utożsamiać powyższego testu z taką kwalifikacją.

## Diagnostyka wariancji parametrów rezonansu — 2026-10-04

Dodano narzędzie `tools.diagnose_spectrum_standard_errors`. Porównuje ono
rozrzut dopasowanych parametrów między niezależnymi próbami z macierzą
lokalnej kowariancji sandwich, bez wykonywania replik bootstrapowych.
Generator `synthetic_trial` jest współdzielony z testem pokrycia; test regresji
sprawdza dokładną zgodność tablic i ziarna bootstrapu ze wcześniejszym
generatorem v1, również dla ujemnego sygnału. Kolejność losowań REF/SIGNAL
została zachowana.

Przeprowadzono po 2000 prób dla obu znaków sygnału, seed 20261006,
40 niezależnych bloków na źródło i 101 punktów częstotliwości. We wszystkich
4000 próbach dopasowanie i estymacja kowariancji zakończyły się powodzeniem.
Raporty zachowują każdą estymatę, pełną macierz kowariancji, identyfikator próby
i ewentualny błąd. Momenty przy niepowodzeniach byłyby jawnie warunkowe na
udanych próbach; niepowodzenia nie zamieniają się w zerowe błędy.

Stosunek empirycznej wariancji parametrów do średniej estymowanej wariancji:

| Parametr | Sygnał dodatni | Sygnał ujemny |
| --- | ---: | ---: |
| Amplituda | 1.01332 | 1.01338 |
| Centrum | 1.03760 | 1.03665 |
| FWHM | 0.99218 | 0.99230 |
| Pole w skończonym oknie | 1.02061 | 1.02066 |

Jest to diagnostyka jednego generatora syntetycznego, nie dowód dokładności
wariancji dla dowolnego pomiaru. Kwantyle 2.5%/97.5% standaryzowanego błędu
centrum dla sygnału dodatniego wyniosły -2.05823 i 2.11449; FWHM: -1.97742
i 1.96669. Zatem bliskość średnich wariancji sama nie dowodzi pokrycia
przedziałów. Wyniki nie uzasadniają automatycznego mnożnika korekcyjnego.
Nie zmieniono estymatora ani przedziałów w aplikacji. Dotychczasowe nieudane
testy pokrycia 95% pozostają obowiązujące; `coverage_qualified` oraz
`laboratory_qualified` w nowych raportach są false.

Polecenia i artefakty:

```text
python -m tools.diagnose_spectrum_standard_errors --output artifacts/spectrum-bootstrap/sandwich-diagnostic-2000-positive.json
python -m tools.diagnose_spectrum_standard_errors --output artifacts/spectrum-bootstrap/sandwich-diagnostic-2000-negative.json --negative
```

Weryfikacja: 21 testów generatora, diagnostyki, dotychczasowego testu pokrycia
i studentyzacji przeszło; JUnit:
`artifacts/spectrum-bootstrap/standard-error-regression.xml`.
Następny krok: sprawdzić rozkład pivotów bootstrapowych względem błędów
niezależnych prób, a każdą zmianę konstrukcji CI zweryfikować ponownie na
co najmniej 1000 niezależnych próbach walidacyjnych. Diagnostyka wariancji
nie zastępuje tego testu ani laboratoryjnej kwalifikacji.

## Sparowana diagnostyka granic bootstrapu — 2026-10-04

`tools.diagnose_spectrum_bootstrap_pivots` łączy archiwalny raport pokrycia
studentyzowanych przedziałów 95% z raportem diagnostyki sandwich dla tych
samych prób. Weryfikuje generator, wersje NumPy/SciPy, ziarno, parametry
generatora, znak sygnału, jednostki, kolejność parametrów, identyfikatory
i ziarna prób oraz kompletność wejściowych raportów. Wyłącznie wspólny
początkowy ciąg prób wchodzi do porównania; liczby nieporównanych prób
i brakujących par są jawne. Źródła są tylko odczytywane, ich SHA256
sprawdzane przed i po odczycie, a wynik musi otrzymać nową ścieżkę.

Ze wzoru `CI = estimate - reversed(quantiles) * original_SE` odtwarzane są
granice pivotu. Nie wykonywano kolejnych 200000 dopasowań: wykorzystano
istniejący raport 1000 prób / 200 replik i dopasowania z identycznych prób.
To rekonstrukcja diagnostyczna; zgodność deklarowanej proweniencji nie
dowodzi braku nieudokumentowanych zmian wcześniejszego kodu.

| Parametr | Kwantyle błędu niezależnych prób 2.5% / 97.5% | Mediany granic bootstrapu | Poniżej / powyżej granic |
| --- | --- | --- | --- |
| Amplituda | -1.88332 / 2.04778 | -1.92451 / 1.93734 | 25 / 34 |
| Centrum | -2.05968 / 2.10991 | -1.93102 / 1.94865 | 36 / 31 |
| FWHM | -2.03419 / 2.07751 | -1.93449 / 1.93061 | 36 / 34 |
| Pole w oknie | -1.93097 / 2.06698 | -1.93240 / 1.93033 | 26 / 31 |

Wszystkie 1000 par są dostępne. Pokrycie odtworzone z granic pivotu jest
identyczne z archiwalnym wynikiem: 941/933/930/943 na 1000. Mediany granic
są opisem rozkładu zmiennych granic, nie zamiennym przedziałem dla pomiaru.
Ten test wskazuje na niewystarczające granice bootstrapowe w badanym
scenariuszu, lecz nie rozstrzyga między skończoną liczbą replik, skończoną
liczbą bloków i pozostałymi efektami estymacji. Nie zastosowano mnożnika
ani zmiany poziomu ufności, a kwalifikacja pokrycia nadal jest false.

Artefakt: `artifacts/spectrum-bootstrap/studentized-pivot-diagnostic-1000.json`.
Zachowano także osobną rekonstrukcję istniejącego testu 100 prób / 1000
replik: `studentized-pivot-diagnostic-100-resamples1000.json`; ma ona mniejszą
liczbę prób i nie zastępuje wymaganej pełnej walidacji.

Weryfikacja: 31 testów rekonstrukcji, odmowy niezgodnej proweniencji,
liczenia brakujących par, niezmienności źródeł, sparowanego porównania
i diagnostyki SE przeszło; Ruff bez błędów. JUnit:
`artifacts/spectrum-bootstrap/pivot-diagnostic-regression.xml`.
Pozostaje ocena metody przedziałów o uzasadnieniu statystycznym i ponowny
niezależny test pokrycia; powyższe raporty nie kwalifikują pomiaru w laboratorium.

## Rozszerzone przypadki zachowania sygnału — 2026-10-04

Generator `tools.qualify_spectrum_signal_preservation` obejmuje dodatkowo
`broad_gaussian`, `weak_gaussian`, `reference_contaminated` oraz
`shifted_reference_resonance`. Przypadki przechodzą przez rzeczywisty core
korekcji i konwersję W → dBm → W. Istniejące scenariusze i kolejność ich
losowań zachowano. Raport podaje szerokość i próbkowanie oddzielnie dla
każdego scenariusza oraz jawnie określa stosowalność bramek wysokiego SNR.
Referencja zawierająca sygnał jest tak opisana także w stanie profilu;
nie jest fałszywie deklarowana jako signal-free.

Wynik 100 prób na scenariusz, seed 20261008, 1001 binów:

| Przypadek | Udane / nieudane dopasowania | Średni względny błąd amplitudy | Interpretacja |
| --- | --- | ---: | --- |
| Szeroki Gaussian, FWHM 160 kHz | 100 / 0 | -0.0000003585 | Bramki wysokiego SNR przeszły |
| Słaby Gaussian, amplituda 0.1 pW | 100 / 0 | 0.004460 | Niski SNR; brak kwalifikacji dokładności wysokiego SNR |
| Ten sam rezonans w REF i SIGNAL | 100 / 0 | -1.000009 | Sygnał skasowany przez odejmowanie; różnica stanów |
| Rezonans w REF przesunięty o 2 FWHM | 100 / 0 | 0.027998 | Różnica stanów; nie jest czystym sygnałem |

P95 bezwzględnego względnego błędu amplitudy dla słabego sygnału wynosi
0.10415. Sam niewielki średni bias nie wystarcza do uznania go za dokładnie
odzyskany. Wszystkie bramki przypadków niekwalifikujących się do oceny
wysokiego SNR pozostają false, zamiast ogłaszać sukces na podstawie samego
numerycznego dopasowania. Test skażenia REF pokazuje ograniczenie
identyfikowalności; nie stanowi detektora skażenia nieznanej referencji.

Aktualny raport: `artifacts/spectrum-bootstrap/extended-preservation-100-v2.json`.
Pierwszy raport `extended-preservation-100.json` zachowano jako poprzednią
wersję przed rozszerzeniem metadanych REF. Weryfikacja: 10 testów
zachowania sygnału i dopasowań SI przeszło, Ruff bez błędów; JUnit
`extended-preservation-regression-v2.xml`. Nie kwalifikuje to pokrycia CI,
fałszywych detekcji ani rzeczywistego toru pomiarowego.

## Pełny test konfiguracji 1000 replik — uruchomiony 2026-10-04

Uruchomiono test odpowiadający domyślnej liczbie replik w GUI:

```text
python -m tools.qualify_spectrum_bootstrap_coverage --output artifacts/spectrum-bootstrap/studentized-validation-1000-resamples1000-positive.json --repetitions 1000 --resamples 1000 --interval-method studentized --seed 20261007
```

To oddzielny seed walidacyjny. W chwili dopisania tej sekcji proces był
potwierdzony jako działający przez jego uchwyt wykonania i miał ponad
50 zapisanych prób. Brak jeszcze końcowego raportu i wniosków o pokryciu.
Dziennik `*.trials.jsonl` jest częściowym zapisem, nie wynikiem ukończonej
kwalifikacji i nie mechanizmem resume. Przy kolejnym sprawdzaniu trzeba
ustalić rzeczywisty stan procesu; ten historyczny wpis nie dowodzi,
że proces nadal działa. Dotychczasowe nieudane testy zachowują ważność.

## Globalny test różnicy widm i fałszywe alarmy — 2026-10-04

Dodano offline core `app/spectrum/spectral_difference_test.py`. Statystyka
to maksimum bezwzględnej różnicy średnich REF/SIGNAL, standaryzowanej
niezmienną względem etykiet skalą całej puli, na jawnej, ustalonej masce
częstotliwości. Losowane są etykiety całych bloków F-vector; nie rozrywa się
korelacji częstotliwościowych ani nie losuje REF osobno dla każdego binu.
Test nie modyfikuje danych, nie zeruje binów i nie nadaje etykiety rezonansu
magnetycznego. Wynik to globalny alarm różnicy stanów.

Wybrano `p = (exceedances + 1)/(permutations + 1)`, z zachowawczą obsługą
remisów numerycznych. Uzasadnienie dodatniej poprawki i wymagań testu
permutacyjnego: [Phipson i Smyth, 2010, wersja autorów](https://gksmyth.github.io/pubs/PermPValuesPreprint.pdf).
Źródło nie kwalifikuje specyficznej implementacji ani toru pomiarowego.

Niezależność bloków i wymienność REF/SIGNAL pod pełną hipotezą zerową są
oddzielnymi jawnymi flagami z obowiązkową dokumentacją. Domyślnie false;
wówczas nie powstaje p-value ani alarm ilościowy. Wymagane jest co najmniej
20 bloków na źródło, limit 256, do 10001 częstotliwości i limit jawnych
buforów. Biny identyczne nie generują alarmu; nierozwiązywalna numerycznie
wariancja jest błędem, nie pozornie pewnym wynikiem. Dostępne są callbacki
postępu i anulowania. Pojedynczy test nie kwalifikuje wielokrotnych spojrzeń
Live, dryfu, reszt po dopasowaniu modelu EMI ani silnej kontroli dla
częściowych alternatyw. Integracja z analizą archiwów i GUI pozostaje do
wykonania; obecnie to core i narzędzie walidacji syntetycznej.

`tools.qualify_spectrum_difference_test` stosuje ustaloną przed walidacją
alpha 0.005 i 999 permutacji. Core pozwala na inne jawne alpha (domyślnie
0.01); wynik walidacji nie może zostać przeniesiony na inną konfigurację.
Każda próba ma oddzielny strumień danych i permutacji. Generator tworzy
niezależne dodatnie moce gamma z korelacją częstotliwościową i wspólną modą
widmową. Known injected signal z generatora bootstrapowego jest usuwany,
aby obie grupy miały ten sam zadeklarowany rozkład tła. To nie jest fizyczny
model RBW ani dowód wymienności realnych danych.

Pierwszy test, 1000 prób, seed 20261010, 40 bloków na źródło, 101 binów
1–2 MHz: 4 alarmy, 0 nieudanych testów. Frakcja 0.004; dokładny dwumianowy
95% przedział [0.00109091, 0.01020966]. Bramka górnej granicy ≤0.01 nie
przeszła — wyniku nie zaokrąglono do sukcesu. Nieudane testy, gdyby wystąpiły,
byłyby liczone zachowawczo jako alarmy przy wyznaczaniu górnej granicy.
Artefakt: `artifacts/spectrum-bootstrap/global-difference-null-1000.json`.
Pierwsze wykonanie ukończyło próby, ale nie opublikowało JSON z powodu
typu numpy.bool w bramce; poprawiono typ i dodano regresję serializacji/CLI,
następnie ponownie wykonano identyczny deterministyczny test. Nie utracono
żadnych danych laboratoryjnych.

Uruchomiono niezależny test 5000 prób, seed 20261011, bez zmiany alpha ani
liczby permutacji: `global-difference-null-5000-independent.json`.
W chwili dopisania sekcji proces działał; brak jeszcze ukończonego raportu.
Stan musi być sprawdzony przez aktywny uchwyt procesu, nie przez ten wpis.

Weryfikacja: 16 testów core/kwalifikacji/CLI przeszło; dodatni i ujemny
sygnał, stałe biny, maska wyszukiwania, powielone skorelowane biny,
skalowanie W, limity pamięci, anulowanie, brak kwalifikacji, jawne błędy
i zachowawczy mianownik. Ruff bez błędów. JUnit:
`artifacts/spectrum-bootstrap/global-difference-regression-v2.xml`.
Nie jest to jeszcze odbiór wymaganego detektora rezonansów.

## Wynik niezależnej walidacji globalnej różnicy — 2026-10-04

Proces 5000 prób, seed 20261011, zakończył się poprawnie. Konfiguracja
alpha 0.005 / 999 permutacji pozostała niezmieniona. Wystąpiło 27 alarmów,
0 błędów testu: 0.0054. Dokładny dwumianowy 95% przedział wynosi
[0.00356156, 0.00784706]; górna granica jest mniejsza od 0.01. Wszystkie
bramki tego raportu przeszły. Nie zastępuje to nieudanego raportu 1000 prób;
oba wyniki są zachowane i jawnie opisane.

Jest to walidacja jednego ustalonego pełnego null, 101 binów 1–2 MHz,
40 niezależnych bloków na źródło i pojedynczego spojrzenia. Nie dowodzi
kontroli alarmów dla 10001 binów, częstego odświeżania Live, dryfującego
lub niewymiennego tła, reszt modelu EMI ani identyfikacji rezonansu.
`synthetic_gate_passed` jest true; `laboratory_qualified` pozostaje false.
Raport: `artifacts/spectrum-bootstrap/global-difference-null-5000-independent.json`.

## Globalna różnica z zamkniętych archiwów — 2026-10-04

Dodano `app/storage/spectral_difference_store.py` oraz CLI
`tools.detect_spectrum_difference`. Kontrole wejść i publikację raportu
wydzielono do wspólnego `analyze_spectral_archives` w dotychczasowym
module analizy bootstrapowej; bootstrap korzysta z tych samych kontroli.
Każda analiza dostarcza własny preflight pamięci przed alokacją bloków.
Nie wprowadzono drugiej, mniej rygorystycznej ścieżki odczytu raw.

REF musi być completed, SIGNAL completed lub aborted, oba bez pending,
z jednym wspólnym profilem i kontekstem. Weryfikowane są kompletne punkty,
role, decyzje o wkładzie ilościowym, oś, generacja ustawień, segment,
kolejność/counter, odtworzenie hasha profilu z raw i rozłączność czasowa.
Surowe dBm są konwertowane do W jeden raz. Analiza reszt modelu EMI jest
odrzucana. Częściowy końcowy blok wymaga jawnej zgody na odrzucenie i jest
policzony w manifeście. SHA256 źródeł sprawdza się przed i po obliczeniu.
Raport zawiera zakres żądany i rzeczywiście przeszukane częstotliwości,
konfigurację, bloki/frame/time ranges, simulation, IDN, settings SHA,
kwalifikacje kontekstu i hashe źródeł. Zapis nowego JSON jest atomowy,
exclusive, poprzedzony flush/fsync i sprawdzeniem anulowania.

CLI wymaga jawnych jednostek zakresu i alpha. Domyślnie obie flagi założeń
są false, zatem powstaje raport unqualified bez p-value. Nawet warunkowy
test nie ustawia `false_alarm_rate_qualified` ani `laboratory_qualified`
dla analizowanego archiwum. Poprzednia walidacja syntetyczna nie może
automatycznie kwalifikować realnej konfiguracji i danych.

```text
python -m tools.detect_spectrum_difference REF.h5 SIGNAL.h5 --output new-report.json --block-sweeps 10 --search-start "1 MHz" --search-stop "2 MHz"
```

Weryfikacja: 20 testów współdzielonego odczytu/bootstrapu/testu różnicy
przeszło, w tym source unchanged, niezgodne źródła, preflight przed alokacją,
anulowanie po fsync, zmiana SHA i jawne jednostki CLI. Poprzedni łączny
test core/global difference i bootstrap-store: 28 testów przeszło. Ruff
bez błędów. JUnit: `shared-archive-analysis-regression.xml`.
Wykonano CLI na istniejących syntetycznych archiwach demo-v1; nowe raporty
`difference-unqualified.json` i `difference-conditional.json`. Dane demo
nie są laboratoryjną kwalifikacją wymienności.

Integracja asynchroniczna i GUI dla globalnego testu pozostają do wdrożenia.
Pełny test pokrycia 1000 prób / 1000 replik nadal był potwierdzony jako
działający (ostatni odczyt postępu: 275/1000); nie ma jeszcze końcowego wyniku.

## Asynchroniczna analiza globalnej różnicy — 2026-10-04

`SpectrumCorrectionController` przyjmuje teraz niemutowalny
`SpectrumDifferenceRequest`: osobne ścieżki REF/SIGNAL/nowego JSON,
liczbę sweepów na blok, jawny zakres w Hz, politykę końcówki oraz
`SpectralDifferenceTestConfig`. `difference_archives` zgłasza operację
`spectral_difference` do istniejącego workera CPU/storage.
Obliczenia oraz odczyt/hash plików pozostają poza wątkiem GUI.

Worker odrzuca niewłaściwy typ żądania i aktywny writer. Wspólny mechanizm
offline_busy/session_pending blokuje równoległą analizę różnicy,
bootstrap, finalizację i rozpoczęcie nagrywania. Próba analizy podczas
aktywnego nagrywania jest odrzucana przed kolejką; nie faultuje otwartego
archiwum. Po ukończeniu, anulowaniu lub błędzie blokada offline jest
zwalniana i worker może przyjąć kolejne poprawne żądanie.

Postęp zaczyna się od 0/liczba permutacji i jest ograniczony do około
10 zdarzeń GUI/s oraz końcowego zdarzenia wykonanej pętli. Nie udaje
postępu hashowania ani permutacji pominiętych przez dokładne rozwiązanie
dla identycznych danych. Cancellation Event sprawdzany jest przez odczyt,
obliczenia i przed publikacją raportu. Shutdown ustawia anulowanie,
odprowadza kolejkę i zwalnia wątek; nie używa hard kill.

Weryfikacja: wspólna seria testów global-difference/controller/bootstrap
zakończyła się 19 passed; oddzielny dodany test shutdown 1 passed. Testy
obejmują realne archiwa syntetyczne i worker, heartbeat GUI, monotoniczny
ograniczony postęp, wzajemną wyłączność operacji, anulowanie podczas pętli
permutacji, brak nowego raportu po anulowaniu, niezmienność źródeł,
ponowne użycie po błędzie/anulowaniu oraz poprawny commit raw po odrzuceniu
analizy podczas nagrywania. Existing destination pozostaje niezmieniony.
Żadne testy nie wykonywały komend sprzętowych.

JUnit: `artifacts/spectrum-bootstrap/difference-controller-regression.xml`
i `difference-controller-shutdown.xml`. Ruff oraz diff whitespace check
bez błędów. Interfejs okna dla tego testu globalnego pozostaje do dodania;
nie jest to zakończenie całego etapu GUI ani detektora rezonansów.
Ostatni potwierdzony odczyt procesu pełnego testu pokrycia: 350/1000;
proces nadal działał, bez opublikowanego końcowego raportu.

## Okno globalnej różnicy w aplikacji — 2026-10-04

W Background correction dodano `Compare recorded REF / SIGNAL…`, otwierające
Fluent `SpectrumDifferenceDialog`. Okno działa także bez połączenia
z urządzeniem, używa osobnego workera offline, ma pojedynczą instancję
w workspace i jest pokazywane WindowModal bez zagnieżdżonego exec.
Podczas nagrywania lub operacji I/O profilu przycisk jest niedostępny.
Shutdown workspace obejmuje również worker tego okna.

Formularz zawiera REF, SIGNAL i nowy JSON, jawny zakres z jednostkami,
sweepy/blok, liczbę permutacji, alpha z jednostką procentową, politykę
końcówki i dwie jawne kwalifikacje założeń z dowodem. Domyślne założenia
są false; alpha to 0.5%, permutacje 999. Bez założeń zapisuje się raport
unqualified i wyraźnie pokazuje brak p-value. Wynik warunkowy podaje
globalną różnicę albo brak alarmu, p i alpha, liczbę bloków i przeszukanych
binów; nie identyfikuje rezonansu ani nie ogłasza laboratoryjnej
kwalifikacji. Brak alarmu nie jest dowodem nieobecności sygnału.

Po rozpoczęciu natychmiast widać odczyt źródeł i wskaźnik aktywności.
Formularz zostaje zablokowany, postęp dotyczy rzeczywistych permutacji,
anulowanie jest dostępne. Błędy jednostek, odwróconego zakresu,
braku dowodu lub zbyt małej rozdzielczości p-value są jawne. Istniejący
raport nie jest nadpisywany. Zamknięcie czeka asynchronicznie na zakończenie
workera po anulowaniu; nie usuwa aktywnego QThread ani źródeł.

Weryfikacja: 7 testów nowego okna przeszło (oba motywy, normalny desktop
1000×900 i węższe 750×700, domyślne założenia false i wynik warunkowy,
realne archiwa syntetyczne/worker, widoczny wynik i przyciski, błędy,
anulowanie oraz otwieranie pojedynczej instancji bez komend instrumentu).
W poprzednim wspólnym uruchomieniu dodatkowe 16 testów istniejącego okna
rezonansu i layoutu korekcji przeszło. Dwa nowe testy początkowo miały
niepoprawne arbitralne wymaganie wysokości etykiety >20 px, mimo poprawnej
wysokości tekstu 19 px. Zastąpiono je weryfikacją względem fontMetrics
oraz pełnego położenia widgetu w oknie; wszystkie 7 przeszło ponownie.
Nie dodano sztucznej wysokości ani lokalnego stylu do produktu.

Zrzuty `artifacts/spectrum-difference-ui/result-light-1000-True.png` i
`result-dark-750-True.png` obejrzano: wyniki/akcje widoczne, kontrast
poprawny, formularz w węższym rozmiarze przewijany. JUnit końcowych testów:
`dialog-regression-v2.xml`. Ruff oraz diff whitespace check bez błędów.

Pełny test pokrycia nadal był potwierdzony jako działający przy 475/1000
prób. Nie ma jeszcze końcowego raportu i nie zmieniono statusu CI w GUI.

## Ograniczone partie permutacji dla 10001 binów — 2026-10-04

Dla osi dłuższych niż 2000 punktów globalny test różnicy oblicza teraz
16 permutacji w jednej partii przez macierz binarnych przydziałów etykiet
i przemnożenie przez tę samą wycentrowaną/standaryzowaną pulę widm.
Mniejsze siatki zachowują dotychczasową pętlę; wyniki ukończonej walidacji
101 binów nie zmieniają się. Kolejność losowań i statystyka pozostają te
same; zmienia się sposób obliczenia sum. Bufor nie rośnie do B×F.
Preflight archiwów i core obejmuje dodatkowe 48 wektorów roboczych dla
szerokiej siatki. Postęp odzwierciedla zakończone partie, a anulowanie
sprawdzane jest przed każdą partią. Raport core zawiera batch size.

Test porównawczy na 10001 binów, 20+20 bloków, 999 permutacji:
identyczne 426 przekroczeń i identyczne p-value przy obliczeniu każdej
permutacji osobno oraz w partiach. Siatka ma powtórzony skorelowany wzór
31 binów, aby oddzielić zgodność obliczeń od zmiany problemu statystycznego.
Jawne bufory mają mniej niż 16 MiB. Pierwszy pomiar czasu dał 1.789 s
versus 0.135 s; ponowne sprawdzenie po dodaniu metadanych, równolegle z
dwoma długimi walidacjami CPU: 3.361 s versus 0.793 s. To jeden przypadek
porównawczy, nie kwalifikacja czasu ani peak RSS aplikacji.
Aktualny artefakt: `artifacts/spectrum-bootstrap/wide-permutation-comparison.json`.

Weryfikacja: 17 testów core/walidacji przeszło; dodany test anulowania po
pierwszej partii również przeszedł. Końcowe sprawdzenie obu szerokich
przypadków 2 passed; 13 testów archive-store/controller przeszło po zmianie
preflightu. Ruff i diff whitespace check bez błędów. JUnit:
`wide-permutation-regression.xml`, `wide-grid-final-regression.xml` oraz
`wide-permutation-integration.xml`.

Narzędzie null-validation dopuszcza teraz pełne 10001 punktów. Uruchomiono:

```text
python -m tools.qualify_spectrum_difference_test --output artifacts/spectrum-bootstrap/global-difference-null-5000-grid10001.json --repetitions 5000 --points 10001 --seed 20261012
```

Jest to nowy seed i pełny zakres 1–2 MHz z 10001 binami, z niezmienioną
alpha 0.005 / 999 permutacji / 40 bloków na źródło. Proces był potwierdzony
jako działający i zakończył ponad 200 prób; nie ma jeszcze końcowego
raportu. Walidacja 101 binów nie jest odbiorem tej siatki. Generator ma
korelację sąsiednich binów i wspólną modę widmową, nie fizyczny model RBW.
Pełny test CI 1000×1000 również nadal działał, ostatni postęp 550/1000.

## Odpowiedź czasowa skoku amplitudy — 2026-10-04

Dodano `tools.qualify_spectrum_temporal_response`: 100 prób każdego z
trzech scenariuszy (dodatni skok, ujemny skok, dodatni skok przy nieregularnej
kadencji). Każda próba tworzy nową referencję z 40 sweepów, a następnie
przeprowadza te same 240 raw SIGNAL przez trzy rzeczywiste procesory:
BLOCK, WINDOW 32 klatki i EMA_PREVIEW tau 1 s. Skok następuje po 40
klatkach SIGNAL. Dodatni szum mocy gamma ma shape 16 i korelację trzech
sąsiednich binów. Konwersja W → dBm → W jest rzeczywiście wykonywana.
Seed 20261013, 101 binów. Regularna kadencja to 50 ms, nieregularna
30–70 ms. Ujemna amplituda wynosi -100 pW, dodatnia +100 pW; całkowita
moc wejściowa pozostaje dodatnia.

Amplituda jest mierzona projekcją na znany profil Gaussian, a nie przez
automatyczne wykrywanie lub selekcję pików. Porównanie korzysta z zamkniętych
wzorów dyskretnej odpowiedzi: dla EMA jest to wykładnicza odpowiedź
wyznaczona z rzeczywistych odstępów próbek, WINDOW przyrasta według liczby
próbek po skoku, a BLOCK rozcieńcza zmianę we wszystkich zachowanych
klatkach. Każda odrzucona klatka czyni próbę metody nieudaną; nie usuwa
się jej z mianownika i nie wstawia zerowego błędu.

W 300 próbach × 3 metody nie odrzucono klatek. Średni RMS błędu
znormalizowanej odpowiedzi wyniósł około 3.6e-5–5.3e-5 amplitudy skoku;
maksimum we wszystkich przypadkach wyniosło około 5.1e-4. Osobny test
bez szumu potwierdza zgodność całej krzywej z wzorem z błędem <1e-10
dla obu znaków oraz nieregularnej kadencji.

Dla przykładowej regularnej próby opóźnienie pierwszej próbki przekraczającej
63.2% zmiany: BLOCK 3.45 s, WINDOW 1.05 s, EMA około 1–1.05 s. Granica
EMA może przypaść między próbkami albo na próbkowany próg; szum i
precyzja arytmetyki mogą przesunąć pierwszy crossing o jeden krok 50 ms.
Opóźnienie liczy się od ostatniej próbki przed skokiem, nie od zmierzonego
przejścia sprzętu. Nie jest to kwalifikacja stabilizacji ani latencji GUI.
WINDOW ma zależną od kadencji odpowiedź w sekundach; czasowa EMA
wykorzystuje rzeczywiste odstępy próbek. BLOCK nie jest podglądem
natychmiastowej amplitudy zmiennego sygnału.

Raport: `artifacts/spectrum-bootstrap/temporal-response-100.json`.
Wykres przykładów, oglądnięty po eksporcie standardowym Matplotlib:
`temporal-response-100.png`. Pokazuje po jednej próbie i wzór przerywaną
linią, bez fikcyjnych pasm ufności. Raport laboratory_qualified=false;
nie wnioskuje niezależności, CI ani zachowania dowolnego rezonansu.

Weryfikacja: 47 testów temporal-response/realtime-processor przeszło,
w tym dodatni/ujemny skok, jitter, dokładny wzór, jawne odrzucenia,
powtarzalność i CLI/no-overwrite. Ruff po usunięciu zbędnego importu
bez błędów; diff whitespace check bez błędów. JUnit:
`artifacts/spectrum-bootstrap/temporal-response-regression.xml`.
Równoległe walidacje pełnego CI i 10001-binowego alarmu nadal działały
przy ostatnim odczycie: odpowiednio 625/1000 i 1400/5000.

## Wspólny i asynchroniczny trening modelu REF — 2026-10-04

Przeniesiono przetwarzanie specyfikacji treningu z narzędzia CLI do
`app/storage/interference_training_store.train_from_specification`.
CLI używa teraz tej samej funkcji co worker. Konwersja jawnych przedziałów
z jednostkami do masek jest wspólna w `app/spectrum/frequency_regions.py`.
Nie dodano drugiego algorytmu SVD ani luźniejszej interpretacji specyfikacji.
Zachowano jednostki control_sigma, limit regionów, walidację zakresów
na rzeczywistej osi, preflight pamięci, stałą referencję i ograniczenia
koeficjentów istniejącego treningu. Dodatkowo completed REF z niepustym
`_pending` jest odrzucany.

Controller ma niemutowalny `SpectrumInterferenceTrainingRequest`:
REF, nowy HDF5 oraz JSON object ograniczony do 64 KiB UTF-8. JSON string
zapobiega mutacji masek/specyfikacji podczas oczekiwania w kolejce.
`train_interference_archive` uruchamia operację `train_interference`
na workera CPU/storage. Wspólna blokada offline i sesji obejmuje trening
oraz wcześniejsze analizy. Worker wymaga braku otwartego writera i właściwego
typu requestu. Wynik to ścieżka completed artefaktu oraz kalibracja.

Przy rozpoczęciu wysyłane jest zdarzenie 0/0 oznaczające postęp
nieokreślony. Nie fabrykuje się procentów SVD lub hashowania. Anulowanie
jest przekazywane do odczytu, obu przejść REF i publikacji; natywny LAPACK
może zakończyć bieżące SVD przed sprawdzeniem Event. Dotychczasowe zasady
retencji aborted artefaktu po anulowaniu rozpoczętego zapisu pozostają.
Kwalifikacja control regions nie jest automatycznie ustawiana po treningu;
default false i dowód/operator deklaracja pozostają jawne. Wynik nadal
wymaga oddzielnej walidacji held-out REF oraz testu zachowania SIGNAL.

Weryfikacja: 28 testów training-controller/interference-training/
correction-controller przeszło, w tym self-contained source unchanged,
PyThat require_pythat=True, reimport hasha kalibracji, wzajemna wyłączność
z analizą globalną, anulowanie i reuse, istniejący cel bez nadpisania,
odrzucenie pending REF i ograniczony JSON. Pierwsze uruchomienie testu
64-KiB string miało zbyt długi parametr ID dla ścieżki temp Windows;
nazwy przypadków skrócono, ponownie uruchomiona seria jest w całości zielona.
Ruff i diff whitespace check bez błędów. JUnit:
`artifacts/spectrum-bootstrap/training-controller-regression-v2.xml`.

Okno przygotowania modelu z tym API jest kolejnym krokiem; nie oznaczono
jeszcze interfejsu treningu jako ukończonego. Ostatni potwierdzony postęp
równoległych procesów: CI 700/1000, pełna siatka alarmu 2900/5000.

### Przycisk akwizycji w głównym widoku Raw − background

Usunięto niespójność: „Acquire new spectrum” uruchamiało zwykły surowy
pomiar również po wyborze widoku korekcji. Takie dane nie są wynikiem
odejmowania tła, więc nie trafiały na widoczny wykres.

W trybie Raw − background przycisk wskazuje teraz „Record background…”
albo „Record corrected spectra…” i uruchamia odpowiedni istniejący workflow
z wyborem archiwum oraz natychmiastowym stanem nagrywania. Powrót do
Raw / reference przywraca zwykłą akwizycję. Zmiana widoku sama nie
uruchamia pomiaru. Odejmowanie pozostaje w signed W, z kontrolą zgodności
profilu i zachowaniem surowych danych.

Test workflow obejmuje oba przyciski startu (Live i główny przycisk
akwizycji), brak profilu, rzeczywisty adapter z symulowanym VISA,
zamknięcie archiwum i wyświetlenie skorygowanego wyniku. Testy layoutu
sprawdzają pokazane okno w dwóch motywach i rozmiarach oraz małe
wartości signed W. Nie przeprowadzono pomiaru na fizycznym analizatorze.

Weryfikacja tej poprawki: 21 testów layout/workflow zakończonych powodzeniem,
Ruff i sprawdzenie whitespace bez błędów. Raport:
`artifacts/spectrum-correction-layout/background-acquisition-regression.xml`.
Obejrzano zrzut pokazanego widoku z podpisaną osią W i nową etykietą
przycisku. Wcześniejsza próba w odwrotnej kolejności modułów zakończyła
proces bez podsumowania; wynik 21/21 pochodzi z zakończonego powtórzenia.

### Okno treningu modelu REF

Podłączono `SpectrumTrainingDialog` do przycisku
`Prepare model from recorded REF…` w workspace. Okno jest Fluent,
resizable, z przewijanym formularzem i stale widocznymi stanem, wynikiem
oraz akcjami. Pokazuje się bez nested exec; ponowne otwarcie kieruje do
tego samego okna. Przycisk działa offline, jest blokowany podczas
akwizycji oraz importu/eksportu profilu. Nie wysyła poleceń urządzenia.

Parser przyjmuje jawne jednostki i zakresy rozdzielone średnikiem,
również z przecinkiem dziesiętnym; odrzuca niepoprawne wymiary,
ujemne/odwrócone granice i ponad 32 zakresy. Zgodność z siatką sprawdza
wspólny store. Trening korzysta z istniejącego asynchronicznego API;
UI nie implementuje drugiego SVD, nie używa SIGNAL i nie ustawia
kwalifikacji automatycznie. Skala dopasowania w W jest odróżniona
od kwalifikowanej niepewności. Niekompletne pliki pozostają aborted/faulted,
istniejący cel oraz REF nie są modyfikowane. Anulowanie i zamknięcie
pracują współpracująco; brak twardego zatrzymywania wątku.

16 nowych testów przeszło: rzeczywiste archiwum REF/model/PyThat,
hash/provenance i brak inferred qualification, jawna kwalifikacja tylko
z dowodem, błędy pól i siatki, istniejący cel bez nadpisania, zamknięcie
pending worker oraz jedna instancja okna bez połączenia z urządzeniem.
Weryfikacja rendered geometry obejmuje 1000×900 light i 750×700 dark;
obejrzano zrzuty obu rozmiarów z rzeczywistym wynikiem treningu.
Raport `artifacts/spectrum-training-ui/dialog-regression.xml`;
zrzuty `artifacts/spectrum-training-ui/result-*.png`. Ruff bez błędów.
GUI oddzielnej walidacji held-out REF opisano w kolejnej sekcji poniżej.
Po podłączeniu przycisku przeszły także 23 istniejące testy layoutu oraz
okien resonance/difference. Raport:
`artifacts/spectrum-training-ui/existing-ui-regression.xml`.

### Zakończona walidacja globalnej różnicy na pełnej siatce

Proces `global-difference-null-5000-grid10001.json` zakończył 5000 prób,
każda na 10 001 punktach, z 999 permutacjami, 40+40 niezależnymi blokami
i wcześniej ustalonym alpha 0,005 (seed 20261012). Zarejestrowano
35 fałszywych alarmów i zero niedostępnych testów. Frakcja wyniosła
0,007, dokładny przedział dwumianowy 95% [0,004880459; 0,009721981].
Wszystkie trzy zapisane gates przeszły, w tym górna granica ≤ 1%.
Raport zachowuje wyniki każdej próby i laboratory_qualified=false.

Wynik dowodzi przejścia gate dla konkretnego syntetycznego rozkładu null,
pojedynczego zadeklarowanego zakresu i jednego spojrzenia. Nie kwalifikuje
driftu laboratoryjnego, wielokrotnej detekcji podczas Live, identyfikacji
rezonansu ani innych rozkładów szumu. Nie zastępuje nieudanego wcześniejszego
gate 1000 prób, którego artefakt pozostaje zachowany. Walidacja CI
1000×1000 studentized także zakończyła się; wyniki opisano poniżej.

### Asynchroniczna walidacja i okno held-out REF

Dodano immutable `SpectrumInterferenceValidationRequest` oraz operację
`validate_interference` do wspólnego CPU/storage controller. Zakresy
opcjonalnie przechodzą jako ograniczony, niemutowalny JSON (64 KiB,
1–32 jawne pary ilości); odczyt jednostek i wybór binów odbywają się
we wspólnym store na rzeczywistej siatce modelu. Brak zakresów używa
zapisanej chronionej maski. CLI otrzymało powtarzalny argument `--region`.
Żaden backend nie importuje narzędzi CLI ani UI.

Walidacja współdzieli istniejące zabezpieczenia: idle worker, wyłączność
z akwizycją i innymi operacjami offline, współpracujące anulowanie,
atomiczne exclusive JSON, hash źródeł przed i po analizie. Dodano
odrzucanie niezamkniętego modelu oraz pending checkpointów w obu źródłach,
także przy completed status. Wymiary wektorów raw są sprawdzane przed
ich odczytem. Nie zmieniono obliczeń RMS ani zasad kwalifikacji.

`SpectrumValidationDialog` jest dostępne przez
`Validate model on separate REF…`, także bez połączenia z urządzeniem.
Przewijany formularz, wybór modelu/REF/nowego raportu, natychmiastowy
busy state, Cancel i zamknięcie korzystają z asynchronicznego API.
Wynik pokazuje counts accepted/rejected, liczbę held-out binów i RMS W
modelu/statycznej referencji na tych samych zaakceptowanych dopasowaniach.
Zero zaakceptowanych dopasowań daje RMS unavailable, bez fałszywego
zerowego błędu. UI nie promuje modelu do Live ani nie kwalifikuje SIGNAL.

Weryfikacja: 28 testów pierwszej serii controller/store/training-controller
przeszło; 7 nowych shown GUI testów przeszło, w tym faktyczne archiwa,
wszystkie odrzucone dopasowania, training reuse, existing report,
retry, close/cancel i offline one-dialog. Obserwowano rzeczywiste wyniki
w light 1000×850 oraz dark 750×700; status, wynik i akcje pozostają widoczne.
Następnie przeszło 60 testów integration layout/training/core controller/
validation controller/store oraz 12 testów istniejących okien analiz.
Dodany oddzielnie test powtarzanego CLI --region też przeszedł.
Te serie mają częściowo wspólne przypadki; nie należy sumować ich
jako liczby unikalnych testów. Ruff bez błędów.

Raporty: `artifacts/spectrum-training-ui/validation-controller-regression.xml`,
`artifacts/spectrum-validation-ui/dialog-regression.xml`,
`artifacts/spectrum-validation-ui/integration-regression.xml` oraz
`artifacts/spectrum-validation-ui/analysis-dialog-regression.xml`.
Obejrzane zrzuty: `artifacts/spectrum-validation-ui/result-*.png`.

### Zakończony studentized CI: 1000 prób / 1000 replik

`studentized-validation-1000-resamples1000-positive.json` (seed 20261007)
zakończył wszystkie 1000 niezależnych prób, po 1000 replik, 40+40 bloków
i 101 punktów. Zero niedostępnych CI oraz failures. Marginalne pokrycia
i dokładne 95% przedziały dwumianowe:

| Parametr | Pokrycie | Przedział dla pokrycia |
| --- | --- | --- |
| amplitude_w | 947/1000 | [0,931244817; 0,960050748] |
| center_hz | 948/1000 | [0,932364986; 0,960923465] |
| fwhm_hz | 947/1000 | [0,931244817; 0,960050748] |
| finite_window_area_w_hz | 952/1000 | [0,936859885; 0,964399735] |

Wszystkie zapisane gates przeszły, bo 0,95 należy do każdego z tych
przedziałów. To ograniczony wynik dla dodatniego, stacjonarnego Gaussian
z konkretnym syntetycznym tłem, a nie simultaneous-family ani laboratory
qualification. Wcześniejsze nieudane percentile/B200 pozostają zachowane.
Pełne próby B200 i B1000 mają różne seeds: ten wynik sam nie dowodzi,
że zwiększenie liczby replik usuwa wszystkie przyczyny undercoverage.
GUI otrzymało aktualny, ogólny opis ograniczonej walidacji syntetycznej;
coverage_qualified pozostaje false i automatycznej kwalifikacji nie dodano.

Uruchomiono oddzielną walidację ujemnego sygnału: 1000 prób / 1000 replik,
studentized, seed 20261014, nowy raport
`artifacts/spectrum-bootstrap/studentized-validation-1000-resamples1000-negative.json`.
Proces pozostaje aktywny; nie ma jeszcze końcowego wyniku tej serii.

### Diagnostyka stabilności REF: integralność, worker i GUI

Dodano opcjonalne `expected_profile` do wspólnego diagnostycznego core.
W czasie pojedynczego przebiegu raw buduje pełny profil tym samym
BackgroundProfileBuilder, z jawnie zachowanym context/reference state
i signal-free qualification. Statystyka Allana/ACF i rekonstrukcja profilu
używają tej samej jednorazowo przeliczonej tablicy W. Nie zatrzymuje się
pełnej historii F×T. Profil obejmuje też częściowy ogon, choć ten ogon
nie bierze udziału w Allan/ACF. Brak zgodności content hash odrzuca raport.
Store sprawdza pending checkpointy nawet przy completed status oraz
wymiary raw przed odczytem. Publikowany raport ma `raw_profile_verified=true`
tylko po faktycznej weryfikacji. Starsze raporty nie otrzymują tego pola
wstecznie. Numerics Allana/ACF i zasady diagnostycznej kwalifikacji zachowano.

Immutable `SpectrumReferenceDiagnosticRequest` przenosi bounded config,
source/destination i opcjonalny tuple 1–16 unikalnych nieujemnych binów.
Controller uruchamia `diagnose_reference` na idle worker; ma wyłączność
z akwizycją i innymi operacjami offline, indeterminate progress oraz
współpracujące anulowanie/shutdown. Raport jest osobnym exclusive JSON,
źródło jest weryfikowane hashem przed i po odczycie.

`SpectrumDiagnosticDialog`, dostępne przez
`Inspect recorded REF stability…`, działa także offline. Formularz
przyjmuje jawny czas z jednostką i opcjonalne indeksy binów. Stale widoczne
są stan, counts/issues i akcje. Read-only Fluent table przedstawia
Allan variance (W²) albo ACF (1) dla wybranego binu; tau/lag są w s.
Braki historii nie są interpolowane. Stała moc ma ACF undefined,
nie udawaną zerową korelację. Nie jest kwalifikowany TTL, CI ani
niezależność; wybór binów nie zmienia żadnego filtru ani modelu Live.

Pierwszy rendering test ujawnił kolizję nazwy kontrolki `metric`
z natywną metodą QPaintDevice.metric: podczas grab() doszło do błędu
paint, a kolejny przypadek miał native Qt access violation. Wadliwe
procesy zakończono przez ich znane session handles; nie restartowano
trwającej walidacji CI. Kontrolkę nazwano `metric_selector`, a test
sprawdza zachowanie callable native metric przed renderingiem.
Po poprawce 7 testów GUI przeszło. Obejrzano light 1000×900 oraz
dark 750×700; poprawiono również szerokość nagłówków pustej tabeli,
aby jednostki nie były przycinane przy insufficient_blocks.

Core/store/controller: 45 testów przeszło, w tym raw/profile mismatch,
pending przy completed status, raw dimensions, mathematical oracle,
braki/kadencja, jednostki, immutable bins, cancel/close podczas hash,
existing report bez nadpisania i worker reuse. Report:
`artifacts/spectrum-diagnostic-ui/core-controller-regression.xml`.
Shown UI report po naprawie kolizji:
`artifacts/spectrum-diagnostic-ui/dialog-regression-v2.xml`.
Ostatnia poprawka empty headers także przeszła 7 shown testów:
`artifacts/spectrum-diagnostic-ui/dialog-regression-v3.xml`.
Szersza seria zakończyła 75 testów layout/diagnostic GUI/core controller/
diagnostic controller/reference diagnostics bez błędów:
`artifacts/spectrum-diagnostic-ui/integration-regression.xml`.
Te serie częściowo powtarzają przypadki; nie sumuje się ich jako unikalnych
testów. Ponownie obejrzano zrzut dark insufficient_blocks po poprawce
pełnych nagłówków. Ruff i diff whitespace bez błędów.

Walidacja studentized negative 1000×1000 pozostaje aktywna;
ostatni potwierdzony postęp 275/1000. Brak jeszcze końcowego gate tej serii.

### Powtarzane cykle pipeline: pomiar i naprawa retencji timerów

Dodano `tools.benchmark_spectrum_cycles`: pełny async pipeline, osobny
HDF5/JSON/PNG/resource journal każdego cyklu, całkowity preflight dysku,
warmup ustalony z góry, journal także przy przerwaniu oraz exclusive
końcowy raport. Po każdym cyklu mierzy zasoby po Qt DeferredDelete
i GC. Rozkłady programmatycznego submission Stop i zamknięcia archiwum
są odrębne; nie są reakcją na kliknięcie operatora podczas akwizycji.
Nie dodano kwalifikacji leak-free, soak ani laboratorium.

Pierwsza rzeczywista seria 20 cykli × 25 raw sweepów, 10 001 punktów,
zadane 20 Hz i 2 warmup cycles: 500 ramek, zero utraconych, wszystkie
osobne archiwa zachowane. W 18 mierzonych cyklach RSS wzrósł z
291 422 208 do 346 980 352 B (55 558 144 B), przy stałych 18 wątkach
i 286 uchwytach. Artefakt zachowano:
`artifacts/spectrum-cycle-benchmark/full-grid-20-cycles.json`.

Ukierunkowana diagnostyka Qt w osobnym procesie na 5 cyklach wykazała
3/6/9/12/15 żywych parentless inactive QTimer wrappers po cleanup.
Zatrzymanie timerów nie usuwało ich native signal connections.
Closures utrzymywały lokalny stan cyklu i duże raw/view buffers. To
zidentyfikowany błąd benchmarkowego helpera, nie dowód identycznego błędu
w aplikacji. Zapis diagnostyki:
`artifacts/spectrum-cycle-benchmark/timer-diagnostic-before.json`.

`benchmark_spectrum_async_pipeline` teraz jawnie deleteLater wszystkie
trzy własne parentless timery po ich zatrzymaniu. Regresja w jednym
procesie sprawdza, że po trzech cyklach nie pozostają dodatkowe żywe
Qt timery. 24 testy cycles/pipeline/resources przeszły po poprawce,
w tym rzeczywiste HDF5 signed W, counts/status, źródłowe wszystkie raw,
wyłączność wyjścia, failure drugiego cyklu z zachowaniem pierwszego,
brak katalogów przed nieudanym preflight i telemetry of missing metrics.
Raport: `artifacts/spectrum-cycle-benchmark/regression-v2.xml`. Ruff OK.

Powtórzenie pełnej siatki po naprawie zakończyło 20 cykli i 500 ramek
bez strat. W 18 mierzonych cyklach RSS first→last wyniósł
283 983 872 → 285 233 152 B (1 249 280 B); zakres
283 648 000–285 290 496 B, opisowe nachylenie 63 075,864 B/cykl.
Wątki/uchwyty: stałe 18/286. Wynik ogranicza obserwowaną retencję
w tej krótkiej serii, nie dowodzi stabilności przez 2 h.
Programmatyczny Stop submission p95: 0,027080 ms; archive close p95:
127,758550 ms (walidacja kompatybilności wliczona). Tych dwóch czasów
nie wolno utożsamiać z celem 100 ms reakcji GUI na kliknięcie operatora.

Wszystkie archiwa obu serii mają łącznie po 272 401 720 B. Obserwowane
FIFO max = 1 w każdym mierzonym cyklu. Krótkie achieved_rate_hz
po poprawce mieściło się w 19,753645–20,433416; efekt końców krótkiego
odcinka i backpressure pozostają jawne. Zakres p95 publication na cykl
wyniósł 43,832560–79,284090 ms: niektóre cykle przekroczyły cel 50 ms.
Nie zaliczono tego jako pełnej kwalifikacji latencji. Walidacja CI działała
w innym procesie; nie przypisano jej przyczynowo zaobserwowanych opóźnień.

Raport po poprawce:
`artifacts/spectrum-cycle-benchmark/full-grid-20-cycles-timer-fix.json`.
Wygenerowano i obejrzano naukowy wykres porównania RSS (MiB) i archive
close (ms), z oznaczeniem warmup oraz zakresu interpretacji:
`artifacts/spectrum-cycle-benchmark/timer-cleanup-comparison.png`.

Nadal brak 30 min / 2 h, GUI operator-click Stop oraz danych laboratoryjnych.
Studentized negative CI nadal działa; ostatni potwierdzony postęp 550/1000.

### Tablice skorygowanego widma bez pakowania każdego punktu do krotki

Worker przekazuje teraz `result.values_w` bezpośrednio do `Hdf5RunWriter`.
Writer przyjmuje realny jednowymiarowy `ArrayLike`, tworzy własny snapshot,
sprawdza skończoność, liczbę punktów oraz jednostkę i operację przed mutacją
checkpointu. Ten sam snapshot zasila zapis prywatny i publiczny thaTEC.
Nie zmieniono schematu, kompresji, flush, walidacji zamknięcia ani znaczenia
ujemnych wartości W. Dotychczasowi wywołujący z krotkami pozostają obsługiwani.

Test z mutacją wejściowej tablicy pomiędzy zapisem prywatnym a publicznym
potwierdza zachowanie identycznego pierwotnego wyniku w obu reprezentacjach
oraz zgodność PyThat. Osobne testy odrzucają tablice NaN, complex,
dwuwymiarowe i o błędnej długości przed zmianą pending/points/spectra.
Szerszy zestaw: 101 passed, 4 skipped (brak laboratoryjnych golden fixtures),
3 subtests passed; końcowy zestaw store po dodaniu przypadków tablic: 32 passed.
Raporty: `artifacts/spectrum-cycle-benchmark/array-write-regression-v3.xml`
i `array-write-store-final.xml`. Ruff oraz diff check przeszły.
Dwa testy `test_run_recovery.py` nadal zawodzą na wcześniej znanym wymaganiu
jawnego zakresu źródła prądowego Channel B, przed wejściem w zapis widma.
Nie osłabiono walidacji safety w celu ich przejścia.

Profiler: `worker-profile-before.worker.prof` i `worker-profile-after.worker.prof`
w `artifacts/spectrum-cycle-benchmark/`, po 50 zapisów (40 + 10 warmup).
Łączny czas `_validate_processed`: 139.915 → 102.871 ms; całego append:
731.511 → 812.607 ms. Profilowanie i konkurencja innych procesów ograniczają
interpretację: nie dowodzi to przyspieszenia całego pipeline.

Ponowny pomiar bez profilera: `full-grid-20-cycles-array-write.json`,
20 cykli × 25 ramek, 10001 punktów, wejście 20 Hz, 2 cykle warmup.
500 ramek zapisanych, 0 utraconych, FIFO max 1, HDF5 razem 272401720 B.
W 18 mierzonych cyklach zakres publication p95 wyniósł 44.323–56.574 ms;
13/18 przekroczyło 50 ms. Przed zmianą zakres wynosił 43.833–79.284 ms,
8/18 cykli przekraczało próg. Bramka nie jest spełniona i nie wolno wybierać
samego minimum lub maksimum jako dowodu poprawy. RSS po cleanup wzrósł
o 712704 B; OS threads pozostały 18; handles 286 → 283.
Archive close p95 126.973 ms nie mierzy reakcji GUI na kliknięcie Stop.

Kod wskazuje następny kierunek: snapshot dla GUI jest aktualnie pobierany
przez timer podglądu. Należy oddzielić publikację już zatwierdzonego wyniku
od częstotliwości rysowania, zachowując ograniczenie renderowania do 20 Hz
i brak publikacji danych przed udanym checkpointem. To jeszcze hipoteza
do implementacji i pomiaru, nie potwierdzona przyczyna wszystkich opóźnień.

Negative studentized CI: ten sam proces nadal działa; aktualnie 825/1000.

### Publikacja wyniku po commit, niezależnie od renderowania

Potwierdzenie `frame` zawiera teraz opcjonalny `CorrectionViewSnapshot`
z dokładnie tym wynikiem, który właśnie zapisano. Powstaje po udanym append,
nie przed checkpointem. REF oraz odrzucone SIG przekazują `view=None`.
Nie wykonuje się dodatkowej konwersji dBm/W ani ponownego liczenia korekty.
`_latest_signal_raw` jest aktualizowany dopiero po udanym zapisie.
Workspace odbiera parę wynik/raw od razu i oznacza podgląd jako dirty;
sam rysunek nadal obsługuje timer. Freeze nie blokuje odbioru wyniku.
Jawne snapshoty pozostają dla odświeżania statusu i końca sesji.

Test fault injection potwierdza: po drugim, nieudanym checkpointcie brak
potwierdzenia ramki i brak publikacji niezapisanego wyniku; plik ma jeden
zapisany punkt i status faulted, a późniejszy snapshot jest pusty.
Testy REF, SIG i odrzuconych sweepów sprawdzają tożsamość pary raw/result.
Test GUI przy zatrzymanym render timerze i aktywnym Freeze potwierdza odbiór
wyniku bez rysowania, a po Resume widoczną krzywą z ujemną wartością.

Walidacja: 34 testy worker/benchmark/cycles; 22 testy layout/workflow;
12 dodatkowych testów GUI/cycles oraz 11 testów końcowych benchmark/GUI
(zestawy nakładają się, nie sumować ich jako niezależnych testów).
XML: `commit-publication-worker.xml`, `commit-publication-ui-v2.xml`,
`commit-publication-additional.xml`, `commit-publication-final-additions.xml`
w `artifacts/spectrum-cycle-benchmark/`. Ruff i diff check przeszły.
Pierwszy zbiorczy proces zakończył się bez raportu, a osobny proces GUI
utknął bez postępu i został jawnie przerwany przez jego własny uchwyt.
Powtórzenie z QApplication i Segoe UI załadowaną przed pytest, z aktywnym
faulthandler, przeszło 22/22. Nie traktować przerwanych uruchomień jako PASS.
Obejrzano aktualny zrzut głównego widoku dark/800 z signed residual w pW;
zestaw layout generuje też zrzuty light/1500 i stany pusty/start/error.

Raport benchmarku identyfikuje teraz `publication_source` jako
`committed_frame_acknowledgement`; publication obejmuje każdą mierzoną
zaakceptowaną ramkę. Dawne raporty mierzyły pod tym kluczem odpowiedź na
request_snapshot z timera. Zachowano osobne `receive_to_requested_snapshot`
i liczby próbek obu pomiarów. Populacje snapshotów mogą się różnić,
ponieważ timing entries wyrysowanych ramek są usuwane. Nie przedstawiać
porównania tych dwóch różnych momentów jako bezpośredniego przyspieszenia
samego zapisu. Liczniki aktualizacji wykresu dodano po dużej kampanii;
mały końcowy test sprawdza mniej aktualizacji wykresu niż publikacji przy
renderowaniu 5 Hz i wejściu 20 Hz.

`full-grid-20-cycles-commit-publication.json`: 20 × 25 ramek, 10001 punktów,
20 Hz, 2 cykle warmup; 500 zapisów, 0 strat, FIFO max 1, 272401720 B HDF5.
W 18 mierzonych cyklach 360 publikacji, publication p95 31.856–38.651 ms,
0/18 cykli powyżej 50 ms. Requested snapshot p95 30.802–45.066 ms.
RSS po cleanup: 286720000 → 287481856 B (+761856 B), threads 18,
handles 286 → 285. Archive close p95 118.203 ms nie stanowi pomiaru
reakcji GUI na kliknięcie Stop przy obciążeniu. Krótkie serie nie dowodzą
30-minutowej stabilności ani 2-hour soak; negatywny bootstrap działał
równolegle, więc środowisko nie było izolowane od innych obciążeń.

### Ujemny rezonans: pełne 1000 prób studentized, B=1000

Proces zakończył się poprawnie. Raport:
`artifacts/spectrum-bootstrap/studentized-validation-1000-resamples1000-negative.json`,
seed 20261014, 1000 niezależnych syntetycznych realizacji, B=1000,
40 bloków REF i SIGNAL, 101 punktów, pojedynczy ujemny rezonans Gaussa.
Wszystkie 1000 przedziałów dostępne; brak błędów fit/bootstrap/SE.

| Parametr | Pokrycie | Dokładny przedział dwumianowy 95% |
| --- | --- | --- |
| Amplituda | 940/1000 | [0.923439510, 0.953904965] |
| Środek | 938/1000 | [0.921220050, 0.952138152] |
| FWHM | 950/1000 | [0.934609512, 0.962664602] |
| Pole w skończonym oknie | 942/1000 | [0.925663505, 0.955667140] |

Nominalne 0.95 leży w każdym przedziale; wszystkie zapisane marginalne
bramki tego eksperymentu przeszły. Razem z wcześniejszym positive B=1000
jest to dowód dla obu znaków w tym jednym stacjonarnym generatorze Gaussa.
Nie kwalifikuje to Lorentza, model-EMI, dryfu, skorelowanych bloków czasu,
automatycznego wyszukiwania pików, całej rodziny hipotez ani laboratorium.
Flagi laboratory_qualified i globalne coverage_qualified pozostają false;
nie modyfikowano konfiguracji naukowej na podstawie tego pojedynczego raportu.

### Stop podczas checkpointu: pomiar rzeczywistej karty GUI z syntetycznym kliknięciem

Dodano `tools/benchmark_spectrum_stop_response.py`. Narzędzie pokazuje
rzeczywisty `SpectrumCorrectionWorkspace` w rozmiarze 1000 × 800, prowadzi
przygotowaną sesję syntetyczną przez prawdziwy worker i writer HDF5 oraz
dostarcza syntetyczne odpowiedzi single_sweep do istniejącego handlera strony.
Nie wykonuje VISA ani konfiguracji sprzętu; instrument preflight jest
celowo poza zakresem tego benchmarku. Konfiguracja procesora, wersje,
CPU, środowisko BLAS, pamięć, wątki i uchwyty są zapisane w raporcie.

Wrapper append emituje sygnał na początku wybranego checkpointu w wątku
archiwizującym, bez wstawiania opóźnienia. QueuedConnection dostarcza go
do GUI, gdzie `QTest.mouseClick` naciska prawdziwy przycisk Stop. Rejestruje
się oddzielnie czas post→mouse dispatch, synchroniczny handler kliknięcia,
post→feedback paint oraz post→archive close. Flaga commit_active w momencie
dispatch jest diagnostyczna; sam post zawsze następuje przy wejściu append.

Event filter wykrywa naturalny Paint etykiety „Stopping recording…”.
Bramka używa czasu następującego po tym Paint: callback zero-timera w
kolejnym obiegu pętli zdarzeń. Jest to konserwatywny pomiar software GUI,
obejmujący malowanie i dodatkowe oczekiwanie na obsługę callbacku, a nie
tylko wejście do paintEvent. Nie mierzy fizycznej myszy ani prezentacji
na monitorze. Nie wstawia repaint ani processEvents w handler kliknięcia.
Zrzut Stop jest wykonywany dopiero po pomiarze reakcji; jego zapis może
zwiększać mierzony koszt archive close. Zachowuje się też obraz po zamknięciu.

Narzędzie ma walidację ograniczonych parametrów przed mutacją plików,
wyłączne ścieżki wynikowe i preflight miejsca dla całej kampanii. Journal
jest flushowany po każdym cyklu; błąd zachowuje ukończone archiwa i partial
journal, nie tworząc końcowego raportu sukcesu. Przy zamknięciu producer
jest zatrzymany, zaakceptowane zadania workera kończą się przed teardown,
a obiekty Qt są usuwane przez DeferredDelete i GC. Odczyt sprawdza status
aborted i zgodność liczby przesłanych oraz zatwierdzonych ramek.

13 testów `tests/test_spectrum_stop_benchmark.py` przeszło: bounds, budget
przed mkdir, realne kliknięcie i paint, zachowanie 15/15 raw w trzech
małych cyklach, ujemny wynik, PyThat, exclusive output oraz fault po
pierwszym cyklu z zachowaniem wcześniejszego archiwum. XML:
`artifacts/spectrum-stop-benchmark/regression.xml`. Ruff i diff check PASS.

Pełna kampania: `artifacts/spectrum-stop-benchmark/full-grid-20-cycles.json`.
20 cykli × 15 ramek, 10001 punktów, wejście 20 Hz, 2 cykle warmup.
300 ramek zatwierdzonych, 0 utraconych, FIFO max 2, HDF5 208076104 B.
We wszystkich 18 mierzonych cyklach kliknięcie zostało dostarczone, gdy
append jeszcze trwał; wszystkie zachowały obraz Stop i status aborted.

| Zdarzenie | p50 | p95 | max |
| --- | --- | --- | --- |
| Post → mouse dispatch | 1.226 ms | 2.830 ms | 3.009 ms |
| Handler kliknięcia | 0.833 ms | 0.996 ms | 1.297 ms |
| Post → feedback po Paint | 6.078 ms | 7.218 ms | 7.543 ms |
| Post → archive close | 230.010 ms | 241.640 ms | 242.735 ms |

Próg syntetycznej reakcji GUI p95 ≤100 ms przeszedł w tej karcie i tym
środowisku. Archive close ma inną semantykę i nie jest tym progiem.
RSS po cleanup dla mierzonych cykli 292741120 → 296349696 B (+3608576 B),
threads 18 → 18, handles 289 → 286. To opis zasobów krótkiej kampanii,
nie dowód braku wycieku. Obejrzano obraz Stop dla smoke oraz pełnej siatki.
Raport smoke poprzedza zmianę pomiaru paint-entry na barierę po Paint;
nie używać go jako dowodu końcowej metody.

`operator_input_qualified`, `hardware_shutdown_qualified` i
`laboratory_qualified` pozostają false. Nadal wymagane są pomiary w
produkcyjnym shellu, fizyczne input/display oraz testy 30 min / 2 h.

### Stop w głównym widoku rzeczywistego okna Fluent

Benchmark Stop ma teraz `--host shell`. Tworzy rzeczywisty `MainWindow`
w trybie simulation, pokazuje trasę Anritsu i „Raw − background” oraz
naciska `current_stop_corrected`, który wywołuje istniejący handler Stop
w workspace. Hierarchia shell/page/plot pozostaje produkcyjna. Provider
request_device dla tej przygotowanej sesji jest odłączony od kontrolera
instrumentu i zastąpiony syntetycznymi odpowiedziami; brak VISA i hardware
preflight nadal stanowi jawne ograniczenie benchmarku.

Każdy cykl otrzymuje własne settings.yml, katalog pomiarów, katalog SQLite
oraz audit logs. QSettings w module shell jest skierowany do lokalnego
ui-state.ini na czas budowy i zamknięcia okna. Nie czyta ani nie nadpisuje
preferencji okna operatora. Nie używa catalogue z Documents/PyLab.

Poprawka produkcyjna: główny Stop oraz główny przycisk Live zmieniają
etykietę na „Stopping recording…” w chwili ustawienia `_stopping`.
Wcześniej sam przycisk był disabled, lecz tekst pozostawał „Stop recording”
nawet przy istniejącej krzywej. Zmiana nie przerywa zapisu pending sweep
ani nie oznacza archiwum jako zamkniętego przed zakończeniem workera.

Walidacja: 15 testów benchmarku shell/workspace oraz 2 testy przycisków
głównej strony; po dodaniu dodatkowego cleanup ponownie 17 PASS.
Raporty: `artifacts/spectrum-stop-benchmark/shell-regression.xml`,
`main-stop-feedback.xml`, `shell-regression-v2.xml`. Ruff i diff check PASS.
Test shell sprawdza widoczny przycisk z dodatnią geometrią, obraz Stop,
5/5 zapisanych ramek, PyThat oraz lokalne katalogi i plik preferencji.

`full-shell-grid-20-cycles.json`: 20 × 15 ramek, 10001 punktów, 20 Hz,
2 cykle warmup, okno 1500 × 950. 300/300 raw zapisanych, 0 strat,
FIFO max 1, HDF5 208075296 B. We wszystkich 18 mierzonych cyklach klik
dotarł jeszcze podczas append. Post→feedback po Paint: p50 15.955 ms,
p95 40.634 ms, max 45.670 ms — syntetyczna bramka 100 ms PASS.
Post→archive close: p50 764.300 ms, p95 823.016 ms, max 859.675 ms.
Obejrzano zrzut pełnego shellu i stan Stop w głównej zakładce.
W końcowej części kampanii równolegle działał mały proces testowy;
nie traktować środowiska jako w pełni izolowanego obciążenia.

**Nierozwiązana retencja GUI.** RSS po cleanup w mierzonych cyklach
645386240 → 1636417536 B. Threads 15 → 12 i handles 297 → 289 nie
potwierdzają wycieku tych zasobów, ale wzrost pamięci wymaga diagnozy.
Po tej kampanii dodano drugi DeferredDelete/GC po powrocie `_stop_cycle`,
gdy jego lokalne referencje do hosta i callbacków zostały już zwolnione,
oraz liczniki żywych widgetów i okien najwyższego poziomu.

Kontrolowany `shell-cleanup-diagnostic.json`, 3 cykle pełnej siatki:

| Cykl | Widgety Qt po cleanup | Top-level | RSS |
| --- | --- | --- | --- |
| 0 | 1020 | 261 | 519917568 B |
| 1 | 2040 | 522 | 581947392 B |
| 2 | 3060 | 783 | 641093632 B |

Dodatkowy GC nie usuwa tych native widgetów. Osobny, jednocyklowy
`shell-orphan-classes.json` identyfikuje top-level: QMenu 201,
QComboBoxPrivateContainer 36, ViewBoxMenu 18, BodyLabel 3, SpinBox 1,
DashboardPage 1, PushButton 1. To bezpośredni dowód pozostawania obiektów,
nie tylko domysł z RSS. Nie wskazuje jeszcze, który właściciel zachowuje
każdy podgraf. Następna praca: zbadać rodziców i ownership oraz poprawić
zwalnianie własnych obiektów; nie usuwać globalnie wszystkich top-level
QWidget ani ukrywać problemu przez zmianę miary pamięci lub restart cyklu.

Zakres: powtarzane tworzenie i niszczenie całego okna w jednym procesie.
Nie dowodzi to takiego samego tempa wzrostu przy jednym oknie działającym
30 min / 2 h. Kwalifikacja pamięci pozostaje nieuzyskana. Zrzut pokazuje
też przycięty tekst globalnego E-STOP w safety strip przy tym rozmiarze;
wymaga osobnej poprawki geometrii z zachowaniem dostępności safety action.
Flagi operator_input/hardware_shutdown/laboratory nadal false.

### Regresja pustego głównego widoku Raw − background

Potwierdzono 22 testy układu i przebiegu korekcji, w tym rejestrowanie
REF i SIGNAL przez rzeczywisty adapter z symulowanym VISA. Główny widok
otrzymuje zapisany wynik, dopasowuje osie do signed W i pokazuje przyczynę
braku wyniku wraz z akcją rozpoczęcia właściwego nagrania. Sam zapis REF
nie tworzy jeszcze wyniku SIGNAL − REF.

Dodano osobny test pierwszego zatwierdzonego wyniku przy ukrytej zakładce
Background correction: operator pozostaje w Current spectrum, publikacja
działa przez zwykły timer, pusty stan znika i krzywa obejmuje wartości
dodatnie i ujemne. Test sprawdza również ponowne przełączenie Raw/background
oraz widoczną geometrię przy 1500 × 900. Obejrzano zrzut
`artifacts/spectrum-correction-layout/current-background-first-committed-frame.png`.
To regresja syntetyczna; nie potwierdza poprawności profilu laboratoryjnego.

### Własność menu wykresów i kontrolera dashboardu

Przyczyną retencji były parentless menu `PlotItem` / `ViewBox`, także te
utworzone przez `ColorBarItem`, oraz niewstawione do layoutu kontrolki.
`app/ui/widgets/plot_ownership.py` nadaje menu rodzica będącego ich własnym
wykresem i zachowuje flagę Popup. Wszystkie miejsca tworzenia PlotWidget
oraz pomocnicze ViewBox i ColorBar przechodzą przez tę regułę. Nie usuwa
się cudzych okien ani wszystkich globalnych top-level widgetów.

Dashboard został zastąpiony kontrolerem QObject: widoczne strony overview
i discovery pozostają dziećmi własnego drzewa Fluent, a reflow obserwuje
Resize rzeczywistej strony discovery. Zadania skanowania mają właściciela
i żądanie przerwania podczas zamknięcia. Zamknięcie okna zostaje odrzucone,
jeżeli którykolwiek wątek nadal pracuje; nie używa się terminate.

Raport `shell-ownership-controller.json` (1 cykl, F=101, 5 ramek) pokazuje
0 widgetów Qt i 0 top-level po cleanup; wszystkie 5 ramek zostało zapisanych.
Nie zastępuje to ponownej kampanii pełnej siatki ani długiego soak.
Starsze testy shellu i discovery używały katalogu operatora poza workspace;
15 przypadków zakończyło się błędem dostępu do SQLite lub attachments.
Izolacja testów przekierowuje jedynie pliki ustawień, katalog, audit oraz
QSettings, zachowując rzeczywisty MainWindow i sprawdzenia geometrii.
Okna utworzone przez test są jawnie usuwane po sprawdzeniu zamknięcia.

Jawne usuwanie okien ujawniło dodatkowy błąd produkcyjny: closeEvent
głównego okna nie wywoływał shutdown ukrytej strony korekcji. Test procesu
kończył się bez XML podczas DeferredDelete pracującego QThread. Teraz
główne okno zatrzymuje korekcję przed przyjęciem zamknięcia i odrzuca close,
jeśli checkpoint/obliczenie jeszcze trwa. Kontroler zachowuje rodzica
przez nieudaną próbę zamknięcia; strona Anritsu także odrzuca close podczas
drain. `dashboard-retry-regression.xml`: 2 PASS, w tym aktywny scanner,
fault injection niezakończonego drain, ponowny close i rzeczywisty teardown.

Szerszy przebieg `ownership-shell-shutdown-regression.xml` wykonał
28 zaliczonych przypadków, ale miał 5 błędów teardown — nie jest PASS
całego zestawu. Dotyczyły zero-delay callbacków do już usuniętych
MeasurementTreeView i SegmentedItem. Wywołania singleShot układu results,
metadata oraz drzewa otrzymały kontekst QObject właściciela, żeby Qt
anulował je po usunięciu widgetu. Oddzielny wcześniej błędny przypadek
`queued-layout-lifetime-regression.xml` przeszedł 1/1 po tej poprawce.
`ownership-queued-callback-regression.xml`: 10/10 PASS obejmuje wszystkie
5 wcześniej błędnych przypadków, test anulowania callbacks, menu wykresów
i ColorBar oraz kooperacyjne zamknięcie z ponowną próbą. Po zmianie shutdown
`background-shutdown-regression.xml`: 23/23 PASS dla układu i REF/SIGNAL.
Ruff dla zmienionych modułów i testów przeszedł. Pełny zestaw 28 przypadków
nie był powtórzony po ostatniej zmianie; wcześniejsze 5 błędów mają osobne
zaliczone regresje. Kolejny pomiar pełnego shellu: `full-shell-owned-grid-20-cycles.json`
(20 cykli, 2 warmup, F=10001, 15 ramek na cykl, 20 Hz). Raport końcowy
wolno uznać za dostępny dopiero po terminalnym zakończeniu procesu.

### Powtórzona kampania pełnego shellu po poprawce ownership

`full-shell-owned-grid-20-cycles.json` zakończył się kodem 0: 20 cykli,
2 warmup, F=10001, 15 ramek/cykl, 20 Hz. 300/300 ramek zapisanych,
0 utraconych; max FIFO=1. Wszystkie 20 cykli mają 0 native widgetów Qt
i 0 top-level po cleanup. We wszystkich 18 mierzonych cyklach klik Stop
dotarł jeszcze podczas append. HDF5 łącznie 208073680 B.

Post → feedback po Paint: p50 15.721 ms, p95 40.245 ms, max 43.755 ms:
syntetyczna bramka 100 ms PASS. Post → archive close: p50 881.464 ms,
p95 954.837 ms, max 955.268 ms. To pomiar software queued QTest,
nie fizycznego wejścia, ekranu ani bezpiecznego OFF sprzętu.

**Pamięć nadal niekwalifikowana:** RSS w mierzonych cyklach
631021568 → 1547059200 B (+916037632 B), mimo zerowej liczby widgetów.
Wątki 15 → 12 i handles 297 → 289 nie potwierdzają narastania tych zasobów.
Usunięto pozostawanie native QWidget; nie udowodniono usunięcia wzrostu
pamięci. Trzeba rozróżnić pozostałe obiekty/graphics, Python/NumPy oraz
cache/alokator przez diagnostykę obiektów i pamięci. Nie kwalifikować RSS
na podstawie allWidgets=0 ani resetować procesu między cyklami.
Podczas kampanii wykonywano lekkie inspekcje kodu i Ruff; nie uruchamiano
drugiej kampanii Qt/testów. Proces miał załadowaną wersję safety strip
sprzed poniższej poprawki napisu E-STOP. Długi soak i pomiary laboratoryjne
pozostają nieuzyskane.

### Czytelny E-STOP w pełnym oknie i przy małej szerokości

Fluent PrimaryPushButton rezerwuje teraz minimum na rzeczywisty pogrubiony
napis, padding i obramowanie; szerokość przelicza się przy zmianie tekstu,
fontu i stylu. Wąski pasek pokazuje „E-STOP | ALL OFF” w osobnym wierszu,
z zachowanym pełnym accessibleName/opisem. Przy średniej szerokości statusy
stacji/wyjść i działania zajmują osobne wiersze. Pasek rezerwuje wysokość
nowego układu przed paint; pełne teksty statusów i operatora są w tooltipach.
Nie zmieniono sygnałów, skrótu ani procedury potwierdzania i wysyłania OFF.

`safety-caption-height-regression.xml`: 16/16 PASS (5 szerokości × 2 motywy,
rzeczywisty MainWindow, istniejące testy sygnałów i snapshotów). Pełne okno
sprawdzono dla 1500×950 i 820×650, light/dark, z widocznym E-STOP,
statusami i brakiem nakładania na nawigację po jej natywnej animacji.
Obejrzano `artifacts/safety-strip-layout/shell-light-820.png` oraz
`shell-dark-1500.png`; napis przycisku i statusy nie są przycięte.

Pełny przebieg `full-shell-lifetime-caption-regression.xml`: 39 PASS,
1 FAIL, 0 błędów teardown. Jedyny FAIL dotyczył oczekiwania desktopowego
napisu przed show, gdy pasek miał jeszcze układ narrow. Test uzupełniono
o show, przetworzenie zdarzeń i sprawdzenie tej samej pełnej etykiety
przy rozmiarze desktopowym. `shown-caption-shell-contract.xml`: ten
przypadek PASS 1/1 po zmianie. Nie przedstawiać pierwszego przebiegu
40 testów jako pełnego PASS. Ruff i diff --check przeszły.

### Raw − background: odzyskanie widocznej krzywej (2026-10-04)

Ponowne wybranie widoku Raw − background przywraca widoczność jego krzywej,
wyłącza logarytmiczne osie i dopasowuje zakres do zapisanego wyniku.
Przycisk Show full spectrum również przywraca liniowe osie: logarytmiczna
oś Y nie przedstawia prawidłowo ujemnej mocy resztkowej. Ręczny zoom
pozostaje zachowany między kolejnymi klatkami, dopóki operator pozostaje
w tym widoku. Zmiana dotyczy wyświetlania; nie zmienia korekcji ani archiwum.

Istniejące testy layout/workflow: 23 PASS przed zmianą. Po zmianie pokazany
test odzyskania widoku: 1 PASS, również dla ujemnych wartości pW, ukrytej
krzywej, zoomu poza danymi i osi logarytmicznych. Raport:
`artifacts/spectrum-correction-layout/raw-background-signed-recovery.xml`.
Obejrzano screenshot `current-background-recovery.png` z widoczną krzywą
obejmującą wartości ujemne i dodatnie. Ruff i diff --check przeszły.
Samo nagranie tła nie tworzy wyniku SIGNAL; bez skorygowanego pomiaru
widok pokazuje komunikat i akcję Record corrected spectra.

### Domknięcie pracowników analiz offline (2026-10-04)

Shutdown pięciu okien analiz (diagnostyka, walidacja, trening, porównanie,
rezonans) nie przenosi już kontrolerów do QApplication ani nie usuwa ich
automatycznie po timeout. Kontroler pozostaje dzieckiem dialogu do końca
życia jego właściciela. Workspace anuluje wszystkie analizy bez zatrzymania
na pierwszym niezakończonym pracowniku, zamyka również własny worker
archiwizacji i zwraca False, dopóki dowolny wątek jeszcze pracuje. Istniejący
closeEvent głównego okna odrzuca wtedy zamknięcie. Polling reject ma kontekst
QObject dialogu, więc usunięcie dialogu anuluje oczekujące wywołanie.

Test pięciu rzeczywistych QThread zatrzymanych na kontrolowanym checkpoint:
nieudany shutdown wraca w <300 ms, wysyła anulowanie wszystkim, zachowuje
parent i te same żywe kontrolery przy retry; po zwolnieniu checkpointów
shutdown zwraca True i usunięcie właściciela usuwa kontrolery. Nie użyto
terminate ani poleceń sprzętowych. Regresja dialogów: 43 PASS
(`offline-dialog-lifecycle-regression.xml`); końcowy zestaw anulowania,
własności Qt i census: 12 PASS (`owned-offline-close-final.xml`), oba
w `artifacts/spectrum-correction-layout`. Shell/discovery oraz nowy test:
4 PASS (`offline-worker-shutdown-verified.xml`). Ruff przeszedł.

### Diagnostyka pamięci: natywne pomocniki i pozostałe wrappery

Natywne pomocniki pyqtgraph otrzymały właścicieli: SignalProxy, oba poziomy
timerów, PlotItem.stateGroup i grupy kontrolek menu ViewBox. Model drzewa
pomiaru ma parent strony Execution. Początkowy MainWindow przypisuje parent
animacjom stosów i SmoothScrollBar; niszczenie stosu usuwa wyłącznie jego
wpisy qrouter, zachowując historię innych okien. Testy obejmują działające
scrollowanie, przejście strony, sygnał kursora, natywne usuwanie pomocników
i nawigację drugiego stosu po usunięciu pierwszego.

Raport `artifacts/spectrum-stop-benchmark/qt-wrapper-owned-animations.json`
po trzech cyklach wykazuje już tylko stałe singletony wśród żywych QObject,
ale nadal narastają wrappery zniszczonych obiektów: 8891, 17781, 26671.
Nie jest to dowód rozwiązania problemu pamięci. Eksperyment diagnostyczny
ze słabym callbackiem motywu etykiet Fluent, połączony z cleanup routera
(`qt-wrapper-weak-label-owned-router.json`), również nie usunął retencji:
8030, 16059, 24088 wrapperów, 1/2/3 wrappery MainWindow, RSS
513884160 / 566554624 / 615251968 B; 15/15 raw frames, zero strat.
Eksperyment jest procesowy i nie zmienia zainstalowanej biblioteki ani
produkcyjnego kodu etykiet. Nie kwalifikuje czasu ani pomiaru laboratoryjnego.
Pozostają do ustalenia zewnętrzne korzenie referencji i pełna kwalifikacja RSS,
a także naukowe i laboratoryjne bramki całego planu.

### Odrzucona hipoteza rejestracji stylów i trwały census

Procesowy eksperyment `tools.probe_spectrum_fluent_lifetimes` zachowuje
semantykę rejestracji źródeł QSS, ale używa słabego callbacku destroyed
i opcjonalnie słabego callbacku themeChanged etykiet. To narzędzie
diagnostyczne, nie poprawka biblioteki używana przez aplikację. Kampania
`qt-wrapper-weak-style-labels.json`: 3 cykle, 15/15 raw, zero strat;
wrappery 8030 / 16059 / 24088, RSS 515883008 / 565641216 / 618696704 B.
Wynik nie potwierdza, że callback rejestracji stylów wyjaśnia retencję okna.
Wstępny mały test callbacku destroyed odwoływał się do usuniętego obiektu
C++ i zgłaszał wyjątek; nie należy używać go jako dowodu wycieku callbacku.
Powtórzenie bez takiego wyjątku zwalnia wrapper zarówno dla callbacku
przechwytującego obiekt, jak i dla callbacku słabego oraz bound slotu.
Środowisko eksperymentu: PySide6 6.11.1, QFluentWidgets 1.11.2.

`tools.diagnose_spectrum_qt_lifetimes` zapisuje teraz każdy post-cleanup
census w osobnym, wyłącznie nowym pliku `.census.jsonl`, z flush/fsync
przed następnym cyklem. Nagłówek oznacza dziennik jako niekompletny
i podaje eksperymentalne override, jeśli były zastosowane. Końcowy raport
łączy dziennik z benchmarkiem. Przerwana kampania zachowuje już uzyskane
liczby, a ponowna próba z tym samym basename odmawia nadpisania.
Fault injection po pierwszym census oraz rozróżnienie żywych i zniszczonych
wrapperów: 2 PASS (`census-journal-regression.xml`), Ruff PASS.
Rzeczywista kampania bez override: `qt-wrapper-durable-census.json`,
1 cykl pełnego MainWindow, 5/5 raw, zero strat, 8891 zniszczonych wrapperów;
zapisany census w JSONL jest identyczny z rekordem końcowego raportu.
RSS nadal wymaga diagnozy i kwalifikacji; te wyniki nie kończą całego planu.

### Ścieżki referencji MainWindow

Census obejmuje teraz ograniczone śledzenie wstecz przez GC-visible
referencje: maksymalnie 300 węzłów, głębokość 5, do 100 zapisanych ścieżek,
ze znacznikiem przycięcia. Opisy zawierają wyłącznie typy, nazwy funkcji
i nazwy pól; nie zapisują obiektów ani wartości danych pomiarowych.
Kontenery diagnostyki są jawnie wyłączone i utrzymywane do końca przebiegu,
aby uniknąć fałszywych ścieżek i ponownego użycia ich identyfikatorów.
Terminal bez dalszych widocznych referencji NIE dowodzi zewnętrznego root:
Qt i Shiboken mogą mieć niewidoczne dla Pythonowego GC powiązania.

Test znanego właściciela wykazuje prawidłową nazwę pola i brak retencji
obiektu przez wynik śledzenia. Zestaw census/journal/paths: 3 PASS,
`artifacts/spectrum-stop-benchmark/reference-path-regression.xml`.
Rzeczywisty przebieg `qt-wrapper-backward-paths-stable.json`: pełne okno,
5/5 raw, zero strat, nadal 8891 zniszczonych wrapperów. Ścieżki wskazują
m.in. metadata providers strony Anritsu, MokeFieldWorkflow._authorize,
DeviceController._operation_guard i WindowsWindowEffect.window.
To dotychczas powiązania wewnętrzne, nie ustalony zewnętrzny korzeń retencji.
Nie wprowadzono zgadywanej poprawki produkcyjnej ani nie wyłączono guards.

### Rozszerzenie macierzy o ujemny Lorentzian i pokrycie jego CI

Generator zachowania sygnału obejmuje teraz także `lorentzian_negative`,
z liniową mocą dodatnią przed korekcją i podpisanym ujemnym wynikiem po
odjęciu REF. CLI pozwala jawnie ustawić `--hardware-averages`, aby rozkład
gamma mocy nie był ograniczony do k=16. Nowy scenariusz dopisano na końcu
listy, zachowując kolejność losowań dotychczasowych scenariuszy.

`tools.qualify_spectrum_bootstrap_coverage` przyjmuje `--shape gaussian`
lub `--shape lorentzian`. Generator, point fit, bootstrap fit i prawdziwe
pole w skończonym oknie używają tego samego jawnego kształtu; dziennik
oraz generator w raporcie zapisują wybór. Nieznany kształt jest odrzucany.
Domyślny Gaussian i podział seedów pozostają zgodne z wcześniejszymi
diagnostykami sparowanymi. Nie zmieniono produkcyjnego algorytmu bootstrap.

`artifacts/spectrum-signal-qualification/signed-shapes-gamma1-100.json`:
100 powtórzeń na każdy z czterech przypadków Gaussian/Lorentzian × oba
znaki, 1001 binów, 40 REF i 16 SIGNAL na powtórzenie, k=1 (bez syntetycznej
średniej sprzętowej). Wszystkie cztery bramki średniego biasu PASS, zero
fit failures i zero odrzuconych klatek. Maksymalny bezwzględny średni bias
amplitudy 0.00270%, pola 0.00382%, FWHM 0.00409%, centrum 0.000426 kroku
siatki. To kwalifikacja syntetycznej wysokosygnałowej rekonstrukcji dla
zadeklarowanego rozkładu; nie kwalifikuje CI, detekcji ani laboratorium.
Zestaw coverage/signal/comparison: 28 PASS (`lorentzian-shape-regression.xml`).
Rozszerzony końcowy zestaw CLI/coverage: 10 PASS
(`lorentzian-coverage-cli-regression.xml`), w tym oba kształty, trwały
dziennik, odmowa nadpisania, błędne wartości shape i ujemny Lorentzian.
Ruff PASS.

Uruchomiono pełną, niezależną kampanię ujemnego Lorentzianu:
`python -m tools.qualify_spectrum_bootstrap_coverage --shape lorentzian --negative --interval-method studentized --resamples 1000 --repetitions 1000 --seed 20261023 --output artifacts/spectrum-bootstrap-qualification/lorentzian-negative-studentized-1000.json`.
Jej `.trials.jsonl` zachowuje kolejne powtórzenia. Na czas tego wpisu proces
nadal oblicza; bramka pokrycia jest NIEZWERYFIKOWANA. Nie restartować
kampanii wyłącznie z powodu braku kolejnego komunikatu postępu.
Ostatni potwierdzony komunikat żywego procesu: 25/1000 powtórzeń.

### Impulsy, telegraph, 1/f i dryf: niezależne oracles diagnostyki

`tools.qualify_spectrum_reference_noise` generuje w SI W sześć jawnych
praw: white gamma k=1, AR(1) phi=0.98, telegraph z prawdopodobieństwem
zmiany poziomu 0.01/sweep, impulsy 500 pW z prawdopodobieństwem 0.01/sweep,
finite periodic Fourier Gaussian 1/f z zerowym DC i zadeklarowanym cutoff,
oraz liniowy dryf o 100 pW przez całą historię. Tło 1 nW pozostaje dodatnie;
generator odrzuca nieprawidłową moc zamiast clippingu. Czas sweepu 25 ms,
blok 100 ms; seed i parametry każdego prawa są zapisane. Scenariusz ma
własny strumień SeedSequence, więc wybór podzbioru nie zmienia jego danych.

Każdy sweep przechodzi W → dBm → rzeczywisty diagnose_reference. Średnie
bloków porównano z oryginalnymi mocami, Allan z bezpośrednimi średnimi
sąsiednich slices zamiast sum prefiksowych, a ACF z bezpośrednim iloczynem
wycentrowanych bloków. Żadnych impulsów ani dryfu nie usunięto. Report
zachowuje wygenerowaną historię wybranego binu i jawne jednostki; nie
udaje pełnego archiwum 10001-binowego ani replay pomiaru laboratoryjnego.

`artifacts/spectrum-reference-qualification/temporal-noise-1024-final.json`:
6 scenariuszy × 4100 sweepów, 1024 kompletne bloki, 4 sweepy końcowego
bucket wyłączone jawnie, wszystkie bramki numeryczne PASS. Lag-1 ACF
white=-0.053, AR1=0.949, telegraph=0.760, 1/f=0.775; ostatnia/pierwsza
Allan variance white=0.00476, 1/f=0.894, dryf=65536. Są to wyniki konkretnej
syntetycznej realizacji, nie automatyczna klasyfikacja ani uniwersalny
estymator efektywnego N. Independence, TTL i CI pozostają niezakwalifikowane.
Testy z istniejącą diagnostyką: 44 PASS; końcowe jednostki/limity/CLI:
13 PASS. XML: `temporal-noise-final-regression.xml` oraz
`temporal-noise-units-cli-regression.xml` w tym samym katalogu. Ruff PASS.

`tools.plot_spectrum_reference_noise` eksportuje standalone PNG/SVG
z Allan variance i ACF, z odmową nadpisania. Artefakt do oglądania:
`artifacts/spectrum-reference-qualification/temporal-noise-1024-complete.png`.
Nie zmieniono produkcyjnego filtra ani kryteriów przyjmowania profilu.
Kampania CI ujemnego Lorentzianu pozostaje aktywna; ostatni potwierdzony
komunikat procesu: 150/1000, bez wniosku o pokryciu.

### Jawny wybór profili w historii i częściowego bloku SIGNAL

`read_selected_profile` centralizuje wybór: bez ID wolno odczytać wyłącznie
jeden profil; dla wielu profili wymagany jest konkretny bezpieczny klucz.
Odczyt sprawdza committed/schema/hash/context oraz zgodność klucza z profile_id.
Nie wybiera według porządku HDF5, daty ani podobieństwa widma.
`finalize_spectrum_archives` przyjmuje teraz jawne `before_profile_id`,
`after_profile_id`, `signal_profile_id` i istniejące `point_indices`.
Oba bracketing REF mogą pochodzić z tego samego pliku historii. Manifest
wynikowego artefaktu zapisuje ID, content hash i context_id każdego wybranego
profilu, obok dotychczasowych pełnych hashów źródeł. Publiczny format
thaTEC/PyThat i wcześniejsze single-profile wywołania pozostają obsługiwane.

Worker przyjmuje te parametry w immutable SpectrumFinalizationRequest;
wybór punktów musi być ściśle uporządkowaną krotką int (bez bool, duplikatów
czy listy mutowalnej). API nadal finalizuje JEDEN jawnie wskazany ciągły
segment; nie interpretuje samodzielnie zmian stanu ani osi między segmentami.
Nie deklaruje qualified CI. Interfejs wyboru profili w oknie aplikacji,
batch finalization wielu bloków i chronologiczny replay decyzji całej historii
pozostają niewdrożone — nie jest to zakończenie E3/E5/E6.

Przykład CLI umożliwiający użycie tej funkcji bez modyfikacji źródła:
`python -m tools.finalize_spectrum_history_block --signal signal.h5 --before refs.h5 --after refs.h5 --signal-profile-id live_ref --before-profile-id ref_before --after-profile-id ref_after --point-indices 10 11 12 --output final_block.h5`.
Wszystkie profile i punkty muszą rzeczywiście istnieć; narzędzie odrzuca
nieciągłość, złą rolę, kontekst, brak committed, brak temporal bracket i
istniejący output według walidacji finalizacji. Nie wykonuje poleceń sprzętowych.

Testy: store/core 39 PASS przed rozszerzeniem fixture na wspólny plik REF;
store/controller/workflow z tym rozszerzeniem 44 PASS
(`explicit-profiles-worker-verified.xml`). Wstępna fixture wspólnego pliku
zapomniała committed=True dla wstawionego profilu; walidator prawidłowo
ją odrzucił (33 PASS/1 FAIL). Poprawiono fixture, bez poluzowania walidacji.
Końcowy test prawdziwego queued worker i selection/subset oraz odmowy
mutowalnych/nieuporządkowanych punktów: 7 PASS
(`profile-selection-queued-worker.xml`). Źródła zachowują hash, ujemna reszta
zostaje zachowana, wynik przechodzi require_pythat=True i replay bez oryginałów.
Kampania CI nadal aktywna: ostatni potwierdzony komunikat 325/1000.
CLI finalizuje wybrane dwa punkty i odmawia nadpisania istniejącego pliku:
1 PASS (`history-block-cli.xml`). Ruff i diff --check PASS.

### Wybór historii REF i punktów SIGNAL w aplikacji

Przycisk Finalize between two references ma teraz natywny Fluent split
z menu Choose profiles and SIGNAL block. Główna akcja zachowuje dotychczasowe
wybranie plików dla jednego profilu; menu otwiera nowe okno wyboru historii.
Operator wskazuje trzy archiwa (REF before i after mogą mieć ten sam path),
klika Load available profiles, wybiera konkretne profile oraz uporządkowane
zero-based punkty SIGNAL i nowy output. Puste punkty oznaczają cały plik,
który nadal musi zawierać pojedynczy ciągły segment pasujący do referencji.
Start przekazuje immutable request do istniejącego workera finalizacji;
Cancel processing i publikacja finalnego signed W działają jak wcześniej.

Odczyt list profili odbywa się w osobnym workerze z cooperatively canceled
inspection. Każdy profil jest sprawdzany przez read_selected_profile
(committed/schema/context/content hash); całe tablice są zwalniane przed
przejściem do kolejnego profilu. Skan ma jawne limity 256 profili/source
i wektorów oraz sprawdza shape/dtype przed odczytem danych. Odrzuca aktywne
źródła, niekompletne REF i uszkodzone rekordy; nie wybiera profilu z historii
automatycznie. Dla dokładnie jednego profilu wybór jest jednoznaczny.
Zmiana źródła invaliduje wszystkie wybory; również programmatyczna zmiana
podczas worker load nie pozwala opublikować nieaktualnych wyników.

Combo pokazuje ID, stan REF, sweep count i precyzyjny UTC czasu końca,
bez zaokrąglania epochi do sześciu cyfr. Potwierdzenie najpierw domyka
inspection worker, a dopiero potem uruchamia finalizację. Inspector nie
reparentuje kontrolera poza dialog. Wszystkie retry timers mają kontekst
QObject; shutdown workspace uwzględnia także to okno i odmawia teardown,
dopóki worker nie zakończy checkpointu. Natywne pomocniki Fluent dialogu
otrzymały parent zgodny z lifetime ich widgetu. Brak poleceń urządzeniowych.

Testy shown light 1000×850 i dark 760×650, zachowania wyborów i realnego
queued finalization: 3 PASS (`selection-regression.xml`); regresja istniejącego
workflow/layout/shutdown z nowym dialogiem: 27 PASS
(`workspace-history-regression.xml`). Error/retry, source-change race i
shutdown sześciu realnych QThread: 6 PASS
(`selection-fault-shutdown-regression.xml`). Dialog/store/bounded corrupt
vectors: 31 PASS (`selection-bounds-final-regression.xml`). Ruff i
diff --check PASS. Obejrzano `selection-light-1000.png` i
`selection-dark-760.png` w `artifacts/spectrum-finalization-ui`; główne
akcje i status mieszczą się, w narrow formularz ma normalny scroll.

Ten wpis zamyka wcześniejsze zastrzeżenie o braku UI wyboru profili.
Batch wielu segmentów w jednym workflow i chronologiczny replay decyzji
całej historii nadal pozostają otwarte. CI Lorentzianu: proces nadal
pracuje, ostatni potwierdzony komunikat 625/1000; bez bramki końcowej.

### Finalizacja paczki jawnie wybranych bloków: API i CLI (2026-10-04)

`app/domain/spectrum_finalization.py` zawiera niezmienne kontrakty wyboru
pojedynczego bloku i paczki 1..256 bloków. Przeniesiono kontrakt pojedynczego
bloku z kontrolera UI, bez zmiany dotychczasowych wywołań. Warstwa storage
nie importuje UI. `finalize_spectrum_batch` w
`app/storage/spectrum_finalization_batch_store.py` wykonuje bloki kolejno,
korzystając z istniejącej finalizacji REF-before / SIGNAL / REF-after.
Operator wybiera punkty i profile; program nie wyznacza automatycznie granic
segmentów, stanów magnetycznych ani fizycznego braku sygnału.

Przed zapisem sprawdzane są wszystkie wyjścia, ich katalogi i rozłączność
ze wszystkimi źródłami oraz dziennikiem. Nie ma nadpisywania. Plan zapisuje
wybory i SHA-256 każdego unikalnego źródła, a istniejący finalizator sprawdza
przypięte hashe przed tworzeniem każdego wyniku i ponownie przed zamknięciem.
Zmiana źródła między blokami zatrzymuje paczkę. Wyniki mają zachowane signed W
i osobne, samodzielnie odtwarzalne pliki HDF5 zgodne z publicznym thaTEC/PyThat.
Nie powstaje nowa hybrydowa reprezentacja publicznych danych.

Dziennik `spectrum-finalization-batch-journal-v1` jest tworzony wyłącznie jako
nowy plik. Plan, rozpoczęcie bloku, zatwierdzony wynik i status terminalny
mają UTC oraz `flush` / `fsync`. Zatwierdzenie zapisuje ścieżkę, SHA-256,
liczbę sweepów, frame IDs, segment i jakość. Błąd lub anulowanie zachowuje
ukończone wyniki i zapisuje `faulted` / `aborted`; utrata procesu pozostawia
ostatni trwały checkpoint bez fikcyjnego statusu completed. Uszkodzony lub
urwany dziennik nie jest automatycznie naprawiany. Jeżeli proces zginie między
zamknięciem HDF5 a zatwierdzeniem jego hasha w dzienniku, plik pozostaje na
dysku, ale nie jest zaliczany do zatwierdzonych wyników paczki.

`replay_finalization_batch` sprawdza hash planu, kolejność zdarzeń, statusy,
hashe plików, źródła/profile/punkty z planu i niezależny replay każdego wyniku.
Oryginalne źródła nie są potrzebne. Domyślnie niekompletna paczka jest błędem;
`require_completed=False` pozwala jawnie zweryfikować tylko już zatwierdzone
wyniki i nie kwalifikuje całej paczki jako zakończonej. Pamięć nie zawiera
tablic wszystkich wyników: przetwarzany i odtwarzany jest jeden blok naraz.
Plan ogranicza łączny wybór do 1048576 numerów checkpointów, rekord dziennika
do 8 MiB, a specyfikację CLI czyta z limitem przed parsowaniem. Weryfikacja
pełnych hashów źródeł ma koszt odczytu źródeł na blok; to operacja offline,
nie część pętli renderowania czasu rzeczywistego.

CLI:

```powershell
python -m tools.finalize_spectrum_batch --specification selection.json --journal batch.jsonl
python -m tools.finalize_spectrum_batch --replay-journal batch.jsonl
python -m tools.finalize_spectrum_batch --replay-journal batch.jsonl --allow-partial
```

Specyfikacja ma `schema: spectrum-finalization-batch-selection-v1` oraz listę
`blocks`. Każdy blok zawiera `signal_path`, `before_path`, `after_path`, nowy
`destination`, opcjonalne `point_indices` i trzy opcjonalne `*_profile_id`.
Ścieżki względne bloków odnoszą się do katalogu specyfikacji; ścieżka dziennika
do bieżącego katalogu CLI. Wiele profili wymaga jawnego ID zgodnie z istniejącą
walidacją; brak listy punktów oznacza cały plik, który nadal musi spełnić
warunki jednego ciągłego bloku. Ctrl+C zgłasza kooperatywne anulowanie.

Przykład na jawnie syntetycznych źródłach, rzeczywiście wykonany i odtworzony:
`artifacts/spectrum-finalization/batch-demo-20261004/selection.json`,
`batch.jsonl`, `block-0.h5`, `block-1.h5`. Wyniki mają odpowiednio 1 i 2 sweepy,
quality unqualified i brak kwalifikowanego CI. Nie wykonywano komend sprzętu.

Weryfikacja: `batch-regression.xml` — 66 PASS (paczka, dotychczasowy store
i kontroler); po dodatkowej kontroli zgodności wybranych punktów/profili
`batch-provenance-final-regression.xml` — 23 PASS. Testy obejmują rzeczywisty
PyThat każdego wyjścia, replay po przeniesieniu źródeł, kolizje przed zapisem,
anulowanie/błąd/utratę procesu między blokami, zachowanie ukończonego pliku,
zmianę źródła, uszkodzony plan/kolejność/status/wynik, urwany rekord,
zmienione wybory nawet po przeliczeniu hasha planu oraz CLI bez nadpisywania.
Ruff dla zmienionych modułów PASS.

Ten etap udostępnia paczki przez API/CLI. Wybór paczki w Fluent GUI,
jawne wznawianie z dziennika po przerwaniu i chronologiczny replay decyzji
całej historii pozostają do wdrożenia; nie są zastąpione replayem paczki.
Pełny plan nadal pozostaje w realizacji. Kampania Lorentzian negative:
ostatni potwierdzony postęp 900/1000, bez końcowej bramki.

### Korekcja wykresu przy nowych ujemnych wynikach (2026-10-04)

Nowa publikacja signed W przywraca liniowe osie, jeżeli menu wykresu
włączyło logarytmiczną oś X lub Y, i ponownie dopasowuje zakres. Dotyczy
głównego Raw − background i wykresu korekcji w workspace, także bez
przełączania zakładki. Nie zmienia wartości, jednostek ani archiwizacji.
`signed-live-publication-regression.xml` — 23 PASS. Obejrzano rzeczywiście
wyrenderowany `current-background-recovery.png`: wartości −3..−1 pW
pozostają widoczne. Poprzednie testy zoomu, freeze i nagrywania nadal PASS.

### Wybór paczki w interfejsie Fluent (2026-10-04)

Menu strzałki `Finalize between two references…` zawiera
`Choose multiple SIGNAL blocks…`. Ten wariant istniejącego dialogu wyboru
używa jednego kontrolera inspekcji: operator ładuje profile, wybiera jawne
punkty/profil SIGNAL, oba REF i nowy wynik, a następnie dodaje blok do
kolejki. Fluent TableWidget przedstawia liczbę i wybrane punkty, IDs REF,
plik wynikowy oraz pełne ścieżki w tooltipie. Można usunąć wskazany blok.
Każdy blok jest ciągły, lecz różne bloki mogą mieć własne źródła i profile.
Zmiana formularza następnego wyboru nie zmienia niezmiennych pozycji kolejki.
Limit wynosi 256 bloków. Kolizje wyjść/źródeł i istniejące wyniki są
odrzucane przy dodawaniu; storage ponawia pełną walidację przed zapisem.

Nowy dziennik wybiera się w stale widocznej stopce, niezależnej od scrolla
formularza. Start jest aktywny dopiero po dodaniu bloku i wybraniu dziennika;
jego kolizja z dowolnym źródłem lub wynikiem zatrzymuje wysłanie. Dialog
emituje niezmienny SpectrumFinalizationBatchRequest dopiero po zakończeniu
wątku inspekcji. Bez wyboru plików i Start nie uruchamia przetwarzania.

Operacja `finalize_batch` działa w tym samym ograniczonym workerze CPU/storage
co pojedyncza finalizacja. Emituje postęp po trwałym zatwierdzeniu każdego
bloku i respektuje wspólne anulowanie, blokadę równoległych operacji oraz
bounded shutdown. Nie wywołuje instrumentu. Workspace pokazuje `done/total`
zapisanych bloków i udostępnia istniejące Cancel processing. Zakończenie
pokazuje ostatni jawnie wybrany blok, podaje liczbę zapisanych wyników i
ścieżkę dziennika. Anulowanie zachowuje ukończone pliki i pozostawia ścieżkę
dziennika do kontroli postępu; nie deklaruje, że cała paczka się zakończyła.
Pojedynczy wybór i dotychczasowa finalizacja całego pliku pozostają dostępne.

Weryfikacja: `batch-base-regression.xml` — 16 PASS istniejących workflow,
dialogów i shutdown; `batch-dialog-regression.xml` — 4 PASS nowych przypadków;
`batch-footer-coverage-regression.xml` — 19 PASS po zmianie stopki i opisu
kształtu kampanii. Końcowy `batch-final-worker-lifecycle-regression.xml` —
52 PASS: paczki/storage, wybór pojedynczy i batch, rzeczywisty queued worker,
postęp, anulowanie po pierwszym zatwierdzonym bloku, sześć dialogów shutdown,
brak komend sprzętu, niezmienione hashe źródeł i replay wyników.
Ruff i diff --check PASS. Obejrzano pokazane okna
`artifacts/spectrum-finalization-ui/batch-light-1000.png` (1000×900) i
`batch-dark-760.png` (760×700); stała stopka mieści dziennik, status i akcje,
a tabela jest osiągalna przez przewijanie i widoczna na obu zrzutach.

Wcześniejsze zastrzeżenie o braku GUI paczek jest rozwiązane. Jawne
wznawianie paczki po przerwaniu, chronologiczny replay decyzji całej historii,
kwalifikacje laboratoryjne i pozostałe wymagania pełnego planu są nadal
otwarte. Replay paczki nie zastępuje replayu chronologicznego.

### Zakończona kampania CI: ujemny Lorentzian (2026-10-04)

`artifacts/spectrum-bootstrap-qualification/lorentzian-negative-studentized-1000.json`
jest zakończonym wynikiem procesu, który wykonał 1000 niezależnych prób
z B=1000 bootstrap resamples, po 40 bloków na źródło i 101 punktów,
seed 20261023, studentized, ujemny Lorentzian. Zweryfikowano generator,
ujemną amplitudę truth, bootstrap_resamples, wszystkie 1000 trial rows i
zgodność ich sum covered z agregatami. Brak brakujących przedziałów i fit
failures; wszystkie trzy zapisane bramki kampanii są true.

| Metryka | Covered / 1000 | Pokrycie | Binomial 95% interval |
| --- | ---: | ---: | --- |
| Amplituda | 947 | 0.947 | [0.931245, 0.960051] |
| Środek | 952 | 0.952 | [0.936860, 0.964400] |
| FWHM | 949 | 0.949 | [0.933487, 0.961795] |
| Pole w skończonym oknie | 956 | 0.956 | [0.941380, 0.967851] |

Nominalne 0.95 mieści się we wszystkich marginalnych przedziałach kontrolnych.
To dowód dla zadeklarowanego pojedynczego generatora syntetycznego: gamma
shape 16, niezależne i stacjonarne bloki, znana równoważność REF oraz kernel
częstotliwościowy [0.25, 0.5, 0.25]. Nie dowodzi pokrycia przy dryfcie,
korelacji czasowej, model-EMI, dopasowaniu po automatycznym wyszukiwaniu pików,
jednoczesnej rodzinie metryk lub realnych danych laboratoryjnych. Flaga
laboratory_qualified pozostaje false.

Oryginalny report zawierał historyczny napis `Known single Gaussian hypothesis`
w limitations[0] mimo generator.resonance_shape=lorentzian. Nie zmieniono
gotowego artefaktu ani danych liczbowych. Plik
`lorentzian-negative-studentized-1000.metadata-note.json` zapisuje SHA-256
oryginału, błędny i poprawny opis oraz przyczynę korekty. Narzędzie generuje
teraz opis właściwy dla wybranego kształtu; test CLI sprawdza oba warianty.

Uruchomiono osobną kampanię dodatniego Lorentzianu:

```powershell
python -m tools.qualify_spectrum_bootstrap_coverage --shape lorentzian --interval-method studentized --resamples 1000 --repetitions 1000 --seed 20261026 --output artifacts/spectrum-bootstrap-qualification/lorentzian-positive-studentized-1000.json
```

Proces jest nadal uruchomiony, ostatni potwierdzony komunikat 25/1000.
Nie ma końcowego wyniku ani deklaracji pokrycia dla tej kampanii.

### Jawne wznawianie paczek offline: API i CLI (2026-10-04)

`SpectrumFinalizationResumeRequest` i `resume_spectrum_batch` kontynuują
przerwaną paczkę bez nadpisywania dziennika ani ukończonych/częściowych
wyników. Wymagają jawnego potwierdzenia, że poprzednia operacja przetwarzania
offline już się zatrzymała. To potwierdzenie granicy procesu, zgodne z regułą
resume w storage-contract; nie jest dowodem żadnego stanu fizycznego ani
automatycznym zezwoleniem na pomiar. Nie wykonuje się komend instrumentu.

Przed pierwszą mutacją weryfikowane są hashe ukończonych plików, ich
samodzielny replay i zgodność z pierwotnym wyborem. Dla pozostałych bloków
źródła muszą nadal mieć pierwotne SHA-256. Hash poprzedniego dziennika jest
sprawdzany przed i po walidacji; jego zmiana zatrzymuje wznowienie.
Nie wolno zmienić źródeł, profili, punktów lub ścieżek ukończonych bloków.
Zmienić można jawnie ścieżkę nieukończonego wyniku. Każdy istniejący częściowy
plik wymaga nowej ścieżki, nawet jeżeli przypomina poprawny HDF5.
Plik zamknięty, ale niezatwierdzony w dzienniku również nie jest automatycznie
zaliczany do ukończonych wyników ani nadpisywany.

Nowy dziennik `spectrum-finalization-batch-journal-v2` zachowuje oryginalne
source hashes i wybory oraz zapisuje `resume_parent`: ścieżkę i SHA-256
poprzedniego dziennika, hash poprzedniego planu, liczbę zweryfikowanych
wyników i potwierdzenie zatrzymania. Ukończone wyniki są przenoszone jako
trwałe zdarzenia `carried_from_parent`, bez ponownego zapisu HDF5.
`inherited_commit_utc` zachowuje pierwotny czas zatwierdzenia również przez
kilka wznowień; UTC nowego zdarzenia odnosi się do nowego dziennika. Pozostałe
bloki wykonuje istniejący finalizator z przypiętymi hashami. Przed terminalnym
completed ponownie sprawdzane są hashe wszystkich wyników. Odtwarzanie v1
pozostaje obsługiwane; replay v2 jest samodzielny i nie wymaga dostępności
oryginalnych źródeł lub dziennika rodzica.

Niekompletny ostatni rekord dziennika pozostaje domyślnie błędem. Wyłącznie
jawna opcja `recover_torn_tail` pozwala oprzeć nowy dziennik na poprzednich
kompletnych rekordach; SHA-256 pominiętych bajtów trafia do resume_parent.
Oryginał nie jest obcinany ani naprawiany. Nie można tym sposobem ominąć
uszkodzenia nagłówka, błędnego zamkniętego JSON, rekordów w środku dziennika
lub limitu 8 MiB. Zakończonej poprawnie paczki nie można wznawiać.
Brak tylko ostatniego statusu terminalnego po zatwierdzeniu wszystkich
wyników pozwala utworzyć nowy terminalny dziennik bez ponownego liczenia
i bez wymagania już przeniesionych źródeł.

CLI:

```powershell
python -m tools.finalize_spectrum_batch --resume-journal interrupted.jsonl --journal resumed.jsonl --confirm-previous-processing-stopped
python -m tools.finalize_spectrum_batch --resume-journal interrupted.jsonl --journal resumed.jsonl --confirm-previous-processing-stopped --replacement-output 1 block-1-retry.h5
python -m tools.finalize_spectrum_batch --resume-journal interrupted.jsonl --journal resumed.jsonl --confirm-previous-processing-stopped --recover-torn-tail
python -m tools.finalize_spectrum_batch --replay-journal resumed.jsonl
```

Numery `--replacement-output` są zero-based i odnoszą się do bloku paczki;
można podać opcję kilka razy, bez powtórzenia numeru. Nowe ścieżki odnoszą
się do katalogu roboczego CLI. Anulowanie obejmuje hashowanie, weryfikację
dziennika oraz odtwarzanie surowych sweepów i kolejne bloki. Wznowienie
przerwane ponownie daje następny rodzicielski dziennik, zachowując wcześniejsze
pliki i znaczniki commit. W pamięci odtwarzany jest jeden wynik na raz.

Rzeczywiście wykonany przykład na syntetycznych danych:
`artifacts/spectrum-finalization/resume-demo-20261004/interrupted.jsonl`
został zatrzymany przez ProcessingCancelled po trwałym zapisaniu pierwszego
bloku. CLI utworzyło `resumed.jsonl`, zachowało block-0.h5 i zapisało
block-1.h5; replay CLI zweryfikował oba. Quality pozostaje unqualified,
residual unit W, bez kwalifikowanego CI. Nie ma komend sprzętu.

Weryfikacja: `resume-regression.xml` — 65 PASS (resume/batch/store),
`resume-boundaries-regression.xml` — 19 PASS po rozszerzeniu granic,
`resume-process-gui-final-regression.xml` — 47 PASS po dodaniu prawdziwego
os._exit w osobnym procesie oraz dotychczasowego GUI batch.
`resume-utc-final-regression.xml` — 43 PASS po doprecyzowaniu UTC zdarzenia
przeniesienia i zachowania pierwszego czasu zatwierdzenia. Testy obejmują
aborted i brak terminalnego statusu, częściowe pliki i explicit replacement,
zmianę źródła/ukończonego wyniku/rodzica, kolizje, urwany ostatni rekord,
anulowanie walidacji, przerwanie wznowienia i kolejne wznowienie, utratę
procesu bez finally, PyThat nowego wyniku, replay po przeniesieniu źródeł
i rodzica oraz CLI wymagające potwierdzenia zatrzymania. Ruff PASS.

Ten etap zamyka backend/CLI resume. Fluent GUI wznowienia, jawne przejęcie
zweryfikowanego pliku zamkniętego przed utratą journal commit,
chronologiczny replay decyzji całej historii oraz pozostałe kwalifikacje
pełnego planu pozostają otwarte. Kampania dodatniego Lorentzianu nadal
pracuje; ostatni potwierdzony postęp 225/1000, bez końcowej bramki.

### Fluent GUI wznowienia paczek (2026-10-04)

Menu finalizacji zawiera `Resume a stopped batch…`. Native
`SpectrumFinalizationResumeDialog` pokazuje wybrany poprzedni dziennik,
jawną opcję odzyskiwania urwanego ostatniego rekordu, przycisk weryfikacji
i tabelę bloków. Ukończone pozycje są oznaczone `Verified complete` i nie
pozwalają wybierać replacement. Istniejący nieukończony wynik wymaga nowej
ścieżki; przyciski wyboru i wyczyszczenia replacement działają na wybranym
wierszu. Pełne ścieżki SIGNAL i output znajdują się w tooltipach. Dziennik
wznowienia, potwierdzenie zatrzymania poprzedniego przetwarzania, status
i akcje pozostają w stale widocznej stopce. Nie ma ukrytego legacy shell.

`SpectrumResumeInspectionRequest` / `SpectrumResumeInspection` przenoszą
niezmienne dane bez tablic widma. Operacja `inspect_batch_resume` w osobnym
workerze sprawdza dziennik i niezależny replay ukończonych wyników, zwracając
maksymalnie 256 scalar block summaries, źródła i hash dziennika. Hash przed
i po inspekcji musi się zgadzać. UI odrzuca wynik po zmianie ścieżki lub
opcji recovery podczas inspekcji. Zmiana wyboru kasuje poprzednią inspekcję,
replacements i potwierdzenie zakończenia starego joba. Brak pliku i completed
batch mają jawne stany, w których nie można uruchomić wznowienia.

Start wymaga zweryfikowanej niekompletnej paczki, rozwiązania wszystkich
istniejących nieukończonych outputów, nowego dziennika i zaznaczenia checkboxa
potwierdzającego zakończenie poprzedniego joba offline. Są sprawdzane kolizje
i istniejące pliki. Dialog przekazuje typed resume request po zakończeniu
wątku inspekcji. `expected_parent_hash` przypina wynik inspekcji: zmiana
treści dziennika nawet po zamknięciu selektora blokuje backend przed
utworzeniem nowego dziennika i nowego HDF5. CLI bez wcześniejszej inspekcji
nadal korzysta z wcześniejszej weryfikacji parenta przed i po walidacji.

`resume_batch` wykonuje wznowienie w ograniczonym wspólnym workerze
CPU/storage, z istniejącym anulowaniem i postępem. Po sukcesie pokazuje
ostatni wybrany blok i `Batch resumed: N verified blocks`, podaje ścieżkę
nowego dziennika i zachowuje flagi braku kwalifikowanego CI. Nie wykonuje
komend instrumentu. Dialog wznowienia jest siódmym rzeczywistym offline
dialogiem objętym wspólnym bounded shutdown; worker zachowuje natywnego
rodzica do czasu zakończenia.

Weryfikacja: `resume-base-regression.xml` — 25 PASS (backend resume,
GUI paczek i shutdown); pierwszy `resume-dialog-regression.xml` zawiera
4 PASS / 1 FAIL, ponieważ test oczekiwał obserwacji chwilowego busy przy
natychmiastowym odrzuceniu zmienionego parenta. Poprawiono obserwację testu
na terminalny sygnał workera; nie osłabiono warunku odrzucenia.
`resume-final-layout-shutdown-regression.xml` — 15 PASS po korekcie,
`resume-empty-history-final-regression.xml` — 28 PASS po dodaniu stanów
missing/completed i poprawie komunikatu po skorygowaniu ścieżki. Testy
obejmują shown geometry, light/dark, wymagane potwierdzenie i replacement,
read-only wybór, brak komend sprzętu, rzeczywisty queued resume, zachowanie
hashy źródeł/rodzica/ukończonego i częściowego pliku, odmowę po zmianie
parenta, wyścig inspekcji oraz zamykanie siedmiu blokowanych QThread.
Obejrzano `artifacts/spectrum-finalization-ui/resume-light-1000.png`
(1000×800) i `resume-dark-760.png` (760×700). Wszystkie główne kontrolki
są widoczne i mieszczą się; stan w wierszu zawija się w mniejszym oknie.

Wcześniejsze zastrzeżenie o braku GUI wznowienia jest rozwiązane. Jawne
przejęcie pliku zamkniętego przed utratą journal commit pozostaje osobnym
otwartym przypadkiem; aktualnie wymagany jest nowy output i oryginał nie
jest nadpisywany. Chronologiczny replay wszystkich decyzji i pozostałe
kwalifikacje pełnego planu nadal pozostają otwarte. Kampania dodatniego
Lorentzianu: ostatni potwierdzony postęp 425/1000, bez końcowej bramki.

### Przejęcie zamkniętego HDF5 bez zatwierdzenia w dzienniku (2026-10-04)

Rozwiązano granicę awarii: finalny HDF5 został poprawnie zamknięty, ale
proces zakończył się przed zatwierdzeniem jego rekordu w batch journal.
Przejęcie dotyczy wyłącznie pierwszego nieukończonego bloku z pierwotnego
planu i wymaga jawnego `adopt_closed_output=True`. Nie może jednocześnie
zmienić ścieżki tego bloku przez replacement. Pozostałe częściowe lub
błędne pliki nadal wymagają nowych ścieżek. Nie ma automatycznego przejęcia
na podstawie nazwy, istnienia pliku lub samego statusu completed.

`_verify_closed_output` sprawdza SHA-256 przed i po weryfikacji, pełny
samodzielny replay, źródła i ich pierwotne hashe, jawnie wybrane profile,
punkty i finalny wynik. Wymaga rzeczywistej walidacji publicznego thaTEC
oraz PyThat. Dodatkowo niezależny public reader porównuje wszystkie raw
checkpoints i derived raw mean w dBm oraz końcowy signed result w W z
odtworzonymi danymi. Oś pozostaje w Hz; zgodność liczb jest kontrolowana
z rtol=1e-12 i atol=0, bez zerowania małych lub ujemnych wartości.
Wcześniejsze processed rows muszą pozostać NaN placeholders; końcowy
derived checkpoint jest committed, a pending pozostaje puste. Sama
poprawna struktura publicznego drzewa nie wystarcza, jeżeli wartości publiczne
różnią się od replayu.

Dla `point_indices=None` stare plany nie zapisywały liczby punktów SIGNAL.
Przejęcie wymaga więc dostępności oryginalnego niezmienionego SIGNAL,
sprawdzenia jego pełnego hasha oraz wszystkich checkpoint indices.
Poprawny artifact zawierający tylko podzbiór nie może zostać przyjęty jako
wynik całego archiwum. Porównanie indeksów nie tworzy dodatkowej listy
range(N). Ta kontrola nie zmienia samodzielnego replayu już zatwierdzonych
artefaktów po przeniesieniu źródeł.

Dziennik przejęcia ma `spectrum-finalization-batch-journal-v3`. Zachowuje
rodzica i pierwotne source hashes, liczbę bloków faktycznie zapisanych w
starym dzienniku, index i SHA-256 przejętego pliku. Zdarzenie zawiera
`recovered_without_journal_commit=True`. Replay kontroluje ten znacznik,
granicę i hash; błędny marker lub inny hash nie przechodzi nawet po
przeliczeniu checksum planu. Poprzedni dziennik i HDF5 nie są zmieniane.
Ponieważ dawnego journal commit nie było, `inherited_commit_utc` wynosi
None; zapisywany jest rzeczywisty UTC nowego zatwierdzenia, bez wymyślania
historycznej chwili. Kolejne przerwanie i wznowienie zachowuje znacznik
odzyskania oraz brak nieznanego historycznego czasu commit. Czytnik nadal
obsługuje v1 i v2.

CLI:

```powershell
python -m tools.finalize_spectrum_batch --resume-journal interrupted.jsonl --journal adopted.jsonl --confirm-previous-processing-stopped --adopt-closed-output
python -m tools.finalize_spectrum_batch --replay-journal adopted.jsonl
```

W Fluent GUI inspekcja w workerze przedstawia candidate jako
`Verified closed, not journaled` tylko po przejściu wszystkich kontroli.
Checkbox `Retain verified block N output missing from the journal`
wskazuje konkretny blok i ścieżkę w tooltipie, pozostaje domyślnie
niezaznaczony i jest wyłączony dla niezweryfikowanego lub częściowego pliku.
Operator może wybrać przejęcie albo nową ścieżkę. Wybranie replacement
wyłącza przejęcie tego samego bloku. UI przesyła expected_closed_output_hash
z inspekcji: zmiana pliku przed faktycznym resume odrzuca operację przed
tworzeniem nowego dziennika. Stale widoczny status rozróżnia historyczne
journaled blocks od poprawnie zamkniętego pliku bez zapisu w dzienniku.

Koszt weryfikacji jest offline O(NF): publiczne/raw checkpoints są
odczytywane pojedynczo, bez macierzy wszystkich widm. Cancellation jest
sprawdzane przy hashowaniu, replayu i między publicznymi checkpointami;
walidator PyThat kończy swoją operację przed kolejną kontrolą anulowania.
GUI pozostaje w osobnym wątku od tych odczytów. Nie wysyła komend instrumentu
i nie kwalifikuje CI ani fizycznego braku sygnału.

Rzeczywiście wykonany przykład:
`artifacts/spectrum-finalization/adoption-demo-20261004/interrupted.jsonl`
z końcowym started oraz zamkniętym block-0.h5, następnie CLI utworzyło
adopted.jsonl, przejęło block-0.h5 i zapisało block-1.h5. Replay CLI
zweryfikował oba, quality unqualified, signed W i brak qualified CI.
Ten przykład jest syntetyczny; niezależny test os._exit(81) w osobnym
procesie dowodzi odzyskania na rzeczywistej granicy utraty procesu bez finally.

Testy: `adoption-base-regression.xml` — 43 PASS dotychczasowego resume/batch;
`adoption-regression.xml` — 18 PASS początkowego zakresu;
`adoption-public-values-regression.xml` — 13 PASS dodatkowej zgodności
publicznych liczb; `adoption-gui-final-regression.xml` — 23 PASS;
`adoption-provenance-final-regression.xml` — 46 PASS po dodaniu uszkodzeń
provenance i kolejnego przerwania/wznowienia. Po doprecyzowaniu podpisu
checkboxa i statusu `adoption-caption-shutdown-regression.xml` — 10 PASS.
Testy obejmują błędny raw/public result/status, inny poprawny podzbiór,
zmianę pliku po inspekcji, niezgodne replacement, PyThat, rzeczywisty
process exit, jawny CLI i GUI bez komend sprzętu, niezmienione hashe,
marker/boundary/hash w v3 oraz siedem dialogów shutdown. Ruff i
diff --check PASS. Obejrzano normalny i mniejszy light/dark resume oraz
`resume-adoption-selected.png`; główne kontrolki mieszczą się także
z dodatkowym checkboxem przejęcia.

Wcześniejsze zastrzeżenie o braku przejęcia zamkniętego pliku bez journal
commit jest rozwiązane. Chronologiczny replay wszystkich decyzji,
pełne wymagania pomiarowe, laboratoryjne i wydajnościowe planu pozostają
w realizacji. Kampania dodatniego Lorentzianu: potwierdzony postęp
700/1000, bez końcowej bramki.

### Aktualizacja: chronologiczne decyzje przetwarzania i wynik kwalifikacji Lorentza

Nowe archiwa sesji zapisują prywatną historię
`spectrum_processing_v1/decisions`, schemat
`spectrum-processing-decisions-v1`. Każdy rekord zawiera numer kolejny,
granicę `before_point_index`, UTC, operację, jawne parametry i łańcuch SHA-256.
Kontekst akwizycji, siatka Hz oraz konfiguracja CPU są zapisane osobno
i weryfikowane przed odtwarzaniem. Publiczny kontrakt thaTEC pozostaje bez zmian.

`replay_quantitative_session` odtwarza inicjalizację, wybór profilu i modelu,
rozpoczęcie/zakończenie/anulowanie referencji, reset segmentu oraz zmianę
statusu po kontroli wieku referencji. Decyzje obowiązują przed wskazanym
checkpointem; również decyzje po ostatnim checkpointcie są sprawdzane.
Odczyt zachowuje jeden uchwyt HDF5 i jeden bieżący rekord decyzji, bez
macierzy wszystkich klatek w pamięci. Anulowanie jest sprawdzane przy
decyzjach i checkpointach. Źródło jest otwierane wyłącznie do odczytu.

Starsze archiwa bez historii zachowują dotychczasową ścieżkę jednego profilu.
Brak znacznika przy istniejącej historii, nieznany schemat, brak rekordu,
zły hash, niezgodna konfiguracja lub brak jawnie wybranego profilu/modelu
są błędami; czytnik nie zgaduje historii na podstawie kolejności profili.
Przerwana nowa kalibracja bez zakończonego profilu pozwala odtworzyć surowy
prefiks bez deklarowania wyniku skorygowanego. Nowe żądanie CPU
`SpectrumProcessingChange` nie wysyła komend sprzętowych. Interfejs zmian
jest dostępny w kontrolerze; pełne włączenie do receptur pozostaje otwarte.

Testy: `artifacts/spectrum-decision-history-regression.xml` — 70 PASS; rzeczywista kolejka
Qt, dwa profile, reset, anulowanie referencji, inicjalny i ponownie instalowany
model, wszystkie tryby uśredniania, zgodność każdego checkpointu, ujemne W,
uszkodzona historia, niezmieniony SHA źródła i walidacja `require_pythat=True`.

Kampania `lorentzian-positive-studentized-1000.json` zakończyła się normalnie:
1000 niezależnych prób, B=1000, seed 20261026, brak błędów dopasowania.
Pokrycie marginalne: amplituda 931/1000, środek 961/1000,
FWHM 934/1000, pole w skończonym oknie 937/1000. Bramka pokrycia jest
**FAIL** dla amplitudy i FWHM: ich dwumianowe przedziały 95% nie obejmują
nominalnego 0,95. Zliczenia zweryfikowano z poszczególnych 1000 rekordów.
SHA-256 raportu:
`97fb8664797dcc29ba07e16c037e05e65596cbf1040a6663cce83c5f67f2a08c`.
Nie zmieniono raportu ani progów. Studentyzowane CI dla tego przypadku
nie są zakwalifikowane; diagnoza niedopokrycia oraz pełna macierz
kwalifikacji laboratoryjnej i wydajnościowej pozostają wymagane.

### Diagnoza niedopokrycia dodatniego Lorentzianu

Narzędzia `diagnose_spectrum_standard_errors` i
`diagnose_spectrum_bootstrap_pivots` obsługują teraz oba jawnie wybrane
kształty rezonansu. Generator, prawda dla pola w skończonym oknie,
dopasowanie i kowariancja używają tego samego kształtu. Porównanie pivotów
weryfikuje generator, znak, jednostki, wersje, globalny seed oraz seed
każdej pary. Starsze raporty bez pola kształtu opisują wyłącznie dawny
generator Gaussa i nie mogą być parowane z Lorentzianem.

Raporty w `artifacts/spectrum-bootstrap-qualification/`:

- `lorentzian-positive-standard-errors-1000.json`: seed 20261026, te same
  1000 prób co zakończona kampania bootstrap, brak błędów estymacji.
- `lorentzian-positive-pivots-1000.json`: dokładne odtworzenie pokrycia
  z dopasowania, SE i zapisanych granic. Amplituda: 32 błędy dolnego
  ogona i 37 górnego; FWHM: 36 dolnego i 30 górnego. Brak utraconych par.
- `lorentzian-positive-independent-standard-errors-10000.json`:
  niezależny seed 20261027, 10 000 prób, zero błędów. Stosunek wariancji
  empirycznej do średniej oszacowanej: amplituda 0,999842, środek
  0,993881, FWHM 1,004978, pole 1,001371. To diagnostyka punktowych
  estymat i ich kowariancji, a nie test pokrycia przedziałów bootstrap.

Nie znaleziono dowodu uzasadniającego stały mnożnik SE; nie zmieniono
algorytmu CI ani progów akceptacji. Weryfikacja względem niezależnych
refitów delete-one jackknife i materializowanego resamplingu obejmuje
teraz oba kształty oraz oba znaki sygnału. Raport
`artifacts/spectrum-lorentzian-diagnostic-regression.xml`: 39 PASS;
Ruff dla zmienionych narzędzi i testów PASS.

Uruchomiono osobną, z góry określoną kampanię pełnych CI:
`lorentzian-positive-independent-studentized-1000.json`, seed 20261028,
1000 niezależnych prób, B=1000, te same parametry generatora i bramki.
Raport z seed 20261026 i jego FAIL pozostają zachowane. Nowa kampania
nie służy zastąpieniu niekorzystnego wyniku; jej wynik, a następnie
analiza wszystkich kampanii, wymagają osobnej weryfikacji po zakończeniu.

### Jawny reset segmentu SIGNAL z aplikacji

W `Background correction` dodano Fluent przycisk `New SIGNAL segment`.
Podczas aktywnego SIGNAL operator może zrestartować uśrednianie w tym
samym archiwum, zachowując profil i model tła. Stan oczekiwania pojawia
się natychmiast; kolejne kliknięcia są blokowane do potwierdzenia CPU.
Przycisk jest wyłączony przed otwarciem procesora, w REF i podczas Stop.
Operacja przechodzi przez istniejącą kolejkę CPU, zapisuje decyzję resetu
przed następnym checkpointem i nie wysyła dodatkowych żądań instrumentu.

Po potwierdzeniu resetu poprzednia korekcja jest usunięta także przy Freeze.
Następne raw otrzymują nowy `segment_id`; pierwsza poprawiona klatka ma
licznik średniej 1. Mechanizm resetu procesora i zmiana identyfikatora
w envelope są odtwarzane z archiwum, z zachowaniem wszystkich poprzednich
checkpointów. Nie jest to automatyczny pomiar stabilizacji ani przejście
fizycznego stanu próbki: część receptur E6 pozostaje otwarta.

Weryfikacja: `artifacts/spectrum-signal-segment-ui.xml` — 33 PASS,
obejmujące workflow, layout i chronologiczny replay; po dopracowaniu
etykiety trybu w fixture `spectrum-signal-segment-render.xml` — 2 PASS.
Testy nowego przycisku korzystają z rzeczywistego procesora i writera HDF5:
przed resetem count 1,2, po nim count 1, jeden zapisany reset przy
granicy checkpointu 2, trzy zachowane raw, ten sam profil i archiwum,
nowy segment, zachowane ujemne W i brak dodatkowych żądań instrumentu.
Pokazano i obejrzano `signal-segment-light-1100.png` oraz
`signal-segment-dark-760.png`; przycisk, Stop i oba wykresy są dostępne.
Ruff i diff --check PASS. Testy nie używają rzeczywistego VISA.

Niezależna kampania Lorentzianu seed 20261028 pozostaje aktywna;
ostatni potwierdzony postęp: 125/1000. Nie opublikowano końcowej bramki.

### Naprzemienne REF/SIGNAL z potwierdzeniem operatora (2026-10-04)

W `Background correction` dostępny jest przycisk
`Record alternating REF / SIGNAL…`. Pomiar zapisuje kolejne bloki
REF/SIGNAL/REF/SIGNAL/REF do jednego nowego pliku HDF5. W SIGNAL odejmowana
jest ostatnia ukończona średnia REF; zachowane są dodatnie i ujemne W.
Nie zastosowano notch, obcinania ujemnych reszt ani uczenia tła z pików SIGNAL.
Wynik pozostaje provisional, ponieważ poprawność fizycznego REF i model
zmian pomiędzy blokami nie są zakwalifikowane.

#### Instrukcja uruchomienia

1. Po aktualizacji kodu uruchomić aplikację ponownie. Połączyć analizator
   przez zwykły workflow aplikacji i otworzyć `Background correction`.
2. Wpisać konkretny opis stanu REF oraz stan SIGNAL w nowym polu poniżej
   przycisku przeplatania. REF musi być fizycznym stanem bez docelowego
   sygnału przy takim samym torze RF; sam napis `REF` nie zapewnia tego warunku.
3. Ustawić `Alternating REF duration`, `Alternating SIGNAL duration` oraz
   `Minimum REF sweeps`. Czas wymaga jednostki, np. `10 s` lub `250 ms`.
   Wybrać średnią czasową stosownie do potrzeb eksperymentu. `Measurement block`
   oznacza średnią bieżącego bloku SIGNAL, a nie całej naprzemiennej sesji.
4. Kliknąć `Record alternating REF / SIGNAL…`, wybrać **nową** ścieżkę HDF5
   i Save. Natychmiast pojawia się `Acquisition paused — prepare REFERENCE`.
   Save przygotowuje sesję; archiwum i akwizycja rozpoczynają się po
   pierwszym potwierdzeniu stanu, a nie podczas wybierania ścieżki.
5. Ręcznie przygotować opisany stan REF i zaczekać na rzeczywistą stabilizację.
   Zaznaczyć `I confirm the requested sample state is stable`, a potem
   `Confirm REFERENCE state and record`. Checkbox celowo nie jest zaznaczany
   automatycznie. Pojawia się aktywność, stan pomiaru i licznik zapisanych raw.
6. Po ukończeniu REF aplikacja zatrzymuje zlecanie sweepów i pokazuje
   `prepare SIGNAL`. Ustawić stan SIGNAL, zaczekać na stabilizację,
   zaznaczyć checkbox i potwierdzić. Nie zmieniać ustawień analizatora
   ani toru RF między tymi stanami.
7. Po ukończeniu SIGNAL analogicznie wykonać następny REF. Ostatni wykres
   SIGNAL pozostaje oznaczony `Previous SIGNAL block — acquisition paused`.
   Po potwierdzeniu nowego bloku poprzedni wynik jest usuwany również przy Freeze;
   nowy wynik pojawia się dopiero po zatwierdzeniu odpowiednich raw.
8. Aby później interpolować tło, zakończyć po **ukończeniu kolejnego REF**
   następującego po interesującym SIGNAL, następnie kliknąć `Stop acquisition`.
   Stop zamyka sesję jako aborted, zachowując wszystkie ukończone bloki.
   Jeśli przerwano po SIGNAL bez następnego REF, raw nadal są dostępne,
   ale tego bloku nie można jeszcze finalizować z interpolacją.
9. Otworzyć strzałkę przy `Finalize between two references…` i wybrać
   `Finalize an alternating archive…`. Wybrać nagrany blok SIGNAL; dialog
   podstawia jego punkty i konkretne profile REF przed/po. Wskazać nowy
   plik wynikowy. Źródłowe archiwum pozostaje bez zmian.

Ręczne `Record background…` i `Record corrected spectra…` zachowują
dotychczasową funkcję. Przeplatanie jest dostępne niezależnie od domyślnej
polityki. Aby przyciski głównej zakładki `Raw − background` rozpoczynały
przeplatanie, ustawić `mode: interleaved` we właściwej sekcji istniejących
settings i ponownie uruchomić aplikację:

```yaml
devices:
  anritsu:
    spectrum_correction:
      reference_policy:
        mode: interleaved
        block_duration: "1 s"
        signal_duration: "5 s"
        minimum_sweeps: 2
        maximum_reference_blocks: 256
```

To fragment konfiguracji do połączenia z istniejącymi settings, nie kompletny
plik. `1 s`, `5 s` i dwa REF są wartościami startowymi interfejsu, a nie
parametrami zakwalifikowanymi dla Waszego laboratorium. Zakończenie bloku
wymaga ukończenia całego sweepu oraz spełnienia minimum REF. Przy obecnych
nagraniach nawet `1 s` i dwa sweepy zajmują około 7 s. Więcej REF poprawia
oszacowanie średniej kosztem późniejszego przejścia do SIGNAL.

#### Kontrakty i koszt obliczeń

`InterleavedSpectrumConfig`, `SpectrumOperatorStateConfirmation` oraz
`RecordedInterleavedSignalBlock` są niemutowalnymi modelami domenowymi.
Walidacja odrzuca niejawne role, brak opisu/potwierdzenia, błędne jednostki,
bool w licznikach, nieprawidłowe czasy i limity. Liczba profili REF jest
ograniczona do 256; pojedynczy REF wymaga co najmniej dwóch sweepów.

`InterleavedSpectrumAcquisition` przechowuje fazę, liczniki i ostatni envelope.
Koszt jego kroku oraz pamięć są O(1), bez tablic widma i bez timerów.
Liczniki zmieniają się dopiero po akceptacji procesora i zatwierdzeniu raw.
Źródłem czasu jest start/ukończenie akwizycji, nie publikacja w GUI.
Stan kontekstu i generacji konfiguracji musi być stały; zmiana segmentu
jest dopuszczalna wyłącznie przy granicy bloku. Timer nie pobiera widm,
gdy aplikacja oczekuje na stan operatora.

Nowy tryb używa istniejącego workera CPU i ograniczonej kolejki.
REF jest liczony Welfordem z O(F) pracy i O(F) pamięci na sweep;
konwersja dBm→W, odejmowanie i wybrana średnia SIGNAL także pozostają O(F).
Nie wykonuje się SVD, dopasowania rezonansów ani tworzenia macierzy F×F
w gorącej ścieżce. GUI obsługuje mały scheduler, etykiety i zatwierdzone
snapshoty. Pełny benchmark pipeline i długotrwałego RSS pozostaje oddzielną
bramką; sam ten rachunek kosztu nie stanowi kwalifikacji 20 Hz.

Przed pierwszym i każdym następnym blokiem odczytywane są pełne/advanced
ustawienia analizatora. Ich fingerprint jest porównywany z początkowym.
Niezgodność zatrzymuje sesję przed zleceniem następnego sweepu. Nowy
scheduler nie dobiera nastaw pola, biasu, generatora ani parametrów analizy.
Korzysta z istniejącego kwalifikowanego protokołu single sweep wraz z jego
zabezpieczeniami RF. Między blokami operator korzysta ze zwykłych
bezpiecznych kontroli stacji; nowy filtr nie steruje fizycznym stanem próbki.

Zmiana do REF zapisuje decyzję begin_reference, ukończenie zapisuje profil
i finish_reference, przejście do SIGNAL resetuje segment średniej.
Chronologiczna historia decyzji oraz wszystkie raw pozwalają odtworzyć
każdą korekcję. Nowy REF usuwa wcześniejszą kalibrację modelu zakłóceń;
ten tryb stosuje odświeżaną średnią, nie automatyczną adaptację QR/Kalmana.

#### Zapis, Stop i finalizacja

Prywatne atrybuty `run` określają
`spectrum_correction_acquisition_mode=operator-interleaved-v1` i kanoniczną
konfigurację czasów/liczników. Zapisane potwierdzenia
`spectrum_operator_state_confirmed` wskazują rolę, opis, granicę checkpointu
i `evidence_source=operator_report`. Pola `hardware_readback_verified`
oraz `signal_free_qualified` pozostają false. Potwierdzenie operatora
nie włącza kwalifikacji CI. Publiczny format thaTEC/PyThat nie został rozszerzony.

Stop przed pierwszym potwierdzeniem nie tworzy archiwum ani żądania sprzętu.
Stop w czasie sweepu zachowuje jego surowy checkpoint przed zamknięciem.
Przerwane REF nie publikuje częściowej średniej jako gotowego profilu.
Ukończone REF można wybierać z zamkniętej sesji completed/aborted/incomplete/faulted,
jeżeli nowy znacznik protokołu, kontekst i hash-chain są poprawne.
Nie rozszerzono tego wyjątku na dawne, nieukończone archiwa referencji.

Przed utworzeniem wyniku wybrany REF jest ponownie liczony z dokładnych
raw zapisanych między begin/finish i porównywany przez content hash.
Profil bez ukończonej decyzji jest pomijany w wyborze; próba jego jawnego
użycia jest błędem. Uszkodzenie raw albo historii również kończy operację
przed utworzeniem pliku wynikowego. Nie usuwa się osieroconych danych ze źródła.

Inspekcja bloków zwraca wyłącznie małe metadane; raw nie trafiają do GUI.
Wszystkie trzy źródła finalizacji mogą wskazywać to samo archiwum,
z jawnymi różnymi profile IDs. Zakres `start:end` jest półotwarty,
np. `2:4` oznacza checkpointy 2 i 3. Końcowy SIGNAL bez REF after pozostaje
widoczny jako `missing REF after`, z wyłączoną finalizacją interpolowaną.
Wagi interpolacji wyznacza istniejący finalizer z czasów akwizycji;
nie jest to przyczynowa korekcja czasu rzeczywistego. Hash źródła,
wybrane raw, profile i provenance pozwalają odtworzyć osobny wynik.

#### Analiza rzeczywistych nagrań i granice skuteczności

Odnaleziono zgodne raw:

- `measurements/spectrum_reference_20261004T065740_561422Z.h5` — 30 REF,
  SHA-256 `bbea4355a636af2ca1b652ee836c8875f660aa6f5f0c628a7db0f9aae498e051`.
- `measurements/spectrum_signal_20261004T070015_999220Z.h5` — 62 SIGNAL,
  SHA-256 `61f94633ec883ace5a40177d4f9a1736db2d4fa907d70aa93f0db3aa11c6ef78`.

Odczytano je wyłącznie do odczytu; hashe przed/po są identyczne.
Profil REF odtworzono ze wszystkich raw i potwierdzono jego content hash.
F=10001; zakres 0,200058313–5,999420063 GHz; krok około 579,94 kHz.
Fingerprint zawiera detector NORM, RBW 3 MHz, VBW 30 kHz,
average_count 50 oraz sweep_time_s 0,039. To readback konfiguracji,
nie dowód optymalności ustawień ani pełnej kwalifikacji toru.

Mediana kadencji ukończonych trace: 3,676 s REF, 3,766 s SIGNAL;
mediana zapisanego przedziału start–ukończenie około 3,49 s.
Nie potwierdzono przyczyny różnicy względem sweep_time_s 0,039;
nie zmieniono ustawień w celu uzyskania pozornego przyspieszenia.

Na binie 0,6245715931 GHz średnia REF wynosi 9407,96 pW, sample std
166,24 pW; w SIGNAL średnia 9363,08 pW, sample std 205,06 pW.
Opisowe lag1 to −0,173 i −0,083. Nie jest to dowód niezależności,
braku dryfu ani możliwości przewidywania kolejnego trace. Na części
innych linii w SIGNAL widać zmianę średniej w późniejszej części nagrania,
ale fizyczny stan próbki oraz udział sygnału magnetycznego są nieznane.
Opis REF `est` i flagi kwalifikacji false nie wystarczają do jego zatwierdzenia.

Odświeżanie może ograniczyć wolny dryf średniej. Nie usuwa losowej,
nieprzewidywalnej różnicy między mocą linii w oddzielnych sweepach REF
i SIGNAL. Krótszy REF może wręcz zwiększyć niepewność oszacowania tła.
Uśrednianie reszt ogranicza pewne fluktuacje kosztem odpowiedzi czasowej;
wspólny błąd referencji nie zanika przez uśrednianie SIGNAL.
Nie podano kwalifikowanego TTL, niepewności średniej ani CI.

Raport i obejrzany wykres sześciu linii:
[JSON](artifacts/spectrum-interleaved-lab-diagnostic/recordings-20261004.json),
[PNG](artifacts/spectrum-interleaved-lab-diagnostic/recordings-20261004.png).
Selekcja binów wynika wyłącznie ze średniej REF w 0,35–0,85 GHz;
nie dopasowywano filtra do SIGNAL. Przerwa między nagraniami jest widoczna.
Lokalne przesunięcie argmax na siatce nie jest pomiarem rzeczywistego
dryfu częstotliwości, szczególnie przy RBW 3 MHz.
Skrypt odtworzenia jest zapisany obok raportu; uruchomienie z katalogu repo:

```powershell
python -c "import runpy; runpy.run_path('artifacts/spectrum-interleaved-lab-diagnostic/analyze_recordings.py', run_name='__main__')" measurements/spectrum_reference_20261004T065740_561422Z.h5 measurements/spectrum_signal_20261004T070015_999220Z.h5 --output artifacts/spectrum-interleaved-lab-diagnostic/nowy-raport.json
```

Każde uruchomienie wymaga nowych ścieżek raportu i figury; źródeł ani
wcześniejszych raportów się nie nadpisuje. Jest to diagnostyka opisowa,
nie laboratoryjny dowód tłumienia szumu lub zachowania sygnału.

#### Weryfikacja programowa

Nowe testy wykorzystują prawdziwy adapter Anritsu z SimulatedVisaFactory,
rzeczywiste kolejki Qt, procesor, writer HDF5 i czytniki. Nie komunikują
się z fizycznym VISA. Scenariusz zawiera zmieniający się poziom tła
między cyklami oraz utrzymujący się słaby sygnał w addytywnej mocy,
pokrywający silną linię, i ujemną składową reszty. Każdy causal checkpoint
ma oczekiwaną wartość, profile zmieniają się, a count SIGNAL resetuje się.
Nie jest to kwalifikacja koherentnego toru ani szybkozmiennego szumu laboratoryjnego.

- `artifacts/spectrum-interleaved-final-verified.xml` — **59 PASS** po
  ostatnich poprawkach: scheduler, blokowanie niewłaściwych stanów i
  zmian kontekstu, workflow/Stop, one-file finalization, orphan REF,
  brak REF after, uszkodzenia danych/historii, wybór zakresów,
  główna zakładka zgodna z polityką interleaved, replay i PyThat.
- `artifacts/spectrum-interleaved-final.xml` — 70 PASS, obejmuje także
  settings; raport poprzedza drobne końcowe poprawki zakresu czasu i tooltipów.
- `artifacts/spectrum-interleaved-user-workflow.xml` — 51 PASS,
  obejmuje także istniejące ręczne workflow i layout.
- Poprzednia szeroka regresja `spectrum-interleaved-compatibility.xml`
  miała 93 PASS i jeden test oczekujący dawnego odrzucenia trybu interleaved.
  Ten test zaktualizowano do nowego kontraktu oczekiwania na operatora;
  następnie przeszedł w raporcie user-workflow. Nie wyciszono jego asercji
  dotyczących braku komend sprzętowych ani braku pliku przed potwierdzeniem.
- Obejrzano po ostatnich zmianach
  `artifacts/spectrum-correction-layout/interleaved-light-1100.png`
  (1100×900) i `interleaved-dark-760.png` (760×800).
  Status, wymagane potwierdzenie, Stop i oba wykresy są widoczne;
  pozostałe kontrolki pozostają w przewijanym panelu Fluent.
- `python -m ruff check app tests` i `git diff --check` — PASS;
  brak nowych protokołów wyjściowych.

Automatyzacja pola/biasu, dowód braku sygnału w REF, settling/readback,
kwalifikacja interleaved na realnych cyklach, równoczesny kanał IQ
i nowy filtr temporalny Kalmana nie są ukończone. Nadal wymagane są
otwarte bramki laboratoryjne, false positives, CI i długotrwałej
wydajności z pierwotnego planu. Nowy tryb jest działającą obsługą
operatorowych cykli, nie deklaracją rozwiązania całego problemu szumu.

### Ukończona niezależna kampania Lorentzianu (aktualizacja statusu)

Wcześniej uruchomiona kampania
`lorentzian-positive-independent-studentized-1000.json`, seed 20261028,
zakończyła 1000/1000 niezależnych prób przy B=1000 bez błędów.
Marginalne pokrycie: amplituda 954/1000, środek 960/1000, FWHM 951/1000,
pole 943/1000. Wszystkie określone wcześniej marginalne bramki dwumianowe
tej kampanii są PASS. SHA-256:
`cc766ed28371a459d915157eebbae9bc24593f7ac67ea912d8f5a24851a413d3`.
Oryginalna kampania seed 20261026 i jej FAIL pozostają zachowane.
Ten nowy wynik nie zastępuje poprzedniego i nie kwalifikuje CI dla
rzeczywistego stanowiska ani aktualnej sesji interleaved.

### Zachowanie poszczególnych raw z receptur (uzupełnienie wcześniejszej pracy)

Runner zachowuje każdą surową akwizycję Anritsu również wtedy, gdy punkt
receptury uśrednia kilka trace. `RecipeSpectrumSweep` zawiera niezmienne
Hz/dBm, rolę, recipe node ID, execution ID danej próby, indeks/rozmiar
średniej, czasy akwizycji, dowód sweepu, generację konfiguracji i setpointy SI.
Raw są zatwierdzane przed publicznym punktem średniej, z niezależną
prywatną ścieżką `/recipe_raw_sweeps_v1`; jednakowe osie są hardlinkowane
do `/recipe_raw_grids_v1`. Każdy rekord ma jednostki, content hash,
ordinal i liczbę checkpointów istniejących w chwili jego zapisu.

Po wznowieniu każda próba ma własny execution ID; raw przed przerwanym
punktem pozostają historyczne i nie są automatycznie uznawane za składniki
nowej średniej. Ukończony checkpoint wskazuje konkretne source IDs.
Compiler odrzuca niecałkowite/bool liczniki average_count i zakres poza
1..9999. Obsługa close zachowuje pierwotny błąd wykonania; UI ogłasza
ukończenie dopiero po udanym zamknięciu i właściwej walidacji archiwum.

Raport `artifacts/recipe-raw-source-final-regression.xml`: 218 PASS,
`recipe-raw-source-size-regression.xml`: 18 PASS. Obejmują zapis/wznowienie,
niezależne raw, powiązanie źródeł średniej, współdzielone osie,
quantity/runner/compiler oraz kompatybilność danych. Historyczny raport
compatibility zachowuje siedem błędów wcześniejszych testów stałego
zakresu źródła Keithley; nie poluzowano kontraktu tego urządzenia.
Ten zapis nie automatyzuje fizycznych REF/SIGNAL/TRANSITION ani
stabilizacji — pełny zakres E6 receptur nadal pozostaje otwarty.

### Widoczna reakcja przycisku Record alternating (2026-10-04)

Odtworzono zgłoszenie rzeczywistym kliknięciem przycisku w pokazanym oknie:
brak opisu SIGNAL kończył `_start_dialog` przed wyborem pliku, pozostawiając
tylko informację w statusie. Pole SIGNAL mogło znajdować się poza widocznym
fragmentem przewijanego panelu. Kursor pozostawał na przycisku, więc
operator nie otrzymywał wskazania, co należy poprawić.

Walidacja rozpoczynania sesji ma teraz widoczny Fluent toast oraz odwołanie
do konkretnego edytora. Brak opisu REF/SIGNAL albo niewłaściwy czas
automatycznie przewija panel, ustawia fokus i zaznacza zawartość tego pola.
Komunikat pozostaje też w karcie statusu. Poprawne dane przechodzą do jednego
wyboru pliku; ostrzeżenie znika przy przejściu dalej. Anulowanie wyboru
pokazuje `Recording canceled` i `Recording has not started`.

Tryb interleaved sprawdza swoje własne czasy REF/SIGNAL; nie blokuje go
nieużywany czas ręcznego REF. Parsowanie nadal korzysta z kontraktu
jednostek czasu i odrzuca wartości niepoprawne/niepozytywne. Niedostępny
przycisk przeplatania ma tooltip wskazujący połączenie, zajętość lub brak
kwalifikowanego single sweep. Nie zmieniono schematu HDF5 ani komend sprzętu.

Nowe testy obejmują kliknięcie myszą, przewinięcie, fokus, widoczny komunikat,
poprawienie danych i ponowne kliknięcie, anulowanie oraz niedostępność.
Przy błędach nie ma wyboru pliku, archiwum ani żądania urządzenia;
poprawne Save nadal oczekuje na odrębne potwierdzenie stabilnego REF.
Obejrzano jasny 1100×900 i ciemny 760×800 zrzut
`artifacts/spectrum-correction-layout/record-alternating-missing-signal-*.png`.

Końcowy raport `artifacts/spectrum-recording-feedback-final.xml`: **48 PASS**
(11 nowych testów reakcji przycisku oraz regresja interleaved, ręcznego
workflow, layoutu i shutdown). Pierwsza szeroka regresja zachowuje 47 PASS
i dawną asercję `Ready to record` po Cancel; zaktualizowano ją do jawnego
`Recording canceled`, zachowując kontrolę braku akwizycji i dalszego błędu
preflight. Ruff i diff --check PASS. Nie wykonywano pomiaru fizycznego VISA.

### Rzeczywisty REF i próbne modele (2026-10-04)

Po potwierdzeniu operatora „prąd i magnesy są wyłączone” agent zebrał
30 kompletnych widm REF z MS2830A, serial 6201514799, przez 109,95 s.
Archiwum `measurements/spectrum_reference_agent_20261004_current_and_magnets_off.h5`
ma status `completed` i przeszło walidację `require_pythat=True`.
Średnia, wariancja i hash profilu zostały odtworzone z RAW; analizy nie zmieniły źródła.
Opis stanu jest raportem operatora, nie odczytem sprzętowym prądu/pola.

Model rank-2 z wcześniejszego REF przetestowano na trzech osobnych REF.
Na nowym pomiarze odrzucił 24/30 dopasowań jako poza zakresem kalibracji;
dla pozostałych sześciu zmniejszył RMS o 1,53%. To wynik warunkowy,
nie dowód skutecznego odszumiania całego strumienia. Nie rozszerzono progów
ani nie strojono modelu na tym teście. Przygotowano nowy model diagnostyczny
z dzisiejszych 30 REF, z tym samym rank-2 i maskami. Wymaga osobnego REF
po treningu i oceny zachowania sygnału; nie został aktywowany w aplikacji.

Raport, hashe, wyniki oraz kolejność dalszych pomiarów:
[ref-stage-report.md](artifacts/agent-noise-campaign-20261004/ref-stage-report.md).
Podział parzystych/nieparzystych binów w 350–850 MHz jest diagnostyczny;
nie stanowi fizycznie kwalifikowanej ochrony nieznanego pasma magnetycznego.
Rzeczywisty SIGNAL i REF po nim pozostają do zebrania po potwierdzeniu stanu operatora.

### Dodatkowy filtr wąskich pików w Live (2026-10-04)

W panelu Cleanup wdrożono `Narrow-peak rejection · display only`.
W każdym widmie wykrywa oba znaki odstępstw względem lokalnej mediany
i odpornej skali, następnie ogranicza ich szerokość w Hz i zasięg skrzydeł.
Nie wymaga stabilnej amplitudy ani treningu. Zastępuje tylko przyjętą maskę;
poza nią wartości pozostają niezmienione. Jest to opcjonalny podgląd,
nie kwalifikowana korekcja magnetyczna. Wąski docelowy sygnał wymaga ochrony pasma.

Nowy panel Parameters ma limit szerokości, próg i Protect band. Domyślne
ustawienia: 6 MHz, sześć lokalnych skal. Dla zrzutu użytkownika należy
wybrać źródło `Processed [dB]`, żeby filtrować wynik odejmowania referencji.
Worker nadal oblicza poza GUI i zachowuje tylko najnowszy oczekujący podgląd.

Na 29 późniejszych RAW REF minus pierwsze REF, 10 001 punktów, końcowa
mediana czasu wyniosła 34,88 ms, p95 39,78 ms. RAW pozostało niezmienione.
Test ten nie jest pomiarem SIGNAL i nie dowodzi zachowania sygnału.
Ostatnia regresja 60 PASS oraz dodatkowe 8 PASS widgetu wykresu; jasny/ciemny
panel i rzeczywisty widget strony pokazano i obejrzano.
Opis algorytmu, źródła, ograniczenia oraz instrukcja:
[FILTR_WASKICH_PIKOW_LIVE.md](FILTR_WASKICH_PIKOW_LIVE.md).

