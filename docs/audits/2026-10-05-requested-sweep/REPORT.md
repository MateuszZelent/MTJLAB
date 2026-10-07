# Sweep MOKE × Keithley B × Keithley A — audyt i plik wykonawczy

## Uzgodniony przebieg

Plik: `recipes/moke_30_50mV_keithley_B25_35mA_A0_1p6mA_avg32.yml`.

1. Potwierdzić OFF Keithley A/B, sprowadzić MOKE VOUT0 do kwalifikowanego
   poziomu bezpiecznego 0 V i rozbroić sterowanie. Odczekać 5 s.
2. Zebrać oddzielną referencję: 32 pełne widma, średnia mocy liniowej.
3. Zebrać oddzielne tło: co najmniej 30 s oraz 30 pełnych widm, przy nadal
   wyłączonych źródłach. Ostatni rozpoczęty przebieg kończy się w całości.
4. Skonfigurować Keithley przy OFF, przygotować i uzbroić MOKE VOUT0,
   ustawić +30 mV, następnie włączyć B i A.
5. Wykonać iloczyn kartezjański:
   - MOKE: +30, +40, +50 mV;
   - dla każdego MOKE, Keithley B: 25, 30, 35 mA;
   - dla każdej pary, Keithley A: 0–1,6 mA co 0,05 mA, 33 punkty.
6. Po każdej zmianie A: odczekać 5 s, odczytać A/B i monitor Hall,
   zebrać 32 nowe pełne widma i zapisać średnią mocy liniowej.
7. W `finally`: rampy prądów do zera, OFF A/B oraz bezpieczne 0 V MOKE.
   Rampa B ma deadline 60 s: pierwotne 10 s nie wystarczało w symulacji
   profilu z krokiem 100 µA i opóźnieniem 100 ms. Pierwszy pełny test zapisał
   wszystkie 297 punktów, lecz zakończył się fault podczas tej rampy;
   po błędzie wykonano pozostałe akcje wyłączenia. Nie zwiększono kroku rampy.

Razem: **297 punktów, 9504 surowe przebiegi sygnału**, dodatkowo 32 przebiegi
referencji i czasowo określona liczba przebiegów tła. Stabilizacja punktów
zajmuje 1485 s (24 min 45 s); czas akwizycji dochodzi do tej wartości.

MOKE „wyłączone” oznacza bezpieczne 0 V i rozbrojenie sterowania DAC.
Nie jest to odłączenie zasilania wzmacniacza ani potwierdzenie zerowego pola
magnetycznego. Kod nie przypisuje polu takiej kwalifikacji.

## Jawne ustawienia i granice zmian

Zapisany profil stacji z chwili audytu dopuszcza żądane prądy i MOKE VOUT0.
Początkowa konfiguracja Keithley jest jawna w YAML: A ma compliance 670 mV
i zakres 10 mA; B ma compliance 700 mV i zakres 1 A; oba kanały 2-wire,
NPLC 1, pomiar z autorangingiem i początkowe opóźnienie 100 ms. Są to
wartości z istniejącego profilu, a nie nowe limity uznane za bezpieczne dla
nieznanej próbki. W pętlach nie ma ponownej konfiguracji zakresów/compliance.

YAML nie ustawia częstotliwości, RBW/VBW, detektora ani tłumienia Anritsu:
korzysta z konfiguracji wybranej przez operatora. Przed uruchomieniem należy
wybrać właściwe widmo w trybie Spectrum Analyzer. Każdy blok zapisuje odczyt
konfiguracji i sprawdza jej fingerprint przed/po akwizycji. Kwalifikowany
protokół świeżego widma przygotowuje bufor TRAC1 do zapisu i wykonuje pojedynczy
przebieg; nie korzysta z problematycznego `TRAC:TYPE?`.

Nie uruchomiono tej recepty na fizycznej aparaturze. Potwierdzenie kompilacji
z rzeczywistym profilem oraz testy symulatora nie są kwalifikacją okablowania,
próbki ani zachowania fizycznego urządzenia.

## Znaleziona luka i poprawki kodu

Dotychczas `acquire_reference` obsługiwał wyłącznie liczbę przebiegów.
`average_count: 30` nie oznacza 30 sekund. Dodano:

- `minimum_duration` z jednostką czasu oraz `purpose: reference|background`;
- jednoczesne spełnienie minimalnej liczby widm i minimalnego czasu;
- poprawny deadline obejmujący czas zbierania oraz przerwy między widmami;
- zapis końcowej rzeczywistej liczby widm, czasu i roli pomiaru bazowego;
- kontrole parsera/kompilatora, spójności źródeł w writerze i odczyt roli;
- zachowanie tych opcji przez edytory oraz ich widoczność w drzewie;
- zapis odczytanej pełnej konfiguracji analizatora, także gdy YAML nie zawiera
  kroku konfiguracji urządzenia.

Prywatne rekordy surowe mają dodatkowe `minimum_duration_s`. Dla czasowego
bloku ich `average_count` wynosi `null` (końcowa liczba nie jest jeszcze znana),
a `average_index` rośnie od zera. To jawne rozszerzenie prywatnego formatu;
aktualny reader obsługuje też starsze rekordy bez tego pola. Końcowy rekord
referencji zawiera rzeczywistą dodatnią liczbę i komplet indeksów surowych
źródeł. Nie przepisuje się już zatwierdzonych surowych rekordów.

