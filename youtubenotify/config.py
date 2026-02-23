import os
from dotenv import load_dotenv

load_dotenv()

VERSION = "1.5.1.10000"
STATE_VERSION = "1.5.1.10000"

APP_START_TIME = None  # will be set in main at runtime

SILENT_MODE = os.getenv("SILENT_MODE", "false").lower() == "true"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
BASELINE_MODE = os.getenv("BASELINE_MODE", "true").lower() == "true"

EVENT_TTL = int(os.getenv("EVENT_TTL", "86400"))
MAX_EVENTS_PER_RUN = int(os.getenv("MAX_EVENTS_PER_RUN", "10"))
MAX_FEED_ITEMS = int(os.getenv("MAX_FEED_ITEMS", "20"))

BOT_ENV = os.getenv("BOT_ENV", "prod").lower()
STATE_FILE = f"state.{BOT_ENV}.json"

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
PORT = int(os.getenv("PORT", "8080"))

YOUTUBE_CHANNEL_IDS = [
    cid.strip()
    for cid in os.getenv("YOUTUBE_CHANNEL_IDS", "").split(",")
    if cid.strip()
]

TG_CHANNELS = {
    pair.split(":")[0]: pair.split(":")[1]
    for pair in os.getenv("TG_CHANNELS", "").split(",")
    if ":" in pair
}

# --- NEW CONFIG VARIABLES ---
ADMIN_IDS = [
    int(uid.strip())
    for uid in os.getenv("ADMIN_IDS", "").split(",")
    if uid.strip().isdigit()
]

JSON_LOGS = os.getenv("JSON_LOGS", "false").lower() == "true"
METRICS_ENABLED = os.getenv("METRICS_ENABLED", "true").lower() == "true"
HEALTH_ENDPOINT_ENABLED = os.getenv("HEALTH_ENDPOINT_ENABLED", "true").lower() == "true"

# --- CONFIG VALIDATION ---

def validate_config():
    errors = []

    if not TELEGRAM_TOKEN:
        errors.append("TELEGRAM_TOKEN is missing")

    if BOT_ENV == "prod" and not WEBHOOK_URL:
        errors.append("WEBHOOK_URL required in prod")

    if not YOUTUBE_CHANNEL_IDS:
        errors.append("YOUTUBE_CHANNEL_IDS empty")

    if not TG_CHANNELS:
        errors.append("TG_CHANNELS empty")

    if METRICS_ENABLED and not ADMIN_IDS:
        # Not critical, but warn via config validation
        print("WARNING: METRICS_ENABLED is true but ADMIN_IDS not set")

    for cid in YOUTUBE_CHANNEL_IDS:
        if cid not in TG_CHANNELS:
            errors.append(f"Missing TG_CHANNELS mapping for {cid}")

    if errors:
        raise RuntimeError("CONFIG VALIDATION FAILED:\n" + "\n".join(errors))

# run validation on import
validate_config()