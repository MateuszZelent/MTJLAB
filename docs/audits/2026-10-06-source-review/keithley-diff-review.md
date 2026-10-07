# Przegląd bieżącego diffu Keithley — 2026-10-06

## Zakres i metoda

Punkt odniesienia: HEAD `acce560ebf2810e3c85936670103e9e5595d2bf8`.
Przeczytano diff wszystkich dziewięciu zmienionych plików pakietu Keithley,
diff `app/safety/keithley.py` i pełne źródła czterech nowych helperów.
Przejrzano również powiązane fragmenty kompilatora, runnera, polityk,
ustawień, symulatora i kolejek pracowników. Nie jest to deklaracja przeczytania
każdej linii wszystkich współdzielonych modułów.

Zachowano [snapshot diffu](keithley-current-review.diff) oraz
[identyfikatory i SHA256 22 plików źródłowych](keithley-review-snapshot.json).
Diff współdzielonych plików zawiera również zmiany innych urządzeń.
Nowe pliki nie są widoczne w zwykłym `git diff`:
`characterization/output_state.py`, `output_proof.py`, `single_artifacts.py`
i `single_report.py`. Zostały przeczytane osobno. Nie wykonywano fizycznego I/O.

## Co zmienia bieżący diff

| Obszar | Zmiana i znaczenie |
| --- | --- |
| Konfiguracja adaptera | `changed_fields` wybiera jawnie programowane pola; pozostałe są zachowane z potwierdzonej konfiguracji. Konfiguracja odbywa się przy OUTPUT OFF, z kontrolą konfiguracji przed i po zapisie. |
| Sense | 4-wire jest odrzucany przez walidację ustawień i żądania; komendy konfiguracji używają wyłącznie SENSE_LOCAL. Nie jest to cicha konwersja błędnego żądania. |
| OFF | `emergency_off` zwraca dowód bool; brak połączenia lub brak potwierdzenia nie oznacza sukcesu. Potwierdzone OFF może współistnieć z zatrzaśniętym stanem COMPLIANCE. |
| Sweep | Sprawdzane są kanał, wymiar, tryb źródła, parametr i wybrany zakres. Zwykła oś zmienia poziom źródła; dodatkowa konfiguracja musi być jawna. Kompilator śledzi konfigurację poprzedzających bloków. |
| Autorange i limity | Polityka autorange musi zgadzać się z Settings; konflikt jest błędem. Nie zmieniono liczbowych limitów laboratoryjnych ani szablonu settings. Aktualny szablon ma maksymalny krok rampy prądu 100 µA dla obu kanałów. |
| Charakterystyka | Sprawdzany jest dowód OFF, anulowanie przed kolejnymi operacjami oraz cleanup przy błędzie przygotowania. Usunięto ponawianie mutacji po TypeError. Rezystancja wynika z zmierzonych V/I; niemierzalny prąd daje NaN. |
| UI urządzenia | Widoczne są programowane pola, zakresy i sense, porównania konfiguracji i licznik zmian. Porównanie dotyczy ustawień strony urządzenia, nie świeżego odczytu sprzętu. Samo otwarcie formularza nie dodaje pominiętych pól do zapisu. |
| UI i współbieżność | Preflight OFF, zapis RAW CSV, analiza/hash i PDF pracują poza GUI. CSV powstaje przed analizą. Live/polling i ręczna edycja respektują rezerwację kontrolera; OFF pozostaje dostępne. Odczyty wykonania aktualizują UI bez wywoływania zapisów. |
| Wykresy i raporty | Brakujące pomiary pozostają przerwami NaN, zamiast zer lub starych wartości; brak zakresu w PDF jest jawnie opisany. |
| Kolejki i timeout | Deadline obejmuje oczekiwanie w kolejce; nieuruchomione przeterminowane polecenia są anulowane. Nie ma ślepego ponawiania mutacji. Timeout nie potrafi przerwać niekooperującego backendu VISA. |

## Cztery problemy odtworzone i naprawione podczas tego przeglądu

1. **Brak potwierdzenia OFF przed częściową konfiguracją.** Nowa ścieżka
   zapisywała parametry od razu po poleceniu OFF. Symulator ignorujący OFF
   potwierdził zapis NPLC przy nadal włączonym wyjściu. Dodano odczyt OUTPUT
   przed pierwszym zapisem parametru; brak potwierdzenia przerywa konfigurację.
   Sprawdzono A/B w trybach current/voltage, również poprawną kolejność operacji.
