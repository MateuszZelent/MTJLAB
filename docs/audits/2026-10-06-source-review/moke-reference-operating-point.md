# MOKE: jawny punkt pracy przed baseline

Przyczyna niejednoznaczności: Stop bez wcześniejszego planu przekazywał
`stop_vout()` bez kanału. Adapter używał bieżącego kwalifikowanego profilu,
co nie dowodzi ustawienia VOUT wskazanego dopiero przez przyszły sweep.

Dodano `set_moke_voltage` z jawnymi channel i voltage. Kompilator sprawdza
jednostki i profil, następnie rozwija configure → arm → update z jednym
semantic ID operatora. Adapter potwierdza rampę przed kolejną akcją.
Wartość pozostaje ustawiona; akcja nie zawiera Stop. Kolejny ROI dostaje nowy
plan/arm. Referencja sama nie zmienia MOKE. Receptura smoke jawnie wybiera
VOUT 2 = 0 mV; użytkownik może zmienić tę wartość albo usunąć akcję.

Biblioteka ma `Set MOKE voltage` w Acquisition. Urządzenie MOKE Box pozwala
wybrać Fixed voltage albo Sweep ROI, kanał i wartość. Edycja nowoczesnego
bloku pozwala zmienić tryb w obie strony. Generowanie Fixed value również
korzysta z nowej akcji zamiast wrappera z automatycznym zerowaniem. Dodatkowe
zatwierdzone kanały są uwzględniane w definicjach parametrów strony.
Powrót do zera pozostaje jawnym cleanup.

Dowody: start z 200 mV, ustawienie 0/+5/−5 mV i odczyt przed następnym
checkpointem; cleanup zero; wstawianie i edycja przez bibliotekę i urządzenie;
walidacja modalu oraz zachowany render. Zestaw limity/kompilator/UI:
77 passed + 6 subtests; końcowe nowe regresje 7 passed. Pełna symulacja smoke:
1 passed, 10 widm oraz background/reference, H5/PyThat. Ruff zaliczony.
Nie wykonywano fizycznego I/O. Render offscreen wymagał jawnego załadowania
Arial z Windows, ponieważ Qt nie wykrywał żadnych systemowych fontów.
