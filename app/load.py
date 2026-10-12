import os
import subprocess
from pathlib import Path

shapefile = Path("/data/powiat.shp")

if not shapefile.exists():
    raise FileNotFoundError(f"Nie znaleziono pliku: {shapefile}")

connection = "PG:host=postgis port=5432 dbname=psip user=psip"

command = [
    "ogr2ogr",
    "-f", "PostgreSQL",
    connection,
    str(shapefile),
    "-nln", "powiat",
    "-lco", "GEOMETRY_NAME=geom",
    "-overwrite",
]

environment = os.environ.copy()
environment["PGPASSWORD"] = "psip"

subprocess.run(command, env=environment, check=True)
print("Załadowano powiat.shp do tabeli powiat w bazie psip.")