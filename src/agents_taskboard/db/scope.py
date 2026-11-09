"""The project scope filter shared by every query path (projects plan 3.6)."""

from __future__ import annotations

from agents_taskboard.api.actors import Actor


def scoped(actor: Actor, column: str = "t.project") -> tuple[str, list]:
    """SQL to append to a WHERE clause and its parameters: nothing for an actor that sees every
    project, else `AND <column> = ANY(%s)` over the actor's projects."""
    if actor.projects is None:
        return "", []
    return f" AND {column} = ANY(%s)", [sorted(actor.projects)]
