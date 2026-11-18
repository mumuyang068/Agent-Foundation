"""Optional, bounded MCP projection of public Agent operations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class PublicOperationCaller(Protocol):
    def call(
        self,
        operation_id: str,
        *,
        path: dict[str, str | int] | None = None,
        query: dict[str, str | int | bool | None] | None = None,
        payload: Any = None,
    ) -> Any: ...


@dataclass(frozen=True)
class AgentTool:
    name: str
    operation_id: str
    path_fields: tuple[str, ...] = ()


AGENT_TOOLS: tuple[AgentTool, ...] = (
    AgentTool("memory_recall", "memory.recall"),
    AgentTool("memory_propose", "memory.propose"),
    AgentTool("context_prepare", "context.assemble"),
    AgentTool("evidence_search", "evidence.search"),
    AgentTool("graph_query", "graph.query"),
    AgentTool("turn_prepare", "applications.prepare"),
    AgentTool("turn_observe", "applications.observe", ("turn_id",)),
    AgentTool("turn_outcome", "applications.outcome", ("turn_id",)),
)


class McpAgentSurface:
    """Map a fixed Agent tool set to the public application contract."""

    def __init__(self, caller: PublicOperationCaller) -> None:
        self._caller = caller
        self._tools = {tool.name: tool for tool in AGENT_TOOLS}

    @property
    def tools(self) -> tuple[str, ...]:
        return tuple(self._tools)

    def invoke(self, name: str, arguments: dict[str, Any]) -> Any:
        try:
            tool = self._tools[name]
        except KeyError as error:
            raise ValueError(f"Unknown or unavailable Agent tool: {name}") from error
        values = dict(arguments)
        path: dict[str, str | int] = {}
        for field in tool.path_fields:
            if field not in values:
                raise ValueError(f"Tool {name} requires {field}")
            path[field] = values.pop(field)
        return self._caller.call(
            tool.operation_id,
            path=path or None,
            payload=values,
        )


def create_mcp_server(surface: McpAgentSurface, *, name: str = "agent-foundation"):
    """Create the optional MCP transport only when the extra is installed."""
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as error:  # pragma: no cover - exercised in clean install verification
        raise RuntimeError("Install agent-foundation[mcp] to enable the MCP adapter") from error
    server = FastMCP(name)
    for tool_name in surface.tools:
        def handler(arguments: dict[str, Any], *, _name: str = tool_name) -> Any:
            return surface.invoke(_name, arguments)

        handler.__name__ = tool_name
        server.tool(name=tool_name)(handler)
    return server
