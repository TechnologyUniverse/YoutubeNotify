import os
import logging
from telegram.ext import ApplicationBuilder
from .config import TELEGRAM_TOKEN
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

def create_app():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not configured")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    state_file = os.getenv("STATE_FILE", "state.local.json")
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

    async def health(request):
        from aiohttp import web  # type: ignore
        engine_ref = app.bot_data.get("engine")
        if not engine_ref:
            return web.json_response({"status": "error", "reason": "engine_missing"}, status=500)
        return web.json_response({
            "status": "ok",
            "last_event_sent": engine_ref.last_event_sent,
            "rss_failures": engine_ref.rss.consecutive_failures,
            "safe_mode": engine_ref.safe_mode_active,
        })

    async def metrics(request):
        from aiohttp import web  # type: ignore
        engine_ref = app.bot_data.get("engine")
        if not engine_ref:
            return web.json_response({"status": "error", "reason": "engine_missing"}, status=500)

        metrics_data = {
            "uptime_seconds": engine_ref.metrics.get("uptime_seconds", 0),
            "last_run_duration": engine_ref.metrics.get("last_run_duration", 0),
            "telegram_failures_total": engine_ref.metrics.get("telegram_failures_total", 0),
            "rss_failures_total": engine_ref.metrics.get("rss_failures_total", 0),
            "circuit_breaker_triggers": engine_ref.metrics.get("circuit_breaker_triggers", 0),
        }

        return web.json_response(metrics_data)

    async def post_init(application):
        web_app = getattr(application, "web_app", None)
        if web_app:
            web_app.router.add_get("/health", health)
            web_app.router.add_get("/metrics", metrics)

    app.post_init = post_init

    return app