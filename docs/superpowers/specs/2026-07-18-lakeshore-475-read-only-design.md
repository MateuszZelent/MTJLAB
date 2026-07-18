# Lake Shore 475 - projekt integracji tylko do odczytu

Data: 2026-07-18

## 1. Cel

Do aplikacji Lab Control zostanie dodany kompletny, pionowy moduł gaussometru
Lake Shore Model 475. Integracja ma działać analogicznie do istniejących
modułów MOKE Box, Keithley 2600 i Anritsu MS2830A:

- własny adapter, modele, symulator, manifest i strona UI;
- połączenie i operacje wykonywane w dedykowanym workerze;
- status urządzenia na dashboardzie i pasku aplikacji;
- blok pomiarowy w recepturach;
- udział w preflight, wymaganych urządzeniach i lifecycle runu;
- zapis identyfikacji, capabilities i wyników do HDF5/PyThat;
- testy jednostkowe, integracyjne, UI i obowiązkowa kwalifikacja HIL.

Zakres funkcjonalny jest wyłącznie odczytowy. Moduł nie będzie zmieniał
ustawień gaussometru ani sterował jego wyjściami.

## 2. Źródła prawdy

Implementacja ma być zgodna z:

- lokalnym podręcznikiem
  `docs/External_libraries/475_manual.pdf`, szczególnie rozdziałem 6;
- oficjalnym pakietem Lake Shore `lakeshore==1.10.0`;
- publicznym API `lakeshore.model_425.Model425`;
- istniejącymi kontraktami `DeviceAdapter`, `DeviceModule`,
  `DeviceController`, `RecipeCompiler`, `RecipeRunner` i writerów HDF5;
- zasadą fail-closed opisaną w `docs/NEW_DEVICE_MODULES.md`.

Podręcznik Modelu 475 jest autorytetem dla komend i parametrów transmisji.
Oficjalny pakiet nie deklaruje obsługi Modelu 475. Jego publiczna klasa
Modelu 425 zostanie użyta wyłącznie jako oficjalna, testowalna granica
`query()` nad połączeniem przekazanym przez aplikację.

## 3. Decyzje i granice zakresu

### 3.1. Funkcje objęte zakresem

Moduł obsłuży:

- identyfikację `*IDN?`;
- odczyt bieżącej jednostki `UNIT?`;
- odczyt konfiguracji trybu `RDGMODE?`;
- odczyt zakresu `RANGE?`;
- odczyt autorange `AUTO?`;
- odczyt typu sondy `TYPE?`;
- pole DC i RMS przez `RDGFIELD?`;
- częstotliwość RMS przez `RDGFRQ?`;
- ujemny i dodatni pik przez `RDGPEAK?`;
- pojedynczy pomiar i cykliczny live readout;
- historię odczytów w UI;
- pomiar wykonywany jako checkpoint receptury.

### 3.2. Funkcje poza zakresem

Adapter, dispatcher, UI i receptury nie udostępnią:

- `UNIT`, `RDGMODE`, `RANGE`, `AUTO`;
- `ZPROBE`, `ZCLEAR` ani innych funkcji zerowania;
- ustawień filtrów, relative, min/max i bufora;
- field control, setpointu ani regulatora PI;
- Analog Output 1, 2 lub 3;
- kalibracji i komend serwisowych;
- zmiany baud rate;
- surowej konsoli, dowolnego `command()` lub dowolnego `query()`;
- binarnego `RDGFAST?`;
- obsługi Modelu 425 jako osobnego urządzenia.

Istniejące publiczne klasy `LakeShore425Adapter` i `Model425Config` zostaną
usunięte. Nazwa Modelu 425 pozostanie tylko w prywatnej fabryce oficjalnego
drivera.

## 4. Architektura komunikacji

Przepływ jest następujący:

```text
Lake Shore Model 475
  -> RS-232/ASRL lub IEEE-488/GPIB
  -> jawnie skonfigurowana sesja PyVISA
  -> read-only connection proxy
  -> lakeshore.model_425.Model425(connection=proxy)
  -> LakeShore475Adapter
  -> DeviceController albo RecipeRunner
```

### 4.1. Sesja VISA

Profil przyjmuje jeden zasób VISA, na przykład:

- `ASRL3::INSTR` dla RS-232 lub konwertera USB-RS-232;
- `GPIB0::12::INSTR` dla IEEE-488.

Dla ASRL zostaną wymuszone parametry z podręcznika:

- domyślnie 57600 baud;
- 7 bitów danych;
- odd parity;
- 1 bit stopu;
- brak hardware handshaking;
- `CR LF` jako terminator zapisu i odczytu.

Profil może wybrać jeden z udokumentowanych baud rate: 9600, 19200, 38400
lub 57600. Pozostałe parametry 7O1 nie będą edytowalne.

