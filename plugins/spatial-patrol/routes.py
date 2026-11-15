"""Optional manual DOCX editor API, under its existing plugin URL prefix.

General material previews are provided by the host file workbench. The plugin
only adds lazy ONLYOFFICE editing sessions, save commands and close operations.
"""

from fastapi import APIRouter

from plugins.spatial_patrol.docx_routes import router as docx_router

router = APIRouter()
router.include_router(docx_router)
