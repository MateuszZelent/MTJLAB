# Audyt gotowości produkcyjnej Lab Control / Sweeper

**Data audytu:** 2026-07-18  
**Werdykt:** **NO-GO — oprogramowanie nie jest gotowe do wdrożenia produkcyjnego**  
**Ocena etapu:** zaawansowana wersja laboratoryjna / pre-production beta  
**Zakres:** Sweeper, UI, obsługa wartości i jednostek, uruchamianie wyjść,
bezpieczeństwo, zapis danych oraz integracja urządzeń.

## 1. Podsumowanie zarządcze

Sweeper ma rozbudowaną i dobrze przetestowaną bazę programową, ale **nie jest
ukończony w 100%**. Nie można również potwierdzić produkcyjnej gotowości
wszystkich urządzeń ani bezpiecznego uruchamiania ich wyjść na fizycznym
stanowisku.

Najważniejsze przyczyny decyzji NO-GO:

1. Brak zakończonej i podpisanej kwalifikacji HIL na docelowym sprzęcie.
2. Aktywny profil stanowiska ma stan `unverified`; brakuje części zasobów VISA,
   numerów seryjnych i limitów RF.
3. Wszystkie dostarczone receptury YAML są obecnie blokowane przez profil
   Anritsu, w którym akwizycja jest wyłączona.
4. Ręczna ścieżka Rigola pozwala w symulacji wykonać `ARM` i `OUTPUT ON` przy
   profilu `unverified`, mimo że UI pokazuje profil jako `LOCKED`, a receptury są
   w tym stanie blokowane.
5. Parser jednostek ma potwierdzone błędy dla `Ω`, `kΩ` i `µT`.
6. Model gotowości stacji nie uwzględnia MOKE Box ani Lake Shore i nie rozpoznaje
   RF OUTPUT ON generatora Anritsu.
7. MOKE Box udostępnia w UI operacje, których protokół lub funkcja wykonawcza
   nie są zakwalifikowane; oś pola w Sweeperze jest widoczna, lecz kompilator
   celowo ją odrzuca.
8. Lake Shore 475 jest właśnie aktywnie integrowany. Kod, UI, runner i testy
   zmieniały się podczas audytu, więc nie istnieje zamrożony kandydat wydania.
9. Środowisko zgłasza ostrzeżenie możliwej niezgodności binarnej NumPy, a
   środowisko wykonawcze nie jest odtwarzalne z lockfile.

Wniosek: bezpieczne mechanizmy programowe są na dobrym poziomie, ale ich
obecność nie zastępuje kwalifikacji fizycznego toru pomiarowego, wyjść,
okablowania, E-STOP i zachowania po błędach transportu.

## 2. Stan kodu i wyniki weryfikacji

W czasie audytu repozytorium było równolegle modyfikowane. Ostatni zaobserwowany
commit bazowy to:

```text
3b73567 feat: implement LakeShore 475 Gaussmeter driver and MokeBox adapter with associated unit tests.
```

Po tym commicie w drzewie roboczym nadal znajdowały się liczne zmiany i nowe
pliki, między innymi w:

- `app/devices/lakeshore_gaussmeter/`;
- `app/engine/compiler.py` i `app/engine/runner.py`;
- `app/ui/run_worker.py` i `app/ui/shell/main_window.py`;
- `app/storage/thatec_writer.py`;
- `pyproject.toml` i `requirements.txt`;
- testach oraz plikach recovery receptur.

Z tego powodu wynik testów opisuje konkretny chwilowy snapshot, a nie ostateczny,
niezmienny kandydat wydania.

### Wyniki automatyczne

Na jednym z końcowych snapshotów:

```text
376 passed, 73 warnings, 34 subtests passed in 161.18s
```

Dodatkowo:

- `ruff check app tests` — PASS na późniejszym snapshotcie;
- `compileall -q app tests` — PASS;
- `git diff --check` — PASS.

To jest mocny wynik dla warstwy programowej. Nie stanowi jednak zgody
produkcyjnej, ponieważ pliki zmieniały się również po pełnym uruchomieniu
testów.

### Ostrzeżenia środowiska

Testy zapisu HDF5/PyThat zgłaszają:

```text
RuntimeWarning: numpy.ndarray size changed, may indicate binary incompatibility
```

