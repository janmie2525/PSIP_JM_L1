
import os
import subprocess
from pathlib import Path

import psycopg2


DATA_DIR = Path("/data")

DB_HOST = os.getenv("PGHOST", "postgis")
DB_PORT = os.getenv("PGPORT", "5432")
DB_NAME = os.getenv("PGDATABASE", "psip")
DB_USER = os.getenv("PGUSER", "psip")
DB_PASSWORD = os.getenv("PGPASSWORD", "psip")


def main():
    files = sorted(DATA_DIR.glob("*.gpkg"))

    if not files:
        raise FileNotFoundError(
            f"Nie znaleziono plikow GPKG w {DATA_DIR}"
        )

    # Przygotowanie schematu na warstwy BDOT10k.
    connection = psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
    )

    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "CREATE SCHEMA IF NOT EXISTS bdot"
                )
    finally:
        connection.close()

    pg_connection = (
        f"PG:host={DB_HOST} "
        f"port={DB_PORT} "
        f"dbname={DB_NAME} "
        f"user={DB_USER}"
    )

    environment = os.environ.copy()
    environment["PGPASSWORD"] = DB_PASSWORD

    print(f"Liczba plikow do importu: {len(files)}")

    for number, file in enumerate(files, start=1):
        layer = file.stem.split("__")[-1]

        print(
            f"[{number}/{len(files)}] Importuje: {layer}",
            flush=True,
        )

        command = [
            "ogr2ogr",
            "-f", "PostgreSQL",
            pg_connection,
            str(file),
            "-nln", layer,
            "-lco", "SCHEMA=bdot",
            "-lco", "OVERWRITE=YES",
        ]

        subprocess.run(
            command,
            env=environment,
            check=True,
        )

    print("Import BDOT10k zakonczony.")
    print(f"Zaimportowano plikow: {len(files)}")


if __name__ == "__main__":
    main()
