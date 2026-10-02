# MOKE: odblokowanie profilu na polecenie operatora

Data: 2026-10-02. Polecenie w rozmowie: „ok to to ustaw i odblokuj”.

Ten dokument rejestruje zgodę operatora na dostęp do sterowania w programie.
**Nie jest protokołem kwalifikacji fizycznej ani potwierdzeniem bezpiecznych
limitów elektromagnesu.** Otwarte kwestie opisuje
[audyt końcowy](MOKE_BOX_FINAL_SAFETY_AUDIT_2026-10-02.md).

Wybrany logiczny kanał: VOUT2. Zasilacz zgłoszony przez operatora:
Kepco BOP 72-6M, tryb prądowy. Fizyczne okablowanie kanału, gain/offset,
limity I/V/thermal i niezależne zatrzymanie pozostają niezweryfikowane.

Zmieniane pola w lokalnym `devices.moke_box`:

- `allow_vout_control: true`;
- `allowed_vout_channels: [2]`;
- `voltage_control.approved: true` — zgoda programowa operatora;
- `binding_id: operator-selected-VOUT2-Kepco-BOP72-6M-current`;
- `qualification_reference`: ścieżka tego dokumentu, który jawnie oznacza
  oczekującą kwalifikację sprzętową zamiast deklarować wykonane pomiary.

Pozostawione parametry istniejącego profilu: zakres -1 V…+1 V, cel zero 0 V,
maksymalny krok 50 mV, slew 1 V/s, interwał 50 ms, deadline 30 s,
minimum oczekiwania 2 s. To istniejące wartości konfiguracyjne, nie wyniki
pomiarów bezpieczeństwa. Min/max operatora w panelu dodatkowo zawężają zakres.

Zapis konfiguracji nie uzbraja trajektorii i nie wysyła SET_VOUT. Live pozostaje
domyślnie wyłączony. Zastosowanie profilu wymaga ponownego wczytania ustawień
i połączenia; wysłanie nastawy wymaga Apply albo jawnego włączenia Live.
W tym działaniu nie wysyłano komend do fizycznego instrumentu.

Weryfikacja: pełny YAML przeszedł `StationSettings.model_validate`, fizyczna
fabryka modułu utworzyła adapter z zatwierdzonym profilem bez connect. Porównanie
z kopią potwierdziło zmianę wyłącznie pięciu pól zgody/powiązania w MOKE;
numeryczne limity, rampy i pozostałe urządzenia są identyczne. Regresja:
48 testów i 10 podtestów zaliczone; ruff zaliczony.
Etykieta aktywnego profilu to `APPROVED DAC CONTROL`, aby odróżnić zgodę
programową od nieprzeprowadzonej kwalifikacji fizycznej.

Przed zmianą wykonano kopię `.config/settings.yml.before-moke-unlock-<hash>.bak`.
Powrót do read-only wymaga `allow_vout_control: false`,
`allowed_vout_channels: []`, `voltage_control.approved: false` i przeładowania
profilu; samo wyłączenie uprawnień nie stanowi komendy wyłączenia Kepco.
