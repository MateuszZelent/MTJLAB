# Masterplan aplikacji do sterowania stanowiskiem pomiarowym

## 1. Decyzja architektoniczna

Pierwszą wersję produkcyjną należy wykonać jako aplikację desktopową w **Pythonie i PySide6 (Qt 6)**. Logika urządzeń, bezpieczeństwa, receptur i zapisu danych musi pozostać całkowicie niezależna od GUI. Dzięki temu w przyszłości można dodać interfejs webowy bez przepisywania sterowników.

Qt jest właściwszy od aplikacji webowej dla tego stanowiska, ponieważ:

- komunikacja VISA/USB/TCPIP pozostaje lokalna i nie wymaga serwera pośredniczącego;
- `QAbstractItemModel` i `QTreeView` dobrze pasują do drzewa receptury;
- wykresy, dokowanie paneli, skróty klawiaturowe i trwały układ okien są naturalnymi elementami aplikacji desktopowej;
- jeden instalator ma mniejszą powierzchnię błędów i zabezpieczeń niż backend, WebSocket, przeglądarka i autoryzacja;
- responsywność zapewnią osobne wątki robocze urządzeń, a nie sam wybór technologii renderowania.

Interfejs webowy ma sens dopiero jako drugi klient: zdalny podgląd, przegląd wyników i ewentualne uruchamianie zatwierdzonych receptur. Nie powinien być pierwszym ani jedynym interfejsem do awaryjnego sterowania wyjściami.

| Kryterium | PySide6/Qt | Web + backend |
|---|---|---|
| VISA i lokalny sprzęt | bezpośrednio | przez proces backendu |
| Drzewo receptury | natywne Model/View | komponent JS + API |
| Opóźnienia UI | bardzo małe lokalnie | zależne od WebSocket i przeglądarki |
| Zdalny dostęp | wymaga dodatkowej usługi | naturalny |
| Bezpieczeństwo sieciowe | mała powierzchnia | TLS, logowanie, sesje, role |
| Instalacja v1 | jedna aplikacja | backend i frontend |
| Rekomendacja | **wersja produkcyjna v1** | opcjonalny klient v2 |

