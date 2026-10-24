"""桌面接口装配；请求处理归属各业务领域的接口模块。"""

from fastapi import APIRouter

from backend.app.desktop.api.agents import router as agents_router
from backend.app.desktop.api.capabilities import router as capabilities_router
from backend.app.desktop.api.contexts import router as contexts_router
from backend.app.desktop.api.conversations import router as conversations_router
from backend.app.desktop.api.file_links import file_link_router
from backend.app.desktop.api.files import file_router
from backend.app.desktop.api.materials import router as materials_router
from backend.app.desktop.api.patrol import router as patrol_router
from backend.app.desktop.api.runs import router as runs_router
from backend.app.desktop.api.system import router as system_router
from backend.app.desktop.api.workspace_materials import workspace_material_router
from backend.app.desktop.api.workspaces import router as workspaces_router
from backend.app.desktop.browser.routes import browser_router
from backend.app.desktop.conversations.changes import task_changes_router

desktop_router = APIRouter(prefix="/desktop/api")
desktop_router.include_router(workspace_material_router)
desktop_router.include_router(file_link_router)
desktop_router.include_router(file_router)
desktop_router.include_router(task_changes_router)
desktop_router.include_router(browser_router)

desktop_router.include_router(system_router)

desktop_router.include_router(workspaces_router)

desktop_router.include_router(conversations_router)

desktop_router.include_router(contexts_router)

desktop_router.include_router(capabilities_router)

desktop_router.include_router(patrol_router)

desktop_router.include_router(runs_router)

desktop_router.include_router(agents_router)

desktop_router.include_router(materials_router)
