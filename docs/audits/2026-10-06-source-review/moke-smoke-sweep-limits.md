# Limity sweepa MOKE/Anritsu — 2026-10-06

Receptura: `recipes/anritsu_background_reference_smoke_test.yml`.
Źródła sprawdzone: generator ROI, generowanie punktów, provider MOKE,
kompilacja configure/update, wykonanie configure/arm/update, profil stanowiska,
plan napięciowy, konfiguracja/arm/ramp adaptera oraz kodowanie DAC.

## Wynik przeglądu

- Formularz już blokował Create i bezpośrednie accept dla punktów poza
  rozpoznanym zakresem. Na zrzucie `10000 mV` nie jest zaakceptowaną wartością:
  komunikat błędu i nieaktywny przycisk są dowodem odrzucenia.
- Znaleziono błędny wybór profilu UI: zawsze kanał główny zamiast kanału osi.
  Poprawiono wybór po target VOUT. Limity są ponownie rozpoznawane przy
  podglądzie i accept; brak kwalifikowanego profilu MOKE blokuje zatwierdzenie.
- Kompilator sprawdza jednostkę voltage, profil danego kanału, granice
  stanowiska i roboczy zakres. Każdy target trafia do walidowanego planu.
- Adapter configure i arm ponownie walidują profil/fingerprint i wszystkie
  punkty. ramp wymaga uzbrojenia, właściwego kanału i dokładnie następnego
  targetu; odrzuca dowolnie podmienioną wartość przed SET.
- Kodowanie DAC zaokrągla do wnętrza zakresu. Każdy krok rampy jest ograniczony
  profilem stanowiska; readback poza tym profilem jest błędem. Kod nie gwarantuje
  fizycznej dokładności urządzenia ani nie może zapobiec awarii sprzętowej.

## Ta receptura

10 żądanych punktów: liniowo od 0 do 0.010 V, kanał VOUT 0.
Robocze min/max: 0 i 0.010 V. Nie są limitami całego stanowiska; muszą mieścić
się w aktualnym zatwierdzonym profilu stanowiska. Ostatni reprezentowalny
target wewnątrz zakresu wynosi około 9.766 mV. Nie zmieniono limitów stanowiska.
Każde widmo poprzedza jawne wait 3 s; adapter może ponadto wymagać własnego
minimalnego czasu stabilizacji. Baseline i cleanup zachowane.

## Dowody

`tests/test_moke_smoke_sweep_limits.py`: odrzucenie 10000 mV, 10 V, wartości
tuż za granicą ±1 V, złego wymiaru i braku jednostki; blokada UI także przy
bezpośrednim accept; odrębne limity dodatkowego kanału; zaostrzenie ustawień
po otwarciu modalu; wszystkie 10 punktów receptury; brak SET dla błędnych
planów i podmienionego targetu; odrzucenie zmienionego fingerprintu;
zaokrąglanie DAC wewnątrz granic. UI pokazano i przetworzono zdarzenia.

Łącznie z testami adaptera i trajektorii: **54 passed**, Ruff zaliczony.
W poprzednim przebiegu pełna symulacja tej receptury zapisała 10 widm,
background/reference i przeszła walidację H5/PyThat. Nie wykonywano fizycznego I/O.

Walidacja chroni przed przekroczeniem zatwierdzonych limitów. Nie odgaduje,
że poprawnie zapisane `1 V` miało oznaczać `1 mV`, jeśli 1 V mieści się we
wszystkich zakresach danego planu. Dla tej receptury 1 V przekracza robocze
10 mV, więc zostanie odrzucone. Gwarancji 100% działania fizycznej aparatury
nie można wyprowadzić wyłącznie z kodu i symulatora.
