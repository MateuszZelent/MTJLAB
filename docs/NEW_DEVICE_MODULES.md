# Dodawanie modułu urządzenia

Każde nowe urządzenie jest pionowym pakietem w `app/devices/<family_model>/`.
Pakiet nie może importować PySide6 ani prywatnych klas UI innego urządzenia.

Minimalny pakiet zawiera:

```text
<family_model>/
  __init__.py        # małe API publiczne
  models.py          # dataclass/Pydantic bez Qt
  adapter.py         # bezpieczne operacje wysokiego poziomu
  simulator.py       # symulacja tych samych operacji
  module.py          # DeviceModule i dispatcher workera
  safety.py          # gdy urządzenie ma własne ograniczenia
  recipe.py          # gdy urządzenie wnosi akcje do receptur
  ui/                # dopiero po kwalifikacji UI; page/panels/dialogs
```

## Checklist przed rejestracją

1. Zdefiniuj stabilny `key`; nie zmieniaj go po zapisaniu go w recepturach.
2. Udostępnij tylko bezpieczne, wysokopoziomowe operacje adaptera.
3. Zaimplementuj `connect`, `disconnect` i idempotentne `emergency_off`.
4. Dodaj symulator i testy adapter–symulator, także dla błędów transportu.
5. Dodaj `DeviceModule` z dispatcherem i zarejestruj go w
   `app/devices/registry.py`.
6. Moduł mający wyjście energii jest domyślnie zablokowany do czasu kwalifikacji
   safety oraz HIL.
7. Dopiero po HIL dodaj konfigurację do `StationSettings`, stronę UI i rozszerzenie
   receptur.

## MOKE Box

`app/devices/moke_box` zawiera niezależny od transportu kontrakt
`MokeBoxTransport`. Do czasu kwalifikacji rzeczywistego protokołu moduł jest
read-only i nie ma implementacji sterowania aktuatorami. Następny krok to adapter
transportu z dokumentacją komend, testy symulacyjne oraz HIL dla identyfikacji i
odczytu sygnału.

## Lake Shore Gaussmeter

`app/devices/lakeshore_gaussmeter` zapewnia dwa rozdzielone adaptery: Model 425
korzysta z opcjonalnego, oficjalnego sterownika `lakeshore.model_425.Model425`,
a Model 475 z bezpiecznej ścieżki VISA z jedyną operacją pomiarową `RDGFIELD?`.
Zmiana ustawień, w tym `UNIT`, jest świadomie zablokowana do czasu HIL. Model 475
pozostaje na ścieżce VISA, dopóki jego API nie zostanie potwierdzone w dokumentacji
i na sprzęcie.

Lokalny podręcznik referencyjny: `docs/External_libraries/475_manual.pdf`.
