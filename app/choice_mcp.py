"""Read-only MCP App alongside the existing GPT Actions server."""
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations

from .scene_choices import saved_scene_choices

UI_DIR = Path(__file__).resolve().parent.parent / "ui"
RESOURCE = "ui://roman-ai/scene-choices-v1.html"
def choice_resource() -> str:
    return (UI_DIR / "scene_choices.html").read_text().replace(
        "/* CHOICE_APP */", (UI_DIR / "scene_choices.js").read_text())


READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)


def show_scene_choices(session_id: str, expected_turn: int) -> CallToolResult:
    """Display interactive choices from an already committed scene. Does not change the session."""
    data = saved_scene_choices(session_id, expected_turn)
    return CallToolResult(content=[TextContent(type="text", text="Варианты сохранённого хода.")],
                          structuredContent=data)


def check_scene_choices(session_id: str, expected_turn: int, expected_scene_sha256: str) -> CallToolResult:
    """Before sending a selection, verify that the displayed scene is still current."""
    data = saved_scene_choices(session_id, expected_turn)
    if data["scene_sha256"] != expected_scene_sha256:
        raise ValueError("SCENE_CHANGED_REOPEN_CHOICES")
    return CallToolResult(content=[TextContent(type="text", text="Сцена актуальна.")],
                          structuredContent={"session_id": data["session_id"],
                                             "turn_number": data["turn_number"], "current": True})


def create_choice_mcp() -> FastMCP:
    # StreamableHTTP managers have a single lifespan. Construct a fresh manager
    # for each application startup, including test clients and worker restarts.
    mcp = FastMCP(
        "Roman AI choices", host="0.0.0.0", streamable_http_path="/",
        stateless_http=True, json_response=True,
        instructions="After committing a scene with existing Actions, call show_scene_choices with its session_id and committed turn number. This tool only reads; selected text returns as the user's exact next input. Never generate or commit a turn just to display choices.",
    )
    mcp.resource(RESOURCE, mime_type="text/html;profile=mcp-app", meta={
        "ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
        "openai/widgetDescription": "Выбор действий, реплик и мыслей из сохранённой сцены.",
    })(choice_resource)
    mcp.tool(annotations=READ_ONLY, meta={
        "ui": {"resourceUri": RESOURCE}, "openai/outputTemplate": RESOURCE,
    })(show_scene_choices)
    mcp.tool(annotations=READ_ONLY, meta={"ui": {"visibility": ["app"]}})(check_scene_choices)
    return mcp