Występują również liczne ostrzeżenia `DeprecationWarning` z warstwy
xarray/netCDF4 przy NumPy 2.5. W osobnym teście diagnostycznym podniesienie
`RuntimeWarning` do błędu przerwało zamknięcie/walidację pliku przez PyThat.
Ryzyko dotyczy powtarzalności środowiska i integralności końcowego zapisu.

Projekt ma wersję `0.1.0`. Większość zależności ma szerokie zakresy wersji.
Nie znaleziono workflow CI, lockfile środowiska wykonawczego ani gotowego,
zweryfikowanego artefaktu instalacyjnego.

## 3. Sweeper

### Elementy dojrzałe

- kompilacja receptur do jawnego planu wykonania;
- statyczna kontrola kolejności konfiguracja → ARM → OUTPUT ON;
- jednorazowe i wygasające uzbrojenie wyjść;
- limity liczby punktów i akcji;
- wielowymiarowe receptury przez zagnieżdżone węzły;
- anulowanie kosztownego generowania punktów;
- wykonywanie w osobnych wątkach Qt;
- checkpointy, recovery, HDF5 oraz walidacja PyThat;
- testy 100 × 20, 10 × 100 i wielowymiarowego round-trip danych;
- końcowe wyłączenie urządzeń, watchdog oraz obsługa E-STOP w testach
  programowych i symulacyjnych.

### Braki

1. `moke_box.field_target` jest dostępny w rejestrze parametrów Sweepera, ale
   `configure_moke_box` jest bezwarunkowo odrzucany przez kompilator, ponieważ
   nie ma zakwalifikowanej funkcji przenoszenia pole → nastawa sprzętowa.
2. Lake Shore 475 ma nowy odczyt tylko do odczytu, ale integracja była nadal
   zmieniana podczas audytu i nie ma fizycznego HIL.
3. Nie wszystkie funkcje dostępne na stronach ręcznych urządzeń mają
   odpowiadające im, zakwalifikowane osie lub akcje Sweepera.
4. Każda z czterech dostarczonych receptur w `recipes/*.yml` nie kompiluje się
   przy bieżącym profilu:

```text
SafetyViolation: Anritsu acquisition is locked by the safety profile.
```

5. Nie ma zamrożonego testu akceptacyjnego całego Sweepera na rzeczywistych
   urządzeniach, obejmującego co najmniej 1 punkt, 2 × 2, awarie, pełny przebieg
   100 × 20 i soak test.

**Ocena Sweepera:** baza programowa jest zaawansowana, ale produkt nie jest
ukończony w 100% i nie jest gotowy do uruchomienia produkcyjnego.

## 4. Wartości i jednostki

Parser prawidłowo obsługuje między innymi:

- jawne jednostki ASCII, np. `1 mV`, `50 ohm`, `1 uT`;
- przecinek dziesiętny, np. `1,5 mA`;
- notację naukową, np. `1e3 Hz`;
- odrzucanie wartości bez jednostki na granicy bezpieczeństwa;
- odrzucanie `NaN`, nieskończoności i niezgodnego wymiaru.

Potwierdzone błędy:

| Wpis | Oczekiwane | Wynik |
|---|---:|---|
| `50 Ω` | 50 Ω | odrzucone jako nieznana jednostka |
| `1 kΩ` | 1000 Ω | odrzucone jako nieznana jednostka |
| `1 µT` | 1e-6 T | odrzucone jako nieznana jednostka |
| `1 uT` | 1e-6 T | poprawne |

Przyczyny w kodzie:

- klucze zawierają wielkie `Ω`, natomiast parser wykonuje `.lower()`, zmieniając
  symbol na małe `ω`;
- alias mikrotesli jest zapisany jako uszkodzony tekst `Âµt`;
- wpis `mΩ` mapuje się na mnożnik `1e6` i etykietę `Mohm`, co jest niezgodne ze
  standardowym znaczeniem `mΩ` jako milioma.

**Ocena:** nie można stwierdzić, że UI nie ma problemów z jednostkami.

## 5. Uruchamianie OUTPUT i bezpieczeństwo

### Elementy pozytywne

- Keithley wymaga zatwierdzonego profilu, konfiguracji, compliance i świeżego
  ARM przed OUTPUT ON.
