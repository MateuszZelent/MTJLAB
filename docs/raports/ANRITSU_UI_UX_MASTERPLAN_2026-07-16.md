# Anritsu MS2830A - raport rozbudowy UI/UX do wersji produkcyjnej

**Data:** 2026-07-16  
**Zakres:** ręczna obsługa Spectrum/Live, pojedyncze i uśrednione widma, referencje, przetwarzanie, wykres, bezpieczeństwo, zapis danych i diagnostyka  
**Analizowane źródła:** aktualny kod aplikacji, testy automatyczne oraz `MS2830A_40A_SpectrumAnalyzer_Remote_Manual_e_43_0.pdf`  
**Poza zakresem:** modyfikacje kodu w `submodules/`

## 1. Podsumowanie wykonawcze

Zakładka Anritsu ma już działający fundament: odczyt konfiguracji, pojedyncze widmo, pasywny Live, uśrednianie w mocy liniowej, referencję uśrednioną, kilka operacji matematycznych, nakładanie śladów oraz eksport wykresu. Nie jest jednak jeszcze spójnym narzędziem produkcyjnym.

Najważniejsze problemy:

1. brakuje jawnego przycisku pobrania pojedynczej referencji;
2. akcje akwizycyjne, konfiguracja i obróbka danych są wymieszane;
3. operator nie widzi pełnego stanu ramki, referencji i efektywnej szybkości Live;
4. referencja istnieje wyłącznie w pamięci i może zostać nadpisana bez śladu;
5. podczas Live nie wszystkie konflikty operacji są blokowane przez jeden model stanu;
6. zaawansowane możliwości analizatora wymagają kwalifikacji dokładnego firmware przed odblokowaniem zapisu;
7. interfejs nie rozróżnia wystarczająco jasno ustawień urządzenia od operacji wykonywanych lokalnie na danych.

Najpierw należy ukończyć spójny przepływ akwizycyjny i referencyjny. Dopiero potem warto dodawać kolejne komendy SCPI.

## 2. Stan obecny - mocne strony

- `Read from instrument` odczytuje Start, Stop, Reference Level i Points bez zmiany urządzenia.
- `Read current spectrum` używa pasywnego odczytu aktualnego `TRAC1`.
- Live używa tej samej bezpiecznej ścieżki co pojedynczy odczyt i nie zmienia trybu Trace/Sweep.
- `_fetch_pending` zapobiega kolejkowaniu równoległych ramek Live.
- wskaźnik `LIVE OFF/STARTING/ON/PAUSED/STOPPING` pokazuje potwierdzony stan.
- kolejne identyczne ramki są wykrywane i raportowane.
- uśrednianie jest wykonywane w liniowej mocy mW, a nie bezpośrednio w dBm.
- operacje referencyjne kontrolują zgodność siatki częstotliwości.
- log transportu nie wypisuje całego payloadu widma.
- opcje sprzętowe są odczytywane best-effort przez `*OPT?`.
- wykres zachowuje piki podczas downsamplingu.

## 3. Brakująca pojedyncza referencja - P0

### 3.1. Co istnieje w kodzie

Metoda `capture_current_reference()` już istnieje, ale nie jest podłączona do przycisku. Kopiuje ona `_latest_trace` do `_reference_trace`. Oznacza to „użyj ostatniego widma”, a nie „pobierz teraz pojedynczą referencję”.

### 3.2. Wymagane dwie osobne akcje

#### A. Acquire single reference

Przycisk pobiera jedną nową ramkę z urządzenia i dopiero po poprawnym zakończeniu zapisuje ją jako referencję.

Przebieg:

1. zablokuj konfliktujące akcje;
2. pokaż stan `ACQUIRING REFERENCE...`;
3. wykonaj `fetch_current_trace("TRAC1")`;
4. sprawdź liczbę punktów, wartości skończone i poprawną oś;
5. zapisz ramkę jako `_reference_trace`;
6. włącz overlay `Reference`;
7. zapisz metadane referencji;
8. pokaż czas, liczbę punktów i zakres częstotliwości;
9. przywróć kontrolki również po błędzie.

