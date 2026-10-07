# Końcowy stan urządzeń po sweepie

## Polityka

Domyślne zakończenie pozostaje safe shutdown. Jawny bloczek `final_state` w gałęzi Finally pozwala wybrać urządzenie, kanał, końcowe wartości i tryb `off` albo `hold`. Dotyczy wyłącznie poprawnego ukończenia pomiarów. Błąd, watchdog, Stop i E-STOP zachowują awaryjne wyłączenie; procedura awaryjna nie wykonuje końcowych nastaw mogących podtrzymać wyjście.

Obsługiwane końcowe nastawy:

| Urządzenie | Kanały | Nastawy po sukcesie |
| --- | --- | --- |
| MOKE Box | VOUT 0–7, zgodnie z zatwierdzonymi profilami | zero i rozbrojenie albo podtrzymanie zadanej wartości napięcia |
| Keithley | A, B | zadany prąd/napięcie w istniejącym trybie źródła albo ostatnia zaplanowana wartość; OUTPUT OFF albo pozostawienie wcześniej potwierdzonego OUTPUT ON |
| Rigol | CH1, CH2 | częstotliwość, high/low level albo ostatnie zaplanowane wartości; OUTPUT OFF albo pozostawienie wcześniej potwierdzonego OUTPUT ON |

Podtrzymanie nie włącza potajemnie wyjścia wyłączonego w planie. Keithley/Rigol wymagają wcześniejszej konfiguracji i OUTPUT ON w tym samym sweepie. MOKE wymaga użycia wybranego VOUT w głównym planie. Podtrzymywane nastawy przechodzą przez istniejące kompilatory i adaptery: limity stanowiska/DUT, jednostki, zakres źródła, compliance i readback pozostają obowiązkowe. Tryb źródła, sense, compliance, NPLC oraz inne niepodane parametry Keithley nie są zmieniane przez końcową nastawę. Pomiar czteroprzewodowy pozostaje zabroniony.

## UI i YAML

Bloczek „State after completion” znajduje się w bibliotece „Completion and shutdown”. Można go również dodać z inspektora lub dwuklikiem na gałęzi Finally. Wartości są sprawdzane w kontekście całej receptury przed zapisaniem zmiany. Bloczek ma zarejestrowany identyfikator `recipe.final_state`.

```yaml
finally:
  - id: final-moke-state
    type: final_state
    block_type: recipe.final_state
    device: moke_box
    channel: 2
    output: hold
    voltage: 5 mV
```

Ten przykład wymaga wcześniejszego użycia VOUT 2 w głównym planie. Pozostawia napięcie programujące 5 mV po sukcesie; nie potwierdza wartości pola magnetycznego ani stanu zasilania Kepco. Przy awarii/Stop następuje kwalifikowany powrót DAC do zera.

Drzewo pokazuje numer kanału oraz końcowe wartości. Automatyczne wiersze zakończenia wskazują, które kanały zostaną podtrzymane po sukcesie i wyłączone przy awarii. Wcześniejszy jawny bloczek zero/OFF dla kanału zastąpionego końcowym stanem jest oznaczony „Fault / Stop only”: po sukcesie nie powoduje pośredniego powrotu do zera przed zadaną wartością.

`stop_moke_voltage` przyjmuje teraz jawny `channel`. Starsze polecenia bez kanału zachowują semantykę wszystkich VOUT użytych przez run; drzewo rozwija ich numery na podstawie receptury. Testowa receptura `anritsu_background_reference_smoke_test.yml` ma w zakończeniu jawny VOUT 2 i nadal domyślnie wraca do zera.

## Wykonanie i przechowywanie danych

Silnik rozróżnia akcje końcowe wykonywane tylko po sukcesie od awaryjnego cleanup. Plan oraz hash obejmują tę politykę. Awaryjna ścieżka pomija akcje podtrzymania i próbuje wyłączyć pozostałe urządzenia nawet po błędzie jednego z nich. Brak potwierdzenia końcowego OUTPUT ON powoduje błąd i awaryjne OFF.

Po sukcesie z podtrzymaniem wynik ma stan `HOLDING`; UI opisuje pomiar jako ukończony i informuje o aktywnych wyjściach. Plik H5 jest zamknięty jako `completed`. Końcowe nastawy/readback trafiają do istniejących zdarzeń i snapshotów urządzeń, a zdarzenie `run_completed` zawiera `retained_outputs`. Dry run nie podtrzymuje ani nie włącza wyjść.

