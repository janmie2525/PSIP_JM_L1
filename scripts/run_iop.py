#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
IOP – indeks oceny przejezdności terenu dla danych BDOT10k/H3.

Domyślnie łączy się wyłącznie z testowym PostGIS na localhost:5434.
Źródło: grid.bdot_aggregation
Wynik:  grid.iop_test

Uruchomienie:
  py -3.12 ./scripts/run_iop.py --dry-run
  py -3.12 ./scripts/run_iop.py
  py -3.12 ./scripts/run_iop.py --replace
  py -3.12 ./scripts/run_iop.py --test
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import unittest
from pathlib import Path
from typing import Any



ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "config" / "iop_config.json"
OUTPUT_DIR = ROOT / "outputs"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)

    weights = cfg["weights"]
    if set(weights) != {"k_land", "k_hydro", "k_roads", "k_buildings"}:
        raise ValueError("Konfiguracja musi zawierać dokładnie cztery wagi Ki.")
    if any(not math.isfinite(float(v)) or float(v) <= 0 for v in weights.values()):
        raise ValueError("Wszystkie wagi muszą być dodatnie i skończone.")
    if not math.isclose(sum(map(float, weights.values())), 4.0, abs_tol=1e-9):
        raise ValueError("Suma wag musi wynosić 4 zgodnie z przyjętym wzorem.")

    return cfg


