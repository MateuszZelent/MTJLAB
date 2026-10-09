# Audyt rzeczywistego ustawiania parametrów w sweepach — 2026-10-07

## Wynik przeglądu kodu

Przed poprawką zasada „zmienia się jeden parametr, więc zapisujemy tylko ten parametr” **nie była spełniona dla bloków konfiguracji**. `changed_fields` określało pola jawnie wybrane w przepisie, a nie różnice względem sprzętu. Adapter Keithley zapisywał każde wybrane pole, również zielone „Same”. Wewnątrz ROI aktualizacja prądu miała już osobną, wąską ścieżkę.

Poprawiono stosowane w przepisach ścieżki konfiguracji Keithley, podstawowego generatora Rigol, toru wyjścia Rigola, podstawowych/kwalifikowanych zaawansowanych ustawień analizatora Anritsu oraz powtarzanego celu DAC MOKE. Wznowienie Keithley również korzysta z zapisu różnic. Nie zmieniono receptury testowej ani pliku limitów stanowiska.

Podstawą wniosków jest przegląd instrukcji wykonujących konfigurację i ich wywołań od UI/YAML przez kompilator do adaptera. Testy uruchomiono **po znalezieniu i poprawieniu problemów**, jako weryfikację zmian. Nie jest to deklaracja przeczytania każdej linii wszystkich ekranów, algorytmów filtracji i niezwiązanych usług aplikacji.

## Ścieżki źródłowe sprawdzone w audycie

| Plik | Sprawdzona odpowiedzialność / funkcje |
| --- | --- |
| `app/ui/recipes/configuration_comparison.py` | Porównanie SI, zielone/pomarańczowe/szare pola i licznik; porównanie offline, bez wpływu na komendy. |
| `app/devices/keithley_2600/ui/page.py` | `KeithleyNodeEditorDialog`, `programmed_configuration_fields`, snapshot, zachowanie pól pominiętych. |
| `app/ui/recipes/page.py` | `_edit_legacy_keithley_configuration`, zapis pól wybranych do YAML; odpowiednik Rigola. |
| `app/recipes/block_registry.py` | Dopuszczone akcje i ich pola; rozróżnienie przepisów i ręcznych funkcji urządzenia. |
| `app/engine/compiler.py` | `_compile_action`, `_compile_keithley`, `_compile_anritsu`, kompilacja DeviceNode, ROI, `_sync_planned_state`, `_binding_configured`, kontrola ciągłości OUTPUT i konfiguracji. |
| `app/devices/keithley_2600/sweep_provider.py` | Prąd/napięcie → `update_keithley_level`, compliance → osobna aktualizacja, settling → oczekiwanie. |
| `app/devices/rigol_dg1000z/sweep_provider.py` | Częstotliwość → osobna aktualizacja; napięcia → para poziomów, amplituda/offset. |
| `app/devices/anritsu_ms2830a/sweep_provider.py` | Pojedyncze pole zakresu/poziomu odniesienia analizatora; osobne częstotliwość/moc SG. |
| `app/devices/moke_box/sweep_provider.py` | Wybrany VOUT, cel napięcia i zatwierdzona trajektoria. |
| `app/engine/runner.py` | Wywołania adapterów, potwierdzone stany, metadane konfiguracji, `_acquire_averaged_spectrum`; brak ponownej pełnej konfiguracji źródeł przy rejestracji widma. |
| `app/engine/recovery.py` | `_configuration_prelude`, scalanie częściowych ustawień, odtworzenie pól zachowanych z potwierdzonego stanu. |
| `app/devices/keithley_2600/adapter.py` | `configure_source`, pełna/różnicowa ścieżka, weryfikacja konfiguracji/zakresów, kwantyzacja, `update_source_level`, `update_source_compliance`, potwierdzenie OUTPUT. |
| `app/devices/rigol_dg1000z/adapter.py` | `configure_channel`, `configure_output`, weryfikacja, aktualizacje częstotliwości/poziomów/amplitudy/offsetu, sprzężenie i śledzenie kanałów, stany trybów zaawansowanych. |
| `app/devices/anritsu_ms2830a/adapter.py` | Podstawowe/zaawansowane ustawienia, aktualizacja SG, przygotowanie i wykonanie pojedynczego widma, format transmisji i kontrola błędów. |
| `app/devices/moke_box/adapter.py` | Przygotowanie/uzbrojenie w pamięci, `ramp_vout`, `_ramp`, `_write_vout`, zatrzymanie i potwierdzenie DAC. |
| `app/devices/lakeshore_475/module.py`, `adapter.py` | Operacje sweepów są odczytami pola/statusu; nie ustawiają jednostek ani trybu gaussmetru. |
| `app/devices/*/module.py` | Wywołania konfiguracji i pomiarów kierowane do adapterów; osobne ręczne operacje `quick_configure`. |
| `app/safety/keithley.py`, `app/safety/moke_box.py` | Limity, zakresy sprzętowe, źródło/pomiar, kwantyzacja i autoryzacja trajektorii. |

