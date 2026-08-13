-- Medallion layers as schemas in a single Postgres instance.
--
-- bronze : faithful landing of what CelesTrak actually delivered
-- silver : normalised, deduplicated, historised element sets (dbt)
-- gold   : serving models for the app (dbt)

CREATE SCHEMA IF NOT EXISTS bronze;
CREATE SCHEMA IF NOT EXISTS silver;
CREATE SCHEMA IF NOT EXISTS gold;
