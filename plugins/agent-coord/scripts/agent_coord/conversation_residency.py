"""Bound the provider resources that idle loaded conversations hold.

A loaded Claude Code conversation keeps a CLI process and its MCP servers; a
loaded Codex conversation keeps its MCP servers inside the shared app-server.
Nothing released them, so a long-running runtime accumulated processes: build
17 grew from 136 processes and 1.86 GB RSS to 161 and 2.89 GB in 74 minutes.

Busy conversations always stay loaded. Of the idle ones, the most recently
used stay warm so that reopening and sending remain immediate; older ones are
released once they have been idle for a while. Their durable state (history,
queue and wake pauses, settings) is unaffected, and they reload on the next
open or send.
"""
from __future__ import annotations

from dataclasses import dataclass

WARM_IDLE_LIMIT = 6
MINIMUM_IDLE_SECONDS = 600.0


@dataclass(frozen=True)
class Resident:
    thread_id: str
    last_used: float
    busy: bool


def releasable(residents, now: float, *, warm_limit: int = WARM_IDLE_LIMIT,
               minimum_idle: float = MINIMUM_IDLE_SECONDS) -> list[str]:
    """Return idle conversations beyond the warm set that have been idle long enough."""
    idle = sorted((resident for resident in residents if not resident.busy),
                  key=lambda resident: (resident.last_used, resident.thread_id), reverse=True)
    return [resident.thread_id for resident in idle[max(0, warm_limit):]
            if now - resident.last_used >= minimum_idle]
