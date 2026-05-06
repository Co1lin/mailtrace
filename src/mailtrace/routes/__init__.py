"""Route registration. Imported by app.py as a single combined router."""

from __future__ import annotations

from fastapi import APIRouter

from . import addresses, admin, auth, ingest, main, pieces, setup

router = APIRouter()
router.include_router(setup.router, tags=["setup"])
router.include_router(main.router)
router.include_router(ingest.router, tags=["ingest"])
router.include_router(auth.router, prefix="/auth", tags=["auth"])
router.include_router(admin.router, prefix="/admin", tags=["admin"])
router.include_router(addresses.router, prefix="/addresses", tags=["addresses"])
router.include_router(pieces.router, prefix="/pieces", tags=["pieces"])