### 4.2. Proxy tylko do odczytu

Nowy prywatny obiekt połączenia spełni wymagany przez oficjalny driver
interfejs `write`, `query`, `clear`:

- `query()` dopuści wyłącznie jawny whitelist zapytań Modelu 475;
- `write()` zawsze zgłosi `SafetyViolation`;
- `clear()` wykona wyłącznie transportowe czyszczenie bufora/sesji VISA;
- nie zostanie wysłane `*CLS` ani inne polecenie urządzenia;
- proxy będzie rejestrować ruch przez istniejący callback traffic;
- każde wyjście na transport zostanie ograniczone do maksymalnie
  20 komend na sekundę, czyli co najmniej 50 ms między komunikatami.

### 4.3. Oficjalny driver

Adapter utworzy:

```python
Model425(connection=read_only_connection)
```

To wykorzystuje oficjalnie opisany mechanizm alternatywnego połączenia.
Konstruktor sam wykonuje `*IDN?`; odpowiedź Modelu 475 ma wymagane cztery pola.
Adapter niezależnie zweryfikuje producenta, model i opcjonalny numer seryjny.

Oficjalny driver nie zamyka połączenia przekazanego przez użytkownika.
Własność sesji pozostanie dlatego w `LakeShore475Adapter`, który zamknie ją
w `disconnect()` także po częściowo nieudanym `connect()`.

## 5. Modele domenowe

`models.py` będzie definiował:

- `MeasurementMode`: `dc`, `rms`, `peak`;
- `FieldUnit`: `gauss`, `tesla`, `oersted`, `ampere_per_meter`;
- `ProbeType` z kodami 40, 41, 42, 50, 51 i 52;
- `GaussmeterConfig`;
- `GaussmeterSnapshot`;
- `GaussmeterReading`.

`GaussmeterSnapshot` zawiera:

- pięć pól odpowiedzi `RDGMODE?`;
- kod jednostki;
- kod zakresu;
- autorange;
- typ sondy;
- czas UTC odczytu.

`GaussmeterReading` zawiera:

- tryb i jednostkę próbki;
- `field_t` dla DC/RMS;
- `frequency_hz` dla RMS;
- `negative_peak_t` i `positive_peak_t` dla Peak;
- snapshot konfiguracji;
- czas UTC.

Pola niepasujące do trybu są `None`. Walidacja dataclass wymusi dokładnie
dozwolony zestaw wartości dla każdego trybu i skończoność wszystkich liczb.

## 6. Jednostki i spójność próbki

Kod `UNIT?` oznacza:

- `1`: gauss, przeliczany przez `1 G = 1e-4 T`;
- `2`: tesla, bez zmiany skali;
- `3`: oersted;
- `4`: ampere per meter.

Wyniki w Oe i A/m nie zostaną automatycznie przeliczone do tesli, ponieważ
takie przeliczenie wymaga założenia o relacji pomiędzy `H` i `B`. Pomiar
z kodem 3 lub 4 zakończy się czytelnym błędem i nie utworzy checkpointu.

Jedna próbka będzie wykonywana tak:

1. odczyt `UNIT?` i `RDGMODE?`;
2. komenda pomiarowa właściwa dla trybu;
3. ponowny odczyt `RDGMODE?` i `UNIT?`;
4. akceptacja tylko wtedy, gdy tryb i jednostka nie zmieniły się;
5. jedna pełna ponowna próba po wykryciu zmiany;
6. błąd po drugiej niespójnej próbie.

Pełny snapshot `RANGE?`, `AUTO?` i `TYPE?` będzie odświeżany przy pojedynczym
pomiarze receptury, po połączeniu oraz okresowo podczas live readout.

## 7. Zachowanie adaptera

`LakeShore475Adapter.connect()`:

1. otwiera i konfiguruje sesję VISA;
2. buduje proxy read-only;
3. tworzy oficjalny `Model425(connection=proxy)`;
4. parsuje IDN;
5. akceptuje producenta `LSCI` lub `LAKE SHORE`;
6. wymaga modelu `MODEL475`/`475`;
7. sprawdza numer seryjny, jeżeli profil tego wymaga;
8. odczytuje snapshot;
9. ustawia stan `VERIFIED` i capabilities.

Capabilities obejmą:

- `field_reading`;
- `dc`;
- `rms`;
- `peak`;
- `read_only`;
- `official_driver_bridge`.

Publiczne operacje adaptera:

- `connect`;
- `disconnect`;
- `emergency_off`;
- `read_snapshot`;
- `read_measurement`.

`emergency_off()` nie wyśle żadnej komendy. Dla podłączonego urządzenia
zachowa stan bezpiecznego urządzenia pomiarowego; zamknięcie lifecycle wykona
następnie `disconnect()`.

## 8. Konfiguracja i symulacja

