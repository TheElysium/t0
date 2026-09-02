# AGENTS.md

- Comments: 2 lines max. Be frugal; the code must be self-descriptive.
- Docs: concise and direct. Short sentences, no prose. Stay pragmatic.
- The repo must stay infra-agnostic: no homelab-specific names, IPs, or hostnames in committed docs. Anyone can deploy t0 on their own infrastructure.
- Run lint/typecheck before handing off: `ruff check .`, `ruff format --check .`, `mypy src tests`, `pytest`.
