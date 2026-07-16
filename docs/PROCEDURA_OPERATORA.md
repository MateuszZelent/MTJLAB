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

## Zatrzymanie i awaria

- **Pause after point** kończy bieżący checkpoint, nie pozostawia niekompletnego trace i zatrzymuje dalsze kroki.
- **Stop safely** żąda przerwania, przeprowadza rampę Keithleya do zera, wyłącza Rigola i przerywa Anritsu.
- **E-STOP w aplikacji** natychmiast wysyła najlepszą próbę OFF/ABORT. Nie zastępuje fizycznego odcięcia energii lub RF.
- Gdy komunikacja, compliance albo limit mocy jest nieprawidłowy, nie wznawiaj runu automatycznie. Odłącz energię, zapisz raport i rozpocznij od kwalifikacji pojedynczego punktu.
