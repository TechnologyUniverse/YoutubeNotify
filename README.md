# YouTube Notify Bot

Бот публикует уведомления о новых видео и прямых эфирах YouTube в закрытый Telegram-канал.

## Установка

1. Клонируйте репозиторий
   ```bash
   git clone https://github.com/USERNAME/repo.git
   cd repo
   ```
2. Создайте `.env` из шаблона:
   ```bash
   cp .env.example .env
   ```
3. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```
4. Запустите бота:
   ```bash
   python youtube_notify_bot.py
   ```

## Переменные окружения
В `.env` укажите:
- `TELEGRAM_TOKEN` — токен бота
- `YOUTUBE_API_KEY` — ключ YouTube Data API v3
- `YOUTUBE_CHANNEL_ID` — ID канала
- `TG_CHANNEL_ID` — ID вашего закрытого канала (например `-1001234567890`)

## Docker (опционально)
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "youtube_notify_bot.py"]
```

## CI / Tests
- Тесты в `tests/`
- GitHub Actions: `.github/workflows/ci.yml`