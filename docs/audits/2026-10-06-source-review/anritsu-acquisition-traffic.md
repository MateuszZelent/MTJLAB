# Anritsu: ruch VISA podczas akwizycji

Dalszy przegląd i poprawki duplikatów odczytów, ASCII, UI referencji oraz
pollingu Execution opisano w
[anritsu-communication-second-pass.md](anritsu-communication-second-pass.md).
UI Acquire once teraz również pozostawia Single; przywrócenie Continuous
jest nadal dostępne jako jawna opcja niskopoziomowego API adaptera.

## Zakres i dowody

Przeczytano dostarczony log VISA oraz ścieżki Live, pojedynczego sweepa,
transferu ASCII/binarnego, transportu GPIB i akwizycji w runnerze.
Nie wykonywano pomiarów ani komend na fizycznym stanowisku.

Log użytkownika przedstawia Live: po przygotowaniu `TRAC1:TYPE WRIT`
analizator pozostaje w Continuous (`INIT:CONT?` = 1). Każda ramka wykonuje
dwa zapisy: `FORM REAL,32` i `FORM:BORD SWAP`, a następnie `TRAC? TRAC1`.
Pozostałe widoczne operacje odczytują konfigurację lub sprawdzają oś.
Nie ma w tym logu ponownej pełnej konfiguracji pasma, RBW, VBW, detektora,
tłumienia ani generatora RF przy każdym widmie.

Udany VISA write potwierdza transport, a nie akceptację komendy przez
firmware. Log nie zawiera odczytu kolejki SCPI, więc nie wskazuje przyczyny
krótkiego komunikatu na ekranie. Nie uznajemy jej za rozpoznaną.

## Wprowadzone zmiany

- Live odczytuje `FORM?` i `FORM:BORD?`; ustawia format tylko wtedy,
  gdy jest inny. Po zmianie sprawdza readback przed transferem.
  Zmiana formatu przez innego klienta lub wcześniejszy odczyt ASCII
  powoduje ponowne przygotowanie. Kontrole osi przed i po transferze zostają.
- `SYST:ERR?` przed i po etapach przygotowania/akwizycji/transferu zapisuje
  kod i tekst rzeczywistego błędu w logu i zgłoszeniu awarii. Odczyt jest
  ograniczony do 16 odpowiedzi; nie używamy `*CLS`. Błąd lub nieprawidłowa
  odpowiedź zatrzymuje publikację widma.
- Przy rozpoczęciu pomiarów przez GPIB jawnie ustawiamy REN
  `asrt_address` raz na połączenie. Discovery nie przełącza Remote.
  Nie włączamy Local Lockout ani nie wyłączamy LCD.
- Runner wykonuje pojedyncze, potwierdzone sweepy bez ponownego uruchamiania
  Continuous po każdej akwizycji (`restore_continuous=False`). Między punktami
  pozostaje Single. Ręczna akwizycja zachowuje domyślne przywracanie
  wcześniej aktywnego Continuous. Start Live może ponownie uruchomić Continuous.
- RF generator pozostaje poza ścieżką pomiaru widma. Normalne ukończenie
  planu odbiornikowego nie wysyła abortu ani RF OFF.

Remote, Single/Continuous i aktualizacja LCD to osobne mechanizmy.
Continuous podczas Live celowo daje kolejne widma; samo Remote nie jest
obietnicą zamrożenia ekranu. Nie dodano komendy wygaszającej ekran, aby
ukryć nierozpoznany błąd.

## Konkretny plan użytkownika

Skompilowany `recipes/anritsu_background_reference_smoke_test.yml` ma
41 akcji i 10 punktów pomiarowych. Zawiera dwie konfiguracje MOKE,
dwa uzbrojenia MOKE, 11 aktualizacji napięcia (punkt początkowy i 10 ROI),
13 oczekiwań, dwie akwizycje referencyjne oraz 10 akwizycji widma.
Jawny stan końcowy to jeden `stop_moke_voltage`; manifest automatyczny
obejmuje MOKE i flush danych. Plan nie zawiera konfiguracji Anritsu
ani Keithley. Konfiguracja analizatora jest odczytywana przed i po
akwizycji dla metadanych oraz kontroli zgodności referencji; odczyty
te nie programują parametrów urządzenia.

W aktualizacjach ROI Keithley i Rigola rozróżniamy odczyty kontrolne
od zapisów żądanego parametru. Nie ma nominalnego wyłączania outputu
między punktami aktualizacji. Rampy MOKE mają osobną walidację i readback.
Ścieżki awarii nadal mogą wyłączyć używane wyjścia zgodnie z polityką.

## Dokumentacja producenta

[Spectrum Analyzer Remote Control, ed. 57](https://dl.cdn-anritsu.com/en-au/test-measurement/files/Manuals/Operation-Manual/MS269xA/MS269xA_2830A_40A_50A_SpectrumAnalyzer_Remote_Manual_e_57_0.pdf)
opisuje `TRACe[n]:TYPE WRITe` (2-190), transfer REAL,32 i SWAP
(2-220–223), Single i odczyt statusu sweepa. Trace type ma ograniczenia
w aplikacjach SEM/Spurious, dlatego sam `INST? = SPECT` nie dowodzi
akceptacji tej komendy we wszystkich kontekstach firmware.

[Mainframe Remote Control, ed. 42](https://dl.cdn-anritsu.com/en-au/test-measurement/files/Manuals/Operation-Manual/MS2830A/MS269xA_2830A_40A_50A_Mainframe_Remote_Manual_e_42_0.pdf)
opisuje `SYST:ERR?` (4-44). Zero oznacza brak błędu, a odpowiedzi
niezerowe zawierają kod i opis.

## Weryfikacja

Nowe regresje sprawdzają powtarzane transfery, zmianę byte order/ASCII,
błędy SCPI mimo udanego zapisu VISA, błąd zastany przed pomiarem,
niepoprawną i nieopróżniającą się kolejkę błędów, ignorowany zapis formatu,
unikalne identyfikatory świeżych sweepów, brak restartów Continuous
oraz Remote na GPIB bez Local Lockout i bez zmian podczas discovery.

W szerszej wcześniejszej kontroli trzy testy recovery/memory-budget
nie przeszły: ich plik testowy nie zawiera zgodnego `plan_sha256`, więc
recovery odrzuca plik przed kontrolą pamięci. Nie osłabiano tej walidacji.
Końcowy zestaw adapter/runner/raw-sweep/axis: **156 testów zaliczonych**
(95,78 s), w tym 16 nowych regresji ruchu akwizycji. Ruff dla zmienionych
plików Python: bez błędów.
Dodatkowo 42 testy szybkiej akwizycji i symulatorów zaliczone (22,29 s).
Łącznie końcowe zestawy: **198 zaliczonych testów**.

Do rozstrzygnięcia błędu fizycznego firmware potrzebny jest log nowej
wersji zawierający odpowiedzi `SYST:ERR?`. Testy symulacyjne nie dowodzą
zgodności konkretnego firmware ze wszystkimi komendami.
