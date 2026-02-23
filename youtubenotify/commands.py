import time
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes
from .config import VERSION
from .engine import Engine

import logging
from datetime import datetime
import asyncio
from . import config

logger = logging.getLogger(__name__)

UPTIME_START = int(time.time())


# Helper for formatting timestamps
def _fmt_ts(ts: int):
    if not ts:
        return "-"
    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return "-"


def register_commands(app, engine: Engine):
    from telegram.ext import CommandHandler

    app.bot_data["engine"] = engine

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("checknow", lambda u, c: checknow(u, c, engine)))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("metrics", metrics))
    app.add_handler(CommandHandler("health", health))
    app.add_handler(CommandHandler("version", version))


def _is_admin(update: Update) -> bool:
    if not update.effective_user:
        return False

    admin_ids = getattr(config, "ADMIN_IDS", [])

    if not admin_ids:
        return True

    return update.effective_user.id in admin_ids


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            f"🤖 Бот запущен (v{VERSION})"
        )


async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE, engine: Engine):
    message = update.effective_message
    if not message:
        return

    if not _is_admin(update):
        await message.reply_text("⛔ Not allowed")
        return

    await message.reply_text("🔄 Ручная проверка запущена")

    async def _run():
        try:
            await engine.run(context)
            await message.reply_text("✅ Проверка завершена")
        except Exception:
            logger.exception("checknow_failed")
            await message.reply_text("❌ Ошибка при ручной проверке")

    asyncio.create_task(_run())


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    engine = context.application.bot_data.get("engine")

    text = (
        "🩺 <b>Status</b>\n\n"
        f"Версия: <b>{VERSION}</b>\n"
        f"Engine loaded: <b>{'YES' if engine else 'NO'}</b>\n"
        "Статус: <b>OK</b>"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def metrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    if not _is_admin(update):
        await message.reply_text("⛔ Not allowed")
        return

    engine = context.application.bot_data.get("engine")
    if not engine:
        await message.reply_text("Engine not initialized")
        return

    avg_time = (
        round(engine.check_duration_total / engine.check_run_count, 3)
        if engine.check_run_count > 0 else 0
    )

    text = (
        "📊 <b>Metrics</b>\n\n"
        f"Events sent: <b>{engine.events_sent_total}</b>\n"
        f"RSS total failures: <b>{engine.rss.total_failures}</b>\n"
        f"RSS consecutive failures: <b>{engine.rss.consecutive_failures}</b>\n"
        f"Check runs: <b>{engine.check_run_count}</b>\n"
        f"Avg check time: <b>{avg_time}s</b>\n"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    engine = context.application.bot_data.get("engine")
    if not engine:
        await message.reply_text("Engine not initialized")
        return

    uptime = int(time.time()) - UPTIME_START

    text = (
        "🩺 <b>Health</b>\n\n"
        f"Uptime: <b>{uptime}s</b>\n"
        f"Last RSS success: <b>{_fmt_ts(engine.rss.last_success)}</b>\n"
        f"RSS consecutive failures: <b>{engine.rss.consecutive_failures}</b>\n"
        f"Last event sent: <b>{_fmt_ts(engine.last_event_sent)}</b>\n"
    )

    await message.reply_text(text, parse_mode=ParseMode.HTML)


async def version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return
    await message.reply_text(f"📦 Version: v{VERSION}")