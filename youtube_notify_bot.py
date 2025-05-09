import os
import sys

import json
import logging
import feedparser
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, JobQueue

# Настройки логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
# Подавляем подробные логи APScheduler до уровня WARNING
logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN     = os.getenv('TELEGRAM_TOKEN')
TG_CHANNEL_ID      = os.getenv('TG_CHANNEL_ID')
YOUTUBE_CHANNEL_ID = os.getenv('YOUTUBE_CHANNEL_ID')
STATE_FILE         = 'state.json'

# Проверка обязательных переменных окружения
for var in ('TELEGRAM_TOKEN', 'TG_CHANNEL_ID', 'YOUTUBE_CHANNEL_ID'):
    if not globals().get(var):
        logger.error(f"❌ {var} не задан в .env!")
        sys.exit(1)

# Работа с файлом состояния
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

# Получение RSS-фида канала
def fetch_feed():
    url = f'https://www.youtube.com/feeds/videos.xml?channel_id={YOUTUBE_CHANNEL_ID}'
    return feedparser.parse(url)

# Проверка новых видео
async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    feed = fetch_feed()
    state = load_state()

    if not feed.entries:
        logger.warning("RSS-фид пуст или недоступен")
        return

    entry = feed.entries[0]
    vid = entry.yt_videoid
    title = entry.title
    link = entry.link
    thumb = None
    if 'media_thumbnail' in entry and entry.media_thumbnail:
        thumb = entry.media_thumbnail[0]['url']
    description = entry.summary

    if vid != state.get('last_video'):
        caption = (
            f"📹 <b>Новое видео:</b> {title}\n"
            f"<i>{description}</i>\n"
            f"🔗 <a href=\"{link}\">Смотреть на YouTube</a>"
        )
        btn = InlineKeyboardButton("🚀 Смотреть", url=link)
        markup = InlineKeyboardMarkup([[btn]])

        if thumb:
            await context.bot.send_photo(
                chat_id=TG_CHANNEL_ID,
                photo=thumb,
                caption=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup
            )
        else:
            await context.bot.send_message(
                chat_id=TG_CHANNEL_ID,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=markup
            )

        state['last_video'] = vid
        save_state(state)

# Обработчик команды /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущен и отслеживает новые видео через RSS-ленту YouTube-канала."
    )

# Точка входа
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Инициализация JobQueue
    jq = app.job_queue or JobQueue()
    jq.set_application(app)
    jq.run_repeating(check_updates, interval=1800, first=10)

    app.add_handler(CommandHandler('start', start))

    logger.info("Бот успешно запущен")
    app.run_polling()

if __name__ == '__main__':
    main()

