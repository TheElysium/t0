-- Migration 020: read-only grants for the MCP server (role t0_mcp_ro).
-- Source: docs/HANDOFF-MCP.md section 3.
-- Batch L1 (t0) -- do not modify the DDL manually outside migration.
--
-- The role itself is created CNPG-side (homelab managed.roles) and must exist
-- before this runs; a GRANT to a missing role fails loudly, by design. The
-- statements are naturally re-runnable (re-GRANT is a no-op).
--
-- FOR ROLE t0: default privileges bind to the role that will CREATE the future
-- marts (the migration runner connects as the app user t0), not to whoever
-- executes this file.

GRANT USAGE ON SCHEMA mart TO t0_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO t0_mcp_ro;
GRANT SELECT ON core.threshold TO t0_mcp_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE t0 IN SCHEMA mart GRANT SELECT ON TABLES TO t0_mcp_ro;
