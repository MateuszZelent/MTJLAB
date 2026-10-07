# Sweep MOKE: przygotowanie jako część osi

2026-10-06. Usunięto jawne configure/arm z receptury
`anritsu_background_reference_smoke_test.yml`. Dla nieskonfigurowanej osi MOKE
kompilator przygotowuje configure → arm przed pierwszą aktualizacją napięcia.
Obie operacje mają source/semantic ID sweepa, więc nie tworzą osobnych
technicznych wierszy drzewa. Pozostają w planie wykonawczym i diagnostyce.

Robocze min/max wynikają z żądanych punktów. Dla pojedynczej wartości używany
jest profil stanowiska, ponieważ plan wymaga niezerowej szerokości zakresu.
Wszystkie wartości i reprezentowalne kody DAC są sprawdzane przez dotychczasowy
provider, plan, configure i arm adaptera. Nie zmieniono limitów stanowiska.
Jawne konfiguracje starszych receptur nadal są respektowane.

Automatyczne przygotowanie jest wykonywane ponownie dla kolejnych/powtarzanych
sweepów; nie dodaje zerowania między punktami. Nowy generator UI tworzy sweep
bez configure/arm oraz widoczny powrót do zera. Ręczne pojedyncze akcje zachowują
dotychczasową ścieżkę konfiguracji. Stop ma czytelną etykietę
„MOKE · Return to 0 V and disarm”. Receptura zachowuje Stop przed baseline
i w finally. Zero DAC nie jest dowodem wyłączenia Kepco ani zerowego pola.

Weryfikacja: limity/trajectory 26 passed; limity/repeat/generator UI 24 passed;
pełny smoke z H5/PyThat i kompilator 48 passed + 6 subtests. Zestawy częściowo
się pokrywają. Ruff zaliczony. Bez fizycznego I/O.