Ograniczenie: blok czasowy zbiera maksymalnie 9999 przebiegów. Jeżeli osiągnie
ten pułap przed zadanym czasem, kończy się błędem z zachowaniem RAW, zamiast
publikować zbyt krótkie tło. Estymacja dysku rezerwuje ten górny pułap.

## Dane do późniejszego odejmowania

W jednym pliku wynikowym:

- `/references/0`: referencja, `purpose=reference`;
- `/references/1`: tło, `purpose=background`;
- `/reference`: zgodny wstecznie alias pierwszej referencji;
- `/recipe_raw_sweeps_v1`: wszystkie surowe przebiegi, w tym źródła obu baz;
- `/spectra/<index>`: średnia z 32 przebiegów danego punktu;
- `/points/<index>`: żądane/zastosowane/odczytane nastawy, A/B i monitor Hall,
  znaczniki czasu oraz kontekst bezpieczeństwa;
- zdarzenia i snapshoty urządzeń: konfiguracja, przebieg wykonania i zakończenie.

Bazy mają `frequency_hz`, `power_dbm`, `average_count`, powiązania ze źródłami
oraz `acquisition_metadata_json` z czasem, fingerprintem, odczytami urządzeń
i stanami wyjść. Odejmowanie mocy należy wykonywać po konwersji do W:
`P_W = 10 ** ((P_dBm - 30) / 10)`. Ujemne reszty pozostają ujemne.
W YAML odejmowanie online jest wyłączone — oba wybory pozostają dostępne
w postprocessingu bez utraty surowego sygnału.

## Dowody i stan weryfikacji

`compile-current-profile.json` zapisuje hashe YAML/profilu/planu, 297 punktów,
9504 przebiegi sygnału oraz estymację. Model dla profilu stacji daje około
82 min, lecz rzeczywisty czas zależy od analizatora i transmisji. Konserwatywna
rezerwacja archiwum wynosi około 5,01 GB, wliczając maksymalny blok tła i
metadane; nie jest to prognoza rzeczywistego rozmiaru pliku.

Przeszły testy kompilacji, średniej liniowej, minimalnego czasu, zachowania
RAW po błędzie, dwóch baz w jednym HDF5, jednostek i zgodności PyThat.
Przeszły także testy deadline/retry, estymacji, zapisów i błędów storage,
odzyskiwania referencji, selektywnych zmian parametrów oraz regresji protokołu.
Edytor czasu/roli sprawdzono po `show()` w jasnym i ciemnym motywie.

Ruff dla zmienianych plików przechodzi; pełne `ruff check app tests` zgłasza
10 istniejących wcześniej nieużywanych importów w dwóch niezwiązanych
plikach testowych (`test_spectrum_correction_layout.py`,
`test_sweep_release_contracts.py`).

Pełny test skali **297 × Avg32 × 10001 przeszedł**, wraz z zakończeniem
wszystkich 1812 akcji i poprawną walidacją PyThat. Wynik zapisano w
`simulation-297-avg32.json`: 9504 przebiegi sygnału, 32 referencji,
912 przebiegów tła, plik 995 779 254 B. Liczba przebiegów tła w realnym
pomiarze zależy od szybkości analizatora. W tym teście 5-sekundowe oczekiwania
są przechwycone i sprawdzane, natomiast 30 s zbierania tła biegnie rzeczywiście.
Sprawdzono również zgodność zapisanych średnich z ich surowymi źródłami
w punktach 0, 148 i 296 oraz stany OFF obu kanałów przy zbieraniu obu baz.

Po przeglądzie końcowego snapshotu znaleziono i poprawiono dodatkową usterkę:
udana rampa do zera nie aktualizowała zapamiętanego prądu w stanie runnera.
Fizyczny adapter zerował i wyłączał wyjście, lecz końcowe metadane nadal
pokazywały ostatni prąd pomiarowy. Aktualizacja snapshotu została naprawiona
bez zmiany poleceń aparatury; osobny test sprawdza odczyt HDF5 po rampie:
`source_level_si=0`, `output_enabled=false`, status wyjścia `off`.
Pełny test skali powyżej poprzedza tę korektę metadanych; po korekcie
przeszło 18 testów ukierunkowanych, w tym polityka wykonania i recovery.

| Zestaw | Wynik |
| --- | --- |
| `test_timed_sweep_reference.py` z pełnym testem skali | 8 passed, 428,87 s |
| Kompilator, audyt sweepów, storage faults, recovery reference | 86 passed + 6 subtests |
| Deadline/policy, estymacja, reference store, storage faults, UI sweepów | 36 passed + 4 subtests |
| Nowy edytor czasu, recovery, selektywne mutacje, release, protokół | 70 passed |
| Końcowa regresja po korekcie snapshotu (bez ponownego testu skali) | 18 passed + 4 subtests |

Zestawy częściowo się pokrywają. Recepta jest przygotowana do wczytania po
restarcie aplikacji z tym kodem. Weryfikacja obejmuje kod, kompilację z profilem
stacji, symulację i archiwum; nie obejmuje fizycznego uruchomienia eksperymentu.
