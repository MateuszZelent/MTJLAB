# Anritsu — drugi przegląd logiki komunikacji

## Wniosek

Nie było podstaw do deklaracji „zero zbędnych operacji”. Ponowny przegląd
ujawnił dalsze powtórzenia w odczytach metadanych, transferze ASCII,
akwizycji tła/referencji w UI oraz pollingu Live podczas Execution.
Poprawiono te ścieżki. Zgodności konkretnego firmware i braku chwilowego
komunikatu na jego ekranie nie potwierdzono fizycznym pomiarem.

## Prześledzone ścieżki

- `adapter.py`: connect/disconnect, odczyty ustawień, configure/patch,
  Live, Single, oczekiwanie, ASCII/binary, Remote oraz stop/emergency.
- `module.py` i `ui/workers.py`: dispatch modułu oraz ścieżka bez dispatchera,
  własność sesji w wątku worker, rezerwacja i wywołania runnera.
- `runner.py`: konfiguracja z planu, referencja/tło, uśrednianie, odczyty
  przed/po bloku, kontrole referencji, retry, zakończenie i błąd.
- `sweep_provider.py`: aktualizacja wybranej osi przez maskę changed_fields.
- `ui/page.py`, `ui/correction_card.py`, `ui/background_assistant.py`:
  timery, pojedynczy pending fetch, kolejne widma dopiero po zapisie,
  zmiany bloków referencji/sygnału, odbiór spóźnionych odpowiedzi.
- `ui/shell/main_window.py`, `ui/run_worker.py`: blokada ręcznych operacji,
  rezerwacja urządzeń, projekcja telemetrii na strony, start/koniec Execution.
- `acquisition_context.py`: zgodność fingerprintów; Single/Continuous jest
  oddzielone od ustawień toru wejściowego i dowodu świeżości widma.

To przegląd ścieżek komunikacji i ich wywołań. Nie jest deklaracją przeczytania
wszystkich niezwiązanych z komunikacją funkcji całej aplikacji.

## Znalezione powtórzenia i poprawki

1. Runner i weryfikacja tła pobierały full + advanced osobno. Dwanaście
   zapytań advanced powtarzało dane już obecne w full. Nowe
   `read_acquisition_configuration()` zwraca oba modele z jednego świeżego
   odczytu. Nie używa cache. Zachowuje semantykę Video/Power versus
   auto/manual/off VBW, normalizację detektora, opcję preamp i kontrolę
   skończonych wartości advanced.
2. Okno zbierania tła/referencji wykonywało dwa osobne requesty ustawień.
   Teraz korzysta z tego samego połączonego odczytu na początku bloku.
3. ASCII było programowane przy każdym widmie. Teraz `FORM?` sprawdza
   reprezentację; zapis `FORM ASC` następuje tylko przy zmianie. Zmiana
   wymaga braku błędów SCPI oraz potwierdzenia readback przed `TRAC?`.
4. UI `single_sweep` przywracało Continuous między kolejnymi widmami
   tła/referencji, choć runner już tego nie robił. Dispatch modułu i worker
   bez dispatchera teraz używają `restore_continuous=False`. Ręczny przycisk
   Acquire once również pozostawia Single. Start Live pozostaje jawną
   czynnością uruchamiającą Continuous. Niskopoziomowe API adaptera nadal
   pozwala jawnie wybrać przywrócenie wcześniej aktywnego Continuous.
5. `set_execution_controlled()` wcześniej zmieniało tylko prezentację.
   Teraz zatrzymuje lokalny timer Live i timer weryfikacji podglądu tła,
   kończy lokalne uśrednianie bez wznowienia Live i blokuje kolejne fetch.
   Spóźniony wynik Start Live nie uruchamia pollingu podczas Execution.
   Nie wysyła z tego powodu ABOR, INIT ani RF OFF. Po zwolnieniu blokady
   Live wymaga jawnego uruchomienia przez operatora.

## Operacje, które celowo pozostają

| Ścieżka | Zapisy nominalne | Odczyty i ich cel |
| --- | --- | --- |
| Connect | brak konfiguracji | IDN oraz opcje sprzętowe |
| Start Live | TRAC1:TYPE WRIT; INIT:MODE:CONT tylko jeśli potrzebny; REN raz na połączenie GPIB | tryb i ustawienia początkowe, Continuous, błędy SCPI |
| Kolejna ramka Live | brak przy zgodnym formacie | TRAC?, FORM/BORD, oś przed/po, błędy SCPI |
| Single w sweepie/UI | TRAC1:TYPE WRIT, INIT:MODE:SING, *WAI | tryb, potwierdzenie zakończenia, trace, oś i błędy SCPI |
| Zmiana reprezentacji transferu | tylko wymagane FORM/BORD | potwierdzenie nowego formatu |
| Odczyt metadanych bloku | brak | jeden full readback; advanced jest wyprowadzony z tych danych |
| Konfiguracja/ROI analizatora | parametry jawnego bloku; ROI ma maskę pojedynczego pola | zgodność niezmienianych pól i readback zmiany |
| Sukces planu odbiornikowego | brak ABOR i RF OFF | zapis/flush danych; stan innych urządzeń według planu |
| Stop/błąd planu odbiornikowego | kontrolowany abort bez komend SG | potwierdzenie stopu lub UNKNOWN/FAULT |
| Jawny E-STOP / shutdown aplikacji | emergency_off może również wyłączyć zainstalowany SG | potwierdzenie OFF/stopu lub UNKNOWN/FAULT |