## Keithley — dokładna sekwencja

1. UI/YAML zachowuje jawnie zadane cele. Zielone pola pozostają celami przepisu: rzeczywisty stan urządzenia przy uruchomieniu może być inny niż karta UI przy otwarciu modala.
2. Kompilator przekazuje listę wybranych pól. Parametry pominięte nie są dopisywane jako domyślne operacje sprzętowe.
3. Adapter bierze potwierdzoną konfigurację z cache; przy pierwszej konfiguracji odczytuje ją ze sprzętu. Parametry niewybrane bierze z tego stanu, a nie z domyślnych pól żądania.
4. Sprawdza limity i zakresy żądania przed oraz po kwantyzacji.
5. Weryfikuje konfigurację sprzętu przed użyciem jej do pominięcia zapisów. Cache, kolory modala i samo wysłanie poprzedniej komendy nie wystarczają.
6. Oblicza różnice wybranych pól. Dla poziomu/compliance porównuje reprezentowalne wartości wysyłane do TSP. Nie stosuje tolerancji, która mogłaby połknąć krok 1 pA. Również weryfikacja odczytu poziomu/compliance nie ma już bezwzględnej tolerancji 1 pA: niezgodność o jeden taki krok nie jest akceptowana jako potwierdzony stan. Dla zakresów pomiarowych porównuje wybrany fizyczny zakres sprzętowy.
7. Potwierdza OUTPUT OFF przed konfiguracją. Jeśli OUTPUT był już potwierdzony OFF, nie powtarza zapisu OFF.
8. Zapisuje tylko różniące się pola; następnie odczytuje i weryfikuje wynik. Zmiana pojedynczego kanału nie programuje konfiguracji drugiego kanału.
9. Awaria odczytu/zapisu prowadzi do dotychczasowej ścieżki wyłączenia awaryjnego. Takie polecenia bezpieczeństwa nie są optymalizowane kosztem potwierdzenia stanu.

### Przykład odpowiadający zgłoszeniu

Stan źródła: current, 1 mA, compliance 670 mV, sense 2wire, source autorange OFF, source range 10 mA. Cel: 1,5 mA; pozostałe pięć jawnych pól identyczne. NPLC i software settling pominięte.

Przy OUTPUT OFF jedyny zapis parametru jest następujący:

```text
smua.source.leveli = 0.0015
```

Przy OUTPUT ON blok konfiguracji najpierw wyłącza i potwierdza wyjście:

```text
smua.source.output = smua.OUTPUT_OFF
<odczyt i potwierdzenie OFF>
smua.source.leveli = 0.0015
```

Analogicznie dla B: `smub.source.leveli = 0.0015`. Ponowne zastosowanie identycznego celu nie zapisuje żadnego parametru. Odczyty kontrolne nadal występują.

Pełne listy zapytań i zapisów dla A/B, z OUTPUT OFF/ON, zapisano w [dowodzie komend](sweep-configuration-delta-command-evidence-2026-10-07.json). To komunikacja **symulowana**, z syntetycznym profilem limitów w pamięci. Nie jest to zapis z fizycznego Keithley.

### Wyjątki wynikające ze sprzętu i bezpieczeństwa

- Zmiana current ↔ voltage przełącza bank poziomu, compliance i zakresu. Ta sama liczba w A i V nie oznacza tego samego ustawienia. Wymagane są jawne poziom, compliance i polityka zakresu; pominiętych celów adapter nie wymyśla.
- Zmiana autorange może wymagać ustawienia również jawnie wybranego zakresu ręcznego. Pole liczbowe pominięte w przepisie nie jest dodawane jako ukryty domyślny zakres.
- Pomiar wielkości aktualnie źródłowanej jest sprzężony z zakresem źródła. Zapis niezależnego zakresu tej wielkości pozostaje blokowany przez istniejącą politykę adaptera.
- Pomiar 4wire pozostaje zabroniony. Nie dodano żadnego polecenia przełączającego na SENSE_REMOTE.
- `settle_time_s` jest oczekiwaniem aplikacji. Nie jest zapisem `source.delay` ani `measure.delay` w Keithley. Pominięte NPLC, settling i zakresy pomiarowe nie są programowane.
- Jeśli ktoś zmieni sprzęt poza kontrolowaną ścieżką, a jego stan nie zgadza się z potwierdzonym cache, konfiguracja zatrzymuje się z błędem odczytu. Nie kontynuuje na podstawie starego zielonego oznaczenia UI.

