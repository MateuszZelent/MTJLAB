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

## Korekta kanału na polecenie operatora — 2026-10-03

Operator wyjaśnił, że Kepco jest podłączone do VOUT0; wcześniejsze próby na
VOUT2 dotyczyły pustego kanału i były wykonywane dla bezpieczeństwa. Na jego
polecenie profil sterowania w `.config/settings.yml` został przypisany do
VOUT0: `allowed_vout_channels: [0]`, `voltage_control.channel: 0` oraz
`binding_id: operator-selected-VOUT0-Kepco-BOP72-6M-current`.

Zakres napięcia, parametry rampy i istniejąca zgoda programowa pozostały
takie same. Informacja o połączeniu pochodzi od operatora; ta korekta nie
jest wynikiem fizycznej kwalifikacji limitów ani zabezpieczeń. Nie łączono
się ze sprzętem i nie wysyłano nastaw. Zmiana powiązania wymaga ponownego
wczytania ustawień i połączenia MOKE; kalibracja VOUT2 nie pasuje do VOUT0.


## Doprecyzowanie operatora: VOUT0 oraz VOUT2 (2026-10-03)

Operator zleci? pozostawienie obu wyj?? dost?pnymi: VOUT0 jest po??czone
z zasilaczem Kepco, a VOUT2 jest puste i s?u?y do testowania DAC.
Bie??ce ustawienia lokalne: `allowed_vout_channels: [0, 2]`,
`test_vout_channels: [2]`, `voltage_control.channel: 0`.
Kalibracja pola i jej identyfikacja fizycznego po??czenia dotycz? nadal VOUT0.
Dodanie testowego VOUT2 nie zmienia identyfikatora profilu VOUT0 ani jego limit?w.
Oba kana?y korzystaj? z dotychczasowej obwiedni -1 V do +1 V i ogranicze? rampy.
Zmiana kana?u i zapis konfiguracji nie wysy?aj? napi?cia. Zwyk?e zerowanie
obejmuje kana? wybrany na stronie sterowania; zatrzymanie awaryjne pr?buje
wyzerowa? wszystkie zatwierdzone wyj?cia. Potwierdzenie zera DAC nie potwierdza
wy??czenia mocy Kepco ani zerowego pola.

Weryfikacja tej zmiany odbywa si? w symulatorze; zapis deklaracji operatora
nie stanowi nowej fizycznej kwalifikacji stanowiska.


## Niezale?ne profile kana??w (2026-10-03)

Na ??danie operatora usuni?to dziedziczenie limit?w i ramp VOUT2 z VOUT0.
`voltage_control` pozostaje profilem g??wnego wyj?cia pola VOUT0;
`channel_profiles` zawiera osobne profile VOUT1 do VOUT7. Ka?dy profil ma
w?asne minimum/maksimum napi?cia, bezpieczne zero, maksymalny krok i szybko??
rampy, odst?p krok?w, termin zako?czenia rampy, minimalny czas ustalania,
identyfikator po??czenia, tryb i zatwierdzenie. Tylko VOUT0 i VOUT2 s? dopuszczone
do sterowania; pozosta?e wyj?cia maj? osobne, niezatwierdzone profile.
Dotychczasowe warto?ci VOUT0 i VOUT2 zachowano bez poszerzania limit?w.
Zmiana profilu VOUT2 nie zmienia identyfikatora kalibracji VOUT0.

W UI prze??czenie kana?u przywraca jego osobny zakres roboczy, szkic napi?cia
i czas ustalania. Quick Controls sprawdza zakres danego kana?u. Adapter
sprawdza niezale?ny profil przed zapisem DAC i podczas rampy. Profile stanowiska
s? zapisywane w pliku ustawie?, natomiast szkice panelu sterowania s? stanem
bie??cej sesji. Weryfikacja odby?a si? w symulatorze, bez polece? do sprz?tu.


## Wy??czanie wyj?cia i obserwacja rampy (2026-10-03)

Przycisk wyj?cia Kepco nosi nazw? `Turn off field`; dla pustych wyj?? testowych
u?ywa nazwy `Turn off output`. Akcja wy??cza Live, ustawia szkic napi?cia na zero
i uruchamia istniej?c? ograniczon? ramp? w?a?ciwego kana?u (lub przerywa bie??ce
zadanie i uruchamia jego istniej?ce zerowanie). Zako?czenie potwierdza wy??cznie
zero DAC. UI nie potwierdza wy??czenia stopnia mocy ani zerowego pola magnetycznego.

