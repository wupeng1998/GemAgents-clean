from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class McpServer:
    name: str
    config: dict[str, object]


class McpRegistry:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.servers = self._load_servers()

    def list_resources(self) -> str:
        if not self.servers:
            return ""
        return "\n".join(
            f"{server.name}\t{server.config.get('command', '')}" for server in self.servers
        )

    def read_resource(self, uri: str) -> str:
        if uri.startswith("file://"):
            path = Path(uri.removeprefix("file://"))
            if not path.is_absolute():
                path = self.workspace / path
            return path.resolve().read_text(encoding="utf-8")
        raise ValueError(f"unsupported MCP resource URI: {uri}")

    def _load_servers(self) -> list[McpServer]:
        path = self.workspace / ".mcp.json"
        if not path.is_file():
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
        servers = data.get("mcpServers", data)
        if not isinstance(servers, dict):
            return []
        return [
            McpServer(name=name, config=config)
            for name, config in servers.items()
            if isinstance(config, dict)
        ]
