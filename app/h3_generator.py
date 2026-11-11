
import os
import json

import h3
import psycopg2
from psycopg2.extras import execute_values


# Rozdzielczosci H3: wieksza liczba oznacza mniejsze heksagony
RESOLUTIONS = [6, 7, 8]

DB_HOST = os.getenv("PGHOST", "postgis")
DB_PORT = os.getenv("PGPORT", "5432")
DB_NAME = os.getenv("PGDATABASE", "psip")
DB_USER = os.getenv("PGUSER", "psip")
DB_PASSWORD = os.getenv("PGPASSWORD", "psip")


def get_cells(geometry, resolution):
    # W miare mozliwosci uwzgledniamy tez komorki
    # przecinajace granice powiatu.
    if (
        hasattr(h3, "geo_to_h3shape")
        and hasattr(h3, "h3shape_to_cells_experimental")
    ):
        try:
            shape = h3.geo_to_h3shape(geometry)
            return h3.h3shape_to_cells_experimental(
                shape, resolution, contain="overlap"
            )
        except (TypeError, ValueError):
            pass

    return h3.geo_to_cells(geometry, resolution)


def cell_to_wkt(cell):
    # H3 zwraca wspolrzedne w kolejnosci: szerokosc, dlugosc.
    boundary = h3.cell_to_boundary(cell)

    coordinates = [
        f"{lng:.10f} {lat:.10f}"
        for lat, lng in boundary
    ]

    coordinates.append(coordinates[0])

    return "POLYGON((" + ", ".join(coordinates) + "))"


def main():
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

                # Pobranie granicy powiatu i transformacja do WGS84.
                cursor.execute("""
                    SELECT ST_AsGeoJSON(
                        ST_Transform(ST_Union(geom), 4326)
                    )
                    FROM public.powiat
                    WHERE geom IS NOT NULL;
                """)

                result = cursor.fetchone()

                if not result or not result[0]:
                    raise RuntimeError(
                        "Tabela public.powiat jest pusta "
                        "lub nie zawiera geometrii."
                    )

                county_geometry = json.loads(result[0])

                # Przygotowanie schematu i tabeli na siatki.
                cursor.execute("""
                    CREATE SCHEMA IF NOT EXISTS grid;

                    CREATE TABLE IF NOT EXISTS grid.hexagons (
                        h3_index TEXT PRIMARY KEY,
                        resolution INTEGER NOT NULL,
                        geom geometry(Polygon, 2180) NOT NULL
                    );
                """)

                # Ponowne uruchomienie odtwarza cala siatke.
                cursor.execute("DELETE FROM grid.hexagons;")

                for resolution in RESOLUTIONS:
                    cells = sorted(
                        get_cells(county_geometry, resolution)
                    )

                    rows = [
                        (
                            cell,
                            resolution,
                            cell_to_wkt(cell),
                        )
                        for cell in cells
                    ]

                    if rows:
                        execute_values(
                            cursor,
                            """
                            INSERT INTO grid.hexagons
                                (h3_index, resolution, geom)
                            VALUES %s
                            """,
                            rows,
                            template="""
                                (
                                    %s,
                                    %s,
                                    ST_Transform(
                                        ST_SetSRID(
                                            ST_GeomFromText(%s), 4326
                                        ),
                                        2180
                                    )
                                )
                            """,
                        )

                    print(
                        f"Rozdzielczosc {resolution}: "
                        f"{len(cells)} heksagonow"
                    )

                cursor.execute("""
                    CREATE INDEX IF NOT EXISTS hexagons_geom_gix
                    ON grid.hexagons USING GIST (geom);
                """)

        print("Siatka H3 zostala zapisana w grid.hexagons.")

    finally:
        connection.close()


if __name__ == "__main__":
    main()