- Generator Anritsu wymaga zatwierdzonego profilu, włączonego uprawnienia RF,
  konfiguracji z readbackiem i świeżego ARM.
- Rigol, Keithley i Anritsu wymuszają lub weryfikują OUTPUT OFF przy połączeniu,
  rozłączeniu albo ścieżce błędu zgodnie ze swoim zakresem.
- Runner próbuje wyłączyć wszystkie wymagane urządzenia także po awarii jednego
  z nich.
- Niejednoznaczny błąd wyłączenia prowadzi do stanu `UNKNOWN`, a nie do
  fałszywego potwierdzenia bezpiecznego stanu.

### Krytyczna niespójność Rigola

Adapter Rigola ustawia `profile_locks_outputs=False`. Bieżący profil ma:

```yaml
profile:
  state: "unverified"

devices:
  rigol:
    safety:
      allow_output_enable: true
```

Diagnostyka na symulatorze, z zachowaniem stanu `unverified`, zakończyła się:

```text
ARM_OK
SET_OUTPUT True output_on
```

UI blokuje uruchomienie receptury i pokazuje `Profile: LOCKED`, ale strażnik
operacji ręcznych sprawdza rolę i zdrowie audytu, nie zatwierdzenie profilu.
Oznacza to, że polityka ręcznego OUTPUT Rigola jest inna niż polityka receptur
i komunikat widoczny dla operatora.

Istniejący test o nazwie
`test_rigol_cannot_enable_output_with_unapproved_profile` nie wykonuje ARM przed
próbą włączenia, więc jego wyjątek nie dowodzi blokady z powodu profilu.

Przed produkcją trzeba jednoznacznie zdecydować i przetestować, czy:

- profil `unverified` blokuje wszystkie nowe operacje energetyzujące; albo
- praca ręczna Rigola jest osobnym, jawnie opisanym trybem z osobnym statusem,
  zatwierdzonymi limitami i procedurą HIL.

Obecne zachowanie jest zbyt niejednoznaczne do akceptacji produkcyjnej.

## 6. Model gotowości stacji i komunikaty UI

`evaluate_station_readiness()` sprawdza tylko:

```text
rigol, keithley, anritsu
```

Nie sprawdza wymaganych urządzeń `moke_box` ani `lakeshore_gaussmeter`.
Plan wymagający jednego z nich może zatem nie otrzymać właściwego wpisu
gotowości urządzenia.

Detekcja planu energetyzującego rozpoznaje tylko:

```text
set_rigol_output, set_keithley_output
```

Nie rozpoznaje `set_anritsu_sg_output`. Dla planu włączającego RF panel
gotowości może więc wyświetlić informację „Plan contains no OUTPUT ON action”.
Kompilator nadal sprawdza RF, więc nie jest to bezpośrednie obejście interlocka,
ale jest to mylący komunikat operatora przy operacji wysokiego ryzyka.

## 7. Ocena urządzeń

| Urządzenie | Stan warstwy programowej | Dowód fizyczny | Werdykt |
|---|---|---|---|
| Rigol DG1032Z | Rozbudowany adapter, readback, ARM, estymacja prądu/mocy, wymuszanie OFF | Tylko częściowa próba OFF; później błąd VISA; brak pełnego testu z obciążeniem i pełnej macierzy HIL | **Nieprodukcyjny** |
| Keithley 2602A | Compliance przed ON, zakresy, sense, NPLC, rampy, runtime trip, dobre testy symulacyjne | Brak zasobu VISA, potwierdzonego IDN/serialu, testu obciążeniowego i podpisanego HIL | **Nieprodukcyjny** |
| Anritsu MS2830A | Single sweep, OPC, ABORT, RF OFF, SG ARM i readback mają mocne testy | Brak realnego IDN/opcji i pełnych limitów RF; akwizycja i SG są wyłączone | **Nieprodukcyjny** |
| MOKE Box | Zakwalifikowana część odczytu VOUT/Hall; zapis VOUT wyłączony | Funkcja pola i bezpieczne field-off niezakwalifikowane; wielostrumieniowa ramka niepotwierdzona | **Tylko diagnostyka read-only; nieprodukcyjny jako aktuator** |
| Lake Shore 475 | Nowy adapter read-only, whitelist zapytań, blokada zapisów, testy symulacyjne | Integracja nadal w toku; brak docelowego profilu, fizycznego IDN/proby i HIL | **Nieprodukcyjny** |
| Lake Shore 425 | Granica architektoniczna/opcjonalny sterownik, bez kompletnej kwalifikacji produktu | Brak HIL i pełnej integracji produkcyjnej | **Nieprodukcyjny** |

