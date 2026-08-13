"""Storage layer: bronze Parquet dataset and the Postgres warehouse.

`parquet_writer` converts raw CelesTrak CSV landings into a partitioned
Parquet dataset; `postgres_loader` loads that dataset into the
`bronze.raw_gp` table that dbt builds the silver and gold layers from.
"""
