# Anritsu: regresja TRAC:TYPE? — 5.10.2026

Zgłoszenie z fizycznego stanowiska: `INIT:CONT?` zwraca `1`, `INST?` zwraca `SPECT`, a następujące `TRAC:TYPE?` kończy się `VI_ERROR_TMO (-1073807339)`.

Zapytanie dodano w niezacommitowanych naprawach sweepów po HEAD `acce560`. Nie występowało w `start_single_sweep()` tego commitu. Ponieważ ręczne pobieranie widma również używa tej funkcji, regresja objęła obie ścieżki. Poprzednia kwalifikacja SIM nie wykryła braku odpowiedzi rzeczywistego urządzenia.

Usunięto zapytanie, bez zastępowania timeoutu fikcyjną odpowiedzią. Jawne żądanie nowego widma przygotowuje teraz bufor Trace A przez `TRAC1:TYPE WRIT`, tak jak istniejąca ścieżka Live. Następnie wykonuje `INIT:MODE:SING`, `*WAI`, kontrolę `INIT:SWP?` i dopiero odczyt `TRAC? TRAC1`. Dotychczasowy tryb Continuous zostaje przywrócony po poprawnym odczycie.

Przygotowanie zmienia tryb bufora Trace A na Write; nie zmienia nastaw RF, pasm, detektora, tłumienia ani wyjść źródeł. Samo konfigurowanie wybranych parametrów nadal nie przełącza bufora. Błąd wysłania polecenia przygotowania zatrzymuje akwizycję przed startem i odczytem.

Test regresji symuluje urządzenie odmawiające odpowiedzi na `TRAC:TYPE?`, uruchamia dwie kolejne akwizycje przez dispatch używany przez UI i sprawdza kolejność poleceń, osobne identyfikatory widm oraz zachowanie stanu Continuous w obu wariantach. Test awarii przygotowania sprawdza brak startu i odczytu danych. Log i manifest poprawki zapisano w `docs/audits/2026-10-05-production-followup/trace-timeout-*`. Starszy manifest tego katalogu opisuje stan sprzed tej poprawki.

Weryfikacja: **153 passed, 5 subtests passed, 66,13 s** (`test_sweep_release_contracts`, `test_anritsu_fast_acquisition`, `test_sweep_recovery_reference`, `test_adapters_and_runner`).

Nie wysyłano poleceń do fizycznego stanowiska. Uruchomiona wcześniej aplikacja wymaga restartu, aby załadować zmieniony kod.