RunWorker zachowuje połączenie podtrzymywanego urządzenia, ponieważ zwykłe `disconnect()` wyłączyłoby wyjście. Własność sesji wraca do trwałego kontrolera karty urządzenia po zwolnieniu rezerwacji runu. Worker odrzuca podtrzymanie bez takiego kontrolera przed komunikacją z urządzeniami. Błąd cleanup lub zwolnienia rezerwacji powoduje ponowne awaryjne wyłączenie przez niezależny dostęp kontrolera, również gdy stara rezerwacja już wygasła.

## Weryfikacja

- Końcowa regresja: **219 testów i 11 subtestów przeszło** — kompilator, adaptery/runner, limity MOKE oraz nowa polityka końcowa.
- Biblioteka UI, rejestr i nowa polityka: **56 testów przeszło**.
- Testy obejmują MOKE/Keithley/Rigol, sukces, Stop, błąd pomiaru, błąd zamknięcia pliku, dry run, utratę potwierdzenia OUTPUT, konflikt ze starym cleanup i błąd zwolnienia rezerwacji.
- Pełna ścieżka Qt RunController → RunWorker → kontroler urządzenia została wykonana z symulatorami dla trzech urządzeń. Sprawdzono utrzymanie połączenia, wartości/OUTPUT po sukcesie oraz zakończony H5.
- Okno wyświetlono po `show()` i przetworzeniu zdarzeń; zrzut znajduje się w `final-state-dialog.png`. Ruff dla zmienionych plików: bez błędów.

Nie wykonywano pomiarów ani mutacji na fizycznej aparaturze.
# Poprawka edycji z menu kontekstowego (2026-10-06)

Menu „Set final output state” otwierało dodawanie nowego bloczka bez kontekstu zaznaczenia.
Dla zaznaczonego „MOKE VOUT 2 · Return to 0 V” modal wybierał więc VOUT 0.
Zapis był odrzucany przez kompilator, gdy VOUT 0 nie występował w planie głównym;
komunikat był widoczny pod polem napięcia, ale użytkownik odbierał to jako brak reakcji.

Menu przekazuje teraz zaznaczony bloczek: zachowuje urządzenie, kanał oraz identyfikator.
Przy konwersji bloczka wyłączenia na wybrany stan końcowy zastępuje go i umieszcza
po pozostałych akcjach cleanup. Edycja istniejącego `final_state` zachowuje jego wartości
i pozycję, bez dodawania duplikatu. Kompilacja całego planu przed zapisem nadal egzekwuje
limity i wymagany wcześniejszy stan wyjścia. Nie zmieniono komend sprzętowych ani shutdown
przy błędzie, Stop czy E-STOP.

Test regresji uruchamia menu kontekstowe oraz rzeczywisty przycisk Save w wyświetlonym
modalu dla MOKE, Keithleya i Rigola. Sprawdza zapis YAML, identyfikator, kanał, kompilację,
ponowną edycję oraz odrzucenie MOKE 10000 mV. Render: `final-state-context-fix.png`.

# Jeden edytor końcowego stanu MOKE (2026-10-06)

Aktywacja drzewa, przycisk edytora w inspektorze i edycja stanu końcowego z menu
otwierają ten sam `FinalStateDialog` zarówno dla starszego `stop_moke_voltage`
w Finally, jak i dla `final_state`. Numer VOUT pozostaje wybrany. Starszy bloczek
zerowania otwiera się z polityką **Ramp to 0 V and disarm**, a istniejący stan
końcowy zachowuje zapisaną politykę. Użytkownik wybiera w jednym modalu zerowanie
lub **Hold requested voltage**; pole napięcia jest aktywne tylko dla podtrzymania.
Przełączenie na zero i z powrotem zachowuje poprzednią wpisaną wartość podtrzymania.
Zapis zastępuje starszy bloczek przez zarejestrowany `recipe.final_state`, zachowuje
identyfikator i przechodzi walidację całego planu. Stop MOKE w głównej sekwencji
pozostaje operacją zatrzymania — nie jest stanem po zakończeniu pomiaru.

Testy obejmują oba wybory przez trzy wejścia do UI, rzeczywiste kliknięcie Save,
render modala i zachowanie YAML. Zrzuty: `unified-moke-final-off.png`,
`unified-moke-final-hold.png`. Polecenia sprzętowe i polityka błędu nie zmieniły się.

