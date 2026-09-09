# Weryfikacja wdrożenia charakteryzacji field line

Stan: audyt końcowy nadal otwarty. Poniższe dowody dotyczą kodu i testów offline, nie pomiaru na fizycznym stanowisku. Pełny zakres pozostaje określony przez PLAN_CHARAKTERYZACJA_FIELD_LINE.md.

## Główne wymagania operatora

| Wymaganie | Dowód wykonawczy i test | Granica dowodu |
|---|---|---|
| A i B pracują w STOP podczas serii | field_worker.py; test_both_stop_before_enable_and_restore_after_both_off i test_each_channel_restores_its_own_original_mode | Atrapa urządzenia; sprawdzona kolejność i wszystkie kombinacje poprzednich polityk stop/warn_clamp |
| Każdy kanał odzyskuje własną poprzednią politykę | Te same testy, również anulowanie; test_restore_failure_attempts_other_channel_and_reports_fault | Błąd przywracania jest raportowany, nie przedstawiany jako sukces |
| Compliance B pomija pozycję listy | field_series.py; test_field_compliance_skips_target_and_keeps_zero_and_repeats | Zachowane pominięcie, powtórzenia B, B=0 i nowy segment historii po recovery |
| Zmiana kanału przywraca jego własny formularz | test_channel_and_dimension_drafts_are_independent; test_restart_preserves_both_channels_modes_and_field_list | Restart ustawień sprawdzony przez rzeczywisty backend INI; zapis do rejestru Windows niezweryfikowany w sandboxie |
| Nastawy sprzętu pochodzą ze wspólnej karty | test_build_reads_fresh_hardware_configuration_without_cached_copy | Porównanie całego KeithleySourceRequest dla obu kanałów po zmianie providera; nie dowodzi fizycznego readbacku |
| Modal pokazuje scenariusz wykonywany po zgodzie | test_confirmed_modal_executes_exact_reviewed_snapshot; test_cancelled_modal_never_acquires_or_mutates | Rzeczywisty worker i raporty z atrapą urządzenia; zatwierdzenie dialogu sterowane testem |
| Przerwanie blokuje dalsze włączenia | test_field_worker_uses_reserved_controller_thread | Rzeczywista kolejka kontrolera, atrapa urządzenia; stop, emergency_off i błąd odczytu B |

## Otwarte obszary pełnego audytu planu

- Pełna integracja rzeczywistego adaptera z symulatorem i zamknięciem okna podczas aktywnej serii.
- Jawne okno i jakość dopasowania w dotychczasowym raporcie pojedynczej krzywej; minimum trzech różnych prądów jest już wymagane, ale nie zastępuje tych metadanych.
- Porównanie każdej pozycji planu z implementacją, w szczególności szczegółowych wymagań instrukcji Rigola i kwalifikacji danych naukowych.
- Weryfikacja pełnego okna z rozwiniętym panelem serii w wąskim układzie; samodzielna karta ma osobny test geometrii.

Nie należy traktować tej tabeli jako zamknięcia całego planu. Wyniki kolejnych uruchomień i ograniczenia są zapisywane w IMPLEMENTATION_CHARACTERIZATION_FIELD_LINE.md.