Telemetria pochodzi z odczyt?w DAC ju? wykonywanych przez ramp?: pocz?tek,
potwierdzone kroki, faza ustalania i zako?czenie. Nie dodaje zapyta? do sprz?tu,
nie zmienia krok?w, szybko?ci ani termin?w rampy. Aktualizacje s? ograniczone do
20 Hz, z wymuszonym pocz?tkiem/ko?cem i ostatnim znanym odczytem przy b??dzie.
Qt dostarcza pr?bki do w?tku UI; napi?cie, historia i Quick Controls pokazuj?
potwierdzone warto?ci, a pasek post?pu przedstawia odleg?o?? do celu. B??d
odbiorcy telemetrii nie blokuje rampy ani zerowania. Nieudane zerowanie pozostawia
komunikat b??du i ostatni potwierdzony odczyt, bez deklaracji wy??czenia.
Zmiana jest sprawdzona w symulatorze i testach op??nie?/awarii; nie wykonywano
polece? do fizycznego urz?dzenia.


## Diagnostyka b??du po??czenia (2026-10-03)

Log operatora z 12:07:24 UTC zawiera? `Unexpected MOKE VOUT readback record`,
ale poprzednia implementacja nie zachowa?a surowych bajt?w dla b??du semantyki
rekordu. Nie mo?na ustali? przyczyny tego konkretnego pakietu po fakcie.
Rozszerzono diagnostyk? o pe?ne 32 bajty odpowiedzi, indeks, origin/type/channel
oraz pow?d odrzucenia (m.in. powt?rzony kana?). Walidacja nadal wymaga checksum,
AD5362, MainBox/Opt2 i ka?dego kana?u 0..7 dok?adnie raz.

Wy??cznie podczas po??czenia dopuszczono jedn? ponown? pr?b? odczytu po
odrzuceniu odpowiedzi protoko?u: zamkni?cie starego TCP, nowe po??czenie,
ponowny READBACK_VOUT. Obie pr?by maj? wsp?lny termin. Nie ponawia si? SET,
nie skanuje ani nie opr??nia niepewnego strumienia. B??dy odczyt?w podczas
sterowania nadal zamykaj? sesj?; nie powoduj? automatycznego wznowienia rampy.

Dwa kontrolowane odczyty z fizycznego MOKE Box zwr?ci?y prawid?owe rekordy
MainBox/AD5362 dla kana??w 0..7 z prawid?ow? checksum?. Drugi odczyt wykonano
poprawionym adapterem w profilu tylko do odczytu; po??czenie by?o zweryfikowane
po jednej pr?bie. W ka?dym sprawdzeniu wys?ano wy??cznie `18 00 00 18`.
Nie ustawiano ani nie zerowano napi??. Artefakty lokalne:
`artifacts/moke-connection-diagnostic.json`,
`artifacts/moke-adapter-readonly-verification.json`.
To potwierdza poprawne bie??ce po??czenie; nie odtwarza wcze?niejszej b??dnej ramki.

## Live voltage: responsive editing and independent readback (2026-10-03)

Operator edits never perform transport I/O. The voltage editor and Fluent slider
remain enabled throughout a manual voltage transaction; refreshes assign their
final enabled state directly, preserving a held mouse gesture, focus, caret and
selection. QuickControls bounds are broadcast only when their effective values
change, rather than rebuilding every slider for each draft or DAC sample.

Live uses one replaceable target mailbox, not a FIFO of obsolete voltage edits.
The 400 ms timer sends the newest valid draft at a bounded cadence and does not
restart indefinitely during a continuous drag. Enter/release can submit sooner.
Each published target passes current station authorization and immutable working
envelope checks. The adapter independently validates profile, channel, working
bounds, target and settling time before consuming it. Only the transport owner
sends commands; a SET and its readback complete before another SET can begin.
A new target can replace an unissued step and interrupt settling, with the ramp
continuing from the last confirmed DAC sample. Step/slew limits still apply.

