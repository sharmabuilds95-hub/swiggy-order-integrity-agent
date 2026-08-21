"""Flatten a (possibly nested) ExceptionGroup to its leaf exceptions.

Shared by the day scripts. anyio raises failures from inside its task groups as
`BaseExceptionGroup`s, and `except*` preserves the group's *nesting* when it
splits — an error raised two task-groups deep (food_session -> http -> session)
comes back as a group-of-groups. Iterating `.exceptions` one level then prints
"unhandled errors in a TaskGroup" instead of the real message; recursing to the
leaves fixes that. (Day 1 originally iterated one level and happened to work
only because its error path surfaced shallow; this is the shared fix.)
"""

from __future__ import annotations

from typing import Iterator


def leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for sub in exc.exceptions:
            yield from leaves(sub)
    else:
        yield exc