### ROI i wznowienie

Aktualizacja prądu w ROI korzysta z `update_source_level`, a nie pełnego `configure_source`. Zapisuje poziom wybranego kanału i sprawdza odczyt oraz ciągłość OUTPUT. Nie zmienia trybu, compliance, NPLC, sense ani zakresu ręcznego. Analogiczna osobna ścieżka istnieje dla compliance.

Wznowienie po przerwaniu wcześniej mogło przekazać `changed_fields=None` do pełnej ręcznej konfiguracji, zawierającej m.in. ustawienie `source.offmode`. Teraz potwierdzony snapshot jest odtwarzany przez ścieżkę różnicową. Wznowienie nadal odbywa się ze sprawdzonego bezpiecznego stanu; nie jest kontynuacją aktywnego OUTPUT bez jego kontroli.

## Pozostałe urządzenia

| Urządzenie | Przed poprawką | Po poprawce / znaczenie |
| --- | --- | --- |
| Rigol: podstawowy carrier | Wybrane FUNC/FREQ/poziomy/kształt zapisywano także dla „Same”. | Wybrane cele porównywane z odczytem urządzenia. Zmiana samej częstotliwości nie powtarza FUNC, amplitudy, offsetu, fazy, load ani advanced modes. |
| Rigol: napięcia | Poziomy High/Low reprezentowano przez amplitudę i offset; para zapisów mogła zawierać niezmienioną składową. | Zapis tylko zmienionej składowej amplituda/offset. Przesunięcie obu końców o tę samą wartość zapisuje tylko offset. Zmiana pojedynczego końca może wymagać obu komend, bo zmienia obie składowe. Pominięty drugi koniec wymaga potwierdzenia. |
| Rigol: output path | Domyślne pola load/polarity/gate/SYNC mogły być dopisane do częściowego żądania. | Kompilator przekazuje jawne pola. Adapter zachowuje pozostałe odczytane ustawienia i zapisuje tylko różnice. Runner zapisuje rzeczywiście zastosowane pola, a recovery wymaga ich potwierdzonego snapshotu. |
| Anritsu: podstawowe parametry | Literalny blok konfiguracji nie miał maski; ponownie wysyłał wszystkie cztery wartości. | Jawne cele mają maskę i porównanie z bieżącym odczytem. Zmiana samego RLEV zapisuje tylko RLEV. Nie dodaje RBW/VBW/detektora ani konfiguracji generatora RF. |
| Anritsu: przesunięcie całego zakresu | Start był wysyłany zawsze przed stop. | Przy przesunięciu zakresu ponad poprzedni stop najpierw ustawiany jest stop, aby nie utworzyć przejściowego start ≥ stop. |
| Anritsu: advanced spectrum | Powtarzano każde zadane pole; nawet identyczne żądanie preamp ON mogło powodować OFF/ON. | Porównywane są tylko jawne cele; identyczny preamp nie jest przełączany. Zmiana tłumienia przy aktywnym preampie wymaga jawnego OFF preamp w planie — brak ukrytego cyklu. Funkcje wymagają dotychczasowej kwalifikacji firmware. |
| Anritsu: aktualizacja SG | Wybrane częstotliwość/moc zapisywano bez porównania. | Aktualizacja wybranego celu pomija równą wartość i zachowuje potwierdzony RF OUTPUT. Nie jest używana przez recepturę testową. |
| MOKE | Nawet identyczny cel mógł ponownie wykonać SET_VOUT. | Świeży odczyt i równość skwantowanego celu pozwalają pominąć SET. Zachowane settling, cancellation, retarget, deadline i kontrola limitów. SET nadal jest wymagany w shutdown po niepewnym wcześniejszym zapisie. |
| Lake Shore | Odczytowe akcje sweepa. | Nadal tylko odczyt pola, ustawień i statusu; bez ustawiania jednostek/trybu. |

