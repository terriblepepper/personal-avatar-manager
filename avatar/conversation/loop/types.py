"""Contracts for a native ReAct turn and its authorized tools."""
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel


Disclosure = Literal['core', 'state', 'deferred']


@dataclass(frozen=True)
class LoopPolicy:
    require_terminal_tool: bool = False
    require_html: bool = False
    max_steps: int = 20
    max_consecutive_errors: int = 4
    max_duplicate_calls: int = 3
    max_continuations: int = 4
    finish_corrections: int = 2


@dataclass(frozen=True)
class ToolSpec:
    schema: dict
    execute: Callable[[Any], Awaitable[Any]]
    parameters: type[BaseModel] | None = None
    control: bool = False
    checkpoint: bool = False
    disclosure: Disclosure = 'core'
    summary: str = ''
    when: Callable[[], bool] | None = None


@dataclass(frozen=True)
class ToolOutcome:
    value: str | dict
    is_error: bool = False
    action: dict | None = None
    activated: tuple[str, ...] = ()

    @classmethod
    def error(cls, message, *, activated=()):
        return cls({'error': message}, True, activated=activated)
