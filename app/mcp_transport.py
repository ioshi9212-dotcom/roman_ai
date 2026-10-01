"""Expose the Actions contract over MCP without a second persistence path."""

from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

import yaml
from fastapi import HTTPException
from fastapi.routing import APIRoute
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.concurrency import run_in_threadpool


def _tool_endpoint(endpoint):
    # Keep the original typed signature: MCP validates the same Pydantic body
    # model as REST, including nested objects and required fields.
    @wraps(endpoint)
    async def call(**kwargs):
        try:
            return await run_in_threadpool(endpoint, **kwargs)
        except HTTPException as exc:
            # A backend rejection must remain an MCP tool error, not a
            # successful result. Preserve validation/retry details verbatim.
            raise ToolError(f"HTTP {exc.status_code}: {exc.detail}") from exc

    return call


def install_mcp(app):
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "openapi.yaml").read_text(encoding="utf-8")
    )
    routes = {
        route.operation_id: route
        for route in app.routes
        if isinstance(route, APIRoute) and route.operation_id
    }
    server = FastMCP(
        "Roman AI",
        instructions=(
            "Persistent novel backend. Tool names match the Actions operationIds. "
            "Pass path/query parameters by name and the JSON request payload under body. "
            "Read every required chunk; reuse request_id for technical retries."
        ),
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[
                "ai-roman-yumikofv.mia0.amvera.tech",
                "localhost:*", "127.0.0.1:*", "[::1]:*", "testserver",
            ],
            allowed_origins=[
                "https://ai-roman-yumikofv.mia0.amvera.tech",
                "http://localhost:*", "http://127.0.0.1:*",
            ],
        ),
    )
    for path, methods in contract["paths"].items():
        for method, operation in methods.items():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            name = operation["operationId"]
            route = routes[name]
            if route.path != path or method.upper() not in route.methods:
                raise ValueError(f"MCP contract does not match REST route: {name}")
            description = "\n".join(
                operation[key] for key in ("summary", "description") if operation.get(key)
            )
            server.add_tool(_tool_endpoint(route.endpoint), name=name, description=description)

    transport = server.streamable_http_app()
    original_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        # Preserve existing startup hooks (including optional session migration).
        async with original_lifespan(application) as state:
            async with server.session_manager.run():
                yield state

    app.router.lifespan_context = lifespan
    # REST routes take precedence. The mounted app serves exactly /mcp.
    app.mount("/", transport)
    return server