MOKE `configure_voltage_plan` i `arm_voltage_plan` zmieniają stan planu w pamięci; nie zapisują DAC. Fizyczny zapis jest wykonywany dla wybranego VOUT w rampie. Nie ma automatycznego ustawiania innych VOUT przy zwykłym punkcie sweepa.

## Co nadal wymaga poleceń, choć nie jest pełną konfiguracją

Rejestracja świeżego widma Anritsu nadal przygotowuje Trace A, uruchamia pojedynczy sweep i potwierdza zakończenie. Są to operacje pomiarowe wynikające z `acquire_spectrum`/`acquire_reference`, a nie powtórne programowanie częstotliwości, RBW, VBW, poziomu wejścia lub generatora RF. Nie usunięto przygotowania Trace A na podstawie niepotwierdzonego cache: problematyczne zapytanie typu trace na firmware użytkownika nie może być używane jako dowód.

Odczyt to nie ustawienie: pełniejsze zapytania o konfigurację i limity są nadal potrzebne do bezpieczeństwa i metadanych. Brak setterów nie oznacza całkowitego braku komunikacji VISA.

Pełne **ręczne** API (`configure_source`/`configure_channel` z `changed_fields=None`, `quick_configure`, początkowe jawne przygotowanie SG oraz ręczna konfiguracja modulation/burst/firmware sweep Rigola) zachowują swoje szersze kontrakty. Nie należy rozszerzać wniosku o różnicowych zapisach receptur na każde ręczne „Apply” w aplikacji. Zaawansowane ręczne operacje Rigola nie są akcjami konfiguracji w obecnym rejestrze recipe. Aktywny analizator przed częściową zmianą musi być w potwierdzonym Spectrum Analyzer mode.

## Weryfikacja i granice pewności

- Osobny przebieg symulacji zapisał pełne komendy dla przykładu A/B 1 → 1,5 mA, 670 mV, OUTPUT OFF/ON oraz ponownego zastosowania tych samych celów.
- Nowe regresje obejmują także najmniejszy reprezentowalny krok prądu, zakresy pomiarowe wybierające ten sam zakres fizyczny, nieaktualny cache, pominięte parametry, delta RLEV, kolejność zakresu, powtarzany DAC oraz odtwarzanie zachowanych wartości toru Rigola.
- Grupa obejmująca adaptery, OFF przed konfiguracją, częściowe baseline, sprzężone zakresy, MOKE/recovery, Anritsu acquisition/generator i pełny HDF5 smoke: pierwszy przebieg 154 passed / 6 failed. Sześć usterek dotyczyło rozpoznawania zamaskowanej pełnej konfiguracji Anritsu jako baseline w kompilatorze; zostały poprawione przez `is_complete_configuration`.
- Po poprawce: **49 passed** w grupie delta, generator, wybrane mutacje i recovery; następnie **60 passed** w grupie delta z precyzją, potwierdzonym OFF, częściowym baseline i zakresami Keithley. Pełny smoke z rzeczywistym czasem oczekiwania w symulacji przeszedł w pierwszej grupie; nie łączymy różnych przebiegów w fikcyjny wynik jednego testowania.
- Po końcowym zaostrzeniu odczytu poziomu/compliance: **84 passed** — `test_sweep_configuration_deltas.py`, `test_source_review_keithley_off_before_patch.py`, `test_source_review_keithley_partial_baseline.py`, `test_keithley_coupled_ranges.py`, `test_sweep_selected_mutations.py`; proces zakończył się kodem 0.
- `ruff check app tests` oraz `git diff --check`: poprawne.
- Modal wyświetlono i sprawdzono geometrię, wykonano oraz obejrzano obraz 1120 × 780. Przebieg renderowania wykonał sześć przypadków do `[100%]`, ale proces zakończył się kodem 1 bez podsumowania/tracebacku. To samo wystąpiło dla pojedynczego przypadku i dla procesu utrzymującego QApplication. Nie przedstawiamy tego jako sześciu czysto zakończonych testów; pozostaje problem zakończenia procesu testów Qt. Zapisany obraz pozwala niezależnie obejrzeć poprawiony opis i geometrię.

Nie wysłano poleceń do fizycznych urządzeń. Audyt kodu i symulacja potwierdzają mechanizm selekcji zapisów; nie dają 100% gwarancji zachowania fizycznego przy niezweryfikowanym firmware, błędzie protokołu, innej konfiguracji lub utracie komunikacji. Kolory i licznik modala nadal odnoszą się do karty urządzenia w chwili otwarcia, a nie do świeżego odczytu sprzętu.
