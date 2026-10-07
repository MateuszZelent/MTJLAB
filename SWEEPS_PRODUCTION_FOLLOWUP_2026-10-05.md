# Sweepy: naprawy Anritsu, RAM i kwalifikacja Rigola — 5.10.2026

**Późniejsza poprawka po zgłoszeniu sprzętowym:** usunięto regresję timeoutu `TRAC:TYPE?` w ręcznej i automatycznej akwizycji. [Opis, zakres i testy poprawki](ANRITSU_TRACE_TIMEOUT_FIX_2026-10-05.md). Poniższe wyniki i pierwotny manifest dotyczą stanu sprzed tej poprawki.

[Dalszy audyt komunikacji](AUDYT_REGRESJI_KOMUNIKACJI_2026-10-05.md) opisuje dodatkowe poprawki zapytań Anritsu i Rigola oraz granice potwierdzenia zgodności sprzętowej.

Naprawiono SA-R1–SA-R5 z ponownego audytu oraz znalezione błędy Rigola. Zakończone próby potwierdzają działanie ścieżki oprogramowania z natywnymi adapterami i symulowanymi sesjami VISA. **Nie wykonano próby na fizycznym DG1032Z ani pomiaru DUT; ten raport nie stanowi potwierdzenia konkretnego połączenia sprzętowego.** Naprawy kodu nie obejmują edycji rzeczywistego `.config/settings.yml`.

Uwaga o pochodzeniu konfiguracji: końcowy SHA-256 profilu różni się od zapisanego w poprzednim audycie (mtime pliku: 10:20:54). Przyczyny tej zmiany nie ustalono; nie przywracano starszego pliku. Ponownie odczytane kluczowe nastawy analizatora nadal odpowiadają opisanym niżej. Manifest zapisuje obie sumy zamiast deklarować identyczność profilu między audytami.

## Anritsu i aktualna konfiguracja

| Problem | Naprawa i dowód |
|---|---|
| SA-R1: dwa źródła RBW/VBW | Edytor używa jednej pary widocznych kontrolek. Manual / 10 kHz trafia do wybranych akcji. Testy pokazują okna w jasnym i ciemnym motywie oraz przy szerokości 780 px. |
| SA-R2: brak Video/Power w drzewie | Dodano `vbw_filter_mode: VID/POW`, a w selektorach `advanced.vbw_filter_mode`. `vbw_mode` konfiguracji zaawansowanej nadal oznacza auto/manual/off. Test obejmuje zapis węzła, YAML, kompilację, komendę i odczyt. |
| SA-R3: pomijanie zadanej wartości | Samodzielne wartości RBW/VBW/tłumienia/czasu wymagają jawnego trybu manual; brak trybu lub sprzeczny AUTO daje błąd kompilacji. |
| SA-R4: zmyślony odczyt VID | Timeout i nieznana odpowiedź `BAND:VID:MODE?` przerywają kwalifikowany odczyt. Nie ma domyślnego VID udającego potwierdzenie. |
| SA-R5: utrata Video/Power w recovery | Potwierdzony filtr trafia do checkpointu i prelude; test zmienia urządzenie z POW na VID między przerwaniem a wznowieniem i sprawdza przywrócenie POW. |

Filtr jest także zapisany w metadanych widma referencyjnego, odczytywany z HDF5 i porównywany przed użyciem referencji. Starsze pliki pozostają czytelne; referencja bez potwierdzonego VID/POW wymaga ponownego pozyskania przed użyciem w sweepie. Fingerprint zachowuje dotychczasową reprezentację tego pola, unikając podwójnego zapisu tej samej właściwości. Zapis domyślnych ustawień formularza rozróżnia teraz filtr VID/POW od trybu pasma. Usunięto również stałe `preamp=False` przy prezentacji pełnego odczytu: adapter odczytuje stan, jeżeli urządzenie ma odpowiednią opcję.

Historyczny skrypt audytu proponował nazwę `vbw_mode: POW` w podstawowym YAML. Nie zmieniano historycznego dowodu, żeby udawał zielony test. Nowe testy używają jednoznacznej, wdrożonej nazwy `vbw_filter_mode`.

## Limit pamięci

Usunięto **stały domyślny pułap 512 MiB dla nowych serii**. Automatyczny budżet wynosi połowę aktualnie dostępnej pamięci fizycznej. Jawny limit operatora może go obniżyć. Jeśli system nie potrafi podać dostępnego RAM, pozostaje konserwatywny fallback 512 MiB. Starsze archiwa zachowują zapisany budżet.

Kontrola zasobów nadal działa przed połączeniami z aparaturą i przed importem. Nie wprowadzono importu strumieniowego: PyThat nadal tworzy pełne tablice, więc całkowite usunięcie ochrony nie rozwiązywałoby zużycia RAM. Rozwiązany budżet jest utrwalany w archiwum; bieżąca dostępność RAM może ograniczyć późniejszy import.

Pełny scenariusz **MOKE0 0–100 mV × Keithley B −80–80 mA × Keithley A +0,2–1,4 mA**, z przykładowymi krokami 10 mV, 10 mA i 0,2 mA, wykonał **1309 checkpointów × 10 001 punktów widma**:

