
import os
import subprocess
from pathlib import Path

shapefile = Path("/data/powiat.shp")

if not shapefile.exists():
    raise FileNotFoundError(
        f"Nie znaleziono pliku: {shapefile}"
    )

db_host = os.getenv("PGHOST", "postgis")
db_port = os.getenv("PGPORT", "5432")
db_name = os.getenv("PGDATABASE", "psip")
db_user = os.getenv("PGUSER", "psip")
db_password = os.getenv("PGPASSWORD", "psip")

connection = (
    f"PG:host={db_host} "
    f"port={db_port} "
    f"dbname={db_name} "
    f"user={db_user}"
)

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
environment["PGPASSWORD"] = db_password

subprocess.run(command, env=environment, check=True)

print("Zaladowano powiat.shp do tabeli public.powiat.")
