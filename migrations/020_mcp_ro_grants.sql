-- Migration 020: read-only grants for the MCP server (role t0_mcp_ro).
-- Source: docs/HANDOFF-MCP.md section 3.
-- Batch L1 (t0) -- do not modify the DDL manually outside migration.

-- WHY. The MCP server (HANDOFF-MCP.md) exposes the training marts to an LLM over
-- Streamable HTTP. It deliberately does NOT use the t0 app credentials: a dedicated
-- role t0_mcp_ro, holding SELECT only, makes data mutation impossible by construction
-- for that process -- no INSERT/UPDATE/DELETE/DDL grant exists to misuse (HANDOFF-MCP
-- section 5). core.threshold is granted explicitly alongside mart.* because the
-- get_thresholds tool reads it too.
--
-- PRECONDITION, BY DEPLOYMENT ORDER NOT BY GUARD. The role itself is NOT created here:
-- it lives on the CNPG side (homelab repo, managed.roles), because the app user this
-- migration runs as lacks CREATEROLE. The convention of this directory is forward-only
-- migrations whose preconditions are guaranteed before the runner starts -- no other
-- migration carries an existence guard either. Deploy the homelab managed.roles change
-- BEFORE running migrations; a GRANT to a missing role fails loudly, which is the
-- desired failure mode (a guard would convert a deployment-order mistake into a
-- silently ungranted role -- the MCP would then fail on first query anyway, later and
-- less legibly). The statements themselves are naturally re-runnable: re-GRANTing an
-- existing privilege is a no-op, so an interrupted run can be replayed as-is.
--
-- FOR ROLE t0 ON THE DEFAULT PRIVILEGES. ALTER DEFAULT PRIVILEGES binds to the role
-- that will CREATE the future objects, not to the objects that exist today -- without
-- FOR ROLE it would bind to whoever executes this file and grant nothing once the
-- creating role differs. The migration runner connects as the app user t0 (CNPG
-- <cluster>-app Secret, chart values postgresql.auth.username = t0), which owns every
-- object created by migrations 001-019 and will create every future mart. Naming t0
-- explicitly makes the binding independent of who happens to run the file.

GRANT USAGE ON SCHEMA mart TO t0_mcp_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA mart TO t0_mcp_ro;
GRANT SELECT ON core.threshold TO t0_mcp_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE t0 IN SCHEMA mart GRANT SELECT ON TABLES TO t0_mcp_ro;
