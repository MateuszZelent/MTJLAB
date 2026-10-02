# MOKE-Box → Kepco: końcowy audyt implementacji i gotowości stanowiska

**Późniejsza aktualizacja konfiguracji:** po tym audycie operator polecił
odblokować zapis. Lokalny profil ma obecnie zgodę programową dla VOUT2;
szczegóły i kopię poprzedniego stanu opisuje
[rejestr polecenia operatora](MOKE_BOX_OPERATOR_COMMISSIONING_AUTHORIZATION_2026-10-02.md).
Wpisy o wyłączonym zapisie poniżej dokumentują wcześniejszy stan audytu.
Fizyczna kwalifikacja pozostaje otwarta; zgoda programowa nie jest jej wynikiem.

Data: 2026-10-02. Audyt lokalnego kodu PyLab, źródeł LabVIEW, buildów i danych
BLS2. Bez połączenia z instrumentami, uruchomienia VI/EXE i zmiany wyjść.

## Decyzja

**Oprogramowanie przeszło wskazaną regresję. Fizyczne sterowanie elektromagnesem
pozostaje NIEZAKWALIFIKOWANE; audyt nie daje zgody na jego normalne uruchomienie.**

Można uruchomić aplikację w symulacji i ćwiczyć Voltage control, Live/Apply,
kalibrację i sweepy. Pierwszy test energetyczny wymaga osobnej kwalifikacji
stanowiskowej opisanej niżej. Nie stwierdzono w analizowanym zakresie usterki
oprogramowania wymagającej nowej poprawki, ale nie oznacza to dowodu braku
wszystkich błędów ani zatwierdzenia fizycznego toru.

Obecny `.config/settings.yml` zawiera:

```yaml
protocol_qualified: true
allow_vout_control: false
allowed_vout_channels: []
voltage_control:
  approved: false
  binding_id: ''
  qualification_reference: ''
```

`protocol_qualified` dotyczy komunikacji; nie jest zgodą na zmianę napięcia.
Fabryka fizycznego adaptera przy `allow_vout_control: false` nie przypisuje
profilu sterowania. Bez tego profilu nie można przygotować ani uzbroić rampy.
Ustawień i statusu kwalifikacji podczas audytu nie zmieniono.

## 1. Zakres i jakość dowodów

Ponowiono inwentaryzację rzeczywistych plików z
`C:\Users\Shark\Desktop\moke-box`, w tym projektu
`MOKE-Box_in_progress\project`, buildów i `Field calibration`.

- Odczytano 26 plików; wszystkie SHA-256 są identyczne z poprzednim audytem.
- SHA-256 `moke-box-template.llb`:
  `b10ab69251ab5dcf8041cf4413529bb3b691f455104a9adc419813f9dae45b61`.
- Nowa inwentaryzacja: [JSON](evidence/moke_labview_final_audit_2026-10-02.json).
- Metoda: odczyt XML, RSRC/zlib, nazw, stałych, tablic MCAL i eksportów.

To **analiza statyczna**, bez pełnego odtworzenia połączeń diagramu LabVIEW.
Nie porównano wykonywanych ramek PyLab z trace'em działającego VI ani EXE.
Wyniki testów kodeka potwierdzają implementację odtworzonego formatu; same
nie dowodzą równoważności wszystkich ścieżek oryginalnego programu.
Buildy EXE różnią się hashami; ich zgodność z dostępnymi źródłami pozostaje
niepotwierdzona. Wektory na połowie LSB i zachowanie firmware wymagają HIL.

Wcześniejszy [przegląd inżynieryjny](MOKE_BOX_ENGINEERING_REVIEW_2026-10-02.md)
zawiera historyczny opis luk sprzed implementacji. Aktualny stan opisują ten
audyt i [instrukcja operatora](MOKE_BOX_IMPLEMENTATION_AND_OPERATOR_GUIDE_2026-10-02.md).

## 2. Porównanie z LabVIEW

