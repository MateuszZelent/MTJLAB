# Execute — wspólne drzewo przepisu i monitoring na żywo

## Cel

Zakładka **Execute** ma pokazywać dokładnie to samo drzewo recepty co
**Sweep Builder** — z taką samą hierarchią, nazwami, wierszami ROI,
warunkami, blokiem `Finally` i statusem elementów. Różnica ma być wyłącznie
funkcjonalna: w Execute drzewo jest tylko do odczytu, a aktualnie wykonywany
element jest podświetlony.

Execute dostanie także panel na żywo, który pozwoli operatorowi potwierdzić:

- które parametry są właśnie zmieniane przez wykonanie recepty;
- jaką wartość zaplanowano oraz jaką wartość potwierdził sterownik;
- które używane wyjścia urządzeń są rzeczywiście `ON`, `OFF` albo `UNKNOWN`;
- kiedy stan został ostatnio potwierdzony i przez jakie działanie recepty.

## Zakres i zasada bezpieczeństwa

1. Interfejs nigdy nie wywnioskuje stanu OUTPUT z wyglądu przycisku,
   oczekiwanej recepty ani własnego cache UI.
2. Stan `ON` lub `OFF` będzie pokazywany wyłącznie po udanym potwierdzeniu
   sterownika / odczycie zwrotnym przekazanym przez silnik wykonawczy.
3. Przed pierwszym potwierdzeniem, po błędzie komunikacji oraz w trakcie
   awaryjnego wyłączania status będzie `UNKNOWN`; nie zostanie zastąpiony
   fałszywym `OFF`.
4. UI nie wykonuje zapytań VISA w trakcie runu. Odbiera wyłącznie zdarzenia
   z `RecipeRunner`.
5. Podczas wykonania strony urządzeń pozostają widoczne, lecz są tylko do
   odczytu; ręczne sterowanie nie może konkurować z silnikiem recepty.

## Plan wdrożenia

### 1. Jeden renderer drzewa dla Sweep Builder i Execute

- Wyodrębnić z `app/ui/recipes/page.py` wspólny renderer całego drzewa
  recepty.
- Renderer będzie tworzył tę samą strukturę i te same trzy kolumny:
  `Measurement sequence`, `Role / expansion`, `Status`.
- Sweep Builder pozostanie edytowalny i zachowa drag-and-drop oraz metadane
  edytora. Execute użyje tego samego renderera w trybie read-only.
- Zmapować identyfikatory akcji kompilatora na elementy renderowanego drzewa,
  aby zdarzenia `action_started` / `action_finished` podświetlały właściwy
  węzeł, bez zmiany struktury drzewa.

### 2. Manifest monitorowania z planu wykonania

- Podczas kompilacji / startu runu utworzyć manifest parametrów i endpointów
  OUTPUT używanych przez receptę.
- Parametry korzystają z istniejącego rejestru parametrów oraz jego
  wymiarów; wartości pozostają w SI w silniku i są formatowane dla UI dopiero
  na końcu.
- Endpointy obejmą co najmniej `Rigol CH1/CH2`, `Keithley A/B` i wyjścia
  innych urządzeń, jeśli dana recepta z nich korzysta.

### 3. Telemetria wykonawcza

- Rozszerzyć zdarzenia `RecipeRunner` o uporządkowany snapshot monitoringu:
  aktywne parametry, wartość żądana, wartość faktycznie zastosowana,
  źródło potwierdzenia i czas.
- Dla `configure`, aktualizacji setpointu oraz `set_*_output` zapisywać stan
  dopiero po sukcesie adaptera.
- W przypadku błędu najpierw oznaczać dotknięte wyjścia jako `UNKNOWN`, a po
  potwierdzonym bezpiecznym wyłączeniu jako `OFF`.
- Snapshoty będą JSON-serializowalne i zostaną zachowane razem z eventami runu
  w HDF5/checkpoincie.

### 4. Panel „Live execution state” w Execute

- Dodać obok / nad logiem wydajny panel tylko do odczytu.
- Sekcja **Output state** pokaże wyłącznie endpointy używane przez receptę,
  z jednoznacznymi chipami `ON`, `OFF`, `UNKNOWN` oraz czasem potwierdzenia.
- Sekcja **Changing parameters** pokaże kolumny: urządzenie/kanał,
  parametr, `Requested`, `Applied`, stan i czas. Bieżąca akcja będzie
  wyróżniona.
- Wartości będą formatowane przez wspólną warstwę quantities, z jednostką i
  poprawnym prefiksem inżynierskim; żadne mnożniki nie będą ukryte w UI.
- Widoki desktopowy i węższy zostaną sprawdzone render-testem.

### 5. Obsługa początku, wznowienia i zakończenia runu

- Początek oraz wznowienie runu inicjalizują drzewo i panel z manifestu,
  ale nie zakładają stanu OUTPUT.
- Tryb demo pokaże wymuszone wyłączenie tylko po potwierdzeniu przez silnik.
- `Finally`, normalne zakończenie, anulowanie i E-STOP aktualizują panel z
  tej samej telemetrii, bez lokalnych skrótów w UI.

### 6. Testy akceptacyjne

- Test parytetu drzewa: złożona recepta jest renderowana identycznie w
  Builderze i Execute (tekst, hierarchia, role, ROI, `Finally`).
- Test telemetrii: żądana i potwierdzona wartość setpointu, a także output
  `ON/OFF`, są emitowane tylko po sukcesie adaptera.
- Test błędu: przy nieudanej operacji OUTPUT staje się `UNKNOWN`, nigdy
  fałszywie `OFF`.
- Test trybu demo i wznowienia runu.
- Render-test panelu dla zwykłego okna desktopowego i węższego widoku.
- Statyczne kontrole oraz istniejące testy bez komunikacji ze sprzętem.

## Kryteria akceptacji

1. Porównanie drzewa Builder ↔ Execute dla tej samej recepty daje identyczną
   strukturę i treść.
2. Operator widzi bieżący krok, wszystkie zmieniane parametry oraz wyłącznie
   potwierdzony stan używanych outputów.
3. Zdarzenie lub błąd nie może wizualnie zamienić stanu nieznanego na
   potwierdzone `OFF`.
4. Podczas runu nie da się wysłać z UI urządzeń konkurencyjnej komendy
   ręcznej.
5. Nie dodajemy żadnych bezpośrednich zapytań VISA do kodu GUI.
