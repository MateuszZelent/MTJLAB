# Filtr wąskich pików w Live

Wdrożono dodatkowy tryb **Narrow-peak rejection · display only** w panelu
Cleanup analizatora. Wykrywa wąskie dodatnie i ujemne odstępstwa osobno w każdym
widmie, więc nie wymaga stałej amplitudy, treningu ani stałych częstotliwości zakłóceń.
Poza wyznaczoną maską zastąpienia wartości pozostają dokładnie niezmienione.

## Jak włączyć na widoku ze zrzutu

1. Uruchom aktualną wersję aplikacji. W Current spectrum pozostaw View: **Raw / reference**
   i operację **Signal − reference [dB]**, jak na przesłanym zrzucie.
2. W **Analyze trace** wybierz **Processed [dB]**. Wybór Raw analizowałby widmo przed odejmowaniem referencji.
3. W **Cleanup** wybierz **Narrow-peak rejection · display only**.
4. W **Parameters…** ustaw **Maximum peak width** i **Outlier threshold**.
   Domyślnie są to **6 MHz** i **6 × lokalna skala szumu**.
5. Jeśli znasz pasmo właściwego sygnału, zaznacz **Protect band** i podaj początek/koniec
   z zapasem na skrzydła sygnału oraz RBW. Filtr zachowuje całe cechy dotykające tego pasma.
6. Zaznacz **Analysis (Cleaned)**, aby widzieć wynik. Pozostaw też **Processed (Ref op)**
   do porównania wejścia i wyjścia. **Highlight replacements** zaznacza zmienione ekstrema.
7. Użyj **Start Live**. Obliczenia uruchamiają się po nadejściu kompletnego widma;
   status pokazuje liczbę ekstremów i zastąpionych binów.

Zmiana parametrów działa w bieżącej sesji aplikacji. Domyślny tryb pozostaje Raw;
filtr włącza się świadomie. Nie jest automatycznie włączony w osobnym widoku
Raw − background [signed W] ani w ilościowej korekcji zapisywanej przez Background correction.
Wąskie filtrowanie działa na wybranym źródle panelu Cleanup, w tym na wyniku
**Signal − reference [linear power]**; jego jednostka jest zachowana.

## Uzasadnienie i ograniczenie fizyczne

