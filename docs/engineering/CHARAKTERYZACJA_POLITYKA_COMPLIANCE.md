# Keithley: bezpieczna polityka compliance w charakterystyce

Dokument opisuje wdrożony przepływ dla karty **Keithley 2600 → Characterization**.
Jego celem jest zachowanie jednego źródła konfiguracji sprzętowej oraz wymuszenie
zatrzymania charakterystyki przy pierwszym compliance.

## Zasada użytkowa

Zwykła karta Keithleya pozostaje właścicielem wszystkich nastaw sprzętowych:

- kanału i trybu źródła,
- poziomu ręcznie sprawdzonego przez operatora,
- limitu compliance,
- zakresu źródła i zakresów pomiarowych,
- autorange,
- NPLC i czasu ustalania,
- trybu 2-wire/4-wire,
- limitów laboratoryjnych.

Charakterystyka tworzy z tej konfiguracji tylko kolejne poziomy osi sweep. Nie
tworzy własnego profilu Keithleya i nie zapisuje osobnych nastaw źródła. Jedyną
tymczasową zmianą jest reakcja na compliance: podczas charakterystyki musi być
`stop` (wyłączenie wyjścia), nawet jeżeli normalna karta ma `warn_clamp` albo
historyczne `skip`.

Domyślna polityka normalnej pracy to `warn_clamp`. Oznacza to ograniczenie
sprzętowe i ostrzeżenie bez automatycznego zatrzymania ręcznego pomiaru. Nie
jest to jednak polityka dopuszczalna dla charakterystyki, ponieważ charakterystyka
ma zakończyć sweep przy pierwszym punkcie compliance.

Zmiana charakterystyki jest wyłącznie stanem runtime. Nie jest zapisywana do
`settings.yml` ani do profilu użytkownika; po przywróceniu normalna karta i
adapter wracają do polityki sprzed startu.

## Sekwencja startu

1. Karta sprawdza, czy nie trwa już worker charakterystyki ani przejście polityki.
2. Pobiera przez `RunDeviceAdapter` stan połączenia oraz **rzeczywistą** politykę
   kanału z adaptera. Odczyt odbywa się w wątku właściciela VISA.
3. Buduje konfigurację sweep z normalnej karty, wymuszając w obiekcie konfiguracji
   wyłącznie `compliance_policy="stop"`. Wszystkie pozostałe pola pochodzą z
   tego samego `KeithleySourceRequest`, który jest używany w zwykłej karcie.
4. Wykonuje istniejący preflight limitów i zakresów. Jeżeli dane są niepoprawne,
   nie jest odczytywana ani zmieniana polityka sprzętu.
5. Wykonuje wyłącznie odczyt konfiguracji Keithleya i wymaga potwierdzenia, że
   wybrany kanał ma `OUTPUT OFF`. Jeżeli ręczny pomiar nadal zasila kanał,
   charakterystyka jest blokowana przed zmianą polityki.
6. Jeżeli zarówno adapter, jak i normalna karta potwierdzają `stop`, worker może
   wystartować bez modalnego pytania.
7. Jeżeli adapter albo normalna karta ma `warn_clamp`/`skip` (albo oba widoki się
   różnią), pojawia się modal:
   - pokazuje aktualną politykę,
   - wyjaśnia, że na czas pomiaru zostanie użyte `Stop on compliance`,
   - potwierdza, że limity, zakresy, NPLC, settling i sense pozostają bez zmian,
   - informuje, że poprzednia polityka zostanie przywrócona po potwierdzonym
     `OUTPUT OFF`.

   Anulowanie kończy start bez wysłania polecenia OUTPUT.
8. Po akceptacji normalna karta wykonuje `set_compliance_policy(channel, "stop")`.
   Wynik jest traktowany jako sukces dopiero po dwóch warunkach:
   - adapter zwróci dokładnie `stop`,
   - ponowny odczyt `compliance_policy(channel)` zwróci dokładnie `stop`.
9. Dopiero wtedy uruchamiany jest `CharacterizationWorker`. Przed startem workera
   nie jest wysyłane `set_output(..., True)`.

Od momentu rozpoczęcia transakcji polityki do zakończenia przywracania karta
blokuje kanał, tryb, poziomy, liczbę punktów oraz wybór próbki/komórki. Worker
pracuje na niezmiennym snapshotcie, więc edycja formularza nie może rozjechać
konfiguracji użytej przez pomiar.

## Sekwencja wykonania

Runner zachowuje dotychczasową kolejność:

