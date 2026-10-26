-- Runs once when the postgres container's data directory is first
-- initialized (docker-entrypoint-initdb.d convention). Creates the
-- permanent control/logging databases plus a demo template database for
-- the example-buggy-api backend (spec sections 12, 14, 15).
CREATE DATABASE stqa_control;
CREATE DATABASE stqa_logging;
CREATE DATABASE stqa_template_demo;

-- Read-only role for the separate assessment tooling (../assessment).
-- It can only SELECT from the logging database; it has no access to the
-- control database (which holds credential hashes) or team databases.
-- Change the password for any non-local deployment.
CREATE ROLE stqa_assessment LOGIN PASSWORD 'stqa_assessment';
REVOKE ALL ON DATABASE stqa_control FROM PUBLIC;
GRANT CONNECT ON DATABASE stqa_logging TO stqa_assessment;
\connect stqa_logging
GRANT USAGE ON SCHEMA public TO stqa_assessment;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO stqa_assessment;
-- Tables are created later by gateway migrations (run as stqa).
ALTER DEFAULT PRIVILEGES FOR ROLE stqa IN SCHEMA public GRANT SELECT ON TABLES TO stqa_assessment;
