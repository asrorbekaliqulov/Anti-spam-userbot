"""
The "engine" package contains everything that talks to Telegram via Pyrogram.

It is deliberately kept separate from the Django views/models so that the
asynchronous userbot logic never runs inside a synchronous web-request thread.
All Telegram work happens on a dedicated asyncio event loop (see `loop.py`).
"""
