-- Migration 021: core-schema grants for the MCP server (fix of migration 020).
-- 020 granted USAGE on mart only, so core.threshold and the core.activity join
-- in the MCP tools failed with "permission denied for schema core". Re-runnable.

DO $$
BEGIN
    IF current_user <> 't0' THEN
        RAISE EXCEPTION
            'migration 021 must run as role t0 (default privileges bind to it), got %',
            current_user;
    END IF;
END
$$;

GRANT USAGE ON SCHEMA core TO t0_mcp_ro;
GRANT SELECT ON core.threshold TO t0_mcp_ro;
GRANT SELECT ON core.activity TO t0_mcp_ro;