### MOKE Box

UI zawiera przycisk `Acquire streams`, natomiast rekonstrukcja protokołu nadal
nie potwierdza kompletnego formatu wielostrumieniowej odpowiedzi. Wyjściowe
sterowanie VOUT jest domyślnie zablokowane (`allow_vout_control: false`).
`emergency_off()` dla tego modułu zamyka sesję i oznacza stan jako nieznany; nie
jest to zakwalifikowane fizyczne wyzerowanie pola.

### Lake Shore 475

Nowy adapter jest zaprojektowany konserwatywnie jako read-only: proxy dopuszcza
tylko białą listę zapytań i odrzuca zapisy. To dobry kierunek. Nie można jednak
uznać go za produkcyjny, dopóki nie przejdzie kwalifikacji dokładnego modelu,
proby, jednostek B/H, trybów DC/RMS/peak, timeoutów i awarii transportu na
fizycznym urządzeniu.

## 8. Profil stanowiska i HIL

Bieżący profil zawiera między innymi:

```yaml
profile:
  state: "unverified"

devices:
  keithley:
    connection:
      resource:

  anritsu:
    connection:
      resource:
    safety:
      acquisition_allowed: false
      signal_generator_output_allowed: false
      rf_input:
        max_expected_power_at_connector:
        minimum_internal_attenuation:

  moke_box:
    allow_vout_control: false
```

Jedyny znaleziony lokalny raport kwalifikacyjny ma:

```text
overall_status: simulation_passed
simulation: true
```

Przypadki połączenia Rigola, Keithleya i Anritsu zostały w nim pominięte z
powodu braku konfiguracji urządzeń. Dokumentacja projektu sama stwierdza:
„Passing a simulation is not hardware qualification.”

Macierz kwalifikacji potwierdza:

- Rigol: częściowy test OFF i późniejszy błąd VISA;
- Keithley: wymagane realne IDN, kanały, sense, compliance i obciążenie;
- Anritsu: wymagane realne IDN/opcje, limity RF i kwalifikacja sekwencji
  INIT/OPC/ABORT;
- pełna macierz HIL i podpis odpowiedzialnej osoby nadal są wymagane.

## 9. UI

Automatyczne testy pokrywają znaczną część:

- formularzy i limitów;
- zmian jednostek i notacji naukowej;
- potwierdzeń OUTPUT;
- błędów połączenia;
- responsywnego układu Sweepera;
- wątków roboczych i anulowania;
- podglądu wyników i recovery;
- ról operator/engineer/service.

Nie stwierdzono podstawowego problemu uniemożliwiającego start UI w testach
automatycznych. Nie można jednak podpisać stwierdzenia „UI działa dobrze” dla
produkcji, ponieważ:

1. istnieją błędy jednostek;
2. panel gotowości pomija urządzenia i RF OUTPUT;
3. stan `LOCKED` nie odpowiada ręcznej polityce Rigola;
4. UI MOKE pokazuje operację wielostrumieniową przed kwalifikacją protokołu;
5. Lake Shore był integrowany równolegle podczas audytu;
6. nie wykonano udokumentowanego UAT z operatorem i fizycznymi urządzeniami;
7. headless QA nie odtwarzało wiarygodnie docelowych fontów i wyglądu
   produkcyjnej stacji Windows.

## 10. Klasyfikacja ryzyk

### P0 — blokady wydania

1. Brak podpisanego fizycznego HIL dla każdego urządzenia i całej stacji.
2. Profil `unverified`, brakujące zasoby i limity oraz zablokowany Anritsu.
3. Niespójność ręcznego OUTPUT Rigola z blokadą profilu i komunikatem UI.
4. Brak zamrożonego, czystego i ponownie zweryfikowanego commita wydania.
5. Dostarczone receptury nie uruchamiają się z bieżącą konfiguracją.

### P1 — wymagane poprawki

