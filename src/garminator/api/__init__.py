"""HTTP API: manual upload endpoint for FIT/TCX/ZIP files.

This package deliberately re-exports NOTHING (unlike ``garminator.garmin``):
``garminator.api.upload`` (and therefore ``garminator.api.app``) declares a route using
``File(...)``, which makes the module IMPORT fail outright when ``python-multipart`` is
missing. Re-exporting those symbols here would break importing
``garminator.api.ingest`` (pure logic, no FastAPI/multipart dependency) in any
environment without ``python-multipart``. Import the submodule you need directly:
``garminator.api.ingest`` for the business logic, ``garminator.api.app``/``.upload`` for
the HTTP layer.
"""

from __future__ import annotations
