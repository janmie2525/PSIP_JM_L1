PSIP_JM_L1 – moduł IOP (test)

ZAWARTOŚĆ
- scripts/run_iop.py       obliczenia, walidacja, eksport i zapis do PostGIS
- config/iop_config.json   jawne wagi, oceny klas i progi
- tests/README.txt         instrukcja testów
- outputs/                tutaj powstaną CSV i JSON

WAŻNE
Skrypt jest domyślnie skonfigurowany wyłącznie na testowy PostGIS:
localhost:5434, baza psip, tabela wynikowa grid.iop_test.
Nie łączy się z produkcją ani nie zmienia kodu aplikacji GUI.

KROKI W POWERSHELL
1. Rozpakuj zawartość ZIP bezpośrednio do:
   C:\Users\janmi\PycharmProjects\PSIP_JM_L1
   Zgódź się na scalenie folderów scripts, config, tests, outputs (jeśli zapyta).

2. Zainstaluj brakującą bibliotekę, jeśli nie jest zainstalowana:
   py -3.12 -m pip install psycopg2-binary

3. Przejdź do projektu:
   cd C:\Users\janmi\PycharmProjects\PSIP_JM_L1

4. Uruchom testy jednostkowe:
   py -3.12 .\scripts\run_iop.py --test

5. Najpierw uruchom tryb bez zapisu do bazy:
   py -3.12 .\scripts\run_iop.py --dry-run

6. Jeśli dry-run zakończy się bez błędów, zapisz wynik w bazie testowej:
   py -3.12 .\scripts\run_iop.py

7. Jeśli tabela grid.iop_test już istnieje i świadomie chcesz ją zastąpić:
   py -3.12 .\scripts\run_iop.py --replace

W QGIS
- Połącz się z PostGIS: localhost, port 5434, baza psip, użytkownik psip.
- Odśwież schemat grid.
- Dodaj tabelę grid.iop_test jako warstwę.
- Styl: Graduated, pole iop, rampa czerwony (#D73027) – biały (#FFFFFF) – zielony (#1A9850),
  zakres -1 do +1, punkt środkowy 0.
- Pole low_coverage wskazuje heksagony o coverage < 0.1.

METODOLOGIA
IOP = (1.6*K1 + 1.2*K2 + 0.8*K3 + 0.4*K4) / 4.
K1, K2, K3, K4 są ograniczone do [-1,1].
Wartości NULL w miarach obiektów są traktowane jako 0; brak lub niepoprawna
powierzchnia odniesienia A przerywa obliczenia.
Skrypt sygnalizuje rozbieżność sumy klas pokrycia terenu względem A i coverage
większe od powierzchni całego heksagonu. Nie usuwa automatycznie heksagonów
o niskim coverage.