`TRAC1:TYPE WRIT` przed świeżym sweepem pozostaje celowe: bufor w VIEW
może być stary mimo wykonania nowego pomiaru. Nie zastąpiono tego
niepotwierdzonym cache ani problematycznym `TRAC:TYPE?`.

Oś przed i po transferze wykrywa zmianę zakresu przez panel lub innego
klienta. Konfiguracja przed i po bloku sprawdza zgodność referencji i
metadanych. `SYST:ERR?` wykrywa odrzucenie komendy mimo udanego zapisu
VISA. Retry może ponowić akwizycję po błędzie zgodnie z widocznym zdarzeniem
action_retry; nie jest wykonywany w nominalnym kroku.

Jawne Apply ustawień jest osobną operacją: pełny blok wybiera Spectrum;
zmiana pojedynczej osi zachowuje pozostałe pola. Awaria konfiguracji toru
wejściowego może wymusić preamp OFF i 60 dB tłumienia. To istniejący fallback
ochronny po błędzie, a nie działanie każdego kroku pomiarowego. Edycja limitów
może kontrolować SG, jeżeli urządzenie raportuje taką opcję; nie jest
wykonywana podczas akwizycji. Nie usuwano ochrony E-STOP ani fallbacków.

## Pomiar ruchu na symulatorze

Profil symulatora z preamp: full + advanced wcześniej 30 zapytań;
połączony odczyt teraz 18, bez zapisów. Daje to 24 mniej zapytań
na blok runnera przy kontroli przed i po. Bez opcji preamp oba warianty
mają odpowiednio mniej zapytań. Nie jest to benchmark fizycznego GPIB.

Kolejne Single przy zgodnym ASCII: 15 zapytań i dokładnie trzy zapisy
(`TRAC1:TYPE WRIT`, `INIT:MODE:SING`, `*WAI`). Kolejna ramka Live przy
zgodnym REAL,32/SWAP: 12 zapytań, zero zapisów. Błąd, zmiana formatu,
opcje sprzętowe lub dłuższe oczekiwanie zmieniają te liczby.

## Walidacja i ograniczenia

Regresje obejmują brak powtórzonych odczytów w parze konfiguracji,
równoważność modeli, zmianę ustawień poza aplikacją, transfer ASCII/binary,
ignorowany zapis formatu, błędy SCPI, świeże sweepy, brak restartów
Continuous, Remote, UI tła i interleaved, zapis RAW przed błędem,
kontrolę metadanych w wątku właściciela oraz blokadę pollingu Execution.
Test strony po show/processEvents przy 1500×900 zapisuje obraz
`artifacts/anritsu-acquisition-traffic/execution-polling-paused.png`.

Wyniki końcowe:

- 95 testów akwizycji, wątku właściciela, UI korekcji/interleaved/tła,
  transferu, współdzielonego tła i regresji podglądu: zaliczone (156,01 s).
- 34 testy przetwarzania sweepa, RAW/HDF5, zmiany konfiguracji i rezerwacji
  urządzenia: zaliczone (24,42 s).
- Szerszy zestaw adapter/runner/fast/RAW/axis/RF: 152 zaliczone;
  jedno stare oczekiwanie zapisu FORM ASC mimo potwierdzonego ASC zmieniono
  na brak zapisu i ponownie uruchomiono tę regresję: zaliczona.
- `ruff check app tests`: bez błędów.

Przy aktualizacji testu interleaved wstrzykiwanie zmiany ustawień przeniesiono
na parę full/advanced. Test jawnego Stop oczekuje „Recording stopped”;
status archiwum nadal jest aborted. Nie zmieniono produkcyjnej semantyki
Stop, aby uzyskać napis „finished”.

Nie wyłączano kontrolek bezpieczeństwa ani kontroli osi w celu redukcji
liczby zapytań. Nie komunikowano się z fizycznymi urządzeniami.
Do potwierdzenia akceptacji komend konkretnego firmware potrzebny jest
rzeczywisty pomiar z logiem `SYST:ERR?`.
