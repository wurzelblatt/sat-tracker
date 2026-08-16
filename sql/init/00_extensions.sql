-- Extensions the schemas below depend on.
--
-- Numbered 00 because /docker-entrypoint-initdb.d runs its scripts in
-- filename order, and 04_gold_position_snapshot.sql declares a
-- geography(Point, 4326) column that cannot resolve without PostGIS
-- already installed.
--
-- The postgis/postgis image ships the extension files; this statement is
-- what installs it into THIS database. Running these init scripts against
-- a plain postgres image fails here, which is the intended behaviour: a
-- silently absent PostGIS would otherwise surface much later as an
-- unrecognised type error in a table nobody was looking at.

CREATE EXTENSION IF NOT EXISTS postgis;
