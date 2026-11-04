"""会话选模上下文：各任务继承自己的选择，互不影响。"""
from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

Selector = str | Callable[[], str]
_selection: ContextVar[Selector] = ContextVar("spiritscout_session_model", default="")
_entries: ContextVar[Callable[[], list] | None] = ContextVar("spiritscout_session_models", default=None)


def model_entries_source() -> Callable[[], list] | None:
    return _entries.get()


def selection() -> Selector:
    return _selection.get()


def current_model() -> str:
    value = selection()
    return str(value() if callable(value) else value).strip()


@contextmanager
def bind_session_model(selector: Selector, entries: Callable[[], list] | None = None) -> Iterator[None]:
    token = _selection.set(selector)
    entries_token = _entries.set(entries)
    try:
        yield
    finally:
        _entries.reset(entries_token)
        _selection.reset(token)
