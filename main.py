import logging
import time

from youtubenotify.bootstrap import create_app
from youtubenotify import config

logger = logging.getLogger(__name__)


def main():
    # mark application start time for metrics
    config.APP_START_TIME = time.time()

    app = create_app()

    webhook_url = config.WEBHOOK_URL
    port = config.PORT
    bot_env = config.BOT_ENV
    polling_enabled = getattr(config, "POLLING", False)

    logger.info(f"Environment: {bot_env}")

    # --- PROD MODE (webhook) ---
    if bot_env == "prod":
        if not webhook_url:
            raise RuntimeError("WEBHOOK_URL is not configured in prod mode")

        logger.info(f"Starting bot (WEBHOOK) on port {port}")
        logger.info(f"Webhook URL: {webhook_url}")

        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            webhook_url=webhook_url,
        )
        return

    # --- TEST MODE (polling) ---
    logger.info("Starting bot in TEST mode (polling)")

    if polling_enabled:
        app.run_polling()
    else:
        logger.warning("POLLING is disabled in test mode — bot will not receive updates")
        app.run_polling()


if __name__ == "__main__":
    main()