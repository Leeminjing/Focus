"""同名部署请求在不同注册顺序下保留各自已发布的 OpenAPI 引用。"""

import pytest
from fastapi import FastAPI
from pydantic import create_model

from backend.app.desktop.api.openapi import install_desktop_openapi_contract
from backend.app.desktop.api.schemas import DeployRequest


@pytest.mark.parametrize("desktop_first", [True, False])
def test_deployment_schema_identity_is_independent_of_registration_order(desktop_first):
    app = FastAPI()
    plugin_request = create_model(
        "DeployRequest", instruction=(str, ...), permissions=(list[str], ...)
    )

    async def desktop(body: DeployRequest):
        return body.model_dump()

    async def plugin(body: plugin_request):
        return body.model_dump()

    endpoints = [("/desktop", desktop), ("/plugin", plugin)]
    for path, endpoint in endpoints if desktop_first else reversed(endpoints):
        app.post(path)(endpoint)
    install_desktop_openapi_contract(app)
    schema = app.openapi()
    desktop_name = "backend__app__desktop__models__DeployRequest"
    assert (
        "deployment_id" in schema["components"]["schemas"][desktop_name]["properties"]
    )
    assert (
        "instruction" in schema["components"]["schemas"]["DeployRequest"]["properties"]
    )
    for path, name in (("/desktop", desktop_name), ("/plugin", "DeployRequest")):
        assert schema["paths"][path]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"] == {"$ref": f"#/components/schemas/{name}"}
    assert app.openapi() == schema


def test_standalone_deployment_schema_keeps_its_unqualified_name():
    app = FastAPI()

    @app.post("/desktop")
    async def desktop(body: DeployRequest):
        return body.model_dump()

    install_desktop_openapi_contract(app)
    assert set(app.openapi()["components"]["schemas"]) >= {"DeployRequest"}
