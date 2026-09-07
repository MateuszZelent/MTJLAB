# Audyt sterowania i bezpieczeństwa Keithley / Rigol

Data aktualizacji: 2026-09-07

## Zakres

Audyt objął moduł charakteryzacji Keithley, ręczne sterowanie Keithley i Rigol,
Quick Controls, kompilację receptur, adaptery VISA, symulatory, ustawienia stacji
oraz odczyt zwrotny konfiguracji. Głównym kryterium było niedopuszczenie do
wysłania do przyrządu nastawy spoza jawnie skonfigurowanych granic operatora.

## Ustalenia dotyczące Rigola

Rigol DG1032Z nie mierzy prądu płynącego przez MTJ. Prąd zależy między innymi od
rezystancji konkretnego złącza, która może zmieniać się od dziesiątek omów do
około 3 kΩ. Wyliczenie z modelu wyjścia 50 Ω jest tylko konserwatywną estymacją
sprzętową i nie stanowi pomiaru ani zabezpieczenia prądowego próbki. Bez
zewnętrznego pomiaru prądu aplikacja nie może potwierdzić rzeczywistego prądu MTJ.

Poprzedni model zawierał cztery niezależne limity napięcia: `high_level`,
`low_level`, `amplitude_vpp` i `offset`. Pozwalało to na niespójne profile.
Szczególnie niebezpieczny był tryb DC: wyłączony limit offsetu mógł dopuścić
wartość znacznie większą od widocznych granic High/Low. Osobna walidacja
amplitudy i offsetu nie gwarantowała również ograniczenia ich sumy.

Model został zastąpiony jednym obowiązkowym limitem
`combined_voltage_limit`. Nie ma flagi wyłączającej ten limit. Dla każdej
reprezentacji przebiegu obowiązuje jedna reguła:

```text
Vpp + |offset| <= combined_voltage_limit
```

- dla `Amplitude + Offset` używane są bezpośrednio wartości z formularza;
- dla `High Level + Low Level` aplikacja wylicza `Vpp = High − Low` oraz
  `offset = (High + Low) / 2`, a następnie stosuje tę samą regułę;
- dla DC przyjmowane jest `Vpp = 0`, więc obowiązuje
  `|poziom DC| <= combined_voltage_limit`;
- dodatni i ujemny offset zużywają taki sam budżet.

Walidacja działa przed utworzeniem żądania sprzętowego w karcie urządzenia,
w Quick Controls, podczas kompilacji receptury oraz ponownie w adapterze przed
SCPI. Zmiana odrzucona przez którąkolwiek warstwę nie może wygenerować komendy
ustawiającej napięcie. Karta pokazuje bieżące wykorzystanie wspólnego budżetu.
Sterowanie strzałkami ogranicza amplitudę do `limit − |offset|`, offset do
`±(limit − Vpp)`, a w trybie High/Low wyznacza graniczny dopuszczalny poziom z
tej samej funkcji.

Stare profile są migrowane automatycznie. Nowy limit przyjmuje najbardziej
restrykcyjną dodatnią granicę spośród dawnych aktywnych limitów i wartości z
nowego szablonu. Migracja może więc zawęzić zakres, ale nie rozszerza go bez
decyzji operatora. Bieżący profil i szablon mają wartość `100 mV` dla obu
kanałów. Operator może zmienić tę jedną wartość w ustawieniach stacji.

Adapter dodatkowo jawnie ustawia jednostkę amplitudy Rigola na Vpp, sprawdza ją
odczytem zwrotnym i używa właściwej postaci polecenia `APPL:DC` z poziomem DC w
polu offsetu. Usuwa to niejawne założenie dotyczące jednostki oraz wcześniejsze
ryzyko błędnej interpretacji argumentu DC.

Podczas testu pełnej receptury wykryto też błąd kwantyzacji. Amplituda i offset
są zaokrąglane niezależnie do rozdzielczości przyrządu, a ich suma może przesunąć
High/Low o pół kroku. Adapter wcześniej zaokrąglał odczytane High/Low ponownie i
zapamiętywał stan różny od rzeczywistego. Obecnie dokładny stan wynikający z
wartości wysyłanych na przewodzie jest ponownie walidowany przed SCPI, a
rzeczywisty odczyt jest zachowywany bez wtórnego zaokrąglenia. Jeżeli sama
kwantyzacja przekroczyłaby wspólny limit, konfiguracja jest odrzucana przed
jakimkolwiek ruchem sprzętowym.