| Element | Dowód LabVIEW / BLS2 | Obecny PyLab i ocena |
|---|---|---|
| Nastawianie wyjścia | `set_VOUTn_direct.vi`, `number_to_msb_lsb.vi`, `send_command.vi` | Czterobajtowy SET_VOUT, nagłówek kanału, MSB/LSB i checksum; zgodne z rekonstrukcją protokołu |
| Przeliczenie napięcia | Stałe 3276.7, 3276.8, 32768 | Osobna skala dla dodatnich/ujemnych V; zero kodu `0x8000`; około 0.305 mV/LSB |
| Zakres | Kodek ±10 V; archiwalne nastawy ±4, ±6, ±6.1 V, inne stałe w VI | Zakres kodeka nie uprawnia do użycia takiego napięcia na elektromagnesie; obowiązuje zatwierdzony profil i bardziej restrykcyjny min/max operatora |
| Kanał | Zapisane THA wskazują logiczny VOUT2 | Domyślny kanał 2, wybór 0–7; zapis tylko na jednym kanale związanym z profilem. Historyczny zapis nie potwierdza aktualnego kabla |
| Tryby | `set_voltage_mother.vi`: Direct / Wait Time / Wait for Hall | Wyjście programujące jest napięciem. Usunięto mylący wybór Source mode. Kepco pozostaje w trybie prądowym; PyLab nie wysyła komendy zmiany jego trybu |
| Zmiana nastawy | Bezpośredni/timed zapis, stała 0.05 w timed VI | PyLab wymusza ograniczenie kroku, szybkości i czasu rampy oraz odczyt DAC po każdym kroku; nie jest to wierny port wszystkich czasów VI |
| Oczekiwanie | Wait Time albo stabilizacja przez Hall | Minimum profilu 2 s po docelowej nastawie, regulowany czas operatora. Nie zaimplementowano legacy Wait for Hall ani regulatora pola |
| Odczyt DAC | Rekordy AD5362 | 32 B, checksum, dozwolony typ/origin i każdy kanał dokładnie raz; tolerancja porównania 1 mV. To odczyt protokołu DAC, nie multimetru ani prądu Kepco |
| Kalibracja | MCAL, tabele, fitting i korekcje o niejednoznacznych jednostkach/znaku | Nowa kalibracja B(U_DAC) z referencją Lake Shore 475, dwiema gałęziami i surowymi danymi. Legacy MCAL nie jest automatycznie aktywowany |
| Podgląd pola | Historia i korekcje wpływają na wynik | B↑/B↓ są predykcjami kalibracji, bez ekstrapolacji. Dowolne ręczne ruchy nie gwarantują odtworzenia historii kondycjonowania |
| Zerowanie | VI zerowania napięcia | Rampa wyłącznie kwalifikowanego kanału do zera, także poza roboczym min/max. Nie jest komendą OUTPUT OFF Kepco |

W LLB zasób `set_voltage_mother.vi` zaczyna się od `0x7BD50`; zlib od
`0x7E9DC` zawiera `Set Mode`, `Direct`, `Wait Time`, `Wait for Hall`,
`Voltage Channel`. Zasób od `0x7F7F8` zawiera nazwy subVI nastawiania napięcia
i stabilizacji. `New_sub_VIs.zip` zawiera odwołania do kodeka i wysyłania
komendy w `Sub_vi_moke_voltage/set_VOUTn_direct.vi`.
Nazwy i stałe nie są dowodem wszystkich warunków wykonania diagramu.

## 3. Audyt zabezpieczeń oprogramowania

Prześledzono UI → plan SI → profil → adapter → TCP oraz compiler → runner →
finally i kalibracja → storage → aktywacja modelu.

