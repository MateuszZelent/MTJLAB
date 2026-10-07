# Tło, szum i porównywanie widm — 2026-10-05

## Interpretacja zgłoszenia

Na zrzucie operatora widoczne są `Avg: Off`, tło z 30 przebiegów oraz dodatnie
i ujemne pozostałości przy wąskich liniach poniżej około 1 GHz. Sam obraz nie
rozstrzyga, czy to niestabilne zakłócenia, dryft częstotliwości/amplitudy,
czy sygnał badanego układu. Ujemna różnica mocy jest poprawnym wynikiem
odejmowania; nie należy zastępować jej wartością bezwzględną.

`BackgroundProfileBuilder` używa średniej i wariancji w watach, liczonej
strumieniowo metodą Welforda. Dłuższe zbieranie tła zmniejsza jego niepewność,
ale pojedyncza klatka sygnału nadal wnosi własny szum. Nie wprowadzono
automatycznego wycinania pików ani wygładzania profilu tła.

Zalecana próba: `Avg: 16` lub `32`, następnie nowe klatki `Start Live`.
Tło można zebrać przez 120–300 s przy tej samej konfiguracji analizatora.
Po zmianie RBW/VBW, detektora lub toru wejściowego należy zebrać nowe tło.
Przy niezależnych, stacjonarnych próbkach odchylenie średniej maleje jak
1/sqrt(N); korelacja i dryft ograniczają poprawę. Uśrednianie mocy, wpływ
VBW i ograniczenia uśredniania opisuje dokumentacja producenta:
[Keysight — Noise Measurements](https://helpfiles.keysight.com/csg/89600B/Webhelp/Subsystems/powerspectrum/content/ps_noisemeasurements.htm).

## Zmiany

- Widoczne obok filtrów przełączniki `Raw`, `Raw − BG`, `Raw − Ref` pozwalają
  pokazać do trzech krzywych z jednej surowej klatki. Wszystkie różnice są
  obliczane w W. Wyłączenie wszystkich przełączników przywraca dotychczasowy
  widok z uśrednianiem, korekcją i filtrami. Porównania Raw nie są uśredniane.
- `Auto units`, `Linear: W`, `Log: dBm` zmieniają reprezentację widoku.
  dBm pomija nie-dodatnie moce i wyświetla ich liczbę. Powrót do W odtwarza
  pełną różnicę ze źródła, łącznie z ujemnymi wartościami.
- Nie przelicza się ilorazów ani dB na jednostki mocy. UI wyjaśnia niedostępność.
- Tło przechodzi istniejące kontrole wieku, siatki i konfiguracji; referencja
  przechodzi istniejące kontrole zgodności. Brakujące/nieważne krzywe nie są
  zastępowane surowym sygnałem pod błędną etykietą.
- Jednostki i krzywe są współdzielone z niezatrzymanym oknem pływającym.
  Zapis ręczny korzysta z widocznych danych pochodnych wraz z jednostką
  i opisem operacji, zachowując surową klatkę dBm jako źródło.
- Kreator tła oferuje dodatkowo 120 s i 300 s oraz wyjaśnia konieczność
  uśredniania także pomiaru sygnału. Minimum kompletnych przebiegów pozostaje.
- Kompaktowy układ skraca etykiety, zachowując opisy dostępności i podpowiedzi.

## Weryfikacja

33 testy porównania, modelu wyświetlania, workbench i geometrii Fluent przeszły.
Kontrole obejmują przeliczenia W/dBm, znak różnicy, nakładanie krzywych,
odrzucanie nieaktualnego tła, powrót do widoku podstawowego oraz renderowanie
w jasnym i ciemnym motywie, także przy 820 × 700.

Drugi zestaw: 42 testy kreatora tła, filtrów, przetwarzania podglądu i zapisu
porównania przeszły (jeden test powtórzony z pierwszego zestawu; łącznie
74 różne testy). Test zapisu sprawdza surowe źródło, znak danych pochodnych,
jednostkę W i identyfikację profilu tła.

Ruff dla zmienianych plików przechodzi. Pełne `ruff check app tests` nadal
wskazuje 10 wcześniejszych F401 w `test_spectrum_correction_layout.py` oraz
`test_sweep_release_contracts.py`.

Testy używają symulacji/moków; nie wykonano kwalifikacji na fizycznym Anritsu.
Nie zmieniono parametrów aparatury ani ustawień operatora.