## Ustalenia dotyczące Keithley i charakteryzacji

Domyślny tryb pomiaru został zmieniony z `4wire` na `2wire` w profilach kanałów.
Tryb sense jest własnością profilu stacji, a formularz charakteryzacji nie ma
lokalnego pola, które mogłoby go przełączyć. Wybrana wartość przechodzi do żądania adaptera, który ustawia
`SENSE_LOCAL` dla 2-wire albo `SENSE_REMOTE` dla 4-wire i weryfikuje stan
odczytem zwrotnym przed uznaniem konfiguracji za zastosowaną.

Włączenie 4-wire wymaga teraz jawnej zmiany w ustawieniach kanału. Karta
charakteryzacji pokazuje wtedy widoczne ostrzeżenie o konieczności fizycznego
podłączenia przewodów Sense HI i Sense LO. Domyślne wartości poziomów sweepu
charakteryzacji zostały zmniejszone z ±10 mA do ±100 µA. Compliance nie ma już
własnej wartości domyślnej używanej do pomiaru: jest wyświetlane tylko do odczytu
i pochodzi ze zwykłej karty Keithley. Ostateczną granicą pozostają limity kanału
w profilu; adapter ponownie je sprawdza przed wysłaniem nastawy.

Charakteryzacja nie ma odrębnej konfiguracji sprzętowej ani odrębnych pozycji w
ustawieniach. Kanał i tryb są synchronizowane ze zwykłą kartą Keithley, a dokładnie
ten sam konstruktor żądania przekazuje compliance, NPLC, czas settling, 2/4-wire,
autorange oraz ręczne zakresy źródła i obu torów pomiarowych. Tylko poziom źródła
jest zastępowany kolejnymi wartościami start–stop, a liczba punktów jest parametrem
sweepu. Compliance i settling są na karcie charakteryzacji polami tylko do odczytu.
Brak dostępu do konfiguracji zwykłej karty blokuje start zamiast uruchamiać pomiar
z wartościami lokalnymi.

Punkt o zerowej wartości źródła jest usuwany z planu charakteryzacji przed
zaprogramowaniem pierwszej nastawy. Dotyczy to zera będącego początkiem sweepu oraz
zera wypadającego wewnątrz zakresu, także gdy `linspace` reprezentuje je jako
minimalny artefakt zmiennoprzecinkowy. Zero nie jest ustawiane ani zapisywane jako
punkt pomiarowy. Bezpieczny `ramp_to_zero` po pomiarze pozostaje obowiązkowy i nie
jest dodawany do danych.

Przed włączeniem wyjścia runner wymaga ostatniej konfiguracji zastosowanej ręcznie
i potwierdzonej odczytem zwrotnym na tym samym adapterze oraz w tej samej sesji
urządzenia. Po zaprogramowaniu pierwszego punktu przy `OUTPUT OFF` porównuje każdy
parametr poza zmiennym poziomem źródła z konfiguracją ręczną. Niezgodność blokuje
start. Po każdej zmianie poziomu pełna konfiguracja i stan wyjścia są ponownie
odczytywane z urządzenia.

W module charakteryzacji znaleziono dodatkowy błąd krytyczny: runner na czas
sweepu jawnie zmieniał politykę compliance na `skip`, zapisywał punkt ograniczony
przez compliance i przechodził do kolejnej, potencjalnie wyższej nastawy. Limit
compliance przy wymuszaniu prądu ogranicza napięcie wyjściowe; nie jest niezależnym
limitem wartości zadanego prądu. Dlatego kontynuowanie sweepu po compliance nie
chroni próbki, której rezystancja może zmienić się w czasie pomiaru.

Runner charakteryzacji wymaga, aby zwykła karta i urządzenie miały już politykę
`stop`; nie zmienia jej lokalnie. Zmiana oczekująca na potwierdzenie albo polityka
`warn_clamp`/`skip` blokuje start przed włączeniem wyjścia.

