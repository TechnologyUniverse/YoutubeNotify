import logging
import time

from youtubenotify.bootstrap import create_app
from youtubenotify import config

logger = logging.getLogger(__name__)


def main():
    # mark application start time for metrics
    config.APP_START_TIME = time.time()

    app = create_app()

    logger.info("Starting bot mode=%s", config.BOT_MODE)
    if config.BOT_MODE == "polling":
        app.run_polling(drop_pending_updates=False)
    else:
        app.run_webhook(listen="0.0.0.0", port=config.PORT,
                        webhook_url=config.WEBHOOK_URL,
                        drop_pending_updates=False)


if __name__ == "__main__":
    main()