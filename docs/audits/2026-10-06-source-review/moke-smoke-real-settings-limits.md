# Limity testowego sweepa MOKE VOUT 2

Data: 2026-10-06. Sprawdzono zapisane `.config/settings.yml` i
`recipes/anritsu_background_reference_smoke_test.yml`, bez połączenia z aparaturą.
Operator potwierdził w rozmowie, że VOUT 2 celowo jest pustym wyjściem testowym DAC.

## Wynik

| Parametr | Zapisane ustawienie |
| --- | --- |
| Kanał | VOUT 2, osobny zatwierdzony profil `dac_test` |
| Zakres stanowiska | −1…+1 V |
| Bezpieczny cel DAC | 0 V |
| Maksymalny krok rampy | 50 mV |
| Maksymalna szybkość | 1 V/s |
| Interwał rampy | 50 ms |
| Termin rampy | 30 s |
| Minimalne ustalanie | 2 s |
| Nastawa przed tłem/referencją | 0 mV |
| Sweep | 10 punktów, włącznie od 0 do 10 mV |
| Oczekiwanie przed każdym widmem | 3 s |

Zapisany plik kończy się jawnym Stop VOUT 2 do zera. Nie zawiera podtrzymania
0,2 V; ten wariant sprawdzono na kopii dokumentu w pamięci. Nie zmieniono pliku
użytkownika ani limitów stanowiska. Wariant zapisany, wariant z automatycznym
zerowaniem bez jawnego Finally oraz wariant z końcowym 0,2 V wszystkie kompilują
się na rzeczywistych ustawieniach, z 10 widmami i tylko MOKE/Anritsu w planie.

Stan końcowy 0,2 V podlega zakresowi stanowiska −1…+1 V; zakres samego ROI
0…10 mV nie ogranicza osobnej jawnej nastawy końcowej.

DAC kwantyzuje napięcie. Przy dziesięciu punktach nominalny odstęp wynosi
1,111111 mV. Ostatni punkt 10 mV jest zaokrąglany do 9,765923 mV, ponieważ kod
zaokrągla do wnętrza zakresu ROI i nie przekracza jego maksimum. Nastawa
końcowa 0,2 V odpowiada około 0,199896237 V. Żądane wartości nie są dowodem
rzeczywistego odczytu; adapter wymaga jego potwierdzenia.

## Czy czegoś brakuje

Dla deklarowanego pustego wyjścia testowego nie znaleziono brakujących pól
profilu potrzebnych do tego sweepa. Dokument kwalifikacji potwierdza deklarację
operatora i zgodę programową, nie pomiar fizycznych limitów elektromagnesu.
Brak kalibracji pola nie blokuje tego testu napięciowego. YAML nie określa
dodatkowych limitów DUT (`recipe_dut_limits` jest pusty); zakres DAC pozostaje
egzekwowany przez niezależny profil stanowiska.

Błąd „Current DAC value is outside…” dotyczy napięcia zastanego przed rampą,
nie wartości 0 mV/10 mV/0,2 V planowanych w tych wariantach. Jeśli aplikacja
używa właśnie sprawdzonego profilu, odczyt przy błędzie musiał wypaść poza
−1…+1 V. Nie ustalono jego wartości dla przebiegu ze zrzutu. Do rozstrzygnięcia
potrzebny jest konkretny odczyt i profil aktywny w tamtym przebiegu; zapisany
plik ustawień nie dowodzi stanu profilu już załadowanego w działającym procesie.

## Weryfikacja

21 ukierunkowanych testów jednostek, ROI, limitów, rzeczywistego pliku sweepa,
stanu końcowego i napięcia zastanego poza profilem przeszło. Dodatkowo kompilacja
na `.config/settings.yml` sprawdza wartości końcowe: −1 V, +1 V i 0,2 V są
dopuszczane; ±1,001 V, 10000 mV, brak jednostki oraz 0,2 mA są odrzucane.
Weryfikacja nie wysyłała żadnych komend do fizycznych urządzeń.
