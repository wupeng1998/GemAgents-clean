from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from GemAgents.config import _uses_local_ollama, deepseek_api_key
from GemAgents.layout import RepoLayout, UserLayout


@dataclass(frozen=True)
class ModelProfile:
    name: str
    protocol: str
    model: str
    base_url: str | None = None
    api_key: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RuntimeSettings:
    raw: dict[str, Any] = field(default_factory=dict)
    workspace: Path | None = None

    @property
    def default_model(self) -> str | None:
        value = (
            self.raw.get("defaultModel")
            or self.raw.get("model")
            or os.getenv("GEMAGENTS_MODEL")
            or os.getenv("OLLAMA_MODEL")
            or (os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash") if deepseek_api_key() else None)
        )
        return str(value) if value else None

    @property
    def hooks(self) -> dict[str, Any]:
        if self.disable_all_hooks:
            return {}
        value = self.raw.get("hooks")
        return value if isinstance(value, dict) else {}

    @property
    def disable_all_hooks(self) -> bool:
        return bool(self.raw.get("disableAllHooks", False))

    @property
    def output_style(self) -> str | None:
        value = self.raw.get("outputStyle")
        return str(value) if value else None

    @property
    def model_names(self) -> list[str]:
        models = self.raw.get("models")
        if not isinstance(models, dict):
            return []
        return sorted(str(key) for key in models)

    def get_path(self, dotted_key: str) -> object:
        current: object = self.raw
        for part in dotted_key.split("."):
            if not isinstance(current, dict) or part not in current:
                raise KeyError(dotted_key)
            current = current[part]
        return current

    def set_path(self, dotted_key: str, value: object) -> None:
        current: dict[str, Any] = self.raw
        parts = dotted_key.split(".")
        for part in parts[:-1]:
            next_value = current.get(part)
            if not isinstance(next_value, dict):
                next_value = {}
                current[part] = next_value
            current = next_value
        current[parts[-1]] = value

    @property
    def env(self) -> dict[str, str]:
        value = self.raw.get("env")
        if not isinstance(value, dict):
            return {}
        return {str(key): str(_expand_env(item) or "") for key, item in value.items()}

    @property
    def additional_directories(self) -> list[Path]:
        value = self.raw.get("additionalDirectories")
        if not isinstance(value, list):
            return []
        directories: list[Path] = []
        for item in value:
            path = Path(str(item)).expanduser()
            if not path.is_absolute() and self.workspace is not None:
                path = self.workspace / path
            directories.append(path.resolve())
        return directories

    @property
    def respect_gitignore(self) -> bool:
        value = self.raw.get("respectGitignore")
        return True if value is None else bool(value)

    def model_profile(self, name: str) -> ModelProfile:
        models = self.raw.get("models")
        if isinstance(models, dict):
            profile = models.get(name)
            if isinstance(profile, dict):
                return ModelProfile(
                    name=name,
                    protocol=str(profile.get("protocol", "openai-chat")),
                    model=str(profile.get("model", name)),
                    base_url=_expand_env(profile.get("baseURL") or profile.get("base_url")),
                    api_key=_expand_env(profile.get("apiKey") or profile.get("api_key")),
                )

        if name.startswith("deepseek"):
            return ModelProfile(
                name=name,
                protocol="openai-chat",
                model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
                if name == "deepseek"
                else name,
                base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                api_key=deepseek_api_key(),
            )
        if name.startswith("gemini"):
            return ModelProfile(
                name=name,
                protocol="gemini",
                model=name,
                api_key=os.getenv("GEMINI_API_KEY"),
            )
        if name.startswith("claude"):
            return ModelProfile(
                name=name,
                protocol="anthropic",
                model=name,
                base_url=os.getenv("ANTHROPIC_BASE_URL"),
                api_key=os.getenv("ANTHROPIC_AUTH_TOKEN") or os.getenv("ANTHROPIC_API_KEY"),
            )
        if _uses_local_ollama(name):
            return ModelProfile(
                name=name,
                protocol="openai-chat",
                model=name,
                base_url=_ollama_base_url(),
                api_key=os.getenv("OLLAMA_API_KEY"),
            )

        return ModelProfile(
            name=name,
            protocol="openai-chat",
            model=name,
            base_url=os.getenv("OPENAI_BASE_URL"),
            api_key=os.getenv("OPENAI_API_KEY"),
        )


def load_settings(workspace: Path, explicit_path: Path | None = None) -> RuntimeSettings:
    merged: dict[str, Any] = {}
    paths = [
        UserLayout().settings,
        RepoLayout(workspace).resolve(".gemagents/settings.json"),
    ]
    if explicit_path is not None:
        explicit = Path(explicit_path).expanduser()
        if not explicit.is_absolute():
            explicit = workspace / explicit
        paths.append(explicit)

    for path in paths:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged = _deep_merge(merged, data)
    return RuntimeSettings(raw=merged, workspace=workspace.resolve())


def _deep_merge(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    result = dict(left)
    for key, value in right.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _expand_env(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.startswith("${") and text.endswith("}"):
        return os.getenv(text[2:-1])
    return text


def _ollama_base_url() -> str:
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    if base_url.endswith("/v1"):
        return base_url
    return f"{base_url}/v1"