1. Naprawa `Ω`, `kΩ`, `µT` oraz jednoznaczne rozróżnienie `mΩ`/`MΩ`.
2. Dodanie MOKE i Lake Shore do readiness oraz RF OUTPUT Anritsu do detekcji
   energetyzacji.
3. Ukrycie lub wyłączenie niezakwalifikowanych funkcji MOKE w produkcyjnym UI.
4. Zamknięcie zakresu funkcjonalnego Lake Shore i pełna regresja po integracji.
5. Usunięcie ostrzeżenia ABI i kwalifikacja dokładnych wersji bibliotek.

### P2 — dojrzałość wydania

1. Odtwarzalne środowisko z lockfile i dokładną wersją Pythona.
2. CI wykonujące testy, lint, compileall, test pliku HDF5 i walidację PyThat.
3. Zweryfikowany instalator/artefakt i procedura aktualizacji/rollback.
4. UAT UI na docelowym ekranie, DPI, motywie, fontach i kontach operatorów.
5. Spójna terminologia i język UI.

## 11. Warunki GO-LIVE

Wydanie może otrzymać status GO dopiero po spełnieniu wszystkich poniższych
warunków:

1. Zamrożenie jednego commita/tagu i czyste drzewo robocze.
2. Pełne `pytest`, `ruff`, `compileall` i `git diff --check` na tym samym
   commicie, w docelowym środowisku instalacyjnym.
3. Naprawa błędów jednostek, readiness i polityki ręcznego OUTPUT Rigola.
4. Jednoznaczne wyłączenie z wersji 1.0 wszystkich niezakwalifikowanych funkcji
   MOKE/Lake Shore albo ukończenie ich implementacji i HIL.
5. Uzupełnienie produkcyjnego profilu: dokładne zasoby, modele, seriale,
   firmware, opcje, limity DUT/RF, tłumienie, proby i okablowanie.
6. Zatwierdzenie profilu przez uprawnionego inżyniera po fizycznej inspekcji.
7. Wykonanie HIL: passive/OFF, minimalny bezpieczny punkt, 2 × 2, fault
   injection, E-STOP, utrata komunikacji, 100 × 20 i soak test.
8. Pomiar fizycznego napięcia/prądu/mocy/częstotliwości niezależnym
   przyrządem podczas testów wyjść.
9. Podpisany raport kwalifikacyjny oraz archiwizacja logu audytowego i plików
   HDF5.
10. Round-trip danych przez rzeczywisty docelowy system inwentaryzacji.
11. Test akceptacyjny UI przez operatora, obejmujący wpisywanie wartości,
    jednostki, błędne dane, ARM, OUTPUT ON/OFF, E-STOP, anulowanie i recovery.

## 12. Decyzja końcowa

**Nie wdrażać obecnego stanu jako oprogramowania produkcyjnego do sterowania
fizycznymi wyjściami.**

Możliwa jest dalsza praca developerska i diagnostyka read-only w kontrolowanym
środowisku laboratoryjnym. Mechanizmy fail-safe, testy symulacyjne i struktura
Sweepera dają dobrą bazę do domknięcia produktu, ale obecnie nie ma dowodów
pozwalających stwierdzić, że Sweeper jest ukończony w 100%, wszystkie
urządzenia są zakwalifikowane, a UI i OUTPUT są bezpieczne w pełnym scenariuszu
produkcyjnym.

## 13. Główne źródła w repozytorium

- `.config/settings.yml`
- `app/devices/base.py`
- `app/devices/rigol/adapter.py`
- `app/devices/keithley/adapter.py`
- `app/devices/anritsu/adapter.py`
- `app/devices/moke_box/adapter.py`
- `app/devices/moke_box/ui/page.py`
- `app/devices/lakeshore_gaussmeter/`
- `app/domain/quantities.py`
- `app/domain/readiness.py`
- `app/engine/compiler.py`
- `app/engine/runner.py`
- `app/ui/shell/main_window.py`
- `docs/HIL_QUALIFICATION.md`
- `docs/MACIERZ_KWALIFIKACJI_SPRZETOWEJ.md`
- `docs/raports/PLAN_IMPLEMENTATION_MATRIX_2026-07-16.md`
- `tmp/qualification-smoke/HIL-20260716T224623.789192Z-20a297d0.json`

