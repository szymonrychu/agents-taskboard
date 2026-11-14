"""Per-project autostart limits (S-51). No defaults: an absent row or a NULL value means the
project's autostart is off."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

import psycopg
from agents_common.db.pool import notify

from agents_taskboard.db.spec_starts import START_CHANNEL


@dataclass(frozen=True)
class AutostartSettings:
    project: str
    max_active_sessions: int | None
    max_usage_pct: int | None
    updated_at: datetime | None

    @property
    def configured(self) -> bool:
        return self.max_active_sessions is not None and self.max_usage_pct is not None


_COLUMNS = "project, max_active_sessions, max_usage_pct, updated_at"


async def settings_for(
    conn: psycopg.AsyncConnection, projects: list[str]
) -> dict[str, AutostartSettings]:
    """The settings of each named project; a project without a row gets unset values."""
    cur = await conn.execute(
        f"SELECT {_COLUMNS} FROM project_settings WHERE project = ANY(%s)", (projects,)
    )
    found = {row[0]: AutostartSettings(*row) for row in await cur.fetchall()}
    return {p: found.get(p) or AutostartSettings(p, None, None, None) for p in projects}


async def get_settings(conn: psycopg.AsyncConnection, project: str) -> AutostartSettings:
    return (await settings_for(conn, [project]))[project]


async def put_settings(
    conn: psycopg.AsyncConnection,
    project: str,
    max_active_sessions: int | None,
    max_usage_pct: int | None,
    updated_by: str | None = None,
) -> AutostartSettings:
    """Upsert the project's limits and wake the notifier (payload: the project)."""
    cur = await conn.execute(
        "INSERT INTO project_settings (project, max_active_sessions, max_usage_pct, updated_by)"
        " VALUES (%s, %s, %s, %s)"
        " ON CONFLICT (project) DO UPDATE SET max_active_sessions = EXCLUDED.max_active_sessions,"
        " max_usage_pct = EXCLUDED.max_usage_pct, updated_by = EXCLUDED.updated_by,"
        " updated_at = now()"
        f" RETURNING {_COLUMNS}",
        (project, max_active_sessions, max_usage_pct, updated_by),
    )
    await notify(conn, START_CHANNEL, project)
    return AutostartSettings(*(await cur.fetchone()))
