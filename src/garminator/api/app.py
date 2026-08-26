"""FastAPI application factory.

A function rather than a module-level instance, same convention as
:func:`garminator.settings.get_settings`: every caller (tests included) controls when
construction happens, with no import-time side effect that would read the environment
as soon as the module loads.
"""

from __future__ import annotations

from fastapi import FastAPI

from garminator.api.rpe import router as rpe_router
from garminator.api.upload import router as upload_router


def create_app() -> FastAPI:
    """Build the FastAPI application, mounting the manual upload router and the manual
    RPE entry router.

    Intended for LOCAL access only (no Ingress, no TLS, no authentication).
    """
    app = FastAPI(
        title="garminator — manual upload",
        description=(
            "Local endpoint for manually uploading a FIT file, a TCX file, or an "
            '"original" Garmin ZIP archive, and for entering the RPE of an activity '
            "that no computed method could measure."
        ),
    )
    app.include_router(upload_router)
    app.include_router(rpe_router)
    return app