`LakeShoreGaussmeterSettings.enabled` zmieni typ z `Literal[False]` na `bool`.
Profil zawiera:

- `enabled`, domyślnie `false`;
- `display_name`, domyślnie `Lake Shore 475`;
- `resource`;
- `visa_backend`;
- `timeout`;
- `baud_rate`;
- `expected_serial`;
- `require_serial_match`;
- `live_interval`, domyślnie `1 s`, minimum `500 ms`.

Model jest na stałe ograniczony do 475 i nie wymaga pola wyboru modelu.

Gdy urządzenie jest wyłączone lub nie ma zasobu, fabryka zwróci adapter
unavailable zamiast przerywać start aplikacji. Próba połączenia wyjaśni,
które ustawienie jest niekompletne.

Symulator użyje tego samego adaptera i deterministycznej fałszywej sesji.
Obsłuży odpowiedzi DC, RMS, Peak, unit/range/autorange/probe, overload,
błędy formatu i zmianę odpowiedzi pomiędzy kolejnymi zapytaniami.

## 9. Moduł i UI

`module.py` przestanie używać `_not_configured`. Będzie dostarczał:

- fabrykę adaptera fizycznego, symulowanego i unavailable;
- dispatcher operacji `read_snapshot` oraz `read_measurement`;
- `page_factory`;
- nazwę `Lake Shore 475`;
- capabilities tylko do odczytu.

Nowa strona `ui/page.py` będzie wzorowana wizualnie na stronie MOKE Box:

- nagłówek ze stanem urządzenia;
- aktualna wartość pola;
- osobne wartości peak positive/negative;
- częstotliwość w trybie RMS;
- kafle trybu, jednostki, zakresu, autorange i typu sondy;
- przycisk `Read now`;
- przełącznik live readout;
- okres odczytu z profilu;
- wykres historii pola, peaków i częstotliwości;
- timestamp ostatniego poprawnego pomiaru;
- banner błędu.

Nie będzie pól edycji konfiguracji urządzenia. QTimer nie wyśle nowego
żądania, dopóki poprzednie jest w toku. Błąd live zatrzyma timer, pozostawi
połączenie i pokaże przyczynę. Minimalny okres 500 ms oraz limiter transportu
chronią limit 20 komend/s.

Shell aplikacji, dashboard, pasek statusu, settings i lifecycle zostaną
rozszerzone o stabilny klucz `lakeshore_gaussmeter`. Zostanie dodana ikona
`lakeshore.svg`. Przypisanie wykrytego zasobu VISA będzie podlegało tym samym
uprawnieniom co Rigol, Keithley i Anritsu.

## 10. Receptury i wykonanie

Parser receptur dostanie typ:

```yaml
- id: field
  type: measure_lakeshore_field
```

Blok pojawi się w bibliotece tylko przy `enabled: true`. Nie ma konfiguracji
ani osi sweep, ale może być dzieckiem istniejących sweepów i repeatów.

Compiler:

- waliduje `enabled` i obecność resource;
- kompiluje pusty payload;
- liczy akcję jako jeden checkpoint;
- dodaje `lakeshore_gaussmeter` do `required_devices`;
- nie dodaje źródłowej akcji emergency-off.

`RunWorker` utworzy adapter tylko wtedy, gdy plan go wymaga. `RecipeRunner`
otrzyma opcjonalny `LakeShore475Adapter` i wykona `read_measurement()`.

Klucze wyników:

- `lakeshore.field_t`;
- `lakeshore.frequency_hz`;
- `lakeshore.negative_peak_t`;
- `lakeshore.positive_peak_t`;
- `lakeshore.mode_code`;
- `lakeshore.unit_code`;
- `lakeshore.range_code`;
- `lakeshore.autorange_enabled`;
- `lakeshore.probe_type_code`.

Klucze niepasujące do trybu nie są obecne w danej próbce. Publiczny writer
thaTEC uzupełni brakujące wartości `NaN`, zachowując wyrównanie checkpointów.

## 11. HDF5, monitor i wyniki

Nie jest potrzebna zmiana wersji prywatnego formatu HDF5:

- `measurements_json` już przechowuje dowolne skończone skalary;
- `ThatecHdf5Writer` dynamicznie tworzy wskaźniki scalar;
- IDN trafia do `device_idn_json` i `/devices`;
- capabilities trafiają do `capabilities_json`;
- Execution pokazuje generyczne `measurements_si`;
- Results odczytuje generyczne measurements.

`ThatecHdf5Writer._describe_quantity` otrzyma czytelne etykiety i jednostki
dla kluczy Lake Shore, aby publiczne wiersze nie używały surowych nazw.

Recovery zachowa wynik Lake Shore jak każdy inny skalarny checkpoint.
Wznowienie runu ponownie zweryfikuje urządzenie przed dalszym odczytem.

## 12. Błędy i bezpieczeństwo

