"""Aggregate UTF-8 budget for agent control text and user messages."""
from typing import Annotated

from pydantic import AfterValidator, Field

TEXT_BYTES = 64 * 1024
TEXT_LIMIT = '自由文本合计最多 64 KiB（UTF-8）'


def fits_text_budget(*parts: str) -> bool:
    remaining = TEXT_BYTES
    for part in parts:
        if len(part) > remaining:
            return False
        remaining -= len(part.encode('utf-8'))
        if remaining < 0:
            return False
    return True


def checked_text(value: str) -> str:
    if not fits_text_budget(value):
        raise ValueError(TEXT_LIMIT)
    return value


AgentText = Annotated[str, Field(description=TEXT_LIMIT), AfterValidator(checked_text)]
