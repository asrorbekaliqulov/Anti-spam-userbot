"""
Run the anti-spam engine: boot every active userbot and monitor their groups.

Usage:
    python manage.py run_agents

This is a long-running process (run it under systemd / supervisor / a separate
container from the web dashboard). The web dashboard and this worker share the
same database, so changes made in the dashboard (new groups, new bots) are
picked up the next time the worker is (re)started.
"""
import asyncio
import signal
import sys

from django.core.management.base import BaseCommand

from agents.engine.runner import AgentRunner


class Command(BaseCommand):
    help = "Start all active userbots and begin monitoring their groups."

    def add_arguments(self, parser):
        parser.add_argument(
            "--clear-cache",
            action="store_true",
            default=False,
            help="Clear __pycache__ directories before starting (fixes stale .pyc issues)",
        )

    def handle(self, *args, **options):
        if options["clear_cache"]:
            self._clear_pycache()

        self.stdout.write(self.style.SUCCESS("Starting anti-spam engine..."))
        try:
            asyncio.run(self._main())
        except KeyboardInterrupt:
            self.stdout.write(self.style.WARNING("Stopped by operator."))

    def _clear_pycache(self):
        """Remove all __pycache__ dirs to ensure fresh imports."""
        import shutil
        from pathlib import Path

        base = Path(__file__).resolve().parent.parent.parent.parent
        count = 0
        for cache_dir in base.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)
            count += 1
        self.stdout.write(
            self.style.WARNING(f"Cleared {count} __pycache__ directories.")
        )

    async def _main(self):
        runner = AgentRunner()
        await runner.start()

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, stop_event.set)
            except NotImplementedError:  # Windows
                pass

        self.stdout.write(self.style.SUCCESS("Engine running. Press Ctrl+C to stop."))
        await stop_event.wait()
        await runner.stop()