- analizator: 100 MHz–6 GHz, −10 dBm, RBW AUTO, VBW manual 30 kHz, filtr POW, NORM, tłumienie manual 10 dB, preamp OFF, czas AUTO;
- jawne `average_count: 1`; próba nie kwalifikuje czasu wykonania uśredniania 198 razy;
- HDF5: **656 027 500 B**, estymacja pamięci importu **925 058 880 B**;
- PyThat **0.2.14**: odczyt poprawny; 1309 kompletnych punktów, potwierdzone nastawy, brak pozostałości `_pending`;
- wykonanie i zamknięcie w SIM: **194,8 s**; zapisano 1309 żądań Wait 3 s, ale rzeczywiste oczekiwania pominięto w teście skali. Nie jest to prognoza czasu fizycznego pomiaru.

Liczby różnią się od wcześniejszej próby 922 378 048 B, która obejmowała sam analizator. Pełne osie i pomiary dodają dane. [Wynik próby skali](docs/audits/2026-10-05-production-followup/scale-qualification.json).

## Rigol DG1032Z

Naprawiono następujące zachowania:

1. Selektory mają `Unchanged`, a pusta lista akcji pozostaje pusta. Edytor nie zamienia jej w automatyczne ustawienie częstotliwości i napięć. Etykieta OUTPUT mówi teraz zgodnie z zachowaniem: „Leave OUTPUT unchanged”.
2. Włączenie OUTPUT nie programuje ukrytej konfiguracji domyślnej polaryzacji, bramkowania ani SYNC. Odczytuje i sprawdza istniejący tor wyjściowy; nieobsługiwany stan zostaje odrzucony. Jawnie zapisana konfiguracja toru nadal może być zastosowana jako osobna operacja.
3. Odpowiedź HIGHZ `9.9E37` jest rozpoznawana jako HIGHZ, a nie jako rezystor o tej wartości.
4. Po zmianie częstotliwości/napięć sprawdzana jest pełna konfiguracja nośnej. Wstrzyknięta niezamówiona zmiana fazy kończy się błędem i OUTPUT OFF obu kanałów.
5. DC obsługuje offset w edytorze i aktualizację poziomu DC; nie próbuje programować dwóch różnych poziomów przebiegu DC.
6. Wartości potwierdzone dla osi amplitudy i offsetu są obliczane z obu odczytanych poziomów. Karta urządzenia pokazuje także przebieg, fazę, obciążenie i parametry impulsu, bez wysyłania poleceń z projekcji GUI.

Testy poleceń obejmują **CH1 i CH2 × SIN/SQU/RAMP/PULS/DC**, zachowanie SYNC przy OUTPUT ON, pojedyncze zmiany i wykrycie ubocznej zmiany fazy. Sześć prób integracyjnych obejmuje oba kanały × częstotliwość/amplitudę/offset: kompilator → runner → Wait → Anritsu → HDF5 → PyThat. Sprawdzono potwierdzone nastawy w każdym checkpointcie.

Aktualny lokalny profil ma oba kanały włączone i pozwala na OUTPUT, z limitem napięcia złożonego 100 mV. CH2 ma zakres częstotliwości 1 Hz–1 MHz; profil CH1 wpisuje górną granicę 2 GHz, ale program dodatkowo egzekwuje niższe limity modelu DG1032Z. Nie zmieniono profilu. Limity szacowanego prądu i mocy są w tym profilu wyłączone — nie należy utożsamiać ich obecności w YAML z aktywnym zabezpieczeniem DUT.

Kwalifikacja dotyczy krokowych sweepów aplikacji. Sprzętowe tryby modulation/burst/sweep Rigola pozostają odrębnymi funkcjami ręcznymi. Testy używały jawnych profili SIM; nie zatwierdzają dowolnego dzisiejszego zakresu, obciążenia ani nieznanego firmware.

## Dowody i odtworzenie

- `final.log`: **142 passed, 4 subtests** — edytory, referencje, storage faults, recovery i prezentacja widm.
- `integration.log`: **182 passed, 5 subtests** — adaptery, Rigol, wybrane mutacje, recovery i zapisy referencji.
- `qualified.log`: **134 passed** — końcowe kontrakty, pełne odczyty, metadane i błędy importu/odczytu.
- `scale.log`: **1 passed** — pełna seria 1309 × 10 001.
- `readback.log`: **24 passed** — także odczyt włączonego preamp i zapis filtra POW w referencji.
- `defaults.log`: **2 passed, 154 deselected** — odczyt oraz zapis domyślnych nastaw z restartem aplikacji, na izolowanym profilu testowym.

Grupy zachodzą na siebie; nie należy sumować ich jako liczby unikalnych testów. Końcowe dodatkowe wyniki i sumy SHA-256 znajdują się w [manifeście](docs/audits/2026-10-05-production-followup/manifest.json). Zrzuty ekranów w tym samym katalogu sprawdzono wizualnie. Archiwum skali identyfikuje SHA-256 w JSON; dużego pliku testowego nie dołączono do repozytorium.

Przykładowe odtworzenie w SIM:

```powershell
python -m pytest -q -p tests.shell_test_isolation tests/test_sweep_production_followup.py tests/test_reference_store.py tests/test_anritsu_fast_acquisition.py tests/test_sweep_release_contracts.py tests/test_sweep_recovery_reference.py tests/test_sweep_storage_faults.py
python -m pytest -q "tests/test_sweep_scale_qualification.py::test_1309_point_cartesian_archive_and_pythat[10001]"
```
