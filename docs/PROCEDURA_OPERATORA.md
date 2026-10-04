# Procedura operatora — Lab Control v1

## Przed pierwszym uruchomieniem

1. Potwierdź okablowanie, sztuczne obciążenie, wspólną masę oraz fizyczny E-STOP/interlock.
2. W zakładce **Ustawienia** wpisz zasoby VISA i limity DUT; nie używaj przykładowych limitów jako danych katalogowych.
3. Połącz każde urządzenie z Dashboardu i porównaj `*IDN?`, model, numer seryjny oraz firmware z profilem.
4. Dla Rigola potwierdź oba wyjścia `OFF`; opcjonalne funkcje są dostępne tylko, gdy capability probe zwróci odpowiedź.
5. Dla Anritsu wpisz limit mocy na wejściu RF, zakres częstotliwości oraz zakres reference level. Przed recepturą kwalifikuj na tym firmware `INIT:CONT OFF`, `INIT:IMM`, `*OPC?`, `ABORT` i ustaw `single_sweep_mode: standard_scpi_opc`.
6. Dla Keithleya potwierdź model 2600, kanały, sense mode, dopuszczalny I/V/P oraz zachowanie compliance na sztucznym obciążeniu.
7. Zapisz konfigurację. Zapis unieważnia zatwierdzenie; odpowiedzialna osoba zatwierdza profil ponownie z użyciem frazy w GUI.

## Wykonanie pomiaru

1. Skompiluj recepturę. Sprawdź liczbę punktów, zakresy, przewidywany czas i katalog wyników.
2. Użyj najpierw receptury bezenergetycznej lub pojedynczego punktu na sztucznym obciążeniu.
3. Dla zasilania DUT stosuj wyłącznie sekwencję: konfiguracja przy `OUTPUT OFF`, **ARM**, osobne potwierdzenie **OUTPUT ON**, czas ustalania, pomiar.
4. Po runie sprawdź status HDF5 i indeks CSV. Plik wynikowy zawiera snapshot receptury, ustawień, IDN, capabilities i dziennik zdarzeń.

## Widmo Anritsu: akwizycja, korekcja i prezentacja

1. Użyj **Acquire once** do pojedynczego widma albo **Start Live** do kolejnych ramek. **Instrument settings** zawiera ustawienia analizatora i uśrednianie. **Abort acquisition** przerywa akwizycję.
2. W bloku **Correction** wybierz **Background** albo **Reference**. Oba warianty korzystają z tej samej akwizycji oraz widoków **Current spectrum** i **Spectrogram**.
3. **Configure background…** pozwala zarejestrować nowe tło lub wczytać zapisane HDF5. Ustaw wybrany punkt pracy tła, np. niski prąd Keithleya. Wyłączenie wyjść Keithleya nie jest wymagane; formularz nie zmienia ich stanu ani nastaw. Zachowaj okablowanie i ustawienia analizatora. Rejestracja kończy się po osiągnięciu zarówno minimalnego czasu, jak i wymaganej liczby pełnych przebiegów. Po zapisie tła formularz zamyka się i włącza korekcję. Przywróć punkt pracy pomiaru i samodzielnie naciśnij **Start Live**.
4. **Configure reference…** pozwala użyć bieżącej ramki, pozyskać pojedynczą lub uśrednioną referencję oraz wczytać lub zapisać HDF5. Wybierz odejmowanie w dB, dzielenie, dodawanie/odejmowanie mocy albo mnożenie. Odejmowanie tła daje resztę ze znakiem w W. Odejmowanie mocy z prezentacją w dBm pozostawia nieokreślone punkty tam, gdzie wynik mocy jest niedodatni; aplikacja nie wypełnia tych luk.
5. W bloku **Filters** włącz **Narrow peaks**, **Denoise** lub **EMI lines** i użyj **Filter settings…** do konfiguracji. Korekcja działa przed filtrami. EMI wymaga wartości dB/dBm oraz historii ramek. Filtry zmieniają podgląd; surowe dane pozostają zachowane.
6. Blok **Plot** zawiera **Auto peaks**, widoczność markerów, **Compare input**, **Peak settings…**, tabelę pików i **Axes…**. **Compare input** pokazuje wejście przed filtrami cyfrowymi i wynik filtrowania; przy włączonym tle są to linie **Raw − background [W]** i **Filtered · Raw − background [W]**. Legenda opisuje rzeczywiście wyświetlone dane. Jeśli weryfikacja tła lub przetwarzanie nie powiedzie się, wykres pokazuje przyczynę zamiast zastępować wynik surowym widmem. Ponowny odczyt ustawień uruchamia ponowną weryfikację. Szerokości oraz odległości pików podawaj z jednostką częstotliwości. Zakres X/Y dotyczy wykresu; nie zmienia zakresu pomiarowego analizatora. Ręczny zakres Y jest zwalniany, gdy korekcja zmienia jednostkę. W niskim oknie wszystkie bloki ustawień są dostępne pod **Settings…**.
7. **Save / record…** otwiera zapis bieżącego widma lub rejestrację serii. Zapis pochodnej zachowuje surową ramkę, jednostkę i informację o przetwarzaniu. Rejestracja tła nie uruchamia automatycznie rejestracji pomiaru ani Live.