| Zabezpieczenie | Wynik i granica |
|---|---|
| Kwalifikacja i zgoda | Fizyczne sterowanie wymaga enabled, endpoint, protocol_qualified, allow_vout_control, approved, identyfikatora powiązania, referencji kwalifikacji i dokładnie jednego dozwolonego kanału |
| Wartości i jednostki | Jawne V/mV, skończone wartości, uporządkowany zakres; wewnętrznie V. Kwantyzacja docelowego kodu nie rozszerza min/max |
| Prepare → arm → write | Prepare i arm nie zmieniają DAC. Adapter akceptuje wyłącznie kolejny punkt niezmienionej, uzbrojonej trajektorii |
| Live OFF | Edytor, Enter, strzałki i zwolnienie suwaka nie wysyłają nastawy; wymagany Apply voltage |
| Live ON | Debounce 400 ms, jeden worker/lease, tylko najnowsza oczekująca wartość. Nowa nastawa nie omija trwającej rampy i oczekiwania |
| Utrata uprawnienia | Zmiana kanału/zakresu/timingów/ustawień, receptura, kalibracja, rozłączenie, błąd, Stop i zero wyłączają Live i cofają lokalną zgodę |
| Rampa i czas | Ograniczony krok/slew/interval/deadline; minimum 2 s po nastawie. Symulacja jawnie pomija fizyczne opóźnienia; fake transport z prawdziwym zegarem testuje czas i przerwanie |
| Odczyt zwrotny | Po każdym zapisie oraz po końcowym oczekiwaniu. Rozbieżność/timeout przerywa; transportowy błąd zapisu nie jest automatycznie ponawiany |
| Stop i zamknięcie | Przerwanie bez oczekiwania na kolejkę GUI; zatwierdzona rampa do zera, obsługa aktywnego workera i końcowych lease'ów |
| Niepewny stan | Po utracie transportu kod nie deklaruje bezpiecznego OFF. Potwierdzenie DAC zero również pozostawia stan mocy/prądu Kepco UNKNOWN |
| Kalibracja | Dwie wyłączne rezerwacje, MODEL475/DC, stabilność konfiguracji i rozrzutu, pełny cykl kondycjonowania, zapis przed pierwszą zmianą DAC, zachowanie częściowego pliku i próba zerowania przy błędzie |
| Modele i dane | Oddzielne profile SIM/hardware, fingerprint toru, jawna aktywacja po przeglądzie, surowe dane i hash, brak ekstrapolacji |
| Sweep | Cała trajektoria sprawdzona przed wykonaniem; configure/arm/update/finally zero; dry-run nie zapisuje DAC |

Domyślny edytor ma `0 mV`. Całkowity zapis ma krok 100 mV, `0.00 V` krok
0.01 V. Precyzja UI nie zwiększa rozdzielczości DAC: np. 0.01 mV jest mniejsze
niż jeden kod DAC i może nie spowodować fizycznej zmiany. Rzeczywiste napięcie
wynika z kwantyzacji i odczytu. Wykres pokazuje ostatnie 180 s potwierdzonych
odczytów, a nie ciągły pomiar prądu/pola podczas każdego kroku rampy.

## 4. Otwarte kwestie blokujące normalne uruchomienie

1. **Niepotwierdzone powiązanie fizyczne.** Protokół MOKE nie daje pełnego IDN
   ani numeru seryjnego. Poprawna odpowiedź TCP nie dowodzi, że VOUT2 jest
   podłączony do właściwego wejścia Kepco. Wymagany zapis kanału, kabla,
   złącza, znaku, wzmocnienia i offsetu.
2. **Nieustalone fizyczne limity.** Przykładowe ±1 V profilu, ±0.5 V operatora,
   50 mV kroku i 1 V/s nie są zatwierdzonymi limitami cewki. Aplikacja nie
   mierzy ani nie wymusza bezpośrednio prądu, napięcia mocy, temperatury
   lub limitów sprzętowych Kepco. Bezpieczna obwiednia DAC musi wynikać
   z pomiaru toru i limitów magnesu, ze stosownym marginesem.
