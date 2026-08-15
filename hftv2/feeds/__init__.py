from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import websockets

from hftv2.types import Quote

log = logging.getLogger("hftv2.feed")

Emit = Callable[[Quote], Awaitable[None]]


class Feed:
    name: str = "feed"
    url: str = ""

    def __init__(self, emit: Emit) -> None:
        self.emit = emit
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s session died: %s", self.name, exc)
            if self._stop.is_set():
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2.0, 30.0)

    async def _session(self) -> None:
        raise NotImplementedError

    def _connect(self, url: str | None = None, **kwargs: Any):
        headers = {"User-Agent": "hftv2-recorder/0.1"}
        opts = dict(
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=2**23,
            **kwargs,
        )
        try:
            return websockets.connect(url or self.url, additional_headers=headers, **opts)
        except TypeError:
            return websockets.connect(url or self.url, extra_headers=headers, **opts)


async def sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return


def now_ns() -> int:
    return time.time_ns()


def parse_ts_ms(value: Any, fallback_ns: int) -> int:
    if value is None:
        return fallback_ns // 1_000_000
    try:
        n = int(value)
    except (TypeError, ValueError):
        return fallback_ns // 1_000_000
    if n > 10**15:
        return n // 1_000_000
    if n > 10**12:
        return n // 1000
    return n
