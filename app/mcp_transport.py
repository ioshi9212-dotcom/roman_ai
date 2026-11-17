"""Expose the existing Actions contract on the RomanAI MCP endpoint."""

import json
from functools import wraps
from inspect import signature
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, ConfigDict
from starlette.concurrency import run_in_threadpool


ACTIONS_CONTRACT = Path(__file__).resolve().parents[1] / "openapi.yaml"
READ_ONLY_ACTIONS = {
    "listNovels",
    "prepareCharacterBundleRead",
    "getCharacterBundleChunk",
    "prepareSceneArchiveRead",
    "getSceneArchiveChunk",
}


class TurnPrepareOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    packet_id: str | None = None
    chunk_index: int | None = None
    chunk_count: int | None = None
    content: str | None = None
    request_id: str | None = None
    first_chunk_included: bool | None = None
    next_chunk_index: int | None = None
    pending_turn: dict[str, Any] | None = None
    audit_required: bool | None = None
    required_audit: dict[str, Any] | None = None
    already_committed_duplicate: bool | None = None
    scene_output: str | None = None


class TurnChunkOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    packet_id: str
    chunk_index: int
    chunk_count: int
    content: str
    all_chunks_read: bool
    chunk_indices: list[int] | None = None
    unread_chunk_indices: list[int] | None = None


class CharacterBundleOutput(BaseModel):
    model_config = ConfigDict(extra="allow")

    session_id: str
    character_id: str
    read_id: str
    chunk_index: int
    chunk_count: int
    content: str
    next_chunk_index: int | None
    first_chunk_included: bool | None = None
    all_chunks_read: bool | None = None


STRUCTURED_OUTPUTS = {
    "prepareTurn": Annotated[CallToolResult, TurnPrepareOutput],
    "getTurnPacketChunk": Annotated[CallToolResult, TurnChunkOutput],
    "prepareCharacterBundleRead": Annotated[CallToolResult, CharacterBundleOutput],
    "getCharacterBundleChunk": Annotated[CallToolResult, CharacterBundleOutput],
}


def _tool_endpoint(route: APIRoute):
    structured_output = STRUCTURED_OUTPUTS.get(route.operation_id)
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
            result = await run_in_threadpool(route.endpoint, **kwargs)
        except HTTPException as exc:
            detail = json.dumps(exc.detail, ensure_ascii=False)
            raise ToolError(f"HTTP {exc.status_code}: {detail}") from exc

        if structured_output is None:
            return result
        # Keep the exact response fields available to both structured and text clients.
        return CallToolResult(
            structuredContent=result,
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        )

    if structured_output is not None:
        call.__signature__ = signature(route.endpoint, eval_str=True).replace(
            return_annotation=structured_output,
        )
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
