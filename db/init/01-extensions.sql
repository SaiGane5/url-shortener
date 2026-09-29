-- Runs once, on first initialisation of the PostgreSQL data volume.
--
-- Only extensions live here. The schema itself is owned by Alembic
-- (backend/alembic/versions) so that there is exactly one migration path and
-- a fresh database and an upgraded one end up identical.

-- pg_stat_statements aggregates execution statistics per normalised query.
-- It is what turns "the API feels slow" into "this specific statement is
-- responsible for 80% of the time", and it is used in docs/PERFORMANCE.md.
-- Requires `shared_preload_libraries=pg_stat_statements`, set in the
-- postgres service command in docker-compose.yml.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;
