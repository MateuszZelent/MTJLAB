# Powtórny audyt produkcyjny sweepów — 2026-10-06

Status: **audyt zakończony; pełna gotowość produkcyjna niepotwierdzona**.

Wniosek: zakres shutdownu samego analizatora został naprawiony i sprawdzony.
Zakończenie takiego sweepa nie steruje RF ani nie kontaktuje nieużywanych
źródeł. Przechodzą testy protokołów, awarii, konfiguracji i dużego archiwum,
ale test płynności startu GUI pozostaje niezaliczony, cztery testy wzorcowych
plików są pominięte, a fizyczne stanowisko nie było kwalifikowane. Nie ma
podstaw do bezwarunkowego stwierdzenia „całość jest produkcyjna i bezpieczna”.

Ocena dotyczy aktualnego worktree, zawierającego również niezatwierdzone zmiany.
Testy używają symulatorów/transportów testowych; audyt nie upoważnia do
energizowania fizycznego stanowiska. Brak testu sprzętowego nie będzie
zastępowany deklaracją bezpieczeństwa na podstawie samego UI.

## Macierz wymaganych dowodów

| Obszar | Warunek akceptacji | Zakres dowodów i testów |
| --- | --- | --- |
| Receptura i kompilator | Ścisła walidacja, jawne baseline, właściwe jednostki, liczba punktów i kolejność | testy recipe/sweep/compiler/quantity |
| Aparatura | Zmiana tylko wybranych pól, poprawne kanały, 2-wire, readback i limity | rejestrowane komendy i testy negatywne adapterów |
| Zakres planu | Brak poleceń do nieużywanych urządzeń i nieużywanego Anritsu RF | testy scope i blokowanie komend SG/OUTP |
| Akwizycja | Świeże pełne widma, wymagany czas tła, zgodny grid i jednostki przetwarzania | testy acquisition/reference/processing |
| Awaria i zatrzymanie | Obsługa wyjątku na każdym etapie, pozostałe shutdowny mimo jednego błędu, widoczny stan UNKNOWN/FAULT | fault injection, watchdog i worker lifecycle |
| Dane | Spójne checkpointy, surowe przebiegi i metadane, jednoznaczny status zamknięcia | HDF5/thaTEC/PyThat i awarie zapisu |
| Recovery | Weryfikacja tożsamości planu i bezpiecznej granicy, odtworzenie potwierdzonych konfiguracji | testy recovery/reference |
| UI | Zgodność drzewa z planem, edycja/modale, czytelne stany, brak blokad podczas akwizycji | testy Qt po show, regresje interakcji i profilowanie |
| Wydanie | Powtarzalne testy, pliki przykładowe i dowody dostępne w repo | lint, konfiguracja testów, git ignore/status |

## Ustalenia i zmiany bieżącego audytu

1. Konstrukcja adapterów poprzedzała blok obsługi błędów `RunWorker.run`.
   Wyjątek nie musiał emitować terminalnego sygnału i mógł pozostawiać UI
   w stanie wykonywania. Dodano zewnętrzną granicę obsługi błędu.
2. `EmergencyStopWorker` tworzył adaptery w jednym wyrażeniu. Błąd jednej
   fabryki przerywał przygotowanie pozostałych prób OFF. Każda fabryka jest
   teraz niezależnie chroniona, a błędy wracają w zbiorczym wyniku.
3. Worker próbował pobierać lease także dla przekazanych kontrolerów spoza
   zakresu planu. Nieużywane kontrolery nie są już odpytywane o lease.
4. Dostarczona receptura smoke testu oraz raporty ostatnich poprawek były
   ignorowane przez Git. Dodano precyzyjne wyjątki, żeby nowe checkouty mogły
   odtworzyć testy i przeczytać dowody audytu.
5. Test pełnego sweepa odczytywał edytowalną recepturę operatora. Obecna
   receptura ma 20 punktów (2 × 2 × 5), podczas gdy test kontraktu oczekuje
   297 (3 × 3 × 33). Dodano stały wzorzec
   `tests/fixtures/requested_sweep_297.yml`; receptura operatora pozostała
   nienaruszona. Testy nie powinny wymuszać na niej swoich ustawień.
6. Jeden test awarii shutdownu zakładał wyłączanie całego stanowiska nawet
   dla planu bez urządzeń. Poprawiono jego setup: urządzenia, których brak
   połączenia ma powodować błąd, należą teraz jawnie do zakresu planu.
   Brak potwierdzenia nadal nie jest uznawany za bezpieczne wyłączenie.

## Wyniki zebrane w tym audycie

- Backend: 288 zaliczonych, 3 niepowodzenia, 4 pominięcia, 26 podtestów.
  Dwa niepowodzenia wynikały z edytowalnej receptury opisanej wyżej, jedno
  ze starego założenia o globalnym shutdownie. Dwa krótkie testy po korekcie
  przeszły; pełny test archiwum zaliczono osobno.
- Kompilator, protokoły, adaptery, precyzja, izolacja akwizycji, lease,
  awarie workera, konfiguracja, drzewo, timeline i telemetria:
  **239 zaliczonych, 11 podtestów, 143,44 s**.