Inspiracją jest filtr Hampela w dziedzinie częstotliwości: wykrywanie odstających
wartości względem lokalnej mediany i zastępowanie ich lokalnym oszacowaniem.
Allen zastosował tę zasadę do zakłóceń sinusoidalnych w EMG:
[Allen, 2009, DOI 10.1016/j.jneumeth.2008.10.019](https://pubmed.ncbi.nlm.nih.gov/19010353/).
To inny typ danych niż nasz pomiar magnetyczny; wyników skuteczności z EMG
nie przenosi się na analizator.

Dodano osobną bramkę szerokości na oryginalnym przebiegu. Szerokość jest mierzona
na połowie lokalnej prominencji, zgodnie z geometryczną zasadą opisaną w
[dokumentacji peak_widths](https://docs.scipy.org/doc/scipy/reference/generated/scipy.signal.peak_widths.html).
Implementacja korzysta z NumPy i nie wymaga dodania SciPy do zależności aplikacji.

**Wąski sygnał użyteczny i wąskie zakłócenie mogą wyglądać identycznie.**
Bez chronionego pasma lub niezależnej wiedzy o szerokości docelowego sygnału
filtr nie gwarantuje jego zachowania. Nie uznaje pików za dowiedzione EMI.
Na zrzucie oś Y jest w dB; mierzona szerokość jest szerokością cechy w tej dziedzinie,
nie fizyczną szerokością połowy mocy ani wynikiem dekonwolucji RBW.
Wąskość wizualna zależy również od powiększenia i RBW.

## Algorytm

1. Sprawdź skończoność danych, zgodność wektorów, monotonność i równomierną siatkę.
   Malejącą siatkę obsłuż wewnętrznie w odwróconej kolejności, zwracając oryginalny porządek.
2. Przelicz zadany limit szerokości z Hz na biny, korzystając z rzeczywistego kroku siatki.
   Wyznacz nieparzyste okno mediany: `2 × ceil(4 × width_bins) + 1`, co najmniej 9 binów.
3. Wyznacz lokalną medianę `b(f)` i resztę `r(f) = y(f) − b(f)`.
   Zastosuj odporną lokalną skalę z mediany bezwzględnych odchyleń reszty,
   ze współczynnikiem 1,4826 i podłogą wynikającą z precyzji numerycznej.
   Jest to skala heurystyczna, nie kwalifikowana niepewność pomiaru.
4. Wyszukaj dodatnie i ujemne lokalne ekstrema przekraczające zadany mnożnik skali.
   Nie używaj historii czasowej ani stałego progu amplitudy w dB/W.
5. Na oryginalnym przebiegu zmierz lokalną prominencję i szerokość przy 50% prominencji.
   Limitowanie lokalnego sąsiedztwa ogranicza koszt i oznacza, że jest to prominencja lokalna.
6. Przyjmij tylko cechy o szerokości nie większej niż limit użytkownika.
   Dodatkowo sprawdź szerokość przy 80% prominencji: nie może przekraczać trzech limitów.
   Pozostaw cechy z szerokimi skrzydłami, które mogłyby należeć do rezonansu.
7. Zasięg zastąpienia określ w reszcie względem odpornego tła, do 5% wysokości odstępstwa.
   Ujemna sąsiednia cecha nie może sztucznie powiększyć zasięgu dodatniej i odwrotnie.
   Zasięg też nie może przekraczać trzech limitów szerokości.
8. Zachowaj całą cechę, jeżeli jej zasięg wraz z kotwicami przecina chronione pasmo.
   Nie ekstrapoluj pików przy krawędziach widma.
9. Scal nachodzące maski przed wybraniem kotwic. Odrzuć nadmiernie szerokie grupy;
   nie dopasowuj wartości wewnątrz grupy jako kotwic dla sąsiedniego piku.
10. W przyjętych obszarach interpoluj wyłącznie resztę między czystymi kotwicami,
    a następnie dodaj lokalne tło. Poza maską kopiuj wartości wejściowe bez wygładzania.
11. Zwróć wynik, dokładne indeksy zastąpionych binów, centra oraz komunikaty ograniczeń.
    Markery zmian są informacyjne; nie wybierają wierszy niezwiązanych pików w tabeli.

Filtr nie zeruje przedziałów, nie obcina wartości ujemnych i nie uśrednia globalnie
widma. Interpolacja pozostaje rekonstrukcją przybliżoną: w zmienionych binach
nie można twierdzić, że amplituda, pole lub kształt sygnału są zmierzone.

## Koszt i ograniczenia zasobów

- Mediany są liczone porcjami po 512 pozycji; okno nie przekracza 257 binów.
  Największa macierz okien pojedynczej porcji ma około 1 MiB danych float64,
  poza buforami i tymczasowymi kopiami NumPy. Nie jest to limit całkowitego RSS procesu.
- Maksymalna liczba kandydatów wynosi 512, a wejście najwyżej 100 001 punktów.
- Koszt to `O(N × W + P × W)` przy ograniczonym `W ≤ 257`, `P ≤ 512`;
  pamięć robocza jest `O(N + 512 × W)`. Nie powstaje macierz `N × N`.
- Jeżeli wymagane okno przekracza budżet, pojawia się błąd z prośbą o zmniejszenie
  szerokości. Poniżej rozdzielczości siatki lub przy zbyt krótkim widmie filtr
  pozostawia dane i pokazuje przyczynę.
- Zbyt wiele kandydatów albo maska obejmująca ponad 10% widma powodują pozostawienie
  całego przebiegu z komunikatem. To chroni przed usuwaniem gęstej struktury.
- Istniejący worker wykonuje najwyżej jedno obliczenie i zachowuje tylko najnowszy
  oczekujący podgląd. Nie blokuje wątku GUI ani nie odrzuca zapisów RAW.

## Pomiary wykonane 4 października 2026

Źródło: `measurements/spectrum_reference_agent_20261004_current_and_magnets_off.h5`.
Pierwsze RAW REF użyto jako stałej wcześniejszej referencji; każde z kolejnych 29 REF
porównano z nim w dB. Nie jest to SIGNAL ani test zachowania magnetycznego sygnału.
Przykład graficzny wykorzystuje zawsze pierwsze późniejsze REF — nie wybrano go
według skuteczności filtra. SHA-256 źródła po analizie pozostał niezmieniony.

Końcowy pomiar przy **6 MHz**, **6 × skala**, **10 001 punktów**, 29 widmach:
mediana **34,88 ms**, p95 **39,78 ms**, maksimum **40,88 ms**.
Obejmuje konwersję wyniku do tuple, nie obejmuje VISA, zapisu, rysowania ani osobnego
fitowania pików. Nie stanowi gwarancji 20 Hz ani niezależnej kwalifikacji całej aplikacji.
W stałym pierwszym przykładzie zmieniono 10 binów; największy złożony pik pozostał.
Nie spełniał warunku bardzo małej szerokości przy tej lokalnej geometrii.

Próba diagnostyczna z limitem **12 MHz**: mediana 55,46 ms, p95 59,34 ms,
maksimum 65,15 ms; pierwszy przykład ma 21 zmienionych binów. Szerszy limit
zwiększa również ryzyko zmiany właściwego sygnału. Nie jest automatycznie wybierany
na podstawie tego przykładu; domyślny limit pozostaje 6 MHz.

Końcowe artefakty:

- [Diagnostyka 6 MHz](artifacts/spectrum-narrow-spikes/fresh-reference-final-6MHz.json)
- [Porównanie 6 MHz](artifacts/spectrum-narrow-spikes/fresh-reference-final-6MHz.png)
- [Diagnostyka 12 MHz](artifacts/spectrum-narrow-spikes/fresh-reference-final-12MHz.json)
- [Podgląd w aplikacji](artifacts/spectrum-narrow-spikes/live-processed-width-filter.png)

Reprodukcja bez komunikacji z urządzeniem:

```powershell
python -m tools.diagnose_spectrum_narrow_spikes measurements/spectrum_reference_agent_20261004_current_and_magnets_off.h5 --output artifacts/spectrum-narrow-spikes/nowy-raport.json --max-width "6 MHz" --threshold 6
```

Docelowe pliki muszą być nowe. Narzędzie odczytuje RAW i tworzy osobny JSON/PNG;
nie nadpisuje źródła ani nie zapisuje wyniku jako kwalifikowanego pomiaru.

## Weryfikacja

Testy obejmują oba znaki i zmienną amplitudę, szeroki rezonans z nałożonym pikiem,
szerokie skrzydła, krawędzie, ochronę pasma wraz z kotwicami, siatkę malejącą,
zgodność skalowania do signed W, niezmienność wartości poza maską, szum bez pików,
niedozwolone jednostki, niepoprawne dane oraz ograniczenia zasobów.
Testy UI pokazują rzeczywisty widget, sprawdzają geometrię, przejście dwóch kolejnych
widm przez worker, źródło Processed [dB], markery i brak żądania do urządzenia.
Parametry obejrzano w jasnym 720 px i ciemnym 580 px oknie.
Końcowy raport regresji po dodaniu informacyjnych markerów:
`artifacts/spectrum-narrow-marker-final.xml`: **60 PASS**.
Dodatkowa regresja widgetu wykresu:
`artifacts/spectrum-narrow-plot-widget.xml`: **8 PASS**. Ruff `app tests`
oraz narzędzie diagnostyczne przeszły; `git diff --check` bez błędów.

Kwalifikacja na rzeczywistym SIGNAL pozostaje otwarta. Potrzebne jest pasmo lub
niezależna informacja o minimalnej szerokości sygnału oraz porównanie z oryginalnymi
RAW. Żaden próg szerokości sam nie zastąpi tego sprawdzenia.
