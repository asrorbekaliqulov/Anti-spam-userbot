"""
A single, process-wide background asyncio event loop.

Why we need it
--------------
Django views run in synchronous worker threads. Pyrogram is fully asynchronous
and - crucially - a Pyrogram `Client` and its underlying MTProto session must
always be driven by the *same* event loop for their whole lifetime.

If we naively called `asyncio.run(coro)` inside each Django request we would
create (and destroy) a fresh loop every time, which breaks long-lived QR-login
clients that have to survive across several polling requests.

The solution is a dedicated daemon thread that owns one persistent event loop.
Synchronous Django code submits coroutines to it with `run_coro()` and gets the
result back through a `concurrent.futures.Future`.
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Coroutine


class BackgroundLoop:
    _instance: "BackgroundLoop | None" = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run, name="telegram-engine-loop", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    @classmethod
    def instance(cls) -> "BackgroundLoop":
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def run_coro(self, coro: Coroutine, timeout: float | None = None) -> Any:
        """Run a coroutine on the background loop and block for the result."""
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def submit(self, coro: Coroutine):
        """Fire-and-forget: schedule a coroutine without blocking."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)


def get_loop() -> BackgroundLoop:
    return BackgroundLoop.instance()
