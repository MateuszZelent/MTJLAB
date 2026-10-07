# Zastane 2 V na testowym VOUT 2 i edycja MIN/MAX

Data: 2026-10-06. Nie wysyłano poleceń do fizycznej aparatury.

## Przyczyny

Rampa sprawdzała punkt startowy przeciw zakresowi stanowiska przed rozróżnieniem
zwykłej nastawy i zerowania. Przy zastanym 1,99987793 V oraz profilu −1…+1 V
blokowane było także zero. Samo zawężające MIN/MAX operatora nie mogło poszerzyć
profilu stanowiska. Jego edytor zamykał się przed walidacją zakresu, a odrzucenie
było zgłaszane na stronie, poza zamkniętym modalem.

## Zakres poprawki

- Tylko zatwierdzony profil `dac_test`, zadeklarowany jako puste wyjście testowe,
  dopuszcza kontrolowany powrót spoza zakresu do bezpiecznego zera.
- Działa zarówno Stop VOUT, jak i jawna nastawa 0 V przed referencją w sweepie.
- Odczyt początkowy musi być skończony i mieścić się w fizycznym zakresie protokołu
  −10…+10 V. Każdy SET zmniejsza moduł napięcia, bez zmiany znaku; działają
  dotychczasowe ograniczenia kroku, szybkości, interwału i terminu.
- Każdy krok wymaga świeżego potwierdzenia. Stary niezmieniony odczyt może być
  ponownie sprawdzony w terminie, ale nie uznawany za postęp. Wzrost modułu
  napięcia lub odczyt po drugiej stronie zera przerywa odzyskiwanie.
- W odzyskiwaniu Live nie może zmienić celu zera. Zwykła nastawa niezerowa nadal
  nie może wystartować spoza profilu. Profil Kepco/current nie otrzymuje tego
  wyjątku — przypadek dotyczy jawnie pustego wyjścia operatora.
- Nie zmieniono zapisanych limitów stanowiska ani zakresów nowych nastaw.
- MIN/MAX są walidowane przed zamknięciem modala. Błąd pozostaje w nim widoczny
  wraz z zakresem kanału. Poprawny zakres jest stosowany na stronie, bez SET_VOUT.
  To nadal zakres operatora zawężający profil stanowiska, a nie edycja tego profilu.

## Weryfikacja

Testy obejmują odzyskiwanie z +2 V i −2 V, obie drogi do zera, komendy wyłącznie
wybranego kanału, monotoniczność, ograniczenia kroku i fizyczny czas rampy,
stary odczyt bez powtórzenia SET, odczyt zwiększający napięcie oraz blokadę
niezerowej nastawy i niekwalifikowanego wyjątku dla Kepco. Pełna strona MOKE
zastaje 2 V i zeruje je przyciskiem. Modal odrzuca ±3 V z widocznym błędem,
po czym zapisuje ±900 mV przy niezmienionym profilu ±1 V.

Przebieg runnera zaczyna od 2 V, wykonuje jawne zero i kończy poprawnie na
0,2 V lub na automatycznym zerze. Sprawdzono również wcześniejsze scenariusze
zakończenia, fizyczne ograniczenia protokołu oraz wspólny modal Fluent.
Zrzuty: `moke-limits-validation.png`, `moke-recovered-zero.png`.

Końcowe sprawdzenia: 42 testy nowego odzyskiwania, podwójnych kanałów, strony
i modali; 10 testów sweepa i stanów końcowych; 2 testy pełnej ścieżki runnera
z zastanym 2 V — przeszły. Szerszy przebieg całego
`test_fluent_moke_field_workflow.py` przerwano po zatrzymaniu postępu; nie jest
zaliczony jako pełna regresja. Związany test przycisku Edit i testy adaptera
sprawdzono osobno. Ruff zmienionych plików przechodzi.
