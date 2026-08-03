# Quick Controls — wspólny stan, zakresy i suwaki

## Cel

Quick Controls mają być drugą powierzchnią tego samego procesu ręcznego
ustawiania wartości, a nie niezależnym panelem z własnymi wartościami i
limitami. Wartość edytowana w karcie urządzenia ma natychmiast pojawić się w
Quick Controls, a wartość ustawiana w Quick Controls ma zaktualizować kartę.
Obie powierzchnie muszą pokazywać te same zakresy MIN/MAX i ten sam stan
potwierdzenia przez instrument.

## Zakres

Zmiana obejmuje:

- wspólny, UI-neutralny model draftu i readbacku dla zarejestrowanych celów
  Quick Controls;
- jeden resolver zakresów oparty o profil laboratoryjny, envelope hardware i
  istniejące ograniczenia bezpieczeństwa;
- synchronizację formularzy Rigol i Keithley z tym modelem;
- suwaki dla wszystkich parametrów, z mapowaniem SI i sensownym mapowaniem
  logarytmicznym częstotliwości;
- przebudowę pływającego okna na spójny układ Fluent Widgets;
- testy wartości, jednostek, sprzężonych reprezentacji, bezpieczeństwa i
  renderowanej geometrii.

Nie zmieniamy protokołów SCPI/TSP, schematu ustawień ani modelu danych pomiaru.
Istniejące ścieżki OUTPUT ON/OFF i awaryjnego wyłączenia pozostają własnością
stron urządzeń oraz ich adapterów.

## Źródła prawdy

`QUICK_CONTROL_DESCRIPTORS` pozostaje rejestrem tożsamości celu: target,
moduł, wymiar, etykieta i grupa atomowa. Nie powstaje drugi słownik celów w
oknie.

Nowy stan kontrolki przechowuje dla każdego targetu:

- `draft_si` i tekst z jednostką,
- ostatnią wartość `confirmed_si` z readbacku,
- źródło ostatniej zmiany (`device_card`, `quick_controls`, `readback`),
- status (`draft`, `pending`, `confirmed`, `rejected`, `unknown`),
- szczegóły błędu lub readbacku.

Draft jest stanem roboczym operatora, ale nie jest dowodem stanu hardware.
Komenda nadal przechodzi przez `QuickControlCoordinator`, preflight i adapter.
Po błędzie draft wraca do ostatniego potwierdzonego stanu albo pozostaje
oznaczony jako rejected, jeśli nie ma readbacku, bez ponownego wysyłania
komendy.

Readback starszy niż lokalny draft nie może go nadpisać. Po udanej zmianie
Quick Controls wykonywany jest pełny readback snapshot, ponieważ Rigol ma
sprzężone reprezentacje High/Low oraz Amplitude/Offset.

## Zakresy i jednostki

`quick_control_safety_bounds()` zostaje rozszerzone do kompletnego resolvera
zakresów używanego przez Quick Controls i `LimitField` na kartach urządzeń.
Wartości obliczeniowe są w SI; tekst jest formatowany dopiero na granicy UI.

Efektywny zakres to przecięcie:

1. zakresu skonfigurowanego w `StationSettings`, wraz z `max_abs`,
2. immutable hardware envelope instrumentu,
3. ograniczeń wynikających z aktywnego trybu/kanału, jeśli są znane.

Każdy quick-control target ma skończone minimum i maksimum. Wyłączony zakres
profilu nie jest prezentowany jako nieskończone `HARDWARE`; resolver zwraca
liczbowy limit hardware i tekst w tej samej jednostce co karta. Dodatkowe
ograniczenia sprzężone, takie jak High > Low, maksymalna amplituda zależna od
częstotliwości oraz limity prądu/mocy Rigola, pozostają sprawdzane przez
adapter przed mutacją i mogą odrzucić kandydat spoza statycznego suwaka.

Wartości `dBm`, dB i innych wymiarów nie są skalowane jak jednostki SI.
Suwak operuje na `float` SI, a edytor tekstowy zawsze przekazuje do wykonania
liczbę z jawną jednostką.

## Synchronizacja stron

Strony publikują drafty do wspólnego modelu przy zmianie kanału, trybu i pola.
Quick Controls subskrybuje te same sygnały i nie czyta bezpośrednio widgetów
stron.

Rigol publikuje jednocześnie częstotliwość, High Level, Low Level, Amplitude i
Offset. Zmiana jednej reprezentacji aktualizuje wszystkie powiązane wartości.
Keithley publikuje aktywny source level dla kanału i trybu current/voltage.

