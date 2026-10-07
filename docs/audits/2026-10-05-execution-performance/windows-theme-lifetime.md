# Zamknięcie routera Fluent i ostrzeżenia motywu Windows

## Zmiany

- `forget_stack()` nadal usuwa historię tylko niszczonego stosu. Przed emisją sygnału sprawdza teraz `shiboken6.isValid(qrouter)` oraz `QCoreApplication.closingDown()`. Globalny obiekt Pythona może przeżyć swój obiekt C++ Qt; wcześniej emisja kończyła się `RuntimeError: Signal source has been deleted`.
- Przy starcie aplikacji, bezpośrednio po utworzeniu QApplication, ustawiany jest styl bazowy Fusion na Windows. Warstwa Fluent/QSS nadal określa wygląd aplikacji. Fusion omija zależność bazowych kontrolek od uchwytów motywu UXTheme. Nie wyciszamy komunikatów Qt.
- Styl bazowy instalowany jest tylko raz i przed utworzeniem okien. Próbna instalacja z funkcji przełączającej motyw, gdy przyciski już istniały, powodowała access violation w teście Qt 6.11.1. Ta wersja została wycofana; końcowa implementacja instaluje styl w punkcie wejścia aplikacji, a przełączanie light/dark nie zmienia bazowego QStyle.

Źródła techniczne: [Qt 6.11.1 — natywny styl Windows](https://github.com/qt/qtbase/blob/v6.11.1/src/plugins/styles/modernwindows/qwindowsvistastyle.cpp) oraz [OpenThemeData — wymagany uchwyt okna](https://learn.microsoft.com/en-us/windows/win32/api/uxtheme/nf-uxtheme-openthemedata). Nie odtworzono dokładnej sekwencji prowadzącej do ostrzeżeń użytkownika; zmiana bazy stylu usuwa tę zależność dla kontrolek aplikacji.

## Wyniki

- 16 testów motywu i cyklu życia Fluent — zaliczone. W tym wymuszone usunięcie routera przed stosem, zachowanie historii innego stosu, animacje i brak wymiany stylu po utworzeniu kontrolek.
- Test natywnego renderowania Windows — zaliczony: 24 kombinacje modal/motyw, każda w dwóch rozmiarach, z przewijaniem, pobraniem obrazu HWND i zamykaniem. Sprawdzono brak `OpenThemeData() failed`, `Signal source has been deleted` i tracebacku w stderr całego procesu.
- Obejrzano render modalów Keithleya w jasnym i Rigola w ciemnym motywie. Zrzuty w `scratch/windows-theme-fix/`.
- Ruff dla zmienianych plików — zaliczony.

Zmiany nie wymagają aktualizacji pakietów. Obowiązują po ponownym uruchomieniu aplikacji przez `app.main`.