Źródła techniczne decyzji: [Qt for Python](https://doc.qt.io/qtforpython-6/), [Qt Model/View](https://doc.qt.io/qt-6/model-view-programming.html), [Qt threading](https://doc.qt.io/qt-6/threads-technologies.html), [Web Serial API](https://developer.mozilla.org/en-US/docs/Web/API/Web_Serial_API).

## 2. Cel i granice systemu

Aplikacja ma jednocześnie obsługiwać:

1. generator Rigol DG1032Z, dwa kanały;
2. dwukanałowy SMU Keithley 2602A lub model zgodny z rodziną 2600;
3. analizator widma / generator Anritsu MS2830A;
4. ręczne sterowanie każdym urządzeniem;
5. receptury z wielopoziomowymi sweepami;
6. zapis widma i wartości liczbowych dla każdego punktu;
7. limity laboratoryjne, blokady wyjść, zatrzymanie awaryjne i pełny audyt.

Nazwę „Keithley 2062A” z opisu należy potwierdzić z wynikiem `*IDN?`. Repozytorium i API wskazują na rodzinę **2600**, najprawdopodobniej **2602A**. Do czasu potwierdzenia profil pozostaje `unverified`, a wyjścia są zablokowane.

Poza zakresem pierwszej wersji są: zdalne sterowanie przez Internet, współdzielenie stanowiska przez wielu operatorów, automatyczne omijanie zabezpieczeń i dowolna konsola SCPI w trybie operatora.

## 3. Najważniejsze zasady bezpieczeństwa

Każda wartość przechodzi przez cztery warstwy ograniczeń:

1. **możliwości urządzenia** — ustalone z modelu, opcji i firmware;
2. **zatwierdzony profil stanowiska** — `.config/settings.yml`;
3. **limity DUT dla bieżącego eksperymentu**;
4. **zakres receptury** — nie może rozszerzać żadnego limitu wyżej.

Efektywny limit to zawsze najwęższe przecięcie tych czterech warstw. Ograniczenia są sprawdzane:

- przy edycji pola;
- przy kompilacji receptury;
- bezpośrednio przed wysłaniem każdej komendy;
- po każdym odczycie lub zmianie stanu urządzenia.

Żaden przycisk „Override” nie zmienia limitów w pamięci bez śladu. Zmiana wymaga trybu inżyniera, powodu, ponownego zatwierdzenia profilu i zapisu do audytu. Konsola raw SCPI/TSP jest domyślnie wyłączona i dostępna wyłącznie w trybie serwisowym.

### 3.1. Deklaracja limitów DUT w recepturze

Każda receptura, która uzbraja albo włącza wyjście Keithleya lub Rigola, musi zawierać
kompletną deklarację `dut_limits` dla używanego kanału. Brak choć jednego wymaganego limitu
blokuje kompilację jeszcze przed otwarciem sesji VISA. Przykład:

```yaml
dut_limits:
  keithley:
    B:
      current: {min: "0 A", max: "10 mA"}
      voltage: {min: "-67 mV", max: "67 mV"}
      max_abs_power: "670 uW"
  rigol:
    1:
      minimum_impedance: "50 ohm"
      max_abs_current: "50 uA"
      max_abs_power: "100 nW"
  anritsu:
    max_expected_input: "-10 dBm"
```

Wymagane są jawne jednostki, również w notacji naukowej. Wartości są przeliczane do SI,
a następnie przecinane z profilem stanowiska. Walidacja jest ponawiana przy konfiguracji,
ARM, bezpośrednio przed `OUTPUT ON` oraz po odczycie Keithleya. Deklaracja i jej postać JSON
są zapisywane w HDF5 w `/run/dut_limits_json` oraz w `labbook/metadata`.

## 4. Audyt submodułu Keithley

### 4.1. Charakter sterownika

Plik `submodules/keithley2600/keithley2600/keithley_driver.py` nie ma zamkniętej listy wszystkich komend. Dynamicznie odczytuje drzewo Lua/TSP urządzenia za pomocą `next`, `getmetatable`, `Getters`, `Setters` i `Objects`, a następnie odwzorowuje właściwości i funkcje na atrybuty Pythona.

To wygodne do eksperymentów, lecz niebezpieczne w aplikacji produkcyjnej. Warstwa GUI nie może otrzymać bezpośredniego obiektu dynamicznego. Należy zbudować jawny `KeithleyAdapter` z whitelistą operacji, walidacją jednostek, timeoutami i dziennikiem.

### 4.2. Publiczne operacje istniejącej biblioteki

| Metoda | Działanie | Ocena do użycia |
|---|---|---|
| `connect()` | otwiera sesję VISA i ładuje namespace TSP | opakować, nie łączyć w konstruktorze |
| `disconnect()` | zamyka połączenie | użyć w kontrolowanym shutdownie |
| `read_error_queue()` | odczytuje kolejkę błędów | obowiązkowo po grupie komend |
| `read_buffer()` | pobiera dane z bufora SMU | wykorzystać po testach |
| `set_integration_time()` | przelicza czas na NPLC | użyć z walidacją częstotliwości sieci |
| `apply_voltage()` | ustawia napięcie i od razu włącza wyjście | **nie wywoływać bezpośrednio** |
| `apply_current()` | ustawia prąd i od razu włącza wyjście | **nie wywoływać bezpośrednio** |
| `measure_voltage()` | pojedynczy pomiar napięcia | wykorzystać |
| `measure_current()` | pojedynczy pomiar prądu | wykorzystać |
| `ramp_to_voltage()` | rampa napięcia, włącza wyjście | przepisać z anulowaniem i compliance |
| `send_trigger()` | wysyła `*trg` | wykorzystać warunkowo |
| `voltage_sweep_single_smu()` | sprzętowy sweep napięcia | przepisać limity i timeout |
| `voltage_sweep_dual_smu()` | równoległy sweep dwóch SMU | użyć dopiero po testach sprzętowych |
| `transfer_measurement()` | gotowy pomiar tranzystora | potraktować jako przykład, nie rdzeń API |
| `output_measurement()` | gotowy pomiar charakterystyki | potraktować jako przykład |

### 4.3. Komendy i ścieżki TSP używane przez bibliotekę

Połączenie i błędy:

```text
*IDN?
errorqueue.clear()
errorqueue.count
errorqueue.next()
localnode.linefreq
```

Źródło i wyjście:

```text
smua.source.func / smub.source.func
smua.source.levelv / smub.source.levelv
smua.source.leveli / smub.source.leveli
smua.source.output / smub.source.output
smua.source.highc / smub.source.highc
```

Pomiar:

```text
smua.measure.v() / smub.measure.v()
smua.measure.i() / smub.measure.i()
smua.measure.iv() / smub.measure.iv()
smua.measure.nplc / smub.measure.nplc
smua.measure.delay / smub.measure.delay
smua.measure.autorangei / smub.measure.autorangei
```

Bufory:

```text
smua.nvbuffer1.clear() / smua.nvbuffer2.clear()
smub.nvbuffer1.clear() / smub.nvbuffer2.clear()
smuX.nvbufferY.clearcache()
smuX.nvbufferY.n
smuX.nvbufferY.readings.getreading(index)
```

Wyzwalanie i sweep:

```text
smuX.trigger.source.listv(table)
smuX.trigger.source.action
smuX.trigger.source.stimulus
smuX.trigger.count
smuX.trigger.measure.action
smuX.trigger.measure.iv(buffer_i, buffer_v)
smuX.trigger.measure.stimulus
smuX.trigger.endpulse.action
smuX.trigger.endpulse.stimulus
smuX.trigger.endsweep.action
smuX.trigger.arm.stimulus
smuX.trigger.initiate()
smuX.trigger.SOURCE_COMPLETE_EVENT_ID
smuX.trigger.MEASURE_COMPLETE_EVENT_ID
smuX.trigger.PULSE_COMPLETE_EVENT_ID
smuX.trigger.ARMED_EVENT_ID
smuX.trigger.SWEEP_COMPLETE_EVENT_ID
trigger.blender[N].orenable
trigger.blender[N].stimulus[M]
trigger.blender[N].EVENT_ID
status.operation.sweeping.condition
*trg
```

Pozostałe ścieżki:

```text
display.smua.measure.func
display.smub.measure.func
table.insert(table_name, value)
beeper.beep(duration, frequency)
```

### 4.4. Komendy wymagane w nowym adapterze

Po sondowaniu możliwości konkretnego urządzenia adapter ma jawnie udostępnić:

```text
smuX.source.limiti       # compliance prądowy w trybie napięciowym
smuX.source.limitv       # compliance napięciowy w trybie prądowym
smuX.source.rangei / rangev
smuX.source.autorangei / autorangev
smuX.measure.rangei / rangev
smuX.measure.autorangei / autorangev
smuX.source.offmode
smuX.source.offlimiti / offlimitv
smuX.trigger.source.listi(table)       # sprzętowy sweep prądu
smuX.trigger.source.limiti(table)      # jeżeli wspiera model/firmware
smuX.trigger.source.limitv(table)      # jeżeli wspiera model/firmware
```

Nazwy należy potwierdzić na podłączonym modelu przez bezpieczny probe read-only oraz instrukcję TSP. Dynamiczny namespace może wykryć dostępność, ale nie może sam rozszerzać whitelisty operatora.

### 4.5. Znalezione ryzyka

- `raise_keithley_errors=False` jest domyślne; wersja produkcyjna ma zatrzymywać sekwencję na błędzie;
- compliance w istniejących sweepach jest zakomentowane;
- `apply_current()` i `apply_voltage()` włączają wyjście w tej samej metodzie;
- część sweepów kończy się stanem `HOLD`, więc ostatnia wartość może pozostać na wyjściu;
- pętle czekające na `status.operation.sweeping.condition` nie mają deadline ani anulowania;
- istnieje sweep napięcia, ale brak bezpiecznego API sweepu prądu;
- konstruktor może automatycznie łączyć się ze sprzętem;
- część wyjątków połączenia traci szczegóły diagnostyczne.

### 4.6. Docelowy adapter Keithley

Minimalna kolejność włączenia źródła prądowego:

1. sprawdź model i zatwierdzony profil;
2. wymuś `output = OFF`;
3. ustaw funkcję źródła na prąd;
4. ustaw zakres źródła;
5. ustaw `limitv`;
6. ustaw wartość startową bezpieczną dla DUT;
7. ponownie odczytaj ustawienia;
8. pokaż operatorowi podsumowanie „ARM”;
9. włącz wyjście;
10. monitoruj napięcie, prąd, moc, compliance i błędy.

Sweep prądu 1–10 mA, 100 punktów należy realizować przez `listi`, jeśli sprzęt to obsługuje. W przeciwnym razie silnik wykonuje kroki programowe z czasem ustalania, pomiarem i możliwością anulowania pomiędzy punktami. Każdy punkt zapisuje wartość zadaną i rzeczywiście zmierzoną.

## 5. Zabezpieczenia Keithley

Keithley ma rzeczywiste ograniczenie prądowe lub napięciowe zależnie od trybu źródła. To nie zwalnia z limitów programowych.

Dla każdego kanału A i B konfigurujemy osobno:

- `min_current`, `max_current` i `max_abs_current`;
- `min_voltage`, `max_voltage` i `max_abs_voltage`;
- maksymalną wartość compliance;
- progi trip dla zmierzonego prądu i napięcia;
- `max_power` liczony jako `abs(V_measured * I_measured)`;
- dopuszczalne zakresy, NPLC, czas ustalania i szybkość rampy;
- zachowanie wyjścia po zakończeniu, anulowaniu i utracie komunikacji.

W profilu startowym kanał B jest celowo ograniczony do 0–10 mA i ±67 mV, zgodnie z podanym przykładem, ale profil pozostaje `unverified`. Nie wolno traktować tych liczb jako danych katalogowych DUT.

Stan `COMPLIANCE` powoduje zapis ostatniego punktu, natychmiastowe wyłączenie odpowiedniego wyjścia i zatrzymanie receptury, chyba że zatwierdzony profil jawnie definiuje inną politykę.

## 6. Audyt submodułu Anritsu

### 6.1. Charakter obecnej implementacji

Historyczna wersja `submodules/accelerator-gui/AnritsuMS2830A.py` łączyła się przez VISA z zasobem wpisanym na stałe. Hardcode został usunięty: moduł wymaga teraz jawnego `resource` albo `ANRITSU_VISA_RESOURCE`. Nadal nie wolno używać jego funkcji generatora bez produkcyjnej warstwy interlock, ponieważ pobranie trace nie realizuje kwalifikowanego single sweep + `*OPC?`.

Z repozytorium warto zachować nazwy komend Anritsu i wiedzę o przełączaniu aplikacji. Monolityczny plik GUI PyQt5/EPICS nie powinien być bazą nowego interfejsu.

### 6.2. Pełny katalog komend występujących w module

Identyfikacja i aplikacja:

```text
*IDN?
INST?
INST SPECT
INST SG
```

Generator RF:

```text
FREQ?
FREQ <wartosc>MHZ
POW?
POW <wartosc>
UNIT.POW DBM
OUTP?
OUTP 0
OUTP 1
```

Analizator widma — oś częstotliwości:

```text
FREQ:START?
FREQ:START <wartosc>MHZ
FREQ:STOP?
FREQ:STOP <wartosc>MHZ
FREQ:CENT?
FREQ:CENT <wartosc>MHZ
FREQ:SPAN?
FREQ:SPAN <wartosc>MHZ
```

Poziom odniesienia i sweep points:

```text
DISP:WIND:TRAC:Y:RLEV?
DISP:WIND:TRAC:Y:RLEV <wartosc>
SWE:POIN?
SWE:POIN <liczba>
```

Markery:

```text
CALC:MARK:ACT OFF
CALC:MARK:ACT ON
CALC:MARK:RES PEAK
CALC:MARK:MAX
CALC:MARK:X <wartosc>MHZ
CALC:MARK:X?
CALC:MARK:Y?
```

Format i trace:

```text
FORM ASC
TRAC? TRAC1
```

### 6.3. Operacje do dodania po weryfikacji instrukcji konkretnego MS2830A

Nie należy zgadywać dialektu SCPI. Poniższe funkcje są wymaganiami, a dokładne komendy muszą przejść test sprzętowy:

- odczyt i czyszczenie kolejki błędów;
- `ABORT`, single sweep, wyłączenie continuous i oczekiwanie `*OPC?`;
- RBW, VBW, detector, trace mode, averaging i sweep time;
- RF input attenuation, preamp i input coupling;
- format binarny trace z kontrolą endian;
- odczyt liczby punktów i prawdziwej osi X z urządzenia;
- wykrycie zainstalowanych opcji SPECT/SG;
- maksymalny bezpieczny poziom wejścia RF dla bieżącej konfiguracji.

Nowy adapter ma mieć osobne metody `configure_spectrum()`, `start_single_sweep()`, `wait_complete(deadline)`, `fetch_trace()` i `abort()`. Pobranie widma nie może przełączać aplikacji ani zmieniać ustawień w sposób ukryty.

Implementacja rozróżnia dwa tryby. `Live` używa wyłącznie bezpiecznego polling `TRAC?`. Checkpoint receptury wymaga ustawienia `devices.anritsu.acquisition.single_sweep_mode: standard_scpi_opc`; wtedy adapter wykonuje jawnie `INIT:CONT OFF`, `INIT:IMM`, oczekiwanie `*OPC?` z deadline i dopiero `TRAC?`. Wartość domyślna pozostaje `unverified`, więc nie może przypadkiem zapisać starej klatki jako nowego punktu.

## 7. Audyt sterownika Rigol DG1032Z

### 7.1. Komendy systemowe

```text
*IDN?
*CLS
*RST
:SYST:ERR?
:SYST:VERS?
:SYST:CHAN:NUM?
```

`*RST` ma być dostępne wyłącznie w trybie serwisowym, ponieważ może zmienić wiele parametrów poza kontrolą receptury.

### 7.2. Przebieg podstawowy, kanał `n`

```text
:SOUR<n>:FUNC <SIN|SQU|RAMP|PULS|NOIS|DC|ARB...>
:SOUR<n>:FUNC?
:SOUR<n>:FREQ <Hz>
:SOUR<n>:FREQ?
:SOUR<n>:PHAS <deg>
:SOUR<n>:PHAS?
:SOUR<n>:VOLT:HIGH <V>
:SOUR<n>:VOLT:HIGH?
:SOUR<n>:VOLT:LOW <V>
:SOUR<n>:VOLT:LOW?
:SOUR<n>:VOLT:OFFS <V>
:SOUR<n>:VOLT:OFFS?
:SOUR<n>:APPL:DC <offset>
:SOUR<n>:FUNC:SQU:DCYC <percent>
:SOUR<n>:FUNC:SQU:DCYC?
:SOUR<n>:FUNC:RAMP:SYMM <percent>
:SOUR<n>:FUNC:RAMP:SYMM?
:SOUR<n>:FUNC:PULS:WIDT <s>
:SOUR<n>:FUNC:PULS:WIDT?
:SOUR<n>:FUNC:PULS:TRAN:LEAD <s>
:SOUR<n>:FUNC:PULS:TRAN:LEAD?
:SOUR<n>:FUNC:PULS:TRAN:TRA <s>
:SOUR<n>:FUNC:PULS:TRAN:TRA?
```

W GUI `High Level` i `Low Level` są podstawową reprezentacją amplitudy. Pola `Amplitude/Vpp` i `Offset` są widokiem pochodnym:

```text
Vpp    = HighL - LowL
Offset = (HighL + LowL) / 2
HighL  = Offset + Vpp / 2
LowL   = Offset - Vpp / 2
```

Zmiana którejkolwiek pary ma atomowo przeliczyć pozostałe pola i ponownie uruchomić walidację limitów.

### 7.3. Wyjście

```text
:OUTP<n> <ON|OFF>
:OUTP<n>?
:OUTP<n>:LOAD <50|INF|wartosc>
:OUTP<n>:LOAD?
:OUTP<n>:POL <NORM|INV>
:OUTP<n>:POL?
:OUTP<n>:MODE <NORM|GAT>
:OUTP<n>:MODE?
:OUTP<n>:GAT:POL <NORM|INV>
:OUTP<n>:GAT:POL?
:OUTP<n>:SYNC <ON|OFF>
:OUTP<n>:SYNC?
:OUTP<n>:SYNC:POL <NORM|INV>
:OUTP<n>:SYNC:POL?
:OUTP<n>:SYNC:DEL <s>
:OUTP<n>:SYNC:DEL?
```

### 7.4. Modulacje

```text
:SOUR<n>:MOD <ON|OFF>
:SOUR<n>:MOD?
:SOUR<n>:MOD:TYP <AM|FM|PM|ASK|FSK|PSK|PWM>
:SOUR<n>:MOD:TYP?
:SOUR<n>:<typ>:SOUR <INT|EXT>
:SOUR<n>:<typ>:SOUR?
:SOUR<n>:<typ>:INT:FREQ <Hz>
:SOUR<n>:<typ>:INT:FREQ?
:SOUR<n>:<typ>:INT:FUNC <SIN|SQU|RAMP|...>
:SOUR<n>:<typ>:INT:FUNC?
:SOUR<n>:<typ>:INT:RATE <rate>
:SOUR<n>:<typ>:INT:RATE?
:SOUR<n>:<typ>:INT:POL <NORM|INV>
:SOUR<n>:<typ>:INT:POL?
:SOUR<n>:AM:DEPT <percent>
:SOUR<n>:FM:DEV <Hz>
:SOUR<n>:PM:DEV <deg>
:SOUR<n>:ASK:AMPL <V>
:SOUR<n>:FSK:FREQ <Hz>
:SOUR<n>:PSK:PHAS <deg>
:SOUR<n>:PWM:DEV:DCYC <percent>
```

Dokładne warianty nagłówków zależne od typu modulacji należy potwierdzić testami zapytanie/zapis/odczyt na firmware urządzenia.

### 7.5. Sweep częstotliwości

```text
:SOUR<n>:FREQ:STAR <Hz>
:SOUR<n>:FREQ:STAR?
:SOUR<n>:FREQ:STOP <Hz>
:SOUR<n>:FREQ:STOP?
:SOUR<n>:SWE:TIME <s>
:SOUR<n>:SWE:TIME?
:SOUR<n>:SWE:SPAC <LIN|LOG|STEP>
:SOUR<n>:SWE:SPAC?
:SOUR<n>:SWE:STEP <count>
:SOUR<n>:SWE:STEP?
:SOUR<n>:SWE:HTIM:STAR <s>
:SOUR<n>:SWE:HTIM:STOP <s>
:SOUR<n>:SWE:RTIM <s>
:SOUR<n>:SWE:TRIG:SOUR <INT|EXT|MAN>
:SOUR<n>:SWE:TRIG:SLOP <POS|NEG>
:SOUR<n>:SWE:TRIG:OUTP <ON|OFF>
:SOUR<n>:SWE:STAT <ON|OFF>
:SOUR<n>:SWE:TRIG
```

### 7.6. Burst

```text
:SOUR<n>:BURS:STAT <ON|OFF>
:SOUR<n>:BURS:STAT?
:SOUR<n>:BURS:MODE <TRIG|GAT>
:SOUR<n>:BURS:NCYC <count>
:SOUR<n>:BURS:PHAS <deg>
:SOUR<n>:BURS:INT:PER <s>
:SOUR<n>:BURS:TDEL <s>
:SOUR<n>:BURS:TRIG:SOUR <INT|EXT|MAN>
:SOUR<n>:BURS:TRIG:SLOP <POS|NEG>
:SOUR<n>:BURS:TRIG:OUTP <ON|OFF>
:SOUR<n>:BURS:GATE:POL <POS|NEG>
:SOUR<n>:BURS:IDLE <FPT|TOP|CENT|BOT>
:SOUR<n>:BURS:TRIG
:SOUR1:PHAS:SYNC
```

W testowanym wcześniej firmware `00.01.08` zapytanie `:SOUR1:BURS:PHAS?` kończyło się timeoutem mimo obecności w dokumentacji. Capability probe musi więc sprawdzać każdą funkcję opcjonalną i ukrywać niedostępne kontrolki.

W kwalifikacji sprzętowej zasobu `USB0::0x1AB1::0x0642::DG1ZA172902039::INSTR` potwierdzono dodatkowo, że przy `HIGHZ` firmware `00.01.08` wymusza minimalne `2 mVpp`: żądane `HighL = 1 mV`, `LowL = 0 V` odczytano jako `HighL = 1 mV`, `LowL = -1 mV`. Każda transakcja produkcyjna musi odczytać ustawienia zwrotne; profil CH1 ma minimalne `amplitude_vpp = 2 mV`.

Po `*IDN?` adapter sonduje read-only `MOD?`, `SWE:STAT?`, `BURS:STAT?` i `PHAS?`. Funkcje bez odpowiedzi nie są pokazywane jako dostępne, a przy domyślnym `fail_on_unknown_firmware_command` nie mogą wysłać komendy sterującej.

## 8. Zabezpieczenia prądowe Rigola

Rigol DG1032Z jest generatorem napięciowym, a nie źródłem SMU. Nie oferuje prawdziwego programowalnego compliance ani pomiaru prądu DUT. Pole w GUI musi nazywać się **„szacowany prąd obciążenia”**, nigdy „zmierzony prąd”.

Model bezpieczeństwa uwzględnia wewnętrzną rezystancję wyjściową około 50 Ω oraz minimalną deklarowaną impedancję DUT.

Dla ustawienia wyświetlania `High-Z`:

```text
V_th = V_displayed
```

Dla skończonego ustawienia obciążenia `R_set`:

```text
V_th = V_displayed * (50 ohm + R_set) / R_set
```

Szacowany prąd dla najgorszego przypadku:

```text
I_high = V_th_high / (50 ohm + R_dut_min)
I_low  = V_th_low  / (50 ohm + R_dut_min)
I_peak = max(abs(I_high), abs(I_low))
```

Komenda jest odrzucana, jeśli:

- `R_dut_min` nie jest ustawione;
- `I_peak` przekracza `max_abs_estimated_current`;
- HighL/LowL, Vpp, offset lub częstotliwość wychodzą poza profil;
- deklaracja obciążenia w urządzeniu różni się od modelu użytego do obliczeń.

Dla obciążeń reaktywnych, nieznanej impedancji, kabli o istotnej pojemności lub bardzo krótkich zboczy taki model jest niewystarczający. Wtedy profil musi wymagać zewnętrznego ogranicznika, rezystora szeregowego albo pomiaru prądu. Informacja o stałym szeregowym wyjściu 50 Ω: [Rigol FAQ](https://www.rigolna.com/products/waveform-generators/dg1000z/).

## 9. Audyt bieżącego GUI

`gui.py` jest prototypem Tkinter przeznaczonym głównie dla Rigola. Nie powinien być rozszerzany do produkcyjnego GUI trzech urządzeń, bo utrudni drzewo receptury, docki, rozbudowane modele tabel oraz kontrolowane wątki.

Kod komunikacji i obliczenia, które przejdą testy, można przenieść. Warstwy widoku należy zbudować od nowa w PySide6. Migracja ma zachować możliwość uruchomienia starego prototypu do czasu osiągnięcia parytetu funkcji.

## 10. Plik `.config/settings.yml`

Szablon został utworzony w [`.config/settings.yml`](../.config/settings.yml). Plik przechowuje wyłącznie konfigurację stanowiska i limity, a nie bieżący stan przycisków GUI.

Główne sekcje:

```text
schema_version
profile
application
units
execution
storage
ui
devices.rigol
devices.keithley
devices.anritsu
recipe_defaults
```

Każdy parametr liczbowy ma wartość i jednostkę. Parser zamienia dane do SI za pomocą biblioteki jednostek, a walidator Pydantic blokuje nieznane pola i niezgodny schemat. Nigdy nie wykonujemy `float(text)` bez wcześniejszego rozpoznania jednostki.

Zasady pracy z plikiem:

- aplikacja ładuje konfigurację tylko przez `SettingsRepository`;
- zapis odbywa się atomowo do pliku tymczasowego i przez podmianę;
- ostatnia poprawna wersja ma kopię zapasową;
- błędny YAML nie uruchamia profilu domyślnego po cichu;
- receptura zapisuje snapshot konfiguracji i jej hash;
- zmiana limitów ustawia profil na `unverified`;
- połączenia mogą być testowane w profilu niezatwierdzonym, ale wyjścia pozostają OFF;
- wartości tajne nie są zapisywane w YAML.

## 11. Szczegółowy projekt UI

### 11.1. Powłoka aplikacji

```text
┌──────────────────────────────────────────────────────────────────────────────┐
│ STANOWISKO  [SAFE/ARMED/RUNNING/FAULT]  receptura  operator   [E-STOP]      │
├───────────────┬──────────────────────────────────────────┬───────────────────┤
│ Nawigacja     │ Obszar roboczy                           │ Inspektor         │
│ Dashboard     │ zakładki / edytor / wykresy              │ limity i błędy    │
│ Rigol         │                                          │ bieżącego pola    │
│ Keithley      │                                          │                   │
│ Anritsu       │                                          │                   │
│ Receptury     │                                          │                   │
│ Wyniki        │                                          │                   │
│ Ustawienia    │                                          │                   │
├───────────────┴──────────────────────────────────────────┴───────────────────┤
│ log zdarzeń | kolejka komend | postęp | czas | ścieżka pliku                │
└──────────────────────────────────────────────────────────────────────────────┘
```

Kolory stanu są zawsze wspierane tekstem i ikoną. `E-STOP` jest stale widoczny. Wyjścia mają niezależne wskaźniki `OFF`, `ON`, `COMPLIANCE`, `UNKNOWN`. Stan nieznany jest traktowany jak błąd, nie jak OFF.

### 11.2. Dashboard

Każde urządzenie ma kartę z zasobem VISA, wynikiem `*IDN?`, czasem ostatniej odpowiedzi, stanem wyjść i przyciskiem `Połącz/Testuj`. Dashboard zawiera checklistę gotowości:

- profil zatwierdzony;
- trzy tożsamości zgodne;
- limity DUT uzupełnione;
- wyjścia OFF;
- katalog wyników zapisywalny;
- brak błędów urządzeń;
- oszacowana liczba punktów i czas wykonania.

### 11.3. Rigol

Osobne zakładki CH1 i CH2:

- `Basic`: funkcja, częstotliwość, HighL, LowL, Vpp, offset, faza;
- `Shape`: duty, symmetry, pulse width, leading/trailing edge;
- `Output`: ON/OFF, load, polarity, sync;
- `Modulation`, `Sweep`, `Burst`;
- panel bezpieczeństwa z `R_dut_min`, napięciem Thevenina i szacowanym prądem dla HighL/LowL;
- podgląd przebiegu oraz pionowe linie limitów.

Edycja odbywa się lokalnie. `Zastosuj` wysyła jedną zatwierdzoną transakcję, odczytuje wartości z urządzenia i pokazuje różnice. `Output ON` wymaga osobnego przycisku ARM i podsumowania limitów.

### 11.4. Keithley

Kanały A i B pokazują:

- tryb Source I / Source V;
- poziom źródła, zakres i autorange;
- odpowiedni compliance;
- tryb 2-wire/4-wire, NPLC, delay;
- zmierzone I, V i P;
- osobne progi trip i status compliance;
- konfigurator rampy i podgląd przewidywanych punktów.

Przycisk `Apply` nie włącza wyjścia. `ARM`, `Output ON`, `Ramp to zero` i `Output OFF` są oddzielnymi operacjami. Przy opuszczaniu strony żaden stan nie zmienia się automatycznie.

### 11.5. Anritsu

Zakładka analizatora:

- start/stop albo center/span;
- reference level, sweep points, RBW, VBW, detector, attenuation;
- marker i peak search;
- `Single`, `Continuous`, `Abort`, `Fetch trace`;
- wykres widma z markerami i metadanymi.

Tryb `Live` oznacza odpytywanie przez aplikację kolejnych kompletnych trace w kontrolowanym interwale (domyślnie 500 ms), nie strumień push z analizatora. Interfejs pokazuje aktualną klatkę, nie zapisuje wszystkich klatek Live i pilnuje, aby nowe odpytywanie nie zostało zakolejkowane przed zakończeniem poprzedniego. Zapisywany punkt receptury zawsze pobiera osobny, opisany trace.

Zakładka generatora RF jest widoczna tylko przy wykrytej opcji SG. Ma własny ARM i blokadę mocy. Przełączenie `INST SPECT` / `INST SG` jest jawne i nie może nastąpić jako efekt uboczny odczytu pola.

### 11.6. Recipe Builder

```text
Sequence
├─ Setup: Keithley B, Source I, limit V = 67 mV
├─ Sweep: Keithley B current 1 mA → 10 mA, 100 points
│  ├─ Sweep: Rigol CH1 HighL 1 mV → 3 mV, 20 points
│  │  ├─ Set: Rigol CH1 Square, LowL, frequency, duty
│  │  ├─ Wait: settle 100 ms
│  │  ├─ Measure: Keithley B I/V
│  │  └─ Acquire: Anritsu spectrum
└─ Finally
   ├─ Keithley ramp to zero + outputs OFF
   ├─ Rigol outputs OFF
   └─ Anritsu abort
```

Lewa część to drzewo z drag-and-drop. Środek jest edytorem wybranego węzła. Prawy inspektor pokazuje jednostki, efektywne limity i wpływ na liczbę punktów. Dolny panel zawiera skompilowany plan, przewidywany czas, zajętość dysku i wszystkie ostrzeżenia.

Typy węzłów: `Connect`, `Configure`, `Set`, `Sweep`, `Measure`, `AcquireSpectrum`, `Wait`, `If`, `Repeat`, `Checkpoint`, `Comment`, `Finally/SafeShutdown`. Pętla bez limitu iteracji jest zabroniona.

### 11.7. Run Monitor

Widok wykonania pokazuje aktualną ścieżkę drzewa, indeksy obu sweepów, wartości zadane i zmierzone, bieżące widmo, ETA, szybkość zapisu i kolejkę ostrzeżeń. Dostępne akcje:

- `Pause after point` — zatrzymanie po domknięciu punktu;
- `Resume` — po ponownej walidacji stanu;
- `Stop safely` — rampa do wartości bezpiecznych i OFF;
- `E-STOP` — natychmiastowa najlepsza próba OFF bez dalszych pomiarów.

### 11.8. Zakładka Ustawienia

Zakładka ma kategorie po lewej i formularz po prawej:

1. `Profil stanowiska` — nazwa, status, zatwierdzający, uwagi;
2. `Połączenia` — zasób VISA każdego urządzenia, timeout, backend, test połączenia;
3. `Rigol CH1/CH2` — limity HighL, LowL, Vpp, offset, częstotliwość, szacowany prąd, `R_dut_min`;
4. `Keithley A/B` — min/max I i V, compliance, moc, zakresy, rampy;
5. `Anritsu SPECT` — zakres częstotliwości, punkty, poziom odniesienia i limit wejścia RF;
6. `Anritsu SG` — częstotliwość, moc i zezwolenie na RF output;
7. `Wykonanie` — timeouty, retry, settle, checkpoint, limit punktów;
8. `Dane` — katalog, HDF5/CSV, kompresja i flush;
9. `UI i role` — język, motyw, tryb operator/inżynier/serwis;
10. `Diagnostyka` — log, capability report i eksport konfiguracji.

Pola mają kolumny `Minimum`, `Maksimum`, `Jednostka`, `Wartość domyślna`, `Źródło limitu`. Zmiany trafiają najpierw do kopii roboczej. Dostępne są `Waliduj`, `Pokaż różnice`, `Zapisz`, `Odrzuć` i `Zatwierdź profil`. Nie ma autosave limitów bezpieczeństwa.

## 12. Receptura i kompilator

Receptura jest deklaratywnym YAML/JSON, nigdy kodem Python ani listą raw SCPI. Każdy węzeł ma stabilne `id`, typ, urządzenie, parametr, jednostkę, politykę błędów i ewentualne dzieci.

Kompilator receptury:

1. waliduje schemat;
2. rozwiązuje jednostki do SI;
3. rozwija sweepy i liczy iloczyn kartezjański;
4. sprawdza zależności parametrów, np. HighL > LowL;
5. oblicza prąd Rigola dla każdego skrajnego przypadku;
6. sprawdza compliance i moc Keithley;
7. potwierdza capabilities urządzeń;
8. dodaje jawny `safe shutdown`;
9. generuje niezmienny `ExecutionPlan` i hash.

Przykład użytkownika daje 100 × 20 = 2000 widm. Przed ARM aplikacja pokazuje tę liczbę, szacowany czas i rozmiar danych. Dla sweepu liniowego oba końce są wliczone, więc krok wynosi `(stop - start) / (points - 1)`.

## 13. Silnik wykonawczy

Maszyna stanów:

```text
DISCONNECTED → CONNECTED → VERIFIED → ARMED → RUNNING
                                      ↘ PAUSED
dowolny aktywny stan → STOPPING → SAFE
dowolny aktywny stan → FAULT → EMERGENCY_OFF → SAFE/UNKNOWN
```

Każde urządzenie ma jeden długowieczny `QObject` przeniesiony do własnego `QThread`. Tylko ten worker dotyka sesji VISA. Komendy przechodzą przez kolejkę i zwracają sygnały z wynikiem. Nie używamy `QThreadPool` do współdzielenia tej samej sesji; pula nadaje się do obliczeń i zapisu plików.

Każda operacja ma deadline, token anulowania, identyfikator korelacji i snapshot stanu. GUI nigdy nie czeka blokująco. Watchdog sprawdza heartbeat, a utrata komunikacji uruchamia politykę fault. Trzeba jasno komunikować, że software E-STOP nie zastępuje fizycznego odcięcia zasilania lub RF.

Kolejność pojedynczego punktu:

1. ustaw źródła zgodnie z planem;
2. odczytaj i porównaj konfigurację;
3. poczekaj na settle albo warunek stabilności;
4. zmierz Keithley;
5. sprawdź trip/compliance/moc;
6. uruchom pojedynczy sweep Anritsu;
7. poczekaj na zakończenie z deadline;
8. pobierz trace i oś;
9. zapisz atomowy checkpoint;
10. przejdź do kolejnego punktu.

## 14. Dane i odtwarzalność

Format główny: HDF5 zgodny strukturalnie z plikami thaTEC:OS i możliwy do bezpośredniego
odczytu przez bibliotekę PyThat. CSV pozostaje wyłącznie indeksem i wygodnym eksportem.

### 14.1. Obowiązkowa zgodność thaTEC:OS / PyThat

Zgodność z PyThat jest wymaganiem akceptacyjnym, a nie opcjonalnym eksportem. Laboratorium
posiada istniejący system inwentaryzacji i analizy oparty na plikach thaTEC:OS, dlatego każdy
run wykonany przez aplikację musi tworzyć plik `.h5`, w którym zarówno metadane, parametry
drzewa pomiarowego, wartości zadane i zmierzone, jak i kompletne widma zachowują organizację
danych rozpoznawaną przez PyThat.

Punktem odniesienia („golden file”) jest dołączony plik:

```text
05062026_YIG20CoFeB1_FMR_S12_S21_2A_-2A_10MHz_4GHz_Measurement_#3.h5
```

Plik referencyjny ma następujący kontrakt najwyższego poziomu:

```text
/
  attrs:
    measurement running
    thaTEC:OS version
    version information
  devices/
    <nazwa urządzenia>          # dwukolumnowa tabela nazwa parametru / wartość
  labbook/
    comments
    metadata                   # dwukolumnowa tabela klucz / wartość
    parameter
  measurement/
    log                        # czas / komunikat
    row_XX/
      data                     # wartości kontrolki albo pełna tablica wskaźnika
      timestamp                # znacznik dla każdego zapisanego punktu
      metadata                 # wymagane dla danych wielowymiarowych
      scale                    # wymagane dla danych wielowymiarowych
  scan_definition/
    row_XX                     # definicja każdego węzła drzewa
    tree_view                  # kolejność, typ i czytelna reprezentacja drzewa
```

Nie wolno zastępować tabel thaTEC JSON-em ani zapisywać samych datasetów `frequency_hz` i
`power_dbm` w prywatnym układzie. Muszą zostać zachowane:

- dokładne nazwy grup i datasetów, w tym numeracja `row_XX` zgodna pomiędzy
  `/scan_definition` i `/measurement`;
- dwukolumnowe tablice tekstowe w `/devices`, `/labbook`, `/scan_definition` i `metadata`;
- pola definicji wiersza: `device name`, `control name`, `dimensions`, `data type`,
  `tree indent level`, `function` oraz — zależnie od funkcji — `start`, `stop`, `steps`,
  `equation`, `value` albo `waiting period (ms)`;
- atrybuty datasetu `data`: `data type` i `dim of data` z typami zgodnymi z plikiem
  referencyjnym;
- kolejność wymiarów tablicy `data`, informacja o osiach w `metadata` (`name`, `unit`,
  `offset`, `multiplier`) oraz odpowiadający im dataset `scale`;
- pełna precyzja danych liczbowych, jednostki i wartości surowe; konwersja jednostek na
  potrzeby GUI nie może zmieniać danych archiwalnych;
- snapshot parametrów każdego urządzenia, identyfikatory VISA, log procesu, metadane
  labbook oraz znaczniki czasu;
- możliwość odczytu pliku częściowego po awarii; atrybut `measurement running` musi być
  ustawiany przy rozpoczęciu i zerowany dopiero po kontrolowanym zamknięciu runu.

Dla naszej stacji nazwy urządzeń w pliku będą stabilnymi identyfikatorami profilu, np.
`Rigol DG1032Z`, `Keithley 2602A` i `Anritsu Spectrum Analyzer`. Receptura zostanie
przetłumaczona na kolejne wiersze `scan_definition`: ustawienie urządzenia to `control`,
opóźnienie to `internal`, a pomiar lub widmo to `indicator`. Poziom zagnieżdżenia pętli musi
być zapisany jako `tree indent level`, ponieważ PyThat odtwarza z niego wymiary danych.

Widmo Anritsu ma być jednym wskaźnikiem o jawnie opisanej osi częstotliwości i osi wartości
(np. `Frequency [Hz]`, `Power [dBm]`). Dodatkowe kanały/trace są osobnym wymiarem albo
osobnymi wskaźnikami — wybór zostanie zamrożony testem na reprezentatywnym pliku, tak aby
PyThat zwracał poprawne `dims`, `coords`, nazwy, jednostki i wartości w obiekcie xarray.
Analogicznie setpoint i odczyt Keithley oraz parametry HighL/LowL Rigola muszą występować w
drzewie, a nie wyłącznie w prywatnych metadanych aplikacji.

Implementacja powinna rozdzielać model domenowy od formatu przez `ThatecHdf5Writer` oraz
`ThatecSchemaMapper`. Jeśli zachowamy obecny wewnętrzny schemat `/run`, `/points`,
`/spectra`, może on istnieć wyłącznie jako dodatkowa przestrzeń nazw, o ile test PyThat
potwierdzi, że nie zakłóca odczytu. Za kontrakt zewnętrzny odpowiada zawsze struktura
thaTEC:OS.

### 14.2. Dotychczasowy model logiczny aplikacji

Poniższy układ opisuje model logiczny i może pozostać wewnętrznym indeksem aplikacji, ale
nie zastępuje kontraktu z punktu 14.1:

```text
/run/metadata
/run/recipe_yaml
/run/settings_yaml
/run/capabilities_json
/run/device_idn
/points/setpoints
/points/measurements
/points/status
/spectra/frequency_hz
/spectra/power_dbm
/events/timestamp
/events/severity
/events/message
```

Każdy punkt zawiera UTC, czas monotoniczny, indeksy pętli, zadane i odczytane wartości, jednostki, compliance, błędy urządzeń oraz indeks trace. Plik jest flushowany po każdym widmie. Nieudany run pozostaje czytelny i ma status `aborted` lub `faulted`.

Aktualny zapis przechowuje `/events/timestamp`, `/events/severity`, `/events/name` i `/events/message` (JSON) oraz opcjonalny, flushowany przy checkpointcie indeks CSV. Przeglądarka wyników odczytuje te dane bez otwierania sesji VISA.

### 14.3. Walidacja kompatybilności

Test kompatybilności nie może ograniczać się do sprawdzenia, że plik otwiera `h5py`.
Pipeline testowy musi:

1. zinwentaryzować golden file i utrzymywać wersjonowany manifest ścieżek, typów, atrybutów,
   rang, wymiarów i obowiązkowych kluczy tabel;
2. wygenerować minimalny run: setpoint Keithley → konfiguracja Rigol → widmo Anritsu;
3. otworzyć wynik przy użyciu wspieranej wersji PyThat (wersję przypiąć w zależnościach
   testowych po kwalifikacji; punktem startowym jest PyThat 0.2.14);
4. odtworzyć drzewo pomiarowe i przekonwertować wszystkie poprawne wskaźniki do xarray;
5. porównać `dims`, `coords`, jednostki, metadane, liczbę punktów i próbki wartości z
   modelem wykonania aplikacji;
6. sprawdzić run zakończony, przerwany i faulted oraz ponowne otwarcie po każdym flushu;
7. wykonać round-trip przez aktualny system inwentaryzacji laboratorium, nie tylko przez
   samą bibliotekę PyThat.

PyThat jest niezależnym pakietem społecznościowym, dlatego jego zgodność należy traktować
jako testowany kontrakt wersji. Aktualizacja PyThat, h5py, numpy lub zmiana mapowania HDF5
wymaga ponownego uruchomienia całej macierzy golden-file.

## 15. Docelowa struktura projektu

```text
app/
  main.py
  domain/
    quantities.py
    limits.py
    capabilities.py
    recipe.py
    execution_plan.py
  devices/
    base.py
    rigol/
      adapter.py
      capabilities.py
      simulator.py
    keithley/
      adapter.py
      tsp_whitelist.py
      simulator.py
    anritsu/
      adapter.py
      capabilities.py
      simulator.py
  safety/
    validator.py
    rigol_current_model.py
    interlocks.py
  engine/
    compiler.py
    runner.py
    state_machine.py
    emergency.py
  storage/
    hdf5_writer.py
    thatec_hdf5_writer.py
    thatec_schema_mapper.py
    thatec_validator.py
    export_csv.py
  settings/
    models.py
    repository.py
    migrations.py
  ui/
    main_window.py
    viewmodels/
    pages/
    widgets/
tests/
  unit/
  integration/
  hardware/
.config/settings.yml
docs/
```

## 16. Testy

### 16.1. Bez sprzętu

- round-trip ustawień i migracje schematu;
- jednostki `mV/V`, `mA/A`, `kHz/MHz`, przecinek i kropka;
- wartości graniczne, NaN, infinity i bardzo duże wykładniki;
- własności HighL/LowL/Vpp/offset;
- model prądowy Rigola dla High-Z i obciążenia skończonego;
- kolejność ustawiania compliance przed output ON;
- kompilacja zagnieżdżonych sweepów i limit punktów;
- cancel, timeout, compliance i utrata połączenia;
- zapis częściowego HDF5 po każdym punkcie.
- zgodność drzewa, metadanych, skal i tablic z manifestem referencyjnego pliku thaTEC;
- bezpośredni odczyt wygenerowanego pliku przez PyThat i poprawna konwersja do xarray;
- zgodność pliku zakończonego, przerwanego i faulted z systemem inwentaryzacji laboratorium.

### 16.2. Symulatory

Każdy simulator odtwarza: normalną odpowiedź, timeout, błędną odpowiedź, błąd kolejki, rozłączenie i compliance. Symulator Anritsu generuje deterministyczne widmo, a Keithley model DUT z szumem i ograniczeniem.

### 16.3. Ze sprzętem

1. tylko read-only `*IDN?`, capabilities i błędy;
2. wszystkie wyjścia OFF i odczyt stanu;
3. wartości minimalne na sztucznym obciążeniu;
4. test jednego punktu;
5. krótki sweep 2 × 2;
6. przerwanie w każdym etapie;
7. odłączenie każdego przewodu podczas runu;
8. osiągnięcie compliance na kontrolowanym obciążeniu;
9. pełne 100 × 20 z checkpointami;
10. soak test i restart aplikacji po awarii.

Testy destrukcyjne wykonuje się wyłącznie na obciążeniu laboratoryjnym, nigdy na docelowym DUT.

## 17. Etapy wdrożenia

### Etap 0 — potwierdzenie stanowiska

- odczytać pełne `*IDN?`, firmware i opcje;
- potwierdzić, czy Keithley to 2602A;
- zebrać limity DUT i wejścia Anritsu;
- zatwierdzić okablowanie, 2-wire/4-wire i impedancję;
- ustalić wymaganie fizycznego E-STOP/interlock.

### Etap 1 — fundament i ustawienia

- PySide6, modele Pydantic, jednostki i logowanie;
- loader `.config/settings.yml`, walidacja i migracje;
- ekran Ustawienia oraz role;
- baza adapterów i symulatory.

### Etap 2 — adaptery

- Rigol: jawne transakcje i capability probe;
- Keithley: whitelist TSP, compliance przed output ON i current sweep;
- Anritsu: zsynchronizowany single sweep i trace;
- integracyjne testy fake VISA.

### Etap 3 — sterowanie ręczne

- Dashboard i trzy moduły urządzeń;
- spójne ARM/ON/OFF;
- wykresy i odczyty;
- audit log i bezpieczne zamknięcie.

### Etap 4 — receptury

- model drzewa, edytor i inspektor;
- kompilator, jednostki, limity i podgląd punktów;
- zapis/odczyt oraz wersjonowanie receptur.

### Etap 5 — wykonanie i dane

- state machine, workers, watchdog i anulowanie;
- `ThatecSchemaMapper`, zapis HDF5 zgodny z thaTEC:OS/PyThat, checkpoint i CSV;
- golden-file manifest, testy PyThat/xarray i round-trip przez system inwentaryzacji;
- Run Monitor i Data Browser;
- wznowienie wyłącznie od bezpiecznej granicy punktu.

### Etap 6 — kwalifikacja sprzętowa

- macierz testów komenda × model × firmware;
- testy granic i fault injection;
- pomiar realnego czasu 2000 widm;
- zatwierdzenie profilu przez odpowiedzialną osobę;
- instrukcja operatora i procedura awaryjna.

## 18. Kryteria ukończenia

Wersja v1 jest gotowa, gdy:

- każde urządzenie można niezależnie połączyć, zidentyfikować, skonfigurować i bezpiecznie wyłączyć;
- żadna ścieżka GUI nie włącza wyjścia bez ARM i walidacji;
- Keithley zawsze ma compliance ustawione przed output ON;
- Rigol blokuje wartości przekraczające limit szacowanego prądu;
- Anritsu pobiera zsynchronizowane, opisane widmo;
- receptura 100 × 20 tworzy dokładnie 2000 kompletnych rekordów albo czytelny plik częściowy;
- timeout, compliance, rozłączenie i E-STOP kończą się zdefiniowanym stanem;
- wynik zawiera recepturę, settings, IDN, capabilities, wersję aplikacji i log;
- każdy wynik zachowuje strukturę thaTEC:OS i jest bez błędów odczytywany przez przypiętą
  wersję PyThat, z poprawnymi wymiarami, współrzędnymi, metadanymi i kompletnymi widmami;
- plik przechodzi round-trip w istniejącym systemie inwentaryzacji danych laboratorium;
- zestaw testów symulacyjnych oraz kwalifikacja na sztucznym obciążeniu są udokumentowane.

## 19. Otwarte decyzje przed kwalifikacją wyjść

1. Jaki jest dokładny wynik `*IDN?` Keithley i Anritsu?
2. Jakie są zatwierdzone maksymalne I, V i P dla DUT na obu kanałach Keithley?
3. Jaka jest minimalna impedancja DUT widziana przez oba kanały Rigola?
4. Jaki jest bezpieczny maksymalny poziom wejściowy Anritsu dla użytej ścieżki i tłumika?
5. Jakie zatwierdzone zakresy częstotliwości i mocy oraz która opcja/wersja firmware mają zostać
   zakwalifikowane dla generatora RF Anritsu w v1?
6. Czy stanowisko ma fizyczny interlock/E-STOP, który aplikacja może obserwować?
7. Czy po prawidłowym runie Keithley ma zawsze rampować do zera, czy do osobnej wartości bezpiecznej?
8. Jakie dokładne nazwy urządzeń, kontrolek, wskaźników i jednostek oczekuje obecny system
   inwentaryzacji oraz czy dopuszcza dodatkową prywatną grupę danych aplikacji?
9. Czy wszystkie widma Anritsu mają być jednym wielowymiarowym wskaźnikiem PyThat, czy
   osobnymi wierszami dla każdego trace/kanału?

Do czasu odpowiedzi profil pozostaje niezaufany, a `allow_output_enable` jest ustawione na `false`.

## 20. Stan implementacji — 2026-07-16

Zakończono programowo etapy 1–5 w zakresie możliwym do zweryfikowania bez podłączania
energii do DUT. W szczególności:

- wynik jest sprawdzany przez wersjonowany manifest thaTEC oraz przypięty PyThat 0.2.14;
- manifest zawiera SHA-256 dostarczonego pliku referencyjnego, a test golden-file sprawdza
  jego drzewo, tabele, typy, skale i round-trip do xarray;
- kontrakt jest walidowany również dla wyników `completed`, `aborted` i `faulted`, a błąd
  walidacji przy zamykaniu zmienia run na `faulted` zamiast pozostawiać pozornie poprawny plik;
- wznowienie jest możliwe wyłącznie od trwałego zdarzenia `safe_resume_boundary`, po
  potwierdzonym OUTPUT OFF Rigola i Keithleya; niezapisany lub energetyzowany ogon jest odcinany;
- wznowienie wymaga identycznych hashy planu, receptury i settings oraz odtwarza wyłącznie
  pasywną konfigurację potrzebną przed kolejnym checkpointem;
- Recipe Builder ma hierarchiczne drzewo, inspektor, podgląd wartości sweepu, atomowy zapis,
  historię wersji i autosave recovery, także dla chwilowo niepoprawnego YAML;
- symulatory wszystkich trzech urządzeń mają deterministyczne scenariusze normalne, timeout,
  malformed response, device error, rozłączenie i kolejkę błędów; model Keithleya obsługuje
  compliance, rezystancyjny DUT oraz opcjonalny powtarzalny szum odczytu;
- pełny zestaw regresji po tych zmianach przechodzi bez błędów.

Dalsza realizacja etapów 1–5 objęła również:

- trwały, sekwencyjny audit JSONL z redakcją i fail-closed blokadą energii po błędzie zapisu;
- politykę deadline/retry/heartbeat/watchdog, bez retry dla OUTPUT ON i z niezależnym E-STOP;
- typy receptur `Repeat`, `If/else`, `Connect`, `Checkpoint` i `Comment` oraz model estymacji
  czasu, retry i rozmiaru danych przed ARM;
- dynamiczny Dashboard/preflight obejmujący profil, urządzenia, błędy, stany wyjść, katalog
  wyników, DUT i skompilowany plan;
- rozszerzony Run Monitor: aktualny node, setpointy/readback, ETA, heartbeat, write rate,
  ostrzeżenia i ostatnie widmo checkpointu;
- diagnostykę Settings z diffem, odrzuceniem draftu, SHA-256, statusem backupu i redacted export;
- dedykowaną tabelę Safety limits z kolumnami scope, parametr, minimum, maksimum, jednostka,
  wartość domyślna i źródło oraz synchronizacją z pełnym drzewem ustawień;
- bezpieczny DnD drzewa receptury z zachowaniem komentarzy, ponownym parserem i blokadą
  cykli, ruchu root oraz przekraczania granicy `finally`;
- ręczną rampę Keithleya od rzeczywistego poziomu źródła do celu, z limitem kroku, czasu
  ustalania i deadline, podglądem punktów, pomiarem I/V w każdym kroku oraz fail-safe OFF;
- hashowany manifest bezpiecznego wyłączenia w `ExecutionPlan`, wykonywany w jawnej kolejności
  z rejestrowaniem każdego kroku oraz trwałym flush checkpointu;
- produkcyjny przepływ referencji Anritsu: świeża 1×, uśredniona N×, lokalne użycie bieżącego
  śladu, metadane, ochrona nadpisania, HDF5 save/load przez PyThat oraz kontrola zgodności;
- lokalne uwierzytelnienie kontem systemu operacyjnego i role operator/inżynier/serwis,
  egzekwowane w backendzie oraz widoczne w UI, audycie i HDF5; provisioning opisuje
  `docs/ACCESS_CONTROL.md`;
- regresję **221 testów i 34 podtestów**, w tym pełne wykonanie symulacyjne 100 × 20 z dokładnie
  2000 kompletnych widm i ponownym otwarciem przez PyThat, Ruff dla kodu własnego i compileall;
- wykonywalny, service-only harness `app/qualification` dla etapów passive/OFF oraz receptur HIL,
  z wielokrotną bramką trybu energized, atomowym raportem JSON+SHA-256, audytem i wynikiem HDF5;
  procedura użycia znajduje się w `docs/HIL_QUALIFICATION.md`.
- dwukierunkową reprezentację częstotliwości Anritsu `Start/Stop` i `Center/Span`, która przed
  wywołaniem adaptera zawsze wraca do zwalidowanych granic fizycznych;
- bezpieczną ścieżkę opcjonalnego generatora Anritsu: detekcję opcji 020/120/021/121, jawny tryb
  SG, konfigurację wyłącznie przy RF OFF, readback, jednorazowy ARM, limity stacji i DUT,
  receptury, symulator oraz RF OFF w E-STOP. Domyślny protokół `unverified` blokuje aktywację do
  czasu kwalifikacji opisanej w `docs/HIL_QUALIFICATION.md`.
- kwalifikowalną ścieżkę zaawansowanego Spectrum Analyzer: query-only odczyt oraz fail-closed zapis
  RBW/VBW, detektora, attenuation/preamp i sweep time, pełny readback, bramkę dokładnego firmware,
  GUI, receptury i symulator; referencje HDF5 zachowują te parametry i blokują matematykę widm przy
  niezgodnej konfiguracji.

Źródłem tożsamości jest uwierzytelniona sesja systemu operacyjnego; aplikacja nie przechowuje
własnych haseł i nie udostępnia pozornego przełącznika roli. Pierwsze konto serwisowe wymaga
kontrolowanego provisioningu lokalnego profilu. Komendy zależne od firmware oraz kwalifikacja
HIL pozostają etapem 6.

Etap 6 pozostaje otwarty. Wymaga fizycznego stanowiska, zatwierdzonych obciążeń, limitów DUT,
okablowania oraz decyzji osoby odpowiedzialnej za bezpieczeństwo. Wyników z symulatorów nie
wolno traktować jako kwalifikacji sprzętowej.