Ta akcja nie powinna wymagać zatwierdzonego profilu konfiguracji, ponieważ jest pasywnym odczytem aktualnego śladu.

#### B. Use current trace as reference

Akcja lokalna, bez komunikacji VISA. Powinna być dostępna tylko wtedy, gdy istnieje `_latest_trace`. Operator musi widzieć, że wykorzystuje już pobraną ramkę i jej timestamp.

### 3.3. Referencja uśredniona

Obecny `Acquire averaged reference` należy zachować, ale wyraźnie odróżnić go od pojedynczej referencji. Proponowane nazwy:

- `Acquire 1x reference`;
- `Acquire Nx averaged reference`;
- `Use current trace`;
- `Clear reference`.

### 3.4. Ochrona przed nadpisaniem

Jeśli referencja już istnieje, aplikacja powinna pokazać kompaktowe potwierdzenie zawierające:

- typ bieżącej referencji: single/average/imported;
- czas pobrania;
- liczbę uśrednień;
- zakres i liczbę punktów;
- informację, czy referencja została zapisana do pliku.

Nie należy wyświetlać potwierdzenia, jeśli poprzednia referencja została jawnie usunięta.

## 4. Docelowa architektura informacji

Obecny lewy panel powinien zostać podzielony na trzy logiczne sekcje, a nie jedną listę kontrolek.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│ Anritsu MS2830A   VERIFIED   ● LIVE ON · 8.3 Hz · FRAME 124   [i]          │
├───────────────────────────────┬─────────────────────────────────────────────┤
│ ACQUISITION                   │                                             │
│ Start        [ 1 MHz       ]  │                                             │
│ Stop         [ 10 MHz      ]  │                 SPECTRUM                    │
│ Ref level    [ 0 dBm       ]  │                                             │
│ Points       [ 1001      ▼ ]  │        Raw / Average / Reference / Result   │
│ [Read device] [Apply]         │                                             │
│                               │                                             │
│ [Read once] [Start Live]      │                                             │
│                               │                                             │
│ REFERENCE                     │                                             │
│ ● none / single / avg / file  │                                             │
│ [Acquire 1x] [Acquire Nx]     │                                             │
│ [Use current] [Load] [Save]   │                                             │
│ Operation [Signal - Ref ▼]    │                                             │
│                               │                                             │
│ ADVANCED                      │                                             │
│ [RBW/VBW/Detector/Trigger...] │                                             │
└───────────────────────────────┴─────────────────────────────────────────────┘
```

## 5. Nagłówek urządzenia

Nagłówek powinien być stałym centrum informacji, bez konieczności czytania Event Log.

Zalecane elementy:

- stan połączenia: `OFFLINE`, `VERIFIED`, `FAULT`;
- stan Live;
- numer ostatniej ramki;
- efektywna częstość ramek, np. `8.3 Hz`;
- czas transferu ostatniej ramki, np. `42 ms`;
- liczba punktów;
- ostrzeżenie `STALE`, jeśli ślad nie zmienił się przez określony czas;
- przycisk informacji o sprzęcie i opcjach;
- mały przycisk `Reconnect` dostępny tylko po błędzie transportu.

Kolor zawsze musi być uzupełniony tekstem i ikoną. Nie wolno sygnalizować stanu wyłącznie kolorem.

## 6. Pasek głównych akcji

Akcje powinny mieć stałą hierarchię:

### Pierwszorzędne

- `Read once`;
- `Start Live` / `Stop Live`.

### Drugorzędne

- `Read settings from instrument`;
- `Apply configuration`.

### Awaryjne

- `Abort acquisition`.

`Abort` nie powinien wyglądać jak zwykły żółty przycisk. Należy jasno opisać skutek: zatrzymuje akwizycję, ale nie odłącza urządzenia i nie dotyczy zewnętrznych źródeł RF.

Przycisk Live powinien być przełącznikiem jednego stanu, a nie osobnymi akcjami. W czasie przejścia musi pokazywać `Starting...` albo `Stopping...`.

## 7. Referencja jako osobny obiekt domenowy

Referencja nie powinna być tylko zmienną `SpectrumTrace`. Docelowy model powinien zawierać:

```text
ReferenceSpectrum
├── trace
├── kind: single | averaged | imported
├── average_count
├── acquired_at_utc
├── source_device_idn
├── firmware
├── hardware_options
├── start_hz / stop_hz / points
├── reference_level_dbm
├── rbw_hz / vbw_hz / detector / attenuation
├── source_file
├── notes
└── grid_hash
```

Korzyści:

- pewne odtworzenie obróbki;
- jednoznaczny zapis HDF5;
- wykrywanie niezgodnej konfiguracji;
- możliwość biblioteki referencji;
- czytelny status w UI.

## 8. Karta Reference & Processing

Obecna karta łączy uśrednianie sygnału, pozyskiwanie referencji, wybór operacji i widoczność śladów. Powinna zostać rozdzielona wizualnie.

### Sekcja Signal acquisition

- `Average count`;
- `Acquire averaged signal`;
- progres `17 / 200`;
- ETA wyliczane z rzeczywistego czasu ramek;
- `Cancel`.

### Sekcja Reference

- status referencji;
- `Acquire 1x`;
- `Acquire Nx`;
- `Use current`;
- `Load from HDF5`;
- `Save reference`;
- `Clear`.

### Sekcja Processing

Na pierwszym poziomie wystarczą najczęstsze operacje:

- `No correction`;
- `Signal - Reference [dB]`;
- `Signal / Reference [linear]`.

Rzadziej używane `add`, `subtract in linear power` i `multiply` powinny trafić do `Advanced math`, ponieważ bez wyjaśnienia jednostek są łatwe do błędnej interpretacji.

## 9. Wykres

### 9.1. Czytelność

- stan pusty powinien mówić `No spectrum acquired`, zamiast pokazywać arbitralne osie 0-1;
- tytuł powinien opisywać aktywny ślad, np. `Live Raw`, `Averaged 200x`, `Signal - Reference`;
- legenda powinna pokazywać również typ i wiek referencji;
- kolory powinny być spójne: Raw - niebieski, Average - zielony, Reference - bursztynowy, Processed - fioletowy;
- przy zmianie jednostki Y nie należy równocześnie pokazywać śladów o niezgodnych jednostkach.

### 9.2. Toolbar

Zalecane narzędzia podstawowe:

- `Auto scale`;
- `Peak search`;
- `Marker 1`;
- `Delta marker`;
- `Freeze view`;
- `Export`.

`Max Hold` i `Min Hold` powinny mieć stan wciśnięty oraz jawny przycisk wyczyszczenia. Operator musi wiedzieć, czy hold jest wykonywany przez aplikację, czy przez analizator.

### 9.3. Dodatkowe odczyty

- peak frequency i amplitude;
- marker X/Y;
- delta frequency/amplitude;
- noise floor median/percentile;
- span;
- efektywna rozdzielczość częstotliwości;
- liczba punktów;
- timestamp ramki.

## 10. Live - wymagania produkcyjne

Minimalny timer 10 ms jest wartością żądaną, a nie gwarantowaną częstością ramki. UI powinien pokazywać:

- requested interval;
- effective interval;
- frame rate;
- VISA transfer duration;
- stale frame count;
- dropped/coalesced timer ticks.

Kolejne wywołanie nie może zostać zakolejkowane przed zakończeniem poprzedniego. Obecny `_fetch_pending` jest dobrym fundamentem.

Po błędzie transportu Live powinien:

1. zatrzymać timer;
2. ustawić `LIVE OFF - ERROR`;
3. zachować ostatnią poprawną ramkę na wykresie;
4. pokazać nieblokujący banner z komendą, czasem i kodem VISA;
5. zaoferować `Retry` oraz `Reconnect`;
6. nie otwierać wielu modalnych okien przy powtarzającym się błędzie.

## 11. Model stanów i blokowanie kontrolek

Należy wprowadzić jeden jawny model stanu strony:

| Stan | Dozwolone akcje | Zablokowane akcje |
|---|---|---|
| Disconnected | Connect, inspect saved data | Read, Live, Apply |
| Idle | Read once, Live, Apply, references | - |
| Starting Live | Stop/Cancel transition | wszystkie pozostałe |
| Live | Stop Live, markers, display toggles | Apply config, Read once |
| Averaging signal | Cancel, display | Live, config, reference acquisition |
| Averaging reference | Cancel, display | Live, config, other reference actions |
| Acquiring single reference | Cancel if supported | Live, config, other acquisition |
| Stopping | brak ponownej akcji | wszystkie akcje VISA |
| Error | Retry, Reconnect, Export last frame | config until reconnection if transport lost |

Stan musi sterować przyciskami centralnie. Rozproszone `setEnabled()` utrudnia dowodzenie poprawności i prowadzi do niespójności.

## 12. Ustawienia urządzenia do dalszej kwalifikacji

Instrukcja MS2830A opisuje następujące funkcje, które warto dodać po wykonaniu testów Hardware-in-the-Loop:

### P1 - najważniejsze dla typowych widm

- RBW manual/auto: `BAND <freq>`, `BAND?`, `BAND:AUTO`;
- VBW manual/auto/off;
- detector: Normal, Positive, Sample, Negative, RMS, Quasi-Peak oraz warianty zależne od trybu;
- RF attenuation manual/auto: `POW:ATT`, `POW:ATT:AUTO`;
- preamplifier on/off: `POW:GAIN`, wyłącznie gdy wykryta opcja sprzętowa to obsługuje;
- sweep time auto/manual;
- center/span jako alternatywa dla start/stop.

### P2 - zaawansowane

- trigger enable/source/level/slope/delay/holdoff;
- wybór Trace A-F;
- trace Write/View/Blank, ale tylko w trybach, w których dokumentacja dopuszcza komendę;
- device-side Max Hold, Min Hold i Average;
- marker table i wiele markerów;
- Zero Span;
- konfiguracja wejścia 50/75 ohm, jeśli wariant sprzętowy ją obsługuje.

Każde pole musi mieć:

- query przed pierwszym wyświetleniem;
- listę wartości wynikającą z modelu/firmware/opcji;
- write + readback verification;
- rollback albo jednoznaczny błąd;
- tooltip z konsekwencją fizyczną;
- zapis do metadanych.

## 13. Bezpieczeństwo i fizyka pomiaru

- Reference Level nie jest limitem mocy wejściowej RF.
- RF input maximum musi pozostać osobnym limitem bezpieczeństwa.
- Attenuation i preamplifier wpływają na ryzyko przesterowania oraz noise floor.
- UI powinien pokazywać ostrzeżenie, jeśli preamplifier jest włączony przy wysokim oczekiwanym poziomie wejściowym.
- Ręczna zmiana RBW/VBW/detector może znacząco zmienić czas sweepu i porównywalność referencji.
- Operacja referencyjna musi zostać zablokowana, jeśli różnią się: siatka częstotliwości, liczba punktów albo krytyczne parametry akwizycyjne.
- Przy różnym RBW/VBW/detector nie wystarczy zgodność samych częstotliwości.

## 14. Zapis HDF5 i zgodność z PyThat/Thatec

Każda zapisana ramka i referencja powinna zawierać:

- surowe frequencies/powers bez redukcji wykresowej;
- typ: raw, averaged, reference, processed;
- IDN, serial, firmware i `*OPT?`;
- konfigurację instrumentu;
- parametry aplikacyjnego uśredniania;
- operację matematyczną i uczciwą jednostkę wyniku;
- timestamp UTC i czas monotoniczny;
- requested/effective Live interval;
- hash siatki częstotliwości;
- identyfikator referencji;
- snapshot profilu bezpieczeństwa;
- wersję aplikacji i schematu.

Referencja powinna być zapisywana jako dane pierwszej klasy, a nie tylko jako opis w JSON. Należy przewidzieć odtworzenie przetwarzania po ponownym otwarciu pliku.

## 15. Diagnostyka

### Event Log

Log powinien zawierać:

- rozpoczęcie/zatrzymanie Live;
- numer ramki i czas transferu, ale bez danych widma;
- rozpoczęcie/postęp/zakończenie uśredniania;
- utworzenie, nadpisanie, wczytanie i usunięcie referencji;
- zmianę operacji matematycznej;
- błędy VISA z komendą i czasem;
- odczytane opcje sprzętowe.

### Panel diagnostyczny

W popupie `i` warto dodać zakładkę Diagnostics:

- VISA resource/backend;
- IDN/OPT/firmware;
- ostatnia poprawna komenda;
- ostatni błąd;
- średni/p95 transfer time;
- effective FPS;
- liczba stale frames;
- liczba reconnectów;
- możliwość skopiowania raportu diagnostycznego.

## 16. Responsywność i wygląd

- lewy panel powinien mieć minimalną i maksymalną szerokość, ale bez poziomego scrollbara;
- zaawansowane ustawienia powinny być w popupie/drawerze, nie stale na ekranie;
- wszystkie przyciski w jednym wierszu muszą mieć tę samą wysokość;
- ikony powinny pochodzić z jednego nowoczesnego zestawu SVG;
- teksty przycisków powinny używać czasowników i obiektów;
- nie należy używać wielkich bloków objaśnień; szczegóły powinny być w tooltipie i `i`;
- pusty wykres powinien mieć centralny empty state z główną akcją `Read spectrum`;
- Event Log powinien pozostać regulowany splitterem i mieć tryb auto-hide;
- układ powinien być testowany co najmniej dla 1366x768, 1920x1080 i skalowania 125/150%.

## 17. Proponowany backlog

### Etap 1 - P0, zamknięcie przepływu podstawowego

1. `Acquire single reference`.
2. `Use current as reference`.
3. metadane i status referencji.
4. ochrona przed nadpisaniem.
5. centralny state machine kontrolek.
6. nieblokujące błędy Live.
7. effective FPS/transfer time/stale indicator.
8. testy jednostkowe i HIL przepływu Live/reference.

### Etap 2 - P1, produkcyjna jakość danych

1. zapis/load referencji HDF5 zgodny z PyThat/Thatec;
2. walidacja pełnej konfiguracji przed reference math;
3. RBW/VBW/detector/attenuation/preamp z readback;
4. center/span;
5. ETA uśredniania;
6. diagnostyka i retry/reconnect;
7. empty states i uporządkowany toolbar wykresu.

### Etap 3 - P2, funkcje eksperckie

1. trigger i Zero Span;
2. Trace A-F;
3. device-side hold/average;
4. biblioteka referencji;
5. marker table i automatyczne pomiary widmowe;
6. profile presetów pomiarowych;
7. porównanie wielu zapisanych widm.

## 18. Kryteria akceptacji wersji produkcyjnej

- Live pracuje minimum 8 godzin bez wzrostu kolejki operacji i pamięci.
- Start/Stop można szybko klikać bez zdublowanych komend.
- błąd VISA nie generuje lawiny modalnych okien.
- pojedyncza referencja zawsze pochodzi z jednej jawnie pobranej ramki.
- referencja uśredniona zawiera dokładnie N różnych zakończonych ramek.
- UI nie pozwala uruchomić dwóch konfliktujących akwizycji.
- referencja i sygnał o niezgodnej konfiguracji nie są przetwarzane.
- każda operacja ma pełne metadane w HDF5.
- zapisany plik można ponownie otworzyć i odtworzyć Raw/Reference/Processed.
- przy 10001 punktach wykres pozostaje responsywny, a dane zapisane nie tracą pików.
- wszystkie stany są czytelne również bez rozróżniania kolorów.
- testy HIL obejmują GPIB i TCPIP, jeżeli oba transporty są wspierane na stanowisku.

## 19. Rekomendowana kolejna implementacja

Najlepszym następnym krokiem jest mały, zamknięty pakiet zmian:

1. dodać `Acquire 1x reference`;
2. podłączyć istniejące `Use current trace`;
3. dodać status referencji z timestampem i typem;
4. rozdzielić sekcje Signal/Reference/Processing;
5. dodać testy stanów, nadpisania i błędów;
6. następnie rozszerzyć writer HDF5 o referencję.

Ten etap daje natychmiastową wartość operatorowi bez ryzyka wynikającego z dodawania wielu niezweryfikowanych komend SCPI naraz.

## 20. Źródła dokumentacyjne

- `docs/MS2830A_40A_SpectrumAnalyzer_Remote_Manual_e_43_0.pdf`;
- rozdział 2.2 Level: Reference Level, RF Attenuation i Preamplifier;
- rozdział 2.3 Bandwidth: RBW/VBW i tryby Auto/Manual;
- rozdział 2.6 Trace: Trace A-F, Write/View/Blank, Storage Mode i Average Count;
- rozdział 2.7 Sweep/Trigger/Gate: sweep time i trigger;
- aktualne moduły `app/devices/anritsu/adapter.py`, `app/ui/main_window.py`, `app/spectrum/processing.py` oraz `app/storage/`.

## 21. Stan realizacji — aktualizacja 2026-07-16

Zrealizowano programowo pakiet P0 i część etapu P1:

- dodano osobne akcje `Acquire 1× reference`, `Acquire N× reference` i `Use current trace`;
- świeża pojedyncza referencja powstaje dopiero po poprawnym zakończeniu pasywnego odczytu
  `TRAC1`; lokalna akcja nie wysyła żadnej komendy VISA;
- `ReferenceSpectrum` przechowuje typ, liczbę ramek, czas, IDN, firmware, opcje sprzętowe,
  Reference Level, hash siatki, źródło pliku i stan zapisu;
- status referencji jest widoczny bez otwierania Event Log, a jej zastąpienie wymaga jawnego
  potwierdzenia zawierającego parametry istniejącego obiektu;
- centralny `AnritsuPageState` steruje konfiguracją, Live, pojedynczym odczytem, averagingiem,
  referencjami i anulowaniem, eliminując równoległe konfliktujące akwizycje;
- błędy odczytu zatrzymują Live, zachowują ostatnią poprawną ramkę i są prezentowane jako
  trwały nieblokujący banner zamiast powtarzalnego modalnego okna;
- nagłówek Live pokazuje numer ramki, efektywne FPS i czas VISA, a opis ramki rozróżnia czas
  żądany/efektywny oraz liczbę sklejonych wywołań timera;
- referencję można zapisać i wczytać jako niezmienny artefakt HDF5; publiczna część pliku
  przechodzi rzeczywisty odczyt PyThat, a prywatne metadane zachowują pełną proweniencję;
- matematyka referencyjna wymaga zgodnej osi częstotliwości, Reference Level oraz — gdy
  konfiguracja jest znana — zgodnych RBW/VBW, detektora, tłumienia, przedwzmacniacza i czasu
  przemiatania; pełna proweniencja przechodzi round-trip HDF5/PyThat;
- formularz częstotliwości przełącza się między `Start/Stop` i `Center/Span`; jest to lokalna,
  odwracalna reprezentacja, a adapter zawsze otrzymuje te same zwalidowane fizyczne granice
  `start_hz/stop_hz`;
- wykryta opcja generatora sygnałowego otrzymuje osobną kartę, konfigurację z wymuszonym RF OFF,
  readback, jednorazowy ARM oraz ręczne i recepturowe RF ON/OFF. Funkcja pozostaje domyślnie
  zablokowana przez `control_protocol: unverified` do czasu kwalifikacji HIL konkretnej opcji,
  firmware, transportu i toru RF.

Zaimplementowano odczyt oraz kontrolowany zapis RBW/VBW, detectora, attenuation/preamp i sweep
time: adapter ma readback, walidację opcji sprzętowych, konserwatywny fallback, GUI Advanced,
symulator i akcję receptury. Domyślnie zapis pozostaje zablokowany przez
`control_protocol: unverified`; odblokowanie wymaga kwalifikacji HIL dokładnego firmware i wpisania
go do `qualified_firmware`. Poza zakresem tej iteracji pozostają trigger i device-side
hold/average.