3. **Stabilność obciążenia indukcyjnego.** Operator podał BOP 72-6M, nie
   potwierdzony wariant 72-6ML ani rewizję/modyfikacje. Producent opisuje
   zależność stabilności od obciążenia, okablowania i sygnału programującego
   oraz osobny wariant L. Nie wynika z tego, że standardowy M jest zawsze
   nieodpowiedni; wynika konieczność kwalifikacji konkretnego układu.
   [Kepco BOP-004](https://www.kepcopower.com/support/bop004.pdf),
   [warianty dla elektromagnesów](https://www.kepcopower.com/bop-ind.htm).
4. **Brak niezależnego potwierdzenia bezpiecznego zatrzymania.** DAC zero
   nie potwierdza zaniku prądu ani pola. Przy zerwaniu TCP, zatrzymaniu
   procesu/PC lub awarii zasilania aplikacja nie gwarantuje zmiany ostatniego
   kodu DAC. Nie ma wykazanego niezależnego watchdog/interlock toru mocy.
   GUI E-STOP wymaga sprawnego procesu i komunikacji; nie zastępuje
   kwalifikowanego fizycznego zabezpieczenia układu indukcyjnego.
5. **Czas 2 s jest założeniem operatora.** Czasowe oczekiwanie nie jest
   pomiarem stabilizacji prądu/pola. Należy sprawdzić narastanie, opadanie,
   zmianę znaku i oscylacje dla planowanego zakresu.
6. **Referencja i kalibracja.** Potwierdzić rzeczywisty Lake Shore, sondę,
   DC, jednostki, orientację, pozycję i wzorcowanie. Archiwa BLS2 wskazują
   455, a nowy runner wymaga 475. Model 455 nie jest obsługiwany przez
   domniemanie zgodności. Stara kalibracja nie kwalifikuje aktualnego toru.

## 5. Warunki pierwszej próby stanowiskowej

Kolejność jest istotna. To plan kwalifikacji, nie wykonane pomiary.
Każdy etap ma zapis wyniku, osobę odpowiedzialną i ustalone kryteria przed próbą.

| Etap | Sprawdzenie | Warunek przejścia |
|---|---|---|
| A — identyfikacja | Tabliczka/revision Kepco i MOKE, właściwy kanał, wejście programowania, common, limit sprzętowy, charakterystyka cewki i fizyczne zabezpieczenie | Jednoznaczny tor i zdefiniowane bezpieczne I/V/thermal oraz sposób zatrzymania |
| B — DAC bez sterowania cewką | Przy torze mocy zabezpieczonym zgodnie z procedurą stanowiska: porównanie kodu/readback z multimetrem, zero/znak/gain/offset, wpływ restartu i utraty TCP | Zgodność protokołu z napięciem fizycznym i udokumentowane zachowanie awaryjne |
| C — kwalifikowana próba obciążeniowa | Z osobno zatwierdzonym wąskim profilem testowym i pomiarem I/V: małe nastawy, ograniczenia sprzętowe, kształt rampy, czas ustalenia i oscylacje | Wyniki mieszczą się w przyjętych limitach i kryteriach; brak niepożądanego wzbudzenia |
| D — zatrzymanie | Stop, zero, E-STOP, zamknięcie GUI i kontrolowana utrata komunikacji według procedury dla układu indukcyjnego | Niezależny pomiar i fizyczne zabezpieczenie potwierdzają wymagany stan, także gdy aplikacja nie może wysłać komendy |
| E — kalibracja i zatwierdzenie | Lake Shore/sonda, obie gałęzie, kondycjonowanie, powtarzalność, zapis HDF5 i aktywacja modelu | Zatwierdzony raport kwalifikacji i docelowy profil produkcyjny |

Nie ustawiać po prostu `approved: true`, aby ominąć te etapy.
Testowy profil energetyczny wymaga wcześniej zaakceptowanego zakresu i procedury;
nie wolno przyjmować historycznych ±6 V jako bezpiecznego punktu startowego.
Pierwsze kwalifikowane nastawy wykonywać przez Apply z Live OFF, aby każdą
zmianę porównać z niezależnym pomiarem. Live i pełne sweepy włączyć po zaliczeniu
prób rampy i zatrzymania. Kalibracja nie jest konieczna do samego sterowania
DAC, ale jest konieczna do wiarygodnego podglądu B(U).

## 6. Wykonana regresja

Wszystkie testy wykonano bez fizycznego sprzętu, w projektowym `.venv`:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_moke_protocol.py tests/test_moke_voltage_control.py tests/test_moke_calibration.py tests/test_moke_sweep_execution.py tests/test_device_modules.py tests/test_fluent_moke_field_workflow.py tests/test_quick_quantity_slider.py -q --basetemp scratch/pytest-moke-final-audit-am -o cache_dir=scratch/pytest-cache-moke-final-am
# 91 passed, 10 subtests passed in 84.71s

.venv/Scripts/python.exe -m pytest tests/test_adapters_and_runner.py -q -k 'moke or lakeshore' --basetemp scratch/pytest-moke-final-adapters-ao -o cache_dir=scratch/pytest-cache-moke-final-ao
# 2 passed, 108 deselected in 2.16s

python -m ruff check app tests
# All checks passed
.venv/Scripts/python.exe -m compileall -q app
# exit 0
```

Łącznie: **93 testy oraz 10 podtestów zaliczone**, bez błędów w tych zestawach.
GUI zweryfikowano po show/processEvents, w zwykłym oknie i węższym układzie;
testy zapisują zrzuty w katalogach scratch. To końcowa regresja wybranej ścieżki,
nie ponowne uruchomienie całego suite repozytorium. Wcześniejsze niezależne
problemy starych testów Keithley pozostają opisane w instrukcji operatora.

Nie wykonano pomiaru wyjścia multimetrem, prądu/oscylacji Kepco, zaniku prądu,
fizycznego E-STOP, trace'u pracującego LabVIEW ani kalibracji rzeczywistym
Lake Shore. Te pozycje pozostają otwartymi bramkami sprzętowymi.

## 7. Uzupełnienie po zgłoszeniu checksum mismatch przy Connect

Po powyższym audycie operator zgłosił błąd połączenia i podał, że wcześniej
korzystał z LabVIEW, które następnie zamknął. Wykonano diagnostykę **read-only**:
wyłącznie `READBACK_VOUT`, bez SET_VOUT, gain, zerowania czy zmiany profilu.

2026-10-02, seria od 17:10:45 do 17:10:46 UTC: osiem kolejnych odczytów
pojedynczej sesji dało identyczną poprawną odpowiedź:

```text
TX: 18 00 00 18
RX: 10 80 00 10 11 80 00 11 12 80 00 12 13 80 00 13
    14 80 00 14 15 80 00 15 16 80 00 16 17 80 00 17
```

Następnie rzeczywisty `MokeBoxAdapter` z `MokeBoxTcpTransport`, konfiguracją
`allow_vout_control=False` i bez profilu zapisu poprawnie wykonał connect,
read_vouts i disconnect. Odczyt protokołu wszystkich ośmiu kanałów: 0 V.
Nie jest to pomiar multimetrem, prądu cewki ani potwierdzenie OFF Kepco.
Seria potwierdza komunikację dla kodu zera; nie kwalifikuje odpowiedzi
dla niezerowych nastaw ani toru energetycznego.

Zgłoszonego błędu nie odtworzono. Związek z wcześniejszą sesją LabVIEW lub
nieoczekiwanymi danymi jest hipotezą, nie rozstrzygniętą przyczyną. Nie dodano
ignorowania checksum, automatycznej resynchronizacji ani ponawiania zapisu.

Rozszerzono diagnostykę błędu: pełne 32 bajty odpowiedzi oraz konkretny rekord,
oczekiwana i odebrana checksum. Negatywny test potwierdza zamknięcie sesji
po błędzie i brak poleceń zmieniających wyjście. Dodano narzędzie
`python -m tools.moke_readonly_probe HOST:PORT --queries 8`, które wykonuje
wyłącznie odczyty i kończy się na pierwszej błędnej odpowiedzi.

Po zmianie diagnostyki: 31 testów protokołu i rampy oraz 4 podtesty zaliczone;
`ruff check app tests tools/moke_readonly_probe.py` zaliczony.
Decyzja o niedopuszczeniu normalnego sterowania fizycznego pozostaje aktualna.

## 8. Korekta prezentacji stanu po połączeniu read-only

Zrzut operatora ujawnił błąd prezentacji: pasek globalny pokazywał
`Outputs off` przy stanie MOKE `VERIFIED`. Ten stan oznacza poprawny protokół,
nie potwierdzenie wyłączenia Kepco. Poprawiono obliczenie stanu paska:
połączony MOKE jest liczony jako źródło o nieznanym stanie mocy także przed
pierwszą zmianą napięcia. Nie zmieniono uprawnień ani sterowania sprzętem.

Przy braku profilu sterowania nagłówek, opis blokady i tooltipy wyjaśniają,
że połączenie jest read-only. Usunięto nieprecyzyjne zalecenie „Enable control
after choosing the channel and limits”. Wybranie kanału, min/max lub
przesunięcie suwaka nie zatwierdza fizycznego toru.

Skopiowane przyciski `Read device…` i `Measure channel` wcześniej wykonywały
identyczny odczyt wszystkich rejestrów. Otrzymały konkretne nazwy i różne
miejsca prezentacji: `Read all VOUT` otwiera tabelę 0–7,
`Read selected VOUT` odświeża bieżący kanał i historię na Voltage control.
Oba wysyłają tylko READBACK_VOUT; nie mierzą prądu ani pola.

Zestaw `test_fluent_moke_field_workflow.py` wraz z `test_fluent_shell.py`:
31 testów zaliczonych, 12 błędów inicjalizacji ogólnego okna z powodu dostępu
do rzeczywistej bazy inventory/katalogu `Documents/PyLab` w sandboxie.
Wszystkie 24 testy ścieżki MOKE w tym uruchomieniu zostały zaliczone.
Nowe przypadki używają własnych katalogów danych i adaptera read-only z
transportem w pamięci. Sprawdzają blokady, tooltipy, odczyty, zmianę zakładki,
brak SET_VOUT, stan UNKNOWN oraz rendering po show/processEvents.
Po ostatniej korekcie nagłówka ponowiono oba przypadki renderingu:
2/2 zaliczone, rzeczywisty jasny/ciemny motyw, okna 1360×880 i 980×880,
przyciski Live/Apply/zero mieszczą się w widocznym viewportcie.

Zrzuty aktualnej blokady: [jasny](evidence/moke_readonly_light_2026-10-02.png),
[ciemny](evidence/moke_readonly_dark_2026-10-02.png).

## Korekta po zgłoszeniu błędu VOUT i zbędnego Apply settings

Usunięto osobny przycisk `Apply settings` z ręcznego sterowania MOKE.
`Apply voltage` buduje świeży plan z aktualnego napięcia, min/max i czasu
oczekiwania, sprawdza profil oraz uprawnienia i dopiero uruchamia worker.
Konfiguracja i uzbrojenie jednorazowego planu nadal są osobnymi operacjami
wewnątrz adaptera. Edycja zakresu, czasu albo wybranie kanału nie wysyła
napięcia. Live pozostaje wyłączony przy starcie; po jego włączeniu kolejne
poprawne zmiany napięcia są stosowane automatycznie. Zmiana zakresu lub profilu,
STOP, rezerwacja przez receptę i błąd nadal wyłączają Live.

Dotychczas adapter wysyłał `SET_VOUT` i natychmiast `READBACK_VOUT`, uznając
pierwszą niezgodną odpowiedź za błąd. SET nie ma osobnego ACK. Dodano odstęp
25 ms przed odczytem na ścieżce fizycznej oraz ograniczone odpytywanie
poprawnych, lecz niezgodnych odczytów przez maksymalnie 250 ms, również w ramach
deadline rampy. Nie ponawia się SET. Tolerancja pozostaje 1 mV, granice
napięcia i parametry rampy pozostają bez zmian. Odczyt poza zakresem stanowiska
od razu zatrzymuje potwierdzanie. Uszkodzona ramka, niepełny odczyt albo timeout
transportu zamykają niepewną sesję bez dalszego odpytywania i bez ponawiania
zapisu. Trwała niezgodność prawidłowej odpowiedzi uruchamia dotychczasową
ścieżkę potwierdzonego DAC zero.

Komunikat niezgodności zawiera teraz kanał, napięcie żądane dla danego kroku,
odczytane napięcie, różnicę, tolerancję i osiem odczytów VOUT. Dotychczasowy log
operatora nie zawierał tych wartości, więc nie rozstrzyga, czy przyczyną na
urządzeniu był opóźniony odczyt, ignorowany zapis czy problem powiązania kanału.
Nowe okno potwierdzania jest zmianą programową, nie wynikiem kwalifikacji czasu
odpowiedzi hardware. Nie połączono się z fizycznym urządzeniem i nie wysyłano
do niego komend.

Regresja obejmuje `test_moke_voltage_control.py`, `test_moke_protocol.py`,
`test_moke_calibration.py`, `test_moke_sweep_execution.py` oraz
`test_fluent_moke_field_workflow.py`. Nowe przypadki sprawdzają opóźniony odczyt
bez ponowienia SET, ignorowany zapis z diagnostyką i potwierdzonym zerem,
checksum/timeout, anulowanie podczas potwierdzania, aktualne granice i czas,
odmowę uprawnień oraz świadome ponowienie Apply po błędzie Live.
Rendering sprawdzono po show/processEvents w jasnym i ciemnym motywie,
w panelu 1360×880 i 980×720 oraz pełnym oknie Fluent 1360×880. Zrzuty:
`scratch/moke-direct-apply-light.png`, `scratch/moke-direct-apply-dark.png`,
`scratch/moke-direct-apply-full-shell.png`.

Ruff dla E4/E7/E9/F przechodzi w całych `app` i `tests`. Pełny zestaw reguł
aktywny w środowisku zgłasza 1217 problemów, w tym w niezmienianych modułach
audit, Anritsu i Keithley; nie wykonano zbiorczego automatycznego formatowania.

## Przyczyna potwierdzona nową odpowiedzią niezerowego VOUT

W czasie poprawiania przepływu operator dostarczył zrzut kolejnego błędu Connect
z pełną odpowiedzią fizycznego urządzenia:

```text
10 80 00 10 11 80 00 11 12 82 8f 2a 13 80 00 13
14 80 00 14 15 80 00 15 16 80 00 16 17 80 00 17
```

Kanał 2 ma kod `0x828F`, około +0.199896 V. Otrzymana parity `0x2A` jest zgodna
z `(header ^ (MSB << 1) ^ (LSB << 2)) & 0xFF`. Dotychczasowe dodawanie dawało
`0x52` i błędnie odrzucało prawidłową ramkę. Ta sama funkcja budowała sumy
kontrolne wysyłanych komend, więc pomyłka obejmowała również SET_VOUT.
Odrzucanie tych zapisów przez urządzenie jest wyjaśnieniem wcześniejszej
niezgodności odczytu, ale nie przeprowadzono nowego testu zapisu na hardware.

Poprawiono wspólny kodek na XOR. Nie akceptuje on obu algorytmów i nadal
odrzuca uszkodzone ramki. Literalna odpowiedź ze zrzutu jest niezależnym
wektorem regresji Connect/read_vouts, bez żadnego SET. Osobny test sprawdza,
że SET_VOUT2(+0.2 V) koduje tę samą ramkę `12 82 8F 2A`, a arytmetyczna
ramka `12 82 8F 52` jest odrzucana. Zaktualizowano również wektory dla ±1 V
i żądania 100 próbek oraz wcześniejsze raporty zawierające błędną formułę.

Poprzednie testy generowane przez ten sam kodek były niewystarczającym
dowodem. Rzeczywiste odczyty zera z sekcji 7 pasują zarówno do dodawania,
jak i XOR; nie kwalifikowały algorytmu dla niezerowych payloadów.
Nowa ramka zastępuje wcześniejszą nierozstrzygniętą hipotezę problemu sesji
LabVIEW. W tej korekcie nie uruchamiano LabVIEW ani nie komunikowano się
z fizycznym instrumentem.

Wynik końcowy po korekcie XOR: pięć wymienionych zestawów MOKE — 83 testy
i 4 podtesty zaliczone (126.64 s). Dodatkowo przypadki MOKE w
`test_adapters_and_runner.py` i `test_device_modules.py` — 4 testy i 6 podtestów
zaliczone. Łącznie 87 testów i 10 podtestów, w tym regresja literalnej
odpowiedzi operatora i rendering Fluent. Ruff E4/E7/E9/F zaliczony.

## Live: ciągłość przeciągania i zachowanie stanu panelu

Po zgłoszeniu „suwak puszcza” odtworzono dwa błędy interfejsu. W czasie
obsługi rampy `_refresh_controls` najpierw wyłączał wszystkie wejścia, w tym
suwak i pole napięcia, a następnie ponownie włączał edycję Live. Każde takie
odświeżenie generowało EnabledChange na aktywnej kontrolce. Dotychczasowe
testy sprawdzały wyłącznie jej końcowy stan enabled.

Teraz wejścia napięcia otrzymują od razu swój docelowy stan. Suwak i edytor
pozostają stale aktywne podczas ręcznej rampy Live. Kanał, zakres i parametry
kalibracji nadal są zablokowane podczas pracy. Wyłączenie Live, STOP, błędy
i przejęcie sterowania przez receptę zachowują poprzednie blokady. Nadal
działa jeden worker i przechowywany jest tylko ostatni poprawny cel.

Po każdej rampie odczyt profilu przebudowywał ponadto listę zapisanych
kalibracji i resetował jej wybór. Niezmieniony profil jest nadal sprawdzany
przez adapter i porównywany z ustawieniami, ale nie powoduje rekonstrukcji
formularza ani ponownego wczytywania katalogu kalibracji. Katalog odświeża
się przy połączeniu lub zmianie profilu oraz po pracy kalibracji. Jeśli
wybrany model nadal istnieje, odświeżenie zachowuje jego wybór.

Nowe regresje używają pokazanego panelu 1360×880 i rzeczywistych zdarzeń
myszy na uchwycie Fluent. Test przed poprawką wykazał chwilowe wyłączenia
kontrolek. Po poprawce przycisk myszy pozostaje trzymany przez start rampy,
jej zakończenie i aktualizację profilu; dalszy ruch ustawia nową wartość.
Sprawdzane są również brak tych wyłączeń, ostatni cel zamiast kolejki
pośrednich wartości, brak dodatkowego zapisu po puszczeniu uchwytu,
fokus/zaznaczenie edytora i zachowanie wybranego modelu po dwóch rzeczywiście
zapisanych kalibracjach. Regresja wyboru modelu również została odtworzona
przed poprawką.

Zrzuty z przeciągania podczas zajętego workera obejrzano w obu motywach:
`scratch/moke-live-held-slider-light.png` i
`scratch/moke-live-held-slider-dark.png`. Nie zmieniano kodeka, limitów ani
ramp adaptera; nie komunikowano się z fizycznym instrumentem.
Ruff E4/E7/E9/F przechodzi. Pełny lint pozostaje przy dotychczasowych 1217
zgłoszeniach repozytorium.

Pełny zestaw pięciu testowanych plików MOKE po poprawce Live: 87 testów
i 4 podtesty zaliczone w 146.76 s. Ostrzeżenia pochodzą z użycia przestarzałego
`QMouseEvent.pos()` wewnątrz zainstalowanego QFluent Slider, nie są błędami
testów. Pokazanie pełnego okna Fluent, normalny i węższy panel, ręczne Apply,
Live OFF, błędne dane, zmiany profilu, STOP, kalibracja i odczyt read-only
pozostają objęte tym zestawem.