2. **Dry run uznawał VERIFIED za dowód OFF.** Wspólny guard wymaga teraz
   dokładnie `True`, albo legacy `None` wyłącznie wraz ze stanem OUTPUT_OFF.
   `False`, liczba `1` i VERIFIED bez dowodu są odrzucane. Jednocześnie `True`
   wraz z COMPLIANCE jest poprawnym potwierdzeniem OFF.
3. **Kompilator gubił niezmieniane NPLC i settling time.** Częściowa konfiguracja
   zastępowała poprzedni baseline domyślnymi wartościami żądania, mimo że sprzęt
   zachowywał wcześniejsze ustawienia. Kolejny blok continue kończył się błędem
   zgodności. Kompilator scala teraz tylko wybrane pola, zachowując maskę zapisów.
   Pełny przebieg compiler → runner → symulator → H5 potwierdza zachowanie
   NPLC 4, settling 250 ms i ręcznych zakresów dla A/B.
4. **Błędy aktualizacji compliance nie wymuszały OFF.** To problem wcześniejszego
   kodu, nie nowo dodanej gałęzi diffu. Zmieniana wartość nie sprawdzała rozjazdu
   OUTPUT względem cache; błędny readback limitu mógł pozostawić wyjście ON.
   Kontrola OUTPUT jest teraz wspólna dla obu gałęzi, a niezgodny readback
   uruchamia emergency OFF. Osiem przypadków A/B × current/voltage × rodzaj
   awarii odtworzono przed poprawką i zweryfikowano po niej.

Regresje: `test_source_review_keithley_off_before_patch.py`,
`test_source_review_dry_run_off_proof.py`,
`test_source_review_keithley_partial_baseline.py`,
`test_source_review_keithley_compliance_faults.py` oraz rozszerzony
`test_source_review_shutdown_confirmation.py`.

## Ważne: przejścia między punktami

`update_source_level` zapisuje kolejny poziom bez zerowania i bez cyklu OUTPUT
OFF/ON w normalnej kontynuacji. Jest to bezpośrednia zmiana setpointu, **nie
programowa rampa**; metoda nie została zmieniona przez ten diff. Przy fixed range
nie przepisuje pozostałych ustawień. Autorange może zmieniać zakres sprzętu
zgodnie z jawną polityką Settings.

Pełne i częściowe `configure_source` wymuszają OUTPUT OFF. Tryb continue
dopuszcza aktualizację poziomu/compliance/settling, ale odrzuca pełną
rekonfigurację. Jawne akcje OUTPUT, inna polityka bloków i obsługa awarii mogą
wyłączać wyjście. Nie należy obiecywać płynnej rampy dla dowolnej receptury.

## Weryfikacja i ograniczenia

Po poprawkach: **114 passed** dla sterowania adapterem, **73 passed + 6 subtests**
dla kompilatora, **82 passed** dla dry run/Stop/shutdown oraz **9 passed** dla
asynchronicznych artefaktów i PDF. Są to zestawy częściowo pokrywające się,
więc wyników nie sumujemy. Ruff dla zmienionego sterowania i nowych regresji
zaliczony.

Szerszy przebieg wcześniejszego stanu diffu: **445 passed, 11 failed**.
Nie został ponownie wykonany po czterech poprawkach. Nie jest zielony:

- Jeden potwierdzony problem geometrii: przy 1280×720 strona charakterystyki
  wymaga 48 px przewijania, mimo oczekiwania testu pełnej widoczności.
- Pięć testów draftów/panelu field zakłada dawne edytowalne kroki rampy lub
  wcześniejszy limit B, nie aktualną automatyczną politykę Settings. Nie
  rozszerzano limitów aparatury w celu zaliczenia testów.
- Test nazw raportów wywołuje usuniętą synchroniczną metodę; jeden test
  kolejności zakłada natychmiastowy zapis CSV mimo nowego workerowego zapisu.
- Dwa testy shutdown próbują pisać do Documents/PyLab poza izolacją testów;
  kolejny używa atrapy shell bez wymaganego `moke_box_page`.

Dodatkowy zestaw miał **120 passed, 1 failed + 6 subtests**: fixture recovery
tworzy events bez wymaganych timestamp/severity. Nie rozluźniono walidatora
archiwum. Końcowy zestaw kompilatora powyżej jest zielony.

Wnioski: naprawiono odtworzone błędy sterowania i dziedziczenia konfiguracji.
Całego bieżącego diffu nie uznajemy jeszcze za w pełni zakwalifikowany
produkcyjnie. Pozostają geometria, aktualizacja/izolacja części testów oraz
kwalifikacja na fizycznej aparaturze. Rejestracja inventory nadal może wykonywać
I/O w GUI; ten przegląd nie dowodzi całkowitego usunięcia blokowania interfejsu.