Each transaction accepts retargeting for at most five seconds, followed by the
qualified deadline and settling of its final consumed target. A newer draft is
handed to a subsequent authorized transaction after the reservation is released.
This keeps a continuous gesture from extending a single reserved call without a
bound. Explicit adapter deadlines remain absolute. Cancellation takes precedence
over mailbox consumption and performs the existing confirmed-zero cleanup.
Invalid drafts discard pending targets; authorization failure cancels the live
operation. Transport faults close the session without retrying uncertain SETs or
automatically replaying a target. Switching Live off discards pending edits while
the already executing authorized target completes; Turn off requests DAC zero.

History now has independent operator-request and confirmed-DAC traces. A request
includes unapplied edits and cannot create a DAC sample or field prediction.
Invalid text creates a gap. The latest request is drawn as a held step; confirmed
DAC samples remain discrete and are not extended as fresh measurements. Field
curves use only confirmed DAC values and an active calibration. Plot rendering is
paced at 20 Hz, independently of editor events. The displayed window is selectable
as 10/30/60/180 seconds; retention remains three minutes. QuickControls shows a
separate confirmed DAC caption, and telemetry never rewrites its draft editor.
QuickControls Live commits use the same mailbox and transport reservation.

Verification: 119 adapter/workflow/calibration tests and four subtests, plus
33 full-page/shell UI tests passed. A further 34 generic QuickControls and quantity
slider regression tests passed (186 tests and four subtests in total). This includes deliberately blocked sends and
initial readbacks with a GUI heartbeat, target replacement and reversal,
retargeting during settling, immutable envelope rejection, bounded transaction
handoff, authorization revocation, invalid drafts, cancellation and uncertain
write failure. Light/dark views at desktop and narrow sizes and QuickControls
are inspectable under `artifacts/moke-live-responsive/`. These are simulator and
fault-injection checks; no physical output was changed for this implementation.

## Startup readback and floating voltage controls (2026-10-03)

After connection, a read-only snapshot initializes independent voltage drafts
and sliders for VOUT0 through VOUT7, including outputs without write permission.
Operator edits made before the first snapshot are preserved. Subsequent readbacks
update confirmed values and history without overwriting an edited target.
QuickControls receives the same per-channel drafts and confirmed values.

An initial DAC value outside the working range remains visible, with an explicit
explanation. This extends only the slider's display domain; working limits and
station authorization are unchanged. Slider edits remain constrained to the
working range, and an out-of-range initial draft cannot be applied.

Open floating controls detaches the existing voltage panel into a resizable,
modeless Fluent window. Time plot toggles history visibility without creating
another controller, interrupting Live, clearing history, or sending output
commands. Dock panel and the title-bar close button restore the same controls
to the main page. Layout and repeated docking are checked in both themes.

Verification includes 191 tests and four subtests across startup readback,
floating controls, full-page/shell rendering, protocol recovery, independent
channel limits, ramp feedback, Live retargeting, QuickControls, quantity sliders,
and calibration. Screenshots are under `artifacts/moke-floating-controls/`.
These checks use simulators and fault injection; no physical output was changed.

## Explicit voltage keyboard step (2026-10-03)

The hard-coded 100 mV integer increment is replaced by a Step selector beside
the voltage editor. Its default is 1 mV, independent of written decimal places,
unit prefix, decimal separator or scientific notation. Presets are 0.1, 0.5,
1, 2, 5, 10, 20, 50 and 100 mV. Each VOUT has an independent session preference,
shared by the main panel, floating controls and QuickControls. QuickControls
plus/minus buttons use the same selected step.

Selecting a step sends no hardware command, changes no setpoint and does not
commit or clamp a draft on focus transfer. Keyboard increments remain bounded
by the existing operator limits. Slider arrow keys compute the explicit
quantity increment rather than an approximation from integer slider positions;
the display mapping retains enough resolution for initial DAC readbacks.

Verification: 103 MOKE, full-page/shell, explicit-step, generic QuickControls
and slider tests passed. Six additional instrument-precision tests passed;
two existing Keithley tests fail because their source requests omit a required
fixed source range. The Keithley adapter, safety policy and those tests were
not modified. UI images are available in `artifacts/moke-voltage-steps/` and
`artifacts/moke-floating-controls/`. No physical output was changed.
