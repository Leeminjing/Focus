"""稳定桌面与插件同名部署请求的已发布 OpenAPI 组件标识。"""

from fastapi import FastAPI


def install_desktop_openapi_contract(app: FastAPI) -> None:
    original_openapi = app.openapi

    def desktop_openapi() -> dict:
        schema = original_openapi()
        definitions = schema.get("components", {}).get("schemas", {})
        deployments = {
            name: definition
            for name, definition in definitions.items()
            if definition.get("title") == "DeployRequest"
        }
        if len(deployments) != 2:
            return schema
        desktop = next(
            (
                name
                for name, definition in deployments.items()
                if "deployment_id" in definition.get("properties", {})
            ),
            None,
        )
        plugin = next(
            (
                name
                for name, definition in deployments.items()
                if {"instruction", "permissions"}
                <= definition.get("properties", {}).keys()
            ),
            None,
        )
        if desktop is None or plugin is None or desktop == plugin:
            return schema
        names = {
            desktop: "backend__app__desktop__models__DeployRequest",
            plugin: "DeployRequest",
        }
        if all(old == new for old, new in names.items()):
            return schema
        references = {
            f"#/components/schemas/{old}": f"#/components/schemas/{new}"
            for old, new in names.items()
        }
        schema["components"]["schemas"] = {
            names.get(name, name): definition
            for name, definition in definitions.items()
        }

        def rewrite_references(value):
            if isinstance(value, dict):
                reference = value.get("$ref")
                if isinstance(reference, str) and reference in references:
                    value["$ref"] = references[reference]
                for child in value.values():
                    rewrite_references(child)
            elif isinstance(value, list):
                for child in value:
                    rewrite_references(child)

        rewrite_references(schema)
        return schema

    app.openapi = desktop_openapi
