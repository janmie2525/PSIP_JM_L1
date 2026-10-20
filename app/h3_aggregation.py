
import os

import psycopg2
from psycopg2 import sql


DB_CONFIG = {
    "host": os.getenv("PGHOST", "postgis"),
    "port": int(os.getenv("PGPORT", "5432")),
    "dbname": os.getenv("PGDATABASE", "psip"),
    "user": os.getenv("PGUSER", "psip"),
    "password": os.getenv("PGPASSWORD", "psip"),
}

TARGET_TABLE = "bdot_aggregation"


def main():
    conn = psycopg2.connect(**DB_CONFIG)

    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("CREATE SCHEMA IF NOT EXISTS grid")

                # Odtworzenie tabeli wynikowej na podstawie siatki H3.
                cur.execute(
                    sql.SQL("DROP TABLE IF EXISTS grid.{}").format(
                        sql.Identifier(TARGET_TABLE)
                    )
                )

                cur.execute(
                    sql.SQL("""
                        CREATE TABLE grid.{} AS
                        SELECT h3_index, resolution, geom
                        FROM grid.hexagons
                    """).format(sql.Identifier(TARGET_TABLE))
                )

                cur.execute(
                    sql.SQL("""
                        ALTER TABLE grid.{}
                        ADD PRIMARY KEY (h3_index, resolution)
                    """).format(sql.Identifier(TARGET_TABLE))
                )

                cur.execute(
                    sql.SQL("""
                        CREATE INDEX bdot_aggregation_geom_gix
                        ON grid.{} USING GIST (geom)
                    """).format(sql.Identifier(TARGET_TABLE))
                )

                # Pobranie warstw BDOT10k.
                cur.execute("""
                    SELECT f_table_name, f_geometry_column, UPPER(type)
                    FROM geometry_columns
                    WHERE f_table_schema = 'bdot'
                    ORDER BY f_table_name
                """)

                layers = cur.fetchall()
                print(f"Liczba warstw BDOT10K: {len(layers)}")

                if not layers:
                    raise RuntimeError(
                        "Nie znaleziono warstw w schemacie bdot."
                    )

                for table_name, geom_column, geom_type in layers:

                    if geom_type in ("POLYGON", "MULTIPOLYGON"):
                        metric_column = f"area_{table_name}_m2"
                        expression = sql.SQL("""
                            COALESCE(
                                SUM(
                                    ST_Area(
                                        ST_Intersection(h.geom, b.{})
                                    )
                                ),
                                0
                            )
                        """).format(sql.Identifier(geom_column))

                        description = "powierzchnia [m2]"
                        column_type = "DOUBLE PRECISION"

                    elif geom_type in ("LINESTRING", "MULTILINESTRING"):
                        metric_column = f"length_{table_name}_m"
                        expression = sql.SQL("""
                            COALESCE(
                                SUM(
                                    ST_Length(
                                        ST_Intersection(h.geom, b.{})
                                    )
                                ),
                                0
                            )
                        """).format(sql.Identifier(geom_column))

                        description = "dlugosc [m]"
                        column_type = "DOUBLE PRECISION"

                    elif geom_type in ("POINT", "MULTIPOINT"):
                        metric_column = f"count_{table_name}"
                        expression = sql.SQL("COUNT(*)::bigint")

                        description = "liczba punktow"
                        column_type = "BIGINT"

                    else:
                        print(
                            f"Pominieto {table_name}: "
                            f"typ {geom_type}"
                        )
                        continue

                    # Dodanie kolumny z wynikiem.
                    cur.execute(
                        sql.SQL("""
                            ALTER TABLE grid.{}
                            ADD COLUMN {} {} NOT NULL DEFAULT 0
                        """).format(
                            sql.Identifier(TARGET_TABLE),
                            sql.Identifier(metric_column),
                            sql.SQL(column_type),
                        )
                    )

                    # Obliczenie wartosci dla kazdego heksagonu.
                    aggregate_query = sql.SQL("""
                        SELECT
                            h.h3_index,
                            h.resolution,
                            {} AS metric
                        FROM grid.hexagons AS h
                        JOIN bdot.{} AS b
                            ON ST_Intersects(h.geom, b.{})
                        WHERE b.{} IS NOT NULL
                        GROUP BY h.h3_index, h.resolution
                    """).format(
                        expression,
                        sql.Identifier(table_name),
                        sql.Identifier(geom_column),
                        sql.Identifier(geom_column),
                    )

                    update_query = sql.SQL("""
                        UPDATE grid.{} AS a
                        SET {} = agg.metric
                        FROM ({}) AS agg
                        WHERE a.h3_index = agg.h3_index
                          AND a.resolution = agg.resolution
                    """).format(
                        sql.Identifier(TARGET_TABLE),
                        sql.Identifier(metric_column),
                        aggregate_query,
                    )

                    cur.execute(update_query)

                    print(
                        f"OK: {table_name} -> "
                        f"{metric_column} ({description})",
                        flush=True,
                    )

                # Aktualizacja statystyk tabeli.
                cur.execute(
                    sql.SQL("ANALYZE grid.{}").format(
                        sql.Identifier(TARGET_TABLE)
                    )
                )

                # Kontrola liczby heksagonow.
                cur.execute(
                    sql.SQL("SELECT COUNT(*) FROM grid.{}").format(
                        sql.Identifier(TARGET_TABLE)
                    )
                )

                print(
                    f"Liczba heksagonow: {cur.fetchone()[0]}"
                )

                cur.execute(sql.SQL("""
                    SELECT resolution, COUNT(*)
                    FROM grid.{}
                    GROUP BY resolution
                    ORDER BY resolution
                """).format(sql.Identifier(TARGET_TABLE)))

                for resolution, count in cur.fetchall():
                    print(
                        f"Rozdzielczosc {resolution}: {count}"
                    )

                print("Agregacja zakonczona.")
                print("Tabela wynikowa: grid.bdot_aggregation")

    finally:
        conn.close()


if __name__ == "__main__":
    main()