Po udanym `configure`, `quick_setpoint` lub readbacku strony otrzymują
potwierdzone wartości z tego samego modelu. Zmiana draftu przy OUTPUT OFF nie
wysyła komendy; pozostaje widoczna jako draft do czasu istniejącego
`Configure/Apply`. Zmiana przy OUTPUT ON korzysta z obecnej ścieżki quick
setpoint i jej walidacji bezpieczeństwa.

## Suwaki

Komponent `QuickQuantitySlider` ma jeden interfejs:

- `set_bounds(QuickControlSafetyBound)`;
- `set_value_si(float)`;
- sygnał `draft_value_changed(target, quantity_text)`;
- sygnał `commit_requested(target, quantity_text)`.

Mapowanie jest liniowe dla prądu, napięcia, offsetu, High/Low i amplitudy.
Częstotliwość używa mapowania logarytmicznego dla dodatniego zakresu. Wartość
na suwaku jest aktualizowana bezpośrednio podczas przeciągania, ale komenda do
instrumentu jest wysyłana dopiero po puszczeniu suwaka lub przez krótki,
interruptible debounce. Wartość jest zawsze ponownie parsowana i sprawdzana
przez coordinator.

Jeśli zakres jest niekompletny, nieodwracalny lub ma zerową szerokość, suwak
jest disabled, a edytor tekstowy pozostaje dostępny tylko do prezentacji
komunikatu o braku skończonego zakresu. Nie wolno zamieniać nieskończonego
zakresu na arbitralną stałą w UI.

## Układ Fluent

Okno zachowuje `StationDialog`, `WindowStaysOnTopHint`, wybór kontrolek oraz
OUTPUT ON/OFF. Zawartość zostaje uporządkowana jako:

1. nagłówek z tytułem, stanem synchronizacji i akcją wyboru,
2. karta bezpieczeństwa/outputów,
3. osobne `CardWidget` dla Rigol i Keithley,
4. wiersze parametrów z etykietą, wartością, suwakiem, MIN/MAX i badge'em
   stanu.

Używane są istniejące tokeny aplikacji i komponenty QFluentWidgets. Kolor
statusu ma znaczenie semantyczne: success dla confirmed, accent dla draft,
caution dla pending i danger dla rejected/unknown. Nie powstają per-page
kolory ani ręcznie stylowana kopia komponentu Fluent. Układ ma zachować
widoczną geometrię przy normalnym desktopie i przy wąskiej szerokości bez
poziomego scrolla.

## Bezpieczeństwo i awarie

- UI nie jest autorytetem bezpieczeństwa.
- Wartość poza zakresem nie wysyła żadnej komendy.
- Adapter nadal wykonuje końcową walidację dimension, finite value, limitów i
  sprzężeń.
- Timeout, transport loss, compliance lub readback mismatch prowadzi do
  istniejącego stanu `FAULT`/`UNKNOWN` i nie jest maskowany przez suwak.
- OUTPUT ON/OFF nie zostaje przeniesione do Quick Controls jako bezpośrednia
  komenda; okno wywołuje stronę właściciela urządzenia.

## Kryteria akceptacji

## Doprecyzowanie: synchronizacja natychmiastowa i precyzja

Draft is published on every valid edit, not only after Apply or slider release.
A Quick Controls slider movement therefore updates the matching card field in
the same event turn; a valid card edit updates the matching Quick Controls row
in the same way. Hardware submission can remain debounced until release, but
the two UI surfaces must never show different draft values.

Slider precision is derived from the last written digit in the current text:

- `0.00100 A` -> `0.00001 A` per slider step;
- `10.000 kHz` -> `0.001 kHz`, i.e. `1 Hz` per step;
- `1.00e-3 A` -> `1e-5 A` per slider step.

The slider rebuilds its discrete positions after the operator writes a new
precision. It quantizes candidates with `quantity_step_si()` and renders them
with `render_quantity_si_like()` in the same unit and precision, while still
clamping against the shared effective bounds.

1. Edycja wartości w karcie urządzenia aktualizuje Quick Controls bez
   ponownego otwierania okna.
2. Edycja w Quick Controls aktualizuje kartę i po readbacku pokazuje dokładnie
   wartość przyjętą przez instrument.
3. MIN/MAX w obu miejscach pochodzą z jednego resolvera i są identyczne po
   zmianie ustawień, kanału i trybu.
4. Każdy quick-control target ma suwak albo jawny komunikat, dlaczego suwak
   jest niedostępny.
5. Rigol utrzymuje spójność High/Low/Amplitude/Offset.
6. Testy renderują okno po `show()` i `processEvents()` przy normalnej oraz
   wąskiej szerokości.
7. Istniejące testy bezpieczeństwa, adapterów, stron i Quick Controls pozostają
   zielone.
