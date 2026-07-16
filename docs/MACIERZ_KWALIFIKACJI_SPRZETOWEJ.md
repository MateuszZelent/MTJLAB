# Macierz kwalifikacji sprzętowej v1

Ten dokument jest bramką między testami automatycznymi a podaniem energii na
rzeczywisty DUT. Status **niekwalifikowany** oznacza, że profil pozostaje
`unverified` i `allow_output_enable: false`.

## Stan bieżący

| Urządzenie | Potwierdzone | Niekwalifikowane / blokada |
|---|---|---|
| Rigol DG1032Z, `DG1ZA172902039`, FW `00.01.08` | `*IDN?`, 2 kanały, OFF obu wyjść, podstawowa transakcja przy OFF, clamp do 2 mVpp | bieżący backend VISA zgłasza `VI_ERROR_SYSTEM_ERROR`; zaawansowane komendy i test z obciążeniem wymagają ponownego dostępu VISA |
| Keithley 2602A | adapter TSP, model DUT, compliance, range i rampy — testy fake/symulacyjne | wymagane realne `*IDN?`, kanały, sense, compliance i test obciążeniowy |
| Anritsu MS2830A | Live polling i single sweep `standard_scpi_opc` — testy symulacyjne | wymagane realne IDN/opcje, limit wejścia RF oraz kwalifikacja `INIT:CONT OFF` / `INIT:IMM` / `*OPC?` / `ABORT` |

Szczegóły pierwszego testu Rigola: [HARDWARE_TEST_RIGOL_DG1032Z.md](HARDWARE_TEST_RIGOL_DG1032Z.md).

## Zasady wykonania

1. Każdy test prowadzi się na sztucznym obciążeniu, nie na DUT.
2. Rejestruj datę, operatora, zasób VISA, pełny `*IDN?`, firmware, opcje i
   wynik każdego kroku.
3. Przed każdym połączeniem potwierdź fizyczny E-STOP/interlock oraz stan
   `OUTPUT OFF`.
4. Nie zmieniaj profilu na `approved` przed zaliczeniem wymaganych kroków i
   akceptacją osoby odpowiedzialnej.
5. Timeout, rozłączenie lub brak potwierdzenia OFF kończy krok jako **FAIL**;
   stan urządzenia jest wtedy `UNKNOWN`, nie `OFF`.

## Kolejność kwalifikacji

| Etap | Rigol | Keithley | Anritsu | Kryterium zaliczenia |
|---:|---|---|---|---|
| 1 | `*IDN?`, numer seryjny, `:OUTP1?`, `:OUTP2?` | `*IDN?`, `errorqueue.count` | `*IDN?` i opcje | zgodność z profilem, bez zmiany energii |
| 2 | wymuszenie OFF i odczyt zwrotny | OFF A/B i odczyt `source.output` | `ABORT` | adapter potwierdza bezpieczny stan |
| 3 | podstawowy SIN/SQU przy OFF, HighL/LowL readback | source I/V i compliance przy OFF, range/sense/NPLC | konfiguracja zakresu widma przy zdefiniowanym limicie RF | brak błędu urządzenia i zgodny readback |
| 4 | minimalny sygnał na sztucznym obciążeniu | pojedynczy punkt z compliance | `INIT:CONT OFF`, `INIT:IMM`, `*OPC?`, `TRAC?` | kompletny checkpoint HDF5 |
| 5 | krótki sweep 2×2, E-STOP, odłączenie USB | krótki sweep 2×2, compliance, E-STOP | single sweep per punkt, `ABORT`, utrata TCP/IP | `aborted`/`faulted` HDF5 czytelny; wyjścia OFF albo UNKNOWN |
| 6 | pełny 100×20 | pełny 100×20 | pełny 100×20, czas i stabilność | 2000 rekordów albo spójny częściowy plik |

## Dane do uzupełnienia przed zatwierdzeniem

- Keithley: model, serial, firmware, kanały, 2-wire/4-wire oraz bezpieczne
  limity I/V/P dla DUT.
- Rigol: minimalna impedancja DUT, wymagany rezystor szeregowy i potwierdzony
  zakres wszystkich używanych funkcji/firmware.
- Anritsu: model, opcje, maksymalna moc na złączu RF, tłumienie zewnętrzne i
  wewnętrzne, preamp oraz dokładny protokół synchronizacji sweepu.
- Stan fizycznego E-STOP/interlock oraz osoba odpowiedzialna za profil.

Po zakończeniu wypełnioną macierz należy zapisać obok danych stanowiska i
dopiero wtedy zatwierdzić profil w GUI.
