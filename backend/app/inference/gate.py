import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

import portalocker

T = TypeVar("T")


class InferenceGateBusy(RuntimeError):
    pass


class InferenceGate:
    """One inference call at a time in this process and across cooperating processes."""

    def __init__(self, lock_path: str):
        self._async_lock = asyncio.Lock()
        self.lock_path = str(Path(lock_path).resolve())
        Path(self.lock_path).parent.mkdir(parents=True, exist_ok=True)

    async def run(self, operation: Callable[[], Awaitable[T]]) -> T:
        async with self._async_lock:
            try:
                # timeout=0 means a second process fails closed rather than waiting
                # while it may have another in-flight request.
                with portalocker.Lock(self.lock_path, mode="a", timeout=0):
                    return await operation()
            except portalocker.exceptions.LockException as exc:
                raise InferenceGateBusy(
                    "Another process owns the global inference gate; request was not sent."
                ) from exc
