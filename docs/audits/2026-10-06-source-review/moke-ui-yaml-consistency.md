# Jedna struktura MOKE w bibliotece i YAML

Problem: receptura miała samodzielny `set_moke_voltage`, podczas gdy Devices
tworzyło kontener `sequence/device_module=moke_box` z tą operacją. Obie ścieżki
wykonywały pomiar, ale prezentowały inny poziom drzewa i inny przycisk edycji.

Wprowadzono wspólne fabryki w `app/recipes/moke_nodes.py`. Devices MOKE Box,
skrót Acquisition Set MOKE voltage, Fixed value i edycja urządzenia korzystają
z tej samej struktury kontenera. Generator ROI też tworzy kontener MOKE.
Receptura smoke ma identyczny blok urządzenia przed baseline oraz dla ROI.
Configure/arm pozostają automatycznymi operacjami kompilatora; podlegają
dotychczasowym limitom i readback. Jawny cleanup pozostaje w finally,
a automatyczny shutdown jest prezentowany przez istniejący mechanizm drzewa.
Generator ROI nie dodaje dodatkowego lokalnego Stop po osi; shutdown nadal
obejmuje użyte kanały, także przy błędzie lub anulowaniu.

Sprawdzono zgodność struktur fabryk z recepturą, zapis i ponowny odczyt,
render 1360×880, edycję obu ścieżek biblioteki oraz identyczność wszystkich
payloadów wykonawczych przed i po opakowaniu. Nie zmieniło to 10 punktów ani
komend urządzenia. Kanał kontenera i operacji muszą być zgodne.

Wyniki: 32 passed dla limitów/edytora/generatora; szerszy zestaw 79 passed
+ 6 subtests; końcowe regresje 8 passed. Ruff zaliczony. Zestawy się pokrywają.
Render: `canonical-moke-tree.png`. Bez fizycznego I/O.

Zakres zmiany dotyczy MOKE; nie jest deklaracją pełnego ponownego audytu
wszystkich bloków pozostałych urządzeń. Niskopoziomowe operacje wcześniejszych
receptur pozostają czytelne dla istniejących edytorów i kompilatora.
