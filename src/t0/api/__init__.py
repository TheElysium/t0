"""HTTP API: manual upload endpoint for FIT/TCX/ZIP files, manual RPE entry endpoint.

This package deliberately re-exports NOTHING (unlike ``t0.garmin``):
``t0.api.upload`` (and therefore ``t0.api.app``) declares a route using
``File(...)``, which makes the module IMPORT fail outright when ``python-multipart`` is
missing. Re-exporting those symbols here would break importing
``t0.api.ingest`` (pure logic, no FastAPI/multipart dependency) in any
environment without ``python-multipart``. Import the submodule you need directly:
``t0.api.ingest`` for the business logic, ``t0.api.app``/``.upload``/
``.rpe`` for the HTTP layer.
"""

from __future__ import annotations
