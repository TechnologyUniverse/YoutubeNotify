import os
import logging
import time
from telegram.ext import ApplicationBuilder
from .config import STATE_FILE, TELEGRAM_TOKEN
from .commands import register_commands
from .engine import Engine
from .state import StateManager

import json
import sys

class JsonFormatter(logging.Formatter):
    def format(self, record):
        log_record = {
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            log_record["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_record)

handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JsonFormatter())

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
root_logger.handlers = [handler]

# HTTPX logs Telegram Bot API URLs, which contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)

def create_app():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not configured")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    state_file = os.getenv("STATE_FILE", STATE_FILE)
    state_manager = StateManager(state_file)
    engine = Engine(state_manager)
    app.bot_data["engine"] = engine

    register_commands(app, engine)

    # Register scheduler job (30 min interval)
    if app.job_queue:
        app.job_queue.run_repeating(
            engine.run,
            interval=1800,
            first=10,
            name="check_updates",
        )
        # Check only known premieres near their planned start. Other event
        # discovery and delivery retain their existing 30-minute schedule.
        app.job_queue.run_repeating(
            engine.run_premieres, interval=30, first=20, name="check_premieres",
        )

    # Separate loopback HTTP listener works in polling and webhook modes.
    from aiohttp import web
    async def health(request):
        age = time.time() - engine.last_successful_check if engine.last_successful_check else None
        healthy = (app.running and bool(app.updater and app.updater.running)
                   and age is not None and age < 3900 and not engine.last_error)
        return web.json_response({"status": "ok" if healthy else "degraded",
                                  "version": "2.0.2", "last_successful_check": engine.last_successful_check,
                                  "monitor_age_seconds": int(age) if age is not None else None,
                                  "queue": engine.queue_counts, "last_error": engine.last_error,
                                  "safe_mode": engine.safe_mode_active}, status=200 if healthy else 503)

    async def post_init(application):
        web_app = web.Application()
        web_app.router.add_get("/health", health)
        runner = web.AppRunner(web_app, access_log=None)
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", 8081).start()
        application.bot_data["health_runner"] = runner

    async def post_shutdown(application):
        runner = application.bot_data.get("health_runner")
        if runner:
            await runner.cleanup()

    async def on_error(update, context):
        logger = logging.getLogger(__name__)
        logger.warning("telegram_update_error type=%s", type(context.error).__name__)

    app.add_error_handler(on_error)
    app.post_init = post_init
    app.post_shutdown = post_shutdown
    return app
