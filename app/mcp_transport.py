"""Expose the existing Actions contract on the RomanAI MCP endpoint."""

import json
from functools import wraps
from pathlib import Path

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import ToolAnnotations
from starlette.concurrency import run_in_threadpool


ACTIONS_CONTRACT = Path(__file__).resolve().parents[1] / "openapi.yaml"
READ_ONLY_ACTIONS = {
    "listNovels",
    "prepareCharacterBundleRead",
    "getCharacterBundleChunk",
    "prepareSceneArchiveRead",
    "getSceneArchiveChunk",
}


def _tool_endpoint(route: APIRoute):
    # Preserve the handler signature so FastMCP validates the original Pydantic body.
    @wraps(route.endpoint)
    async def call(**kwargs):
        # MCP string arguments must fit a single REST path segment.
        for name in route.param_convertors:
            value = kwargs[name]
            if isinstance(value, str) and (
                value in {"", ".", ".."} or any(char in value for char in ("/", "\\", "\x00"))
            ):
                raise ToolError(f"HTTP 422: Invalid path parameter {name}")
        try:
            return await run_in_threadpool(route.endpoint, **kwargs)
        except HTTPException as exc:
            detail = json.dumps(exc.detail, ensure_ascii=False)
            raise ToolError(f"HTTP {exc.status_code}: {detail}") from exc

    return call


def register_actions(server: FastMCP, app: FastAPI) -> None:
    """Register the supported Actions, keeping internal REST routes out of MCP."""
    contract = yaml.safe_load(ACTIONS_CONTRACT.read_text(encoding="utf-8"))
    routes = {
        route.operation_id: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.include_in_schema and route.operation_id
    }
    for path, methods in contract["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            name = operation["operationId"]
            route = routes.get(name)
            if route is None or route.path != path or method.upper() not in route.methods:
                raise ValueError(f"MCP contract does not match REST route: {name}")
            description = "\n".join(operation[key] for key in ("summary", "description") if operation.get(key))
            server.add_tool(
                _tool_endpoint(route), name=name, description=description,
                annotations=ToolAnnotations(readOnlyHint=name in READ_ONLY_ACTIONS),
            )
