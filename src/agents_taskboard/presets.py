"""Preset capture for new specs (S-51 Q4): the preset of the creating session, else the
project's default preset. Never raises: an orchestrator failure gives None."""

from __future__ import annotations

import logging

from agents_common.orchestrator import (
    NotFound,
    OrchestratorClient,
    OrchestratorError,
    OrchestratorUnavailable,
)

log = logging.getLogger(__name__)


class PresetLookup:
    def __init__(self, client: OrchestratorClient) -> None:
        self._client = client

    async def aclose(self) -> None:
        await self._client.aclose()

    async def of_session(self, session_id: str) -> str | None:
        """The preset id of a session; None when it has none, is unknown or the orchestrator
        cannot answer."""
        try:
            session = await self._client.get_session(session_id)
        except NotFound:
            return None
        except (OrchestratorError, OrchestratorUnavailable):
            log.warning("preset of session %s unavailable", session_id, exc_info=True)
            return None
        return None if session.preset_id is None else str(session.preset_id)

    async def project_default(self, project: str) -> str | None:
        """The id of the project's default preset; None when there is none or on error."""
        try:
            presets = await self._client.list_presets()
        except (OrchestratorError, OrchestratorUnavailable):
            log.warning("presets unavailable for project %s", project, exc_info=True)
            return None
        for preset in presets:
            if preset.is_default and preset.project == project:
                return str(preset.id)
        return None

    async def for_new_spec(self, session_id: str | None, project: str) -> str | None:
        if session_id is not None:
            found = await self.of_session(session_id)
            if found is not None:
                return found
        return await self.project_default(project)
