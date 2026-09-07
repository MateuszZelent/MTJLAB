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

Domyślny tryb pomiaru został zmieniony z `4wire` na `2wire` w modelu
charakteryzacji oraz w profilach kanałów. Tryb sense jest własnością profilu
stacji, a formularz charakteryzacji nie może go przypadkowo przełączyć lokalnym
polem. Wybrana wartość przechodzi do żądania adaptera, który ustawia
`SENSE_LOCAL` dla 2-wire albo `SENSE_REMOTE` dla 4-wire i weryfikuje stan
odczytem zwrotnym przed uznaniem konfiguracji za zastosowaną.

Włączenie 4-wire wymaga teraz jawnej zmiany w ustawieniach kanału. Karta
charakteryzacji pokazuje wtedy widoczne ostrzeżenie o konieczności fizycznego
podłączenia przewodów Sense HI i Sense LO. Domyślne wartości sweepu
charakteryzacji zostały zmniejszone z ±10 mA do ±100 µA, a domyślne compliance
z 670 mV do 500 mV. Ostateczną granicą pozostają limity kanału w profilu;
adapter ponownie je sprawdza przed wysłaniem nastawy.

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

## Ograniczenie fizyczne

Wspólny limit napięcia gwarantuje, że aplikacja nie zleci Rigolowi wartości
większej od granicy ustawionej przez operatora według powyższej reguły. Nie
gwarantuje określonego prądu MTJ, ponieważ aplikacja nie otrzymuje rzeczywistego
pomiaru tego prądu z Rigola. Dobór `combined_voltage_limit` nadal musi wynikać z
bezpiecznej procedury dla danej próbki i jej spodziewanego zakresu rezystancji.
