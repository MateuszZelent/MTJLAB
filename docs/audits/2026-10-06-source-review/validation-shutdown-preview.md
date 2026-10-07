# Validate & Preview — kroki końcowe

Data: 2026-10-06.

## Aktualizacja: stabilne bloczki edytora

Poprzednia zmiana rozdzielała zabezpieczenia, ale po walidacji nadal dodawała ich
wiersze do edytora. Nie spełniało to oczekiwania stabilnego drzewa. Obecnie builder
ma tę samą listę identyfikatorów i tę samą strukturę dzieci przed i po walidacji.
Istniejący wiersz podsumowania zabezpieczeń nie zyskuje dodatkowych bloczków;
pełna polityka jest dostępna w jego metadanych i podpowiedzi. Execution otrzymuje
pełne drzewo z wierszami zdarzeń zabezpieczeń, aby ich wykonanie pozostało widoczne.
Projekcja edytora współdzieli osie i punkty z pełnym drzewem — nie generuje ROI
drugi raz. YAML i komendy wykonawcze nie zmieniły się w tej aktualizacji.

Dodano osobne scenariusze:

- MOKE VOUT 2: jawny stan końcowy 0,2 V; sukces utrzymuje napięcie i połączenie
  kontrolera po zakończeniu RunWorker. Ostatni SET dotyczy 0,2 V, bez późniejszego zera.
- MOKE VOUT 2: brak jawnego Finally; polityka automatyczna kończy na zerze i zamyka
  połączenie. H5 obu zakończonych przebiegów ma status completed.
- Stop i błąd pomiaru: oba warianty kończą na zerze, nie na wartości podtrzymania.
- Edytor: trzy kolejne walidacje dla podtrzymania, jawnego Stop i automatycznego
  zakończenia nie zmieniają YAML, identyfikatorów ani struktury bloczków.
- Rzeczywisty `anritsu_background_reference_smoke_test.yml`: warianty z końcowym
  0,2 V oraz automatycznym zerem zachowują 10 widm i stabilny hash kompilacji.
  Sam plik użytkownika nie został zmieniony przez te testy.

Weryfikacja sprzętu odbywa się w symulacji, z kontrolą wysłanych ramek MOKE.
Końcowy zestaw aktualizacji: **70 testów przeszło** (`test_final_output_state.py`,
`test_shutdown_tree_projection.py`, `test_recipe_semantic_tree.py`); Ruff przechodzi.
Zrzuty obu wariantów: `stable-preview-hold.png` i `stable-preview-automatic.png`.
Historyczny opis poniżej dokumentuje wcześniejszy etap poprawki.

## Wynik analizy kodu

`RecipeCompiler._safe_shutdown_actions` tworzy manifest zabezpieczeń dla urządzeń
użytych przez plan. Dla Anritsu używanego tylko do pomiaru jest to abort akwizycji,
a nie RF OFF. MOKE jest objęty zerowaniem tylko wtedy, gdy plan steruje jego DAC.
Flush dotyczy zapisu pomiarów. `RecipePage.semantic_tree_snapshot` przekazuje manifest
do `normalize_recipe_tree`; ta funkcja dodawała jego prezentację bezpośrednio obok
autorskich kroków Finally. Nie modyfikowała YAML, ale dwa podobne wiersze MOKE
wyglądały jak dopisana instrukcja pomiarowa.

`RecipeRunner` wykonuje autorskie Finally, a następnie niezależne zabezpieczenie.
Przed poprawką kolejne `MokeBoxAdapter.stop_vout` czytało DAC, lecz nawet przy już
potwierdzonym zerze ponownie wysyłało SET. Był to rzeczywisty zbędny zapis.

## Poprawka

- Drzewo ma osobną, niemodyfikowalną gałąź **Automatic engine safeguards** już przed
  kompilacją. Przed walidacją nie wymienia urządzeń, tylko zapowiada podgląd manifestu.
- Po walidacji gałąź pokazuje wyłącznie manifest planu; komunikat wyjaśnia, że YAML
  nie został zmieniony. Autorskie instrukcje pozostają osobno, bez duplikowania YAML.
- Wiersz MOKE wskazuje VOUT i sprawdzenie zera z rampą tylko w razie potrzeby.
- Powtórzony Stop MOKE nie wysyła SET, jeśli świeży odczyt pokazuje dokładnie
  bezpieczny cel i nie ma wcześniejszego niepewnego zapisu na tym kanale.
- Jeżeli wcześniejszy SET pozostaje niepewny, jawne SET zero nadal jest wymagane:
  spóźniona komenda mogłaby zmienić DAC po odczycie. Przy zmianie napięcia przez inne
  źródło wykonywana jest normalna ograniczona rampa. Awaria odczytu nigdy nie
  korzysta z zapamiętanego zera jako potwierdzenia bezpieczeństwa.

## Weryfikacja

Regresje sprawdzają niezmienność YAML po trzech kolejnych podglądach, rozdzielenie
gałęzi, stabilne identyfikatory dla Execution, render strony oraz manifest Anritsu
bez RF OFF i urządzeń nieużywanych w planie. Testy protokołu MOKE obejmują powtórzony
Stop, zewnętrzną zmianę DAC, awarię odczytu i opóźnione/ignorowane SET. Sprawdzono też
testy runnera i stanów końcowych. Zrzut: `preview-safeguards.png`.

Nie wykonano pomiarów na fizycznej aparaturze. Polityka błędu, Stop i E-STOP oraz
walidacja limitów pozostają aktywne. Zabezpieczenia są nadal wykonywane — nie ukryto
ich ani nie usunięto w celu uproszczenia drzewa.

Wyniki końcowe: 86 testów MOKE/stanów końcowych/drzewa semantycznego, 22 testy
projekcji i drzewa UI oraz 110 testów runnera (5 subtestów) — wszystkie przeszły.
W zestawie audytu UI pominięto jeden niezwiązany test pełnego dry run pliku
`untitled_sweep.yml`; nie stanowi on części tej weryfikacji. Ruff dla zmienionych
plików przechodzi. Starszy test generatora Keithley dostosowano do istniejącego
kontraktu jawnego source range i konfiguracji przed ROI; kod generatora nie zmienił się.