1. odczytuje ostatnią ręcznie zastosowaną i zweryfikowaną konfigurację,
2. konfiguruje pierwszy punkt z pełnym `KeithleySourceRequest`, przy wyjściu OFF,
3. wykonuje readback wszystkich pól konfiguracji,
4. włącza wyjście,
5. ustawia kolejne poziomy i mierzy,
6. przy pierwszym compliance zapisuje punkt i nie aplikuje następnego poziomu,
7. w bloku `finally` rampuje poziom do zera i wyłącza wyjście.

Runner nadal ma twardą blokadę: jeśli rzeczywista polityka adaptera różni się od
`stop`, nie włączy wyjścia. Modal nie zastępuje tej blokady; tylko pozwala
operatorowi jawnie zatwierdzić bezpieczną zmianę przed jej sprawdzeniem.

## Przywracanie polityki

Po sygnale zakończenia workera wyjście jest już wyłączone przez runner. Karta:

1. zapisuje CSV/PDF i wynik pomiaru,
2. wysyła do normalnej karty żądanie przywrócenia dokładnie polityki odczytanej
   przed startem (`warn_clamp`, `skip` albo innej dozwolonej),
3. wymaga zwrotu tej samej wartości oraz ponownego readbacku z adaptera,
4. dopiero po pozytywnym readbacku odblokowuje przycisk Start i normalny wybór
   polityki.

Jeżeli przed startem była już polityka `stop`, nie ma przejścia ani przywracania.
Jeżeli przywrócenie się nie powiedzie, wyjście pozostaje OFF, kontrolki polityki
pozostają zablokowane, a karta pokazuje trwały komunikat błędu. Nie można wtedy
uruchomić kolejnej charakterystyki na niepotwierdzonym stanie. Karta pokazuje
`Retry policy restore`; przycisk najpierw ponownie potwierdza `OUTPUT OFF`, a
dopiero potem ponawia przywrócenie. Dzięki temu operator nie musi odblokowywać
polityki ręcznie na podstawie samego komunikatu.

Jeżeli worker zgłosi błąd, karta niezależnie sprawdza `assert_output_state(...,
expected_enabled=False)`. Przy braku potwierdzenia OFF również nie przywraca
polityki i blokuje dalszą pracę. Zapobiega to ukryciu sytuacji, w której błąd
komunikacji mógł pozostawić nieznany stan wyjścia.

## Ochrona przed resetem przy zapisie ustawień

`refresh_station_context()` aktualizuje kontekst i limity, ale nie resetuje
żywej per-kanałowej polityki compliance. Resetowanie jej do wartości z pliku
ustawień w trakcie charakterystyki mogłoby zmienić `stop` na `warn_clamp` przy
włączonym wyjściu. Jawna zmiana profilu lub wymiana adaptera nadal ustawia
politykę wynikającą z ustawień stacji.

Strona Keithleya nie nadpisuje również projekcji tymczasowej polityki podczas
zapisu niezwiązanych ustawień. Normalny selektor polityki jest zablokowany od
potwierdzenia przejścia do `stop` aż do potwierdzenia przywrócenia.

## Warunki, które pozostają gwarantowane

- Charakterystyka nie ma niezależnych nastaw `source range`, `measure range`,
  `NPLC`, `settling`, `sense` ani compliance limit.
- Zmiana polityki nie zmienia limitu napięcia/prądu ani poziomu sweep.
- Żaden modal nie włącza wyjścia; przed potwierdzeniem nie startuje worker.
- Compliance zatrzymuje sweep przed kolejnym setpointem i zapisuje dotychczasowe
  punkty.
- Punkt o zerowym wymuszeniu jest pomijany zgodnie z istniejącą regułą runnera.
- Każdy błąd readbacku kończy się stanem zachowawczym: OUTPUT OFF i blokadą
  dalszej charakterystyki.

## Weryfikacja automatyczna

Dodane testy sprawdzają:

- pełny runner i jego readback polityki,
- niezależne potwierdzenie OUTPUT OFF po błędzie,
- przejście `warn_clamp → stop` przez normalną kartę,
- blokadę kontrolek podczas charakterystyki,
- readback i przywrócenie `stop → warn_clamp`,
- brak wywołania OUTPUT ON przed akceptacją modalnego przejścia.

Uruchomienie testów:

```text
python -m pytest tests/test_keithley_characterization_runner.py -q
python -m pytest tests/test_keithley_characterization_ui.py -q
ruff check app tests
```