def number(value: Any, field: str, h3_id: str, *, null_as_zero: bool = True) -> float:
    if value is None:
        if null_as_zero:
            return 0.0
        raise ValueError(f"{h3_id}: brak wymaganej wartości {field}.")
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{h3_id}: niepoprawna wartość {field}={value!r}.")
    if not math.isfinite(result):
        raise ValueError(f"{h3_id}: wartość {field} nie jest skończona: {result}.")
    if result < 0:
        raise ValueError(f"{h3_id}: wartość {field} jest ujemna: {result}.")
    return result


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def calculate_one(row: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    h3_id = str(row["h3_id"])
    A = number(row.get(cfg["coverage"]["analysis_area_column"]), 
               cfg["coverage"]["analysis_area_column"], h3_id, null_as_zero=False)
    hex_area = number(row.get("hex_area_m2"), "hex_area_m2", h3_id, null_as_zero=False)
    if A <= 0 or hex_area <= 0:
        raise ValueError(f"{h3_id}: powierzchnia A i powierzchnia heksagonu muszą być dodatnie.")

    # K1 – ważona udziałem powierzchni ocena pokrycia terenu.
    land_sum = 0.0
    land_area_sum = 0.0
    for column, score in cfg["land_classes"].items():
        area = number(row.get(column), column, h3_id)
        land_area_sum += area
        land_sum += area * float(score)
    k_land = clamp(land_sum / A)

    # K2 – hydrografia. Maksimum ogranicza wielokrotne naliczanie tej samej bariery.
    hydro = cfg["hydro"]
    water = number(row.get(hydro["water_area_column"]), hydro["water_area_column"], h3_id)
    wetlands = sum(number(row.get(c), c, h3_id) for c in hydro["wetland_area_columns"])
    reeds = sum(number(row.get(c), c, h3_id) for c in hydro["reed_area_columns"])
    rivers = number(row.get(hydro["river_length_column"]), hydro["river_length_column"], h3_id)
    channels = number(row.get(hydro["channel_length_column"]), hydro["channel_length_column"], h3_id)
    ditches = number(row.get(hydro["ditch_length_column"]), hydro["ditch_length_column"], h3_id)

    h1 = (water / A) / float(hydro["water_area_threshold"])
    h2 = (wetlands / A) / float(hydro["wetland_area_threshold"])
    h3 = (reeds / A) / float(hydro["reed_area_threshold"])
    stream_density = (
        rivers + channels + float(hydro["ditch_factor"]) * ditches
    ) * 1000.0 / A
    h4 = stream_density / float(hydro["stream_density_threshold_km_per_km2"])
    k_hydro = -min(1.0, max(h1, h2, h3, h4, 0.0))

    # K3 – gęstość sieci drogowej.
    roads = number(row.get(cfg["roads"]["length_column"]),
                   cfg["roads"]["length_column"], h3_id)
    road_density = roads * 1000.0 / A
    k_roads = min(1.0, max(0.0, road_density /
                           float(cfg["roads"]["saturation_density_km_per_km2"])))

    # K4 – udział powierzchni budynków.
    buildings = number(row.get(cfg["buildings"]["area_column"]),
                       cfg["buildings"]["area_column"], h3_id)
    k_buildings = -min(
        1.0,
        max(0.0, (buildings / A) /
            float(cfg["buildings"]["saturation_area_share"]))
    )

    weights = cfg["weights"]
    iop = (
        float(weights["k_land"]) * k_land
        + float(weights["k_hydro"]) * k_hydro
        + float(weights["k_roads"]) * k_roads
        + float(weights["k_buildings"]) * k_buildings
    ) / 4.0

    coverage = A / hex_area
    low_coverage = coverage < float(cfg["coverage"]["low_coverage_threshold"])

    land_area_ratio = land_area_sum / A
    land_area_warning = abs(land_area_ratio - 1.0) > float(
        cfg["coverage"]["land_area_tolerance_fraction"]
    )
    coverage_warning = coverage > 1.0 + float(
        cfg["coverage"]["coverage_overflow_tolerance_fraction"]
    )

    values = {
        "k_land": k_land,
        "k_hydro": k_hydro,
        "k_roads": k_roads,
        "k_buildings": k_buildings,
        "iop": iop
    }
    for name, value in values.items():
        if not math.isfinite(value) or value < -1.000000001 or value > 1.000000001:
            raise ValueError(f"{h3_id}: {name} poza zakresem [-1,1]: {value}")

    warnings = []
    if low_coverage:
        warnings.append("LOW_COVERAGE")
    if land_area_warning:
        warnings.append("LAND_CLASSES_SUM_DIFFERS_FROM_A")
    if coverage_warning:
        warnings.append("COVERAGE_EXCEEDS_HEX_AREA")

    return {
        "h3_id": h3_id,
        "resolution": int(row["resolution"]),
        "k_land": k_land,
        "k_hydro": k_hydro,
        "k_roads": k_roads,
        "k_buildings": k_buildings,
        "iop": clamp(iop),
        "coverage": coverage,
        "low_coverage": low_coverage,
        "land_area_sum_m2": land_area_sum,
        "land_area_ratio": land_area_ratio,
        "land_area_warning": land_area_warning,
        "coverage_warning": coverage_warning,
        "warnings": ";".join(warnings),
        "geom_ewkb": row["geom_ewkb"]
    }


def source_columns(cfg: dict[str, Any]) -> list[str]:
    cols = set(cfg["land_classes"].keys())
    cols.add(cfg["coverage"]["analysis_area_column"])
    h = cfg["hydro"]
    cols.update([
        h["water_area_column"], h["river_length_column"],
        h["channel_length_column"], h["ditch_length_column"]
    ])
    cols.update(h["wetland_area_columns"])
    cols.update(h["reed_area_columns"])
    cols.add(cfg["roads"]["length_column"])
    cols.add(cfg["buildings"]["area_column"])
    return sorted(cols)


def fetch_source_rows(conn, cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    columns = source_columns(cfg)
    # Kolumny pochodzą z kontrolowanego pliku konfiguracyjnego; cytujemy je przez psycopg2.sql.
    from psycopg2 import sql
    metric_select = [
        sql.SQL("{}").format(sql.Identifier(c)) for c in columns
    ]
    query = sql.SQL("""
        SELECT
            h3_index AS h3_id,
            resolution,
            ST_Area(ST_Transform(geom, %s)) AS hex_area_m2,
            ST_AsEWKB(geom) AS geom_ewkb,
            {}
        FROM grid.bdot_aggregation
        ORDER BY resolution, h3_index
    """).format(sql.SQL(", ").join(metric_select))

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM grid.bdot_aggregation;")
        expected_count = cur.fetchone()[0]
        cur.execute(query, [int(cfg["equal_area_srid"])])
        names = [d[0] for d in cur.description]
        rows = [dict(zip(names, r)) for r in cur.fetchall()]
    if not rows:
        raise RuntimeError("Tabela grid.bdot_aggregation nie zawiera heksagonów.")
    return rows, expected_count


def export_outputs(results: list[dict[str, Any]], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "iop_results.csv"
    json_path = out_dir / "iop_results.json"
    export_fields = [
        "h3_id", "resolution", "k_land", "k_hydro", "k_roads",
        "k_buildings", "iop", "coverage", "low_coverage",
        "land_area_sum_m2", "land_area_ratio", "land_area_warning",
        "coverage_warning", "warnings"
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=export_fields)
        writer.writeheader()
        for r in results:
            writer.writerow({k: r[k] for k in export_fields})
    with json_path.open("w", encoding="utf-8") as f:
        json.dump([{k: r[k] for k in export_fields} for r in results],
                  f, ensure_ascii=False, indent=2, allow_nan=False)
    return csv_path, json_path


def write_result_table(conn, results: list[dict[str, Any]], cfg: dict[str, Any], replace: bool) -> None:
    import psycopg2
    from psycopg2.extras import execute_values
    table = cfg["database"]["result_table"]
    if table != "grid.iop_test":
        raise ValueError("Por bezpieczeństwo wynik może być zapisany wyłącznie do grid.iop_test.")
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(NULLIF(ST_SRID(geom),0),0) FROM grid.bdot_aggregation LIMIT 1;")
        srid = int(cur.fetchone()[0])
        if srid <= 0:
            raise RuntimeError("Nie udało się ustalić SRID geometrii źródłowej.")
        cur.execute("""
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema='grid' AND table_name='iop_test'
            );
        """)
        exists = cur.fetchone()[0]
        if exists and not replace:
            raise RuntimeError(
                "Tabela grid.iop_test już istnieje. Jeśli chcesz ją zastąpić, "
                "uruchom ponownie z parametrem --replace."
            )
        if exists and replace:
            cur.execute("DROP TABLE grid.iop_test;")

        cur.execute(f"""
            CREATE TABLE grid.iop_test (
                h3_id text NOT NULL,
                resolution integer NOT NULL,
                k_land double precision NOT NULL,
                k_hydro double precision NOT NULL,
                k_roads double precision NOT NULL,
                k_buildings double precision NOT NULL,
                iop double precision NOT NULL,
                coverage double precision NOT NULL,
                low_coverage boolean NOT NULL,
                land_area_sum_m2 double precision NOT NULL,
                land_area_ratio double precision NOT NULL,
                land_area_warning boolean NOT NULL,
                coverage_warning boolean NOT NULL,
                warnings text,
                geom geometry(Geometry, {srid}) NOT NULL,
                PRIMARY KEY (resolution, h3_id)
            );
        """)
        values = [
            (
                r["h3_id"], r["resolution"], r["k_land"], r["k_hydro"],
                r["k_roads"], r["k_buildings"], r["iop"], r["coverage"],
                r["low_coverage"], r["land_area_sum_m2"], r["land_area_ratio"],
                r["land_area_warning"], r["coverage_warning"], r["warnings"],
                psycopg2.Binary(r["geom_ewkb"])
            )
            for r in results
        ]
        execute_values(
            cur,
            """
            INSERT INTO grid.iop_test (
                h3_id,resolution,k_land,k_hydro,k_roads,k_buildings,iop,
                coverage,low_coverage,land_area_sum_m2,land_area_ratio,
                land_area_warning,coverage_warning,warnings,geom
            ) VALUES %s
            """,
            values,
            template="(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,ST_GeomFromEWKB(%s))",
            page_size=500
        )
        cur.execute("CREATE INDEX iop_test_geom_gix ON grid.iop_test USING GIST (geom);")
        cur.execute("ANALYZE grid.iop_test;")


class IOPUnitTests(unittest.TestCase):
    def setUp(self):
        self.cfg = json.loads(json.dumps({
            "weights": {"k_land": 1.6, "k_hydro": 1.2, "k_roads": 0.8, "k_buildings": 0.4},
            "land_classes": {
                "area_ot_ptgn_a_m2": -0.2, "area_ot_ptkm_a_m2": 0.0,
                "area_ot_ptlz_a_m2": -0.8, "area_ot_ptnz_a_m2": -0.5,
                "area_ot_ptpl_a_m2": 0.0, "area_ot_ptrk_a_m2": -0.6,
                "area_ot_ptso_a_m2": -0.5, "area_ot_pttr_a_m2": 0.6,
                "area_ot_ptut_a_m2": -0.3, "area_ot_ptwp_a_m2": 0.0,
                "area_ot_ptwz_a_m2": -0.7, "area_ot_ptzb_a_m2": 0.0
            },
            "hydro": {
                "water_area_column": "area_ot_ptwp_a_m2",
                "wetland_area_columns": ["area_ot_oimk_a_m2"],
                "reed_area_columns": ["area_ot_oisz_a_m2"],
                "river_length_column": "length_ot_swrs_l_m",
                "channel_length_column": "length_ot_swkn_l_m",
                "ditch_length_column": "length_ot_swrm_l_m",
                "water_area_threshold": 0.2, "wetland_area_threshold": 0.2,
                "reed_area_threshold": 0.2, "stream_density_threshold_km_per_km2": 5.0,
                "ditch_factor": 0.25
            },
            "roads": {"length_column": "length_ot_skdr_l_m", "saturation_density_km_per_km2": 10.0},
            "buildings": {"area_column": "area_ot_bubd_a_m2", "saturation_area_share": 0.2},
            "coverage": {
                "analysis_area_column": "area_ot_adja_a_m2",
                "low_coverage_threshold": 0.1,
                "land_area_tolerance_fraction": 0.05,
                "coverage_overflow_tolerance_fraction": 0.02
            }
        }))
        self.row = {
            "h3_id": "testhex", "resolution": 7, "hex_area_m2": 10000.0,
            "area_ot_adja_a_m2": 10000.0,
            "area_ot_ptgn_a_m2": 0.0, "area_ot_ptkm_a_m2": 0.0,
            "area_ot_ptlz_a_m2": 0.0, "area_ot_ptnz_a_m2": 0.0,
            "area_ot_ptpl_a_m2": 0.0, "area_ot_ptrk_a_m2": 0.0,
            "area_ot_ptso_a_m2": 0.0, "area_ot_pttr_a_m2": 10000.0,
            "area_ot_ptut_a_m2": 0.0, "area_ot_ptwp_a_m2": 0.0,
            "area_ot_ptwz_a_m2": 0.0, "area_ot_ptzb_a_m2": 0.0,
            "area_ot_oimk_a_m2": 0.0, "area_ot_oisz_a_m2": 0.0,
            "length_ot_swrs_l_m": 0.0, "length_ot_swkn_l_m": 0.0,
            "length_ot_swrm_l_m": 0.0, "length_ot_skdr_l_m": 100.0,
            "area_ot_bubd_a_m2": 0.0, "geom_ewkb": b""
        }

    def test_open_land_and_road(self):
        result = calculate_one(self.row, self.cfg)
        self.assertAlmostEqual(result["k_land"], 0.6)
        self.assertAlmostEqual(result["k_hydro"], 0.0)
        self.assertAlmostEqual(result["k_roads"], 1.0)
        self.assertAlmostEqual(result["k_buildings"], 0.0)
        self.assertGreaterEqual(result["iop"], -1.0)
        self.assertLessEqual(result["iop"], 1.0)

    def test_water_reduces_hydro_score(self):
        row = dict(self.row)
        row["area_ot_ptwp_a_m2"] = 2000.0
        result = calculate_one(row, self.cfg)
        self.assertAlmostEqual(result["k_hydro"], -1.0)

    def test_low_coverage_flag(self):
        row = dict(self.row)
        row["area_ot_adja_a_m2"] = 500.0
        result = calculate_one(row, self.cfg)
        self.assertTrue(result["low_coverage"])

    def test_invalid_zero_area(self):
        row = dict(self.row)
        row["area_ot_adja_a_m2"] = 0
        with self.assertRaises(ValueError):
            calculate_one(row, self.cfg)

    def test_null_non_denominator_metric_is_zero(self):
        row = dict(self.row)
        row["length_ot_skdr_l_m"] = None
        result = calculate_one(row, self.cfg)
        self.assertEqual(result["k_roads"], 0.0)


def run_tests() -> bool:
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(IOPUnitTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return result.wasSuccessful()


def main() -> int:
    parser = argparse.ArgumentParser(description="Obliczanie IOP dla heksagonów H3.")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--dry-run", action="store_true",
                        help="oblicza i eksportuje CSV/JSON bez zapisu tabeli w bazie")
    parser.add_argument("--replace", action="store_true",
                        help="zastępuje istniejącą tabelę grid.iop_test")
    parser.add_argument("--test", action="store_true",
                        help="uruchamia testy jednostkowe bez łączenia z bazą")
    args = parser.parse_args()

    if args.test:
        return 0 if run_tests() else 1

    cfg = load_config(args.config)
    db = cfg["database"]
    if int(db["port"]) != 5434:
        raise RuntimeError("Zabezpieczenie: ten skrypt może łączyć się tylko z testowym portem 5434.")

    import psycopg2
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["dbname"],
        user=db["user"], password=db["password"], connect_timeout=10,
        application_name="PSIP_IОP_ENV_TEST"
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT current_database(), current_user, PostGIS_Full_Version();")
            db_info = cur.fetchone()
        print(f"Baza: {db_info[0]} | użytkownik: {db_info[1]} | TEST localhost:{db['port']}")
        rows, expected_count = fetch_source_rows(conn, cfg)
        if len(rows) != expected_count:
            raise RuntimeError(f"Pobrano {len(rows)} z {expected_count} heksagonów.")
        results = [calculate_one(r, cfg) for r in rows]

        csv_path, json_path = export_outputs(results, OUTPUT_DIR)
        print(f"Obliczono heksagonów: {len(results)}")
        print(f"Zakres IOP: {min(r['iop'] for r in results):.6f} .. {max(r['iop'] for r in results):.6f}")
        print(f"CSV:  {csv_path}")
        print(f"JSON: {json_path}")
        print(f"Ostrzeżenia low_coverage: {sum(r['low_coverage'] for r in results)}")
        print(f"Ostrzeżenia sumy klas PT: {sum(r['land_area_warning'] for r in results)}")

        if args.dry_run:
            conn.rollback()
            print("DRY-RUN: nie zapisano niczego do bazy.")
        else:
            write_result_table(conn, results, cfg, args.replace)
            conn.commit()
            print("ZAPISANO: grid.iop_test (wyłącznie baza testowa localhost:5434).")
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
