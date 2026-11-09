BEGIN;

CREATE TABLE IF NOT EXISTS grid.iop_color_6 (
    h3_id text PRIMARY KEY,
    resolution integer NOT NULL,
    iop double precision,
    geom geometry(Polygon, 2180) NOT NULL
);

CREATE TABLE IF NOT EXISTS grid.iop_color_7 (
    h3_id text PRIMARY KEY,
    resolution integer NOT NULL,
    iop double precision,
    geom geometry(Polygon, 2180) NOT NULL
);

CREATE TABLE IF NOT EXISTS grid.iop_color_8 (
    h3_id text PRIMARY KEY,
    resolution integer NOT NULL,
    iop double precision,
    geom geometry(Polygon, 2180) NOT NULL
);

TRUNCATE grid.iop_color_6;
TRUNCATE grid.iop_color_7;
TRUNCATE grid.iop_color_8;

INSERT INTO grid.iop_color_6
SELECT h3_id, resolution, iop, geom
FROM grid.iop_test WHERE resolution = 6;

INSERT INTO grid.iop_color_7
SELECT h3_id, resolution, iop, geom
FROM grid.iop_test WHERE resolution = 7;

INSERT INTO grid.iop_color_8
SELECT h3_id, resolution, iop, geom
FROM grid.iop_test WHERE resolution = 8;

CREATE INDEX IF NOT EXISTS iop_color_6_geom_gix
ON grid.iop_color_6 USING GIST (geom);

CREATE INDEX IF NOT EXISTS iop_color_7_geom_gix
ON grid.iop_color_7 USING GIST (geom);

CREATE INDEX IF NOT EXISTS iop_color_8_geom_gix
ON grid.iop_color_8 USING GIST (geom);

COMMIT;
