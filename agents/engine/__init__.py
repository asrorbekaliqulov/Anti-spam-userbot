"""
The "engine" package contains everything that talks to Telegram via Pyrogram.

It is deliberately kept separate from the Django views/models so that the
asynchronous userbot logic never runs inside a synchronous web-request thread.
All Telegram work happens on a dedicated asyncio event loop (see `loop.py`).

IMPORTANT (Python 3.12 compatibility):
    Pyrogram calls ``asyncio.get_event_loop()`` at *import time*. On Python 3.12+
    that raises ``RuntimeError: There is no current event loop`` when the import
    happens in a worker thread without an event loop - which is exactly what a
    Django request thread is when the dashboard lazily imports the QR-login
    module. We therefore guarantee a loop exists for the current thread *before*
    any engine submodule (and thus Pyrogram) is imported.
"""
import asyncio as _asyncio

try:
    _asyncio.get_event_loop()
except RuntimeError:
    # No loop bound to this thread (e.g. a Django request thread on 3.12+).
    # Bind a fresh one so Pyrogram's import-time get_event_loop() succeeds.
    _asyncio.set_event_loop(_asyncio.new_event_loop())