Dla zwykłej pracy Keithley domyślną polityką jest ponownie `warn_clamp`: sprzęt
utrzymuje wyjście w ograniczeniu, aplikacja pokazuje compliance i blokuje ruch
nastawy dalej w niebezpiecznym kierunku. Gdy operator wybierze `stop`, compliance
nadal wyłącza i zatrzaskuje wyjście danego kanału, lecz nie wyłącza zaznaczonego
odczytu Live. Live kontynuuje pomiary przy `OUTPUT OFF`, dzięki czemu stan na ekranie
odpowiada zaznaczonej kontrolce. Charakteryzacja pozostaje wyjątkiem wymagającym
jawnego wyboru `stop` przed startem sweepu.
Pierwszy punkt, w którym urządzenie zgłosi compliance, jest zapisywany jako ostatni
punkt danych. Adapter wyłącza kanał i potwierdza `OUTPUT OFF`, runner nie wysyła
następnej nastawy, zeruje nastawę i ponownie żąda wyłączenia wyjścia. Jeśli końcowego
stanu `OUTPUT OFF` nie można potwierdzić, przebieg kończy się błędem zamiast zwrócić
wynik. Częściowy zbiór otrzymuje status `stopped_on_compliance`, liczbę zapisanych
punktów i opis przyczyny. Ten status jest widoczny w karcie, inwentarzu, CSV i PDF;
nie jest prezentowany jako poprawnie zakończony pełny sweep. Anulowanie ma odrębny
status `cancelled`.

## Wynik weryfikacji

Automatyczna weryfikacja warstwy ustawień, walidatorów, adapterów, runnera,
Quick Controls i wskazówek ustawień zakończyła się wynikiem: **179 testów oraz
28 podtestów zaliczonych**. Dodatkowe testy sprawdzają granicę sumy, oba znaki
offsetu, DC, brak ruchu SCPI po odrzuceniu, zmianę przy aktywnym wyjściu oraz
bezpieczne zawężenie starego profilu.

Trzy ukierunkowane testy głównego okna potwierdziły blokadę konfiguracji ponad
wspólnym limitem, ograniczenie wartości po zakończeniu edycji oraz spójność
granic z Quick Controls. Test renderowania potwierdził, że wspólny budżet jest
widoczny i ma niezerową geometrię po pokazaniu okna i przetworzeniu zdarzeń Qt.

Po zmianach charakteryzacji zaliczono 25 testów runnera, analizatora i eksportów,
26 testów UI (z wyłączeniem opisanego niżej testu `QSettings`), 10 pozostałych
testów strony Keithley oraz 58 testów ustawień i symulatorów wraz z 49 podtestami.
Obejmują one przerwanie przed kolejną nastawą, zachowanie ostatniego punktu,
odrzucenie profilu `warn_clamp`, brak niezależnej konfiguracji charakteryzacji,
dziedziczenie wszystkich pól zwykłej karty, identyczność komend TSP konfiguracji
przed `OUTPUT ON`, pełny odczyt zwrotny przy każdym punkcie, potwierdzenie
wyłączenia na pełnym adapterze z symulatorem oraz oznaczenie częściowych danych
w UI, CSV i PDF. Kontrola Ruff i `git diff --check` nie zgłosiły błędów.
Dwa niezależne, istniejące testy trwałości wyboru próbki i ustawienia wykresu w
`QSettings` nie przechodzą w tym środowisku także poza tym zakresem; nie dotyczą
sterowania wyjściem ani konfiguracji charakteryzacji.

## Ograniczenie fizyczne

Wspólny limit napięcia gwarantuje, że aplikacja nie zleci Rigolowi wartości
większej od granicy ustawionej przez operatora według powyższej reguły. Nie
gwarantuje określonego prądu MTJ, ponieważ aplikacja nie otrzymuje rzeczywistego
pomiaru tego prądu z Rigola. Dobór `combined_voltage_limit` nadal musi wynikać z
bezpiecznej procedury dla danej próbki i jej spodziewanego zakresu rezystancji.
