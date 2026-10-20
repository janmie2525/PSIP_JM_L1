
import subprocess
import sys


SCRIPTS = [
    ("/app/load.py", "Wczytywanie granicy powiatu"),
    ("/app/h3_generator.py", "Generowanie siatki H3"),
    ("/app/import_bdot.py", "Import warstw BDOT10k"),
    ("/app/h3_aggregation.py", "Agregacja danych do H3"),
]


def main():
    print("=== ROZPOCZECIE PRZETWARZANIA DANYCH ===", flush=True)

    for script, description in SCRIPTS:
        print(f"\n=== {description} ===", flush=True)

        subprocess.run(
            [sys.executable, script],
            check=True,
        )

    print("\n=== CALY PROCES ZAKONCZONY POPRAWNIE ===", flush=True)


if __name__ == "__main__":
    main()
