"""Liveness and readiness endpoints consumed by the Kubernetes probes of the chart
(deploy/helm/t0/templates/ingest-deployment.yaml: livenessProbe -> /healthz,
readinessProbe -> /readyz).

The split is not cosmetic, it is the whole point:

- /healthz (liveness): the process is alive and the event loop answers. It checks
  NOTHING else, because a failing liveness probe restarts the container -- and a
  restart can fix neither PostgreSQL nor the network. A dependency outage must
  never translate into a restart loop.

- /readyz (readiness): this API does exactly one thing, writing to PostgreSQL on
  behalf of a manual upload; it is "ready" when that write path works. The probe
  opens a real connection (bounded by connect_timeout) and runs SELECT 1.
  Failure answers 503: the Service stops routing to this pod, and -- unlike
  liveness -- readiness failures never restart anything.

No exception text is ever echoed in a response: connection errors carry host and
user names, and the DSN carries the database password (see t0.settings on value
leakage). A generic detail is all a probe consumer needs.
"""

from __future__ import annotations

import psycopg
from fastapi import APIRouter, HTTPException

from t0.settings import get_settings

router = APIRouter(tags=["health"])

# Bounded so a half-open TCP connection to PostgreSQL cannot hold the readiness
# probe past its own timeoutSeconds (5 in the chart).
READYZ_CONNECT_TIMEOUT_S = 3


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: process alive. Deliberately dependency-free, see module docstring."""
    return {"status": "ok"}


@router.get("/readyz")
def readyz() -> dict[str, str]:
    """Readiness: the PostgreSQL write path works. 503 otherwise, see module docstring."""
    dsn = get_settings().postgres_dsn.get_secret_value()
    try:
        conn = psycopg.connect(dsn, connect_timeout=READYZ_CONNECT_TIMEOUT_S)
    except Exception:
        raise HTTPException(status_code=503, detail="database unreachable") from None
    try:
        conn.execute("SELECT 1")
    except Exception:
        raise HTTPException(status_code=503, detail="database unreachable") from None
    finally:
        conn.close()
    return {"status": "ok"}
