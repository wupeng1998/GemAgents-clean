from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from GemAgents.datetime_compat import UTC
from GemAgents.layout import RepoLayout

TodoStatus = Literal["pending", "in_progress", "completed"]


@dataclass
class TodoItem:
    content: str
    status: TodoStatus


@dataclass
class TaskItem:
    id: str
    description: str
    status: TodoStatus = "pending"
    result: str | None = None
    created_at: str = ""
    updated_at: str = ""


@dataclass
class RuntimeState:
    todos: list[TodoItem] = field(default_factory=list)
    tasks: dict[str, TaskItem] = field(default_factory=dict)
    memories: list[str] = field(default_factory=list)
    memory_path: Path | None = None
    task_path: Path | None = None

    @classmethod
    def for_workspace(cls, workspace: Path) -> RuntimeState:
        runtime = RepoLayout(workspace).runtime
        memory_path = runtime / "memory.md"
        task_path = runtime / "tasks.json"
        memories = []
        if memory_path.is_file():
            memories = [
                line.removeprefix("- ").strip()
                for line in memory_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        tasks = _load_tasks(task_path)
        return cls(tasks=tasks, memories=memories, memory_path=memory_path, task_path=task_path)

    def set_todos(self, items: list[dict[str, object]]) -> str:
        self.todos = [
            TodoItem(content=str(item["content"]), status=_status(item.get("status", "pending")))
            for item in items
        ]
        return f"stored {len(self.todos)} todo(s)"

    def create_task(self, task_id: str, description: str) -> str:
        now = _now()
        self.tasks[task_id] = TaskItem(
            id=task_id,
            description=description,
            created_at=now,
            updated_at=now,
        )
        self._save_tasks()
        return f"created task {task_id}"

    def update_task(self, task_id: str, status: str, result: str | None = None) -> str:
        if task_id not in self.tasks:
            raise KeyError(f"unknown task: {task_id}")
        task = self.tasks[task_id]
        task.status = _status(status)
        task.result = result
        task.updated_at = _now()
        self._save_tasks()
        return f"updated task {task_id}"

    def list_tasks(self) -> str:
        if not self.tasks:
            return ""
        return "\n".join(
            f"{task.id}\t{task.status}\t{task.description}" for task in self.tasks.values()
        )

    def get_task(self, task_id: str) -> TaskItem:
        if task_id not in self.tasks:
            raise KeyError(f"unknown task: {task_id}")
        return self.tasks[task_id]

    def clear_tasks(self) -> str:
        self.tasks.clear()
        self._save_tasks()
        return "tasks reset"

    def write_memory(self, content: str) -> str:
        self.memories.append(content)
        if self.memory_path is not None:
            self.memory_path.parent.mkdir(parents=True, exist_ok=True)
            self.memory_path.write_text(
                "".join(f"- {item}\n" for item in self.memories),
                encoding="utf-8",
            )
        return f"stored memory {len(self.memories)}"

    def _save_tasks(self) -> None:
        if self.task_path is None:
            return
        self.task_path.parent.mkdir(parents=True, exist_ok=True)
        payload = [
            {
                "id": task.id,
                "description": task.description,
                "status": task.status,
                "result": task.result,
                "created_at": task.created_at,
                "updated_at": task.updated_at,
            }
            for task in self.tasks.values()
        ]
        self.task_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _status(value: object) -> TodoStatus:
    if value in {"pending", "in_progress", "completed"}:
        return value  # type: ignore[return-value]
    raise ValueError(f"invalid status: {value}")


def _load_tasks(path: Path) -> dict[str, TaskItem]:
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    tasks: dict[str, TaskItem] = {}
    for item in payload:
        task = TaskItem(
            id=str(item["id"]),
            description=str(item["description"]),
            status=_status(item.get("status", "pending")),
            result=None if item.get("result") is None else str(item["result"]),
            created_at=str(item.get("created_at", "")),
            updated_at=str(item.get("updated_at", "")),
        )
        tasks[task.id] = task
    return tasks


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
