"""Track background capability probes for one application lifespan."""
import asyncio
import logging
from collections.abc import Coroutine

log = logging.getLogger(__name__)


class CapabilityProbes:
    def __init__(self):
        self._tasks: set[asyncio.Task] = set()

    def start(self, probe: Coroutine, *, name: str):
        task = asyncio.create_task(probe, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._completed)

    def _completed(self, task: asyncio.Task):
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            log.error('%s failed', task.get_name(), exc_info=error)

    async def close(self):
        while self._tasks:
            tasks = tuple(self._tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
