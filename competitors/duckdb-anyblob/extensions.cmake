# DuckDB extensions for the duckdb-anyblob competitor: the same set the stock
# CLI the Linux arm runs has (tpch, parquet, core functions), and httpfs from
# the checkout beside the DuckDB one, built with its AnyBlob client. Passed
# to DuckDB's CMake as -DDUCKDB_EXTENSION_CONFIGS.
duckdb_extension_load(tpch)
duckdb_extension_load(json)
duckdb_extension_load(httpfs
    SOURCE_DIR ${CMAKE_CURRENT_LIST_DIR}/../../apps/miniduckdb-httpfs
)