Połączenie zostanie odrzucone dla:

- pustego IDN;
- nieoczekiwanego producenta;
- modelu innego niż 475;
- niezgodnego wymaganego numeru seryjnego;
- nieudanego snapshotu początkowego.

Próbka zostanie odrzucona dla:

- `OL` lub innego wskazania over-range;
- pustej odpowiedzi;
- liczby niefinitywnej;
- złej liczby pól;
- nieznanego kodu trybu lub jednostki;
- Oe lub A/m;
- zmiany trybu albo jednostki podczas obu prób.

Błąd transportu ustawi `FAULT` lub `UNKNOWN` zgodnie z możliwością
potwierdzenia stanu połączenia. Żaden błąd nie uruchomi komendy zmieniającej
konfigurację. Błąd pomiaru w recepturze nie zapisze częściowego checkpointu.

## 13. Testy automatyczne

Wymagane testy obejmują:

1. modele i walidację dozwolonych kombinacji DC/RMS/Peak;
2. konwersję G do T oraz brak konwersji Oe/A/m;
3. proxy whitelist, blokadę `write()` i limiter 20 komend/s;
4. rzeczywistą klasę z pakietu `lakeshore==1.10.0` nad fake connection;
5. identyfikację LSCI/MODEL475 i negatywne przypadki IDN/serial;
6. parsowanie `RDGMODE?`, `RANGE?`, `AUTO?`, `TYPE?`;
7. DC, RMS z częstotliwością i Peak z dwiema wartościami;
8. overload, pustą odpowiedź, NaN, infinity i błędny format;
9. zmianę jednostki/trybu, retry i odrzucenie drugiej niespójności;
10. disconnect po pełnym i częściowo nieudanym connect;
11. zgodność adaptera i symulatora;
12. ustawienia, wartości domyślne i unavailable adapter;
13. manifest, dispatcher i page factory;
14. stronę Qt offscreen: read now, live, in-flight guard i błąd;
15. dashboard, toolbar, settings, assignment i lifecycle;
16. parser oraz compiler nowego węzła;
17. required devices, liczbę checkpointów i brak źródłowej akcji shutdown;
18. simulated run dla DC, RMS i Peak;
19. HDF5 private, thaTEC/PyThat, reader i recovery;
20. pełne `pytest` oraz `ruff check app tests`.

## 14. Kwalifikacja HIL

Aktywacja `enabled: true` w zatwierdzonym profilu sprzętowym wymaga raportu HIL:

1. identyfikacja dokładnego egzemplarza i numeru seryjnego;
2. RS-232 57600/7O1 z CR LF albo używany interfejs GPIB;
3. odczyt G i T oraz zgodność przeliczenia;
4. jawne odrzucenie Oe i A/m;
5. DC na co najmniej dwóch zakresach;
6. RMS wraz z częstotliwością;
7. Peak positive/negative;
8. autorange on/off tylko jako readback;
9. rozpoznanie typu sondy;
10. over-range `OL`;
11. odłączenie kabla podczas read now i live;
12. utrata zasilania urządzenia;
13. stabilny live readout przy 500 ms;
14. przechwycony log ruchu potwierdzający brak komend zapisujących;
15. receptura z co najmniej jednym zewnętrznym sweepem;
16. zapis, odczyt i wznowienie HDF5;
17. safe shutdown całej stacji z dołączonym gaussometrem.

Do czasu tej kwalifikacji domyślny profil pozostaje `enabled: false`.

## 15. Zależności i kompatybilność

`pyproject.toml` i `requirements.txt` otrzymają dokładnie:

```text
lakeshore==1.10.0
```

Pin jest konieczny, ponieważ rozwiązanie zależy od zachowania publicznego
`Model425(connection=...)` i lifecycle połączenia użytkownika. Upgrade wymaga
ponownego uruchomienia testu oficjalnej granicy oraz skróconej kwalifikacji HIL.

Istniejące receptury i pliki ustawień pozostają poprawne dzięki domyślnemu,
wyłączonemu profilowi. Nowy typ receptury nie zmienia semantyki starych typów.

## 16. Kryteria akceptacji

Integracja jest gotowa do scalenia, gdy:

- aplikacja uruchamia się z wyłączonym i włączonym profilem Lake Shore;
- w symulacji można połączyć urządzenie, wykonać każdy tryb i użyć live;
- nowy blok receptury tworzy oraz zapisuje poprawny checkpoint;
- PyThat odczytuje publiczne wskaźniki Lake Shore;
- żaden publiczny interfejs modułu nie pozwala wysłać komendy zapisującej;
- test traffic potwierdza wyłącznie whitelisted queries;
- wszystkie testy i ruff przechodzą;
- dokumentacja profilu i operatora jest zaktualizowana;
- profil fizyczny pozostaje wyłączony do pozytywnego raportu HIL.
