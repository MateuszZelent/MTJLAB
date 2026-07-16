# Test sprzętowy — Rigol DG1032Z

Data: 2026-07-16  
Tryb: wyłącznie odczyt + bezpieczne połączenie adaptera (`Output OFF`)

## Zidentyfikowane urządzenie

```text
Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08
```

Zasób VISA:

```text
USB0::0x1AB1::0x0642::DG1ZA172902039::INSTR
```

Profil `.config/settings.yml` jest teraz przypięty do tego numeru seryjnego. Każde połączenie przez adapter produkcyjny wykonuje `:OUTP1 OFF` oraz `:OUTP2 OFF`; profil nadal ma stan `unverified`, więc nie pozwala włączyć wyjścia.

## Wynik zapytań SCPI

| Zapytanie | Odpowiedź |
|---|---|
| `*IDN?` | `Rigol Technologies,DG1032Z,DG1ZA172902039,00.01.08` |
| `:SYST:VERS?` | `1999.0` |
| `:SYST:CHAN:NUM?` | `2` |
| `:OUTP1?` | `OFF` |
| `:OUTP2?` | `OFF` |
| `:SOUR1:FUNC?` | `SIN` |
| `:SOUR2:FUNC?` | `SIN` |

Stan CH1 odczytany bez zmiany konfiguracji:

```text
częstotliwość: 1000 Hz
HighL:          2.5 V
LowL:          -2.5 V
amplituda:      5 Vpp
offset:         0 V
wyjście:        OFF
```

## Potwierdzenie adaptera

Nowy `RigolAdapter` pomyślnie:

1. otworzył zasób VISA;
2. odczytał i zwalidował tożsamość oraz numer seryjny;
3. przeszedł do stanu `verified`;
4. wykonał bezpieczne wymuszenie OFF dla obu kanałów;
5. zamknął sesję.

Nie wykonano: `*RST`, konfiguracji przebiegu, triggera, burst, modulacji ani `Output ON`.

## Test zapisu przy OUTPUT OFF

Wykonano bezpieczną transakcję przez `RigolAdapter`:

```text
CH1: SQU, 1 kHz, żądane HighL = 1 mV, LowL = 0 V, LOAD = HIGHZ
```

Wynik odczytu zwrotnego:

```text
OUTPUT: OFF
FUNC:   SQU
FREQ:   1 kHz
HighL:  1 mV
LowL:  -1 mV
Duty:   50 %
LOAD:   HIGHZ
```

Firmware wymusił minimalne `Vpp = 2 mV`, zmieniając LowL z żądanego `0 V` na `-1 mV`. To wykryto bez włączania wyjścia. Produkcyjny adapter po każdej konfiguracji odczytuje teraz funkcję, częstotliwość, HighL, LowL oraz stan output i odrzuca ciche zaokrąglenie lub clampowanie.

Profil i przykładowa receptura zostały skorygowane do osiągalnego ustawienia `HighL = 1 mV`, `LowL = -1 mV`; minimalne `amplitude_vpp` CH1 wynosi teraz `2 mV`.

## Następny bezpieczny test zapisu

Po potwierdzeniu przez operatora można wykonać test przy nadal wyłączonym wyjściu:

```text
CH1: SQU, 1 kHz, HighL = 1 mV, LowL = -1 mV, LOAD = HIGHZ
```

Ten krok zmieni konfigurację generatora, ale nie dostarczy energii do DUT. Włączenie wyjścia wymaga osobnego zatwierdzenia profilu, deklaracji impedancji DUT oraz jawnej akcji ARM/Output ON.

## Aktualna blokada transportu VISA

Podczas późniejszej, pasywnej próby wykrycia w tym samym środowisku Windows
backend systemowy VISA zwrócił `VI_ERROR_SYSTEM_ERROR (-1073807360)`, a
`pyvisa-py` nie wykrył żadnego zasobu USB. Odczyt PnP został zablokowany przez
uprawnienia systemowe. Nie wysłano w tej próbie żadnej komendy SCPI.

`pyvisa-info` potwierdza obecność NI-VISA i backendu PyUSB/libusb1, jednak
bezpośrednie otwarcie znanego zasobu przez `@py` zwraca `ValueError: No device
found`. To wskazuje na problem widoczności/sterownika USB, a nie brak pakietu
Python. Próba nie otworzyła sesji, więc również nie wysłała `*IDN?`.

Przed następnym testem kwalifikacyjnym należy przywrócić działanie sterownika
VISA/USB (np. przez ponowne podłączenie urządzenia, sprawdzenie NI-VISA/Rigol
UltraSigma i zamknięcie aplikacji blokującej zasób). Dopiero po udanym,
read-only `*IDN?` można kontynuować testy na sztucznym obciążeniu.
