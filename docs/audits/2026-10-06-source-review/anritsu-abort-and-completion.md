# Anritsu: ABORT i zako?czenie sweepa

## Ustalenia z kodu i dokumentacji

Adapter wysy?a? `ABORT` z `abort_acquisition()` oraz `emergency_off()`.
Symulator bezwarunkowo akceptowa? t? komend?. Udany VISA write by? uznawany
za potwierdzenie zatrzymania. Zg?oszenie z fizycznego MS2830A pokazuje, ?e
jego firmware odrzuca? wysy?any nag??wek.

Oficjalny podr?cznik Anritsu Spectrum Analyzer Function Remote Control,
ed. 57, sekcja 2.7, str. 2-321, podaje `ABOR` jako przyk?ad Stop Sweep.
Str. 2-255 dokumentuje `INIT:SWP?`: 0 = sweep done, 1 = during sweep.
?r?d?o: https://dl.cdn-anritsu.com/en-au/test-measurement/files/Manuals/Operation-Manual/MS269xA/MS269xA_2830A_40A_50A_SpectrumAnalyzer_Remote_Manual_e_57_0.pdf

## Poprawki

- Jawne zatrzymanie i awaryjne przerwanie wysy?aj? dokumentowany skr?t
  `ABOR`, nast?pnie sprawdzaj? `INIT:SWP?`. B??d transportu lub status inny
  ni? 0/+0 pozostawia UNKNOWN; nie deklarujemy potwierdzonego zatrzymania.
- Normalne zako?czenie planu, kt?ry u?ywa tylko odbiornika Anritsu, nie
  wysy?a ABOR ani komend RF. Kompilator pomija ten krok w manife?cie;
  runner pomija go r?wnie? dla starszych manifest?w i swojego fallbacku
  na ?cie?ce sukcesu. Zachowany jest zapis/flush danych.
- Awaria, Stop i watchdog zachowuj? ?cie?k? przerwania akwizycji.
  Plan steruj?cy SG nadal wymaga potwierdzonego RF OFF.
- Symulator odrzuca `ABORT` w kwalifikowanym s?owniku aplikacji, co
  zapobiega ponownemu ukryciu tego regresu. Nie jest to twierdzenie,
  ?e pe?na forma jest niedozwolona we wszystkich urz?dzeniach SCPI.
- Akwizycja pojedynczego widma nadal przywraca wcze?niej potwierdzony
  tryb Continuous. Zako?czenie sweepa nie uruchamia Continuous, je?li
  operator pierwotnie wybra? Single.

## Weryfikacja

Testy obejmuj? nominalny worker measurement/dry run z zapisem HDF5,
stary manifest, brak komend RF i ABOR po sukcesie, jawne zatrzymanie,
niepotwierdzony readback, b??dy transportu, obs?ug? RF OFF, b??dy i
anulowanie runnera, kompilacj? oraz projekcj? drzewa UI.
Nie wysy?ano komend do fizycznego urz?dzenia. Akceptacja skr?tu ABOR
przez firmware stanowiska wymaga sprawdzenia przy rzeczywistym Stop;
po normalnym uko?czeniu sweepa komenda nie jest ju? wysy?ana.


Przycisk UI `Abort acquisition` by? po??czony z `emergency_off()`;
zmieniono go na `abort_acquisition()`, aby nie wybiera? SG i nie sterowa?
RF. Regresja pokazuje stron? przy 1500x900, klika widoczny przycisk,
weryfikuje wywo?anie i zapisuje screenshot.

Wyniki: 46 test?w zakresu shutdownu/projekcji UI/potwierdze?; 21 test?w
shutdownu runnera/przycisku Anritsu/ko?cowego stanu MOKE (0 lub 0,2 V).
Szersza kontrola kompilatora, adapter?w, anulowania i cleanupu: 252
zaliczone; dwa stare oczekiwania automatycznego abortu na ?cie?ce sukcesu
zaktualizowano i ponownie sprawdzono w powy?szej grupie 21 test?w.
Ruff dla wszystkich zmienionych plik?w Python: bez b??d?w.

Ograniczenie renderingu: screenshot offscreen pokazuje poprawn? geometri?,
ale font ?rodowiska testowego wy?wietla kwadraty zamiast liter. Nie
potwierdzono jako?ci typografii na podstawie tego obrazu.


Aktualizacja po analizie logu VISA: runner pozostawia Single pomiedzy punktami receptury; przywracanie Continuous dotyczy domyslnej recznej akwizycji. Szczegoly i nowsza weryfikacja: [anritsu-acquisition-traffic.md](anritsu-acquisition-traffic.md).
