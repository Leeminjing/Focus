"""Human-operated storage settings, protected by the Gateway session middleware."""

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from backend.app.desktop.persistence.maintenance import StoragePolicy

storage_router = APIRouter(prefix="/desktop/api/settings/storage")


def _service(request):
    service = getattr(request.app.state, "storage_maintenance", None)
    if service is None:
        raise HTTPException(503, "存储维护服务尚未就绪")
    return service


async def _call(request, operation, *args):
    try:
        return await getattr(_service(request), operation)(*args)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    except (OSError, RuntimeError) as error:
        raise HTTPException(503, str(error)) from error


@storage_router.get("")
async def status(request: Request):
    return await _call(request, "status")


@storage_router.put("/policy")
async def policy(body: StoragePolicy, request: Request):
    return await _call(request, "save_policy", body)


@storage_router.post("/preview")
async def preview(request: Request):
    return await _call(request, "preview")


class CleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str = Field(min_length=1, max_length=100)


@storage_router.post("/cleanup")
async def cleanup(body: CleanupRequest, request: Request):
    return await _call(request, "cleanup", body.token)


@storage_router.post("/backup")
async def backup(request: Request):
    return await _call(request, "create_backup")