### Uśrednianie, ochrona sygnału i jakość korekcji

**Avg: Off / 4 / 8 / 16 / 32 / 64** wybiera liczbę ostatnich odebranych ramek uśrednianych w mocy liniowej. W **Filter settings…** można podać dowolną liczbę 1–64 i czas przerwy resetującej średnią. Kolejność jest wspólna dla widma, okna pływającego i spektrogramu: średnia mocy → Background lub Reference → filtry podglądu. Po zmianie punktu pracy użyj **Quality… → Reset average**. Nowy Live, zmiana tła lub konfiguracji analizatora również rozpoczyna nową historię. Licznik opisuje odebrane ramki; nie potwierdza niezależności przebiegów analizatora.

W **Filter settings…** zaznacz **Protect bands** i podaj pełne pasma badanego sygnału, razem z ogonami i marginesem RBW. Dodatkowe pasma zapisuj np. `780 MHz .. 950 MHz; 1.2 GHz .. 1.4 GHz`. Ochrona dotyczy Narrow peaks, EMI i Denoise. Bez niej wąski rzeczywisty rezonans może zostać usunięty. Domyślnie tabela i markery pików mierzą wejście po uśrednianiu i korekcji, przed filtrami podglądu. **Peak settings… → Filtered preview** świadomie przełącza pomiar na wygładzony wynik. Amplituda, szerokość i pole piku mogą wtedy ulec zmianie.

**Quality…** pokazuje liczbę i czas uśrednianych ramek, opisowy rozrzut czasowy, wiek tła oraz stan kwalifikacji. Rozrzut i progi filtrów nie są przedziałem ufności. Dostępne są diagnostyka stabilności zapisanego REF (Allan/autokorelacja), trening modelu wyłącznie z tła, walidacja na oddzielnych zapisach i wczytanie modelu. Wybrany model działa również w zwykłym Live i spektrogramie. Wymaga zgodnego profilu i kwalifikowanych pasm kontrolnych wolnych od sygnału. Przekroczenie zakresu kalibracji lub kolizja pasma chronionego z kontrolnym zatrzymuje przetwarzanie z komunikatem. Chronione pasma modelu automatycznie obejmują dalsze filtry.

Ustawienia średniej, czasu resetu, pasm chronionych i źródła pomiaru pików zapisuje **SAVE SETTINGS**. Eksport wyniku pochodnego zawiera parametry przetwarzania i identyfikatory tła/modelu, a obok zachowuje źródłową ramkę dBm. Pojedynczy eksport podglądu nie zastępuje rejestracji wszystkich surowych ramek wykorzystanych w średniej. Tło przy małym prądzie definiuje stan odniesienia; wynik pokazuje różnicę względem tego stanu, także gdy był w nim obecny sygnał próbki.

Do powtarzalnego sprawdzenia przenoszenia znanych sygnałów służy `python -m tools.qualify_spectrum_preview --output nowy-raport.json`. Raport obejmuje błędy amplitudy, FWHM i pola, szum resztkowy oraz przykład utraty niechronionego piku. Jest testem syntetycznym. Kwalifikację stanowiska wykonaj przy ustalonych RBW, VBW, detektorze i torze RF: zapisz oddzielne serie tła, powtórz pomiary sygnałów o znanej mocy i szerokości, porównaj wyniki przed/po korekcji i sprawdź dryft w czasie. Zakres ważności i tolerancje muszą wynikać z tych pomiarów; program nie nadaje ich automatycznie.

## Zatrzymanie i awaria

- **Pause after point** kończy bieżący checkpoint, nie pozostawia niekompletnego trace i zatrzymuje dalsze kroki.
- **Stop safely** żąda przerwania, przeprowadza rampę Keithleya do zera, wyłącza Rigola i przerywa Anritsu.
- **E-STOP w aplikacji** natychmiast wysyła najlepszą próbę OFF/ABORT. Nie zastępuje fizycznego odcięcia energii lub RF.
- Gdy komunikacja, compliance albo limit mocy jest nieprawidłowy, nie wznawiaj runu automatycznie. Odłącz energię, zapisz raport i rozpocznij od kwalifikacji pojedynczego punktu.