- Nowa obsługa błędów workera i kontrolerów: 17 zaliczonych w pierwszym
  zestawie; dodatkowy przypadek nieużywanego kontrolera zaliczony również
  w powyższej szerszej regresji.
- `python -m ruff check app tests`: bez błędów. Usunięto 10 nieużywanych
  importów w istniejących testach.
- Cztery pominięcia dotyczą nieobecnych wzorcowych plików thaTEC/HDF5.
  Testy plików generowanych przez aplikację nie zastępują porównania
  z niezależnym archiwum referencyjnym.
- Profilowanie pełnego UI startu: około 296 ms łącznie pod cProfile,
  w tym 173 ms `run_started`, 134 ms tworzenie timeline i 113 ms obsługa
  zdarzeń/renderowanie. Próba przesunięcia `show()` timeline na koniec
  nie dała mierzalnej poprawy i została wycofana. Nie zmieniono progu testu.
- Powtórna regresja po korekcie wzorca receptury i zakresu testu shutdownu:
  **34 zaliczone**, pełne archiwum sprawdzane osobno.
- Pełne archiwum **297 punktów × Avg32 × 10001 wartości: zaliczone**,
  czas testu 519,29 s. Zapisano 9504 surowe przebiegi sygnału, 32 referencji
  i 815 tła zbieranego przez co najmniej 30 s; rozmiar HDF5 987333664 bajty.
  Sprawdzono kompletność punktów, potwierdzenia parametrów, metadane,
  zgodność średnich w watach z surowymi przebiegami, brak niedokończonych
  transakcji i walidację PyThat. Tylko czekanie po zmianie setpointów było
  pomijane przez test; sprawdzono zaplanowane 298 przerw po 5 s.
  Dowód i SHA256: [simulation-297-avg32.json](simulation-297-avg32.json).
- Natywne Windows: katalog modali, przewijanie, nawigacja i lifetime Qt:
  **12 zaliczonych, 227,44 s**. Test katalogu wymaga przynajmniej 56 okien
  w czterech wariantach; test przewijania sprawdza również brak komunikatów
  `OpenThemeData() failed`, usuniętego źródła sygnału i tracebacków.
- Ponowna kwalifikacja Execution 1000 × 10001: **niezaliczona** z powodu
  maksymalnej przerwy GUI **403,0 ms** przy starcie, wobec progu 350 ms.
  Zapisano wszystkie 1000 widm, status HDF5 `completed`, referencja i każde
  widmo mają 10001 wartości — sprawdzone osobno po nieudanym asercie czasu.
  Aktualizacja drzewa maks. 98,9 ms, podglądu 28,9 ms, 7481 taktów timera.
  Test działał równolegle z testem archiwum i natywnego UI; wcześniejsza
  kwalifikacja wykazywała już 381,8 ms. To ograniczenie jest odtwarzalne,
  nie oznacza utraty danych i nie jest zaliczonym testem płynności.
  Dowód: [gui-stress.json](gui-stress.json).

Kopie wyników JUnit są obok tego raportu; indeks:
[test-results.json](test-results.json), środowisko:
[environment.json](environment.json). Zestawy częściowo się pokrywają;
powyższych liczb nie należy sumować jako unikalnych testów. Pierwszy
`backend.xml` zachowuje historyczne trzy niepowodzenia; ich poprawione
przypadki zaliczono w `corrected-contracts.xml` i `full-archive.xml`.

## Anritsu: rzeczywisty zakres poprawki

- Manifest samego analizatora: `anritsu.abort_acquisition`, następnie
  `storage.flush_checkpoint`. RF OFF jest generowane wyłącznie dla planu
  jawnie sterującego generatorem SG.
- Connect samego analizatora jest bez mutacji RF. Akwizycja, dry run,
  zwykły disconnect i automatyczny shutdown nie wybierają `INST SG` ani
  nie wysyłają `OUTP` tylko z powodu obecności opcji generatora.
- Test transportu odrzuca takie polecenia, również dla urządzenia
  deklarującego obsługę SG i starego manifestu zawierającego RF OFF.
- Ręczny, globalny E-STOP zachowuje globalną funkcję awaryjną. Jawne
  sterowanie SG nadal wymaga potwierdzonego OFF; nie usunięto tej ochrony.
- Po aktualizacji należy ponownie skompilować recepturę. Podgląd:
  [drzewo z abort acquisition](../2026-10-05-execution-performance/tree-abort-only-1440.png).

## Otwarte punkty

- Start Execution nie spełnia progu 350 ms. Dalsza poprawka powinna rozdzielić
  przygotowanie prezentacji od pierwszego renderowania i startu workera,
  zachowując spójny plan, aktualny kursor i dostępność zatrzymania. Samo
  przeniesienie widgetów Qt do wątku roboczego byłoby błędne.
- Brakuje niezależnych wzorcowych plików thaTEC/HDF5 dla czterech testów.
- Oddzielna kwalifikacja na fizycznej aparaturze pozostaje wymagana przed
  twierdzeniem o bezpieczeństwie sprzętowym konkretnego stanowiska.
