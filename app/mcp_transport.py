"""Expose the existing Actions contract on the RomanAI MCP endpoint."""

import json
import logging
import secrets
from time import perf_counter
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
from pydantic import BaseModel, ConfigDict, Field
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
    unread_chunk_indices: list[int] | None = Field(default=None, description="Only these indices need reading. Empty means stop reading and commit the prepared scene.")
    read_chunks: list[int] | None = None
    all_chunks_read: bool | None = Field(default=None, description="True means the entire packet has been delivered; do not restart preparation or reading.")
    reused_pending_packet: bool | None = Field(default=None, description="True means this is the existing packet, with its read progress preserved.")
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
    previously_read_chunk_indices: list[int] | None = None


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


_TOOL_LOG = logging.getLogger("uvicorn.error")


def _trace_tool(event, trace_id, name, kwargs, *, started, result=None, error=None):
    # Log only operation metadata, never scene text, RAW, tokens or full payloads.
    body = kwargs.get("body")
    if isinstance(body, BaseModel):
        body = {key: getattr(body, key, None) for key in ("packet_id", "audit_id", "request_id")}
    body = body if isinstance(body, dict) else {}
    result = result if isinstance(result, dict) else {}
    row = {"event": event, "call_id": trace_id, "tool": name}
    for key in ("session_id", "draft_id", "packet_id", "audit_id", "request_id", "chunk_index"):
        value = kwargs.get(key, body.get(key, result.get(key)))
        if value is not None:
            row[key] = value if isinstance(value, int) else str(value)[:128]
    if event != "start":
        row["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        for key in ("prepared_for_turn", "turn_number", "chunk_count", "chunk_indices",
                    "unread_chunk_indices", "previously_read_chunk_indices", "all_chunks_read",
                    "reused_pending_packet", "already_committed", "idempotent_replay"):
            if key in result:
                row[key] = result[key]
    if error is not None:
        row["error_type"] = type(error).__name__
        if isinstance(error, HTTPException):
            row["status"] = error.status_code
            if isinstance(error.detail, dict) and isinstance(error.detail.get("code"), str):
                row["error_code"] = error.detail["code"][:128]
    _TOOL_LOG.info("romanai_tool %s", json.dumps(row, ensure_ascii=True))


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
        started = perf_counter()
        trace_id = secrets.token_hex(6)
        _trace_tool("start", trace_id, route.operation_id, kwargs, started=started)
        try:
            result = await run_in_threadpool(route.endpoint, **kwargs)
        except Exception as exc:
            _trace_tool("error", trace_id, route.operation_id, kwargs, started=started, error=exc)
            if isinstance(exc, HTTPException):
                detail = json.dumps(exc.detail, ensure_ascii=False)
                raise ToolError(f"HTTP {exc.status_code}: {detail}") from exc
            raise
        _trace_tool("end", trace_id, route.operation_id, kwargs, started=started, result=result)

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
