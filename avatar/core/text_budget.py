"""Aggregate UTF-8 budget for agent control text and user messages."""
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated

from pydantic import AfterValidator, Field

TEXT_BYTES = 64 * 1024
TEXT_LIMIT = '自由文本按 UTF-8 合计，须在当前请求的文本预算内'
_active_bytes = ContextVar('agent_text_budget', default=TEXT_BYTES)


@contextmanager
def model_text_budget(input_tokens):
    # Chat HTTP bodies keep their transport bound; already decoded model controls use the window.
    token = _active_bytes.set(max(TEXT_BYTES, input_tokens * 3))
    try:
        yield
    finally:
        _active_bytes.reset(token)


def budget_error():
    return f'自由文本合计超过当前请求的 {_active_bytes.get()} 字节预算（UTF-8）'


def fits_text_budget(*parts: str) -> bool:
    remaining = _active_bytes.get()
    for part in parts:
        if len(part) > remaining:
            return False
        remaining -= len(part.encode('utf-8'))
        if remaining < 0:
            return False
    return True


def checked_text(value: str) -> str:
    if not fits_text_budget(value):
        raise ValueError(budget_error())
    return value


AgentText = Annotated[str, Field(description=TEXT_LIMIT), AfterValidator(checked_text)]
