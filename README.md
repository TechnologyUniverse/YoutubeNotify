# 🚨 YouTube Alerts — Telegram YouTube Monitoring Bot

Production‑готовый Telegram‑бот для мониторинга YouTube‑каналов и автоматической публикации уведомлений.

Разработан исключительно для проекта **Technology Universe** с упором на стабильность, масштабируемость и безопасную эксплуатацию в production.

---

## 🚀 Текущий релиз

**Версия:** 2.0.0
**Статус:** Production Stable
**Ветка:** 2.0.x

Релиз переводит мониторинг каналов с Atom/RSS на официальный YouTube Data API v3.

---

## ✨ Возможности

### 📡 Мониторинг YouTube

* Поддержка нескольких каналов
* YouTube Data API v3
* Retry‑логика (3 попытки)
* Кэширование uploads playlist для экономии квоты
* Точные статусы трансляций через `liveStreamingDetails`

### 🔔 Типы событий

* Новое видео
* Запланированный стрим
* Начало прямого эфира
* Завершение стрима

### 🧠 Event Engine

* TTL‑антидубликация
* Baseline‑инициализация без спама
* Отслеживание жизненного цикла стрима
* Защита от повторной отправки событий
* Version‑aware state migration
* Ограничение количества событий за запуск

### 🛠 Команды

Публичные:

* `/start`
* `/status`
* `/version`
* `/health`

Административные (через `ADMIN_IDS`):

* `/checknow`
* `/metrics`

### 🔄 Режимы работы

Test / Production режимы:

* **prod** → Webhook
* **test** → Polling

Автоматическая регистрация webhook в production.

### 📊 Наблюдаемость

* Structured JSON‑логирование
* Метрики выполнения
* Отслеживание времени запуска
* Health endpoint `/health`
* Docker HEALTHCHECK

### 🐳 Инфраструктура

* Multi‑stage Docker
* Non‑root execution
* `tini` для корректного shutdown
* Graceful SIGTERM обработка
* Полная совместимость с Railway

---

## 🧠 Архитектура

Python 3.11
python‑telegram‑bot 20+
Webhook / Polling dual runtime
Crash‑safe файловая система состояния
Конфигурация через переменные окружения

Структура проекта:

```
youtubenotify/
 ├── engine.py
 ├── youtube_api.py
 ├── state.py
 ├── commands.py
 ├── bootstrap.py
 └── config.py

main.py
```

---

## 🛠 Changelog (v2.0.0)

### Добавлено

* Клиент YouTube Data API v3
* Определение новых видео и стадий трансляции через API
* Обязательная переменная `YOUTUBE_API_KEY`
* Отдельная версия схемы состояния

### Улучшено

* Baseline учитывает текущую стадию трансляции
* Health и metrics отражают ошибки YouTube API
* Удалены RSS-зависимости

### Стабильность

* Защита от дубликатов
* Отсутствие спама при первом запуске
* Корректная работа webhook в Railway
* Независимое версионирование приложения и схемы состояния

---

## 🔐 Безопасность и изоляция

* Изоляция через environment variables
* Admin‑ограничение чувствительных команд
* Non‑root Docker контейнер
* Отсутствие реакции в неразрешённых сценариях
* Без хранения токенов в коде

---

## 📦 Деплой

* Деплой через Railway
* Docker‑контейнеризация
* Все настройки через Environment Variables
* Test и Production полностью изолированы

Обязательные переменные:

```env
TELEGRAM_TOKEN=...
YOUTUBE_API_KEY=...
YOUTUBE_CHANNEL_IDS=UC...,UC...
TG_CHANNELS=UC...:-100...,UC...:-100...
WEBHOOK_URL=https://example.com
```

### Настройка YouTube API

1. Создайте или выберите проект в Google Cloud Console.
2. Включите **YouTube Data API v3**.
3. Создайте API key в разделе **APIs & Services → Credentials**.
4. Ограничьте ключ только YouTube Data API v3.
5. Добавьте ключ в Railway как `YOUTUBE_API_KEY` и перезапустите сервис.

OAuth не требуется: бот читает только публичные данные каналов. Ключ нельзя добавлять в репозиторий или выводить в логи.

### Переход с 1.5.x

Версия 2.0.0 содержит несовместимое изменение конфигурации: без `YOUTUBE_API_KEY` приложение не запустится. Существующий state-файл удалять не нужно — при первом запуске его схема будет обновлена до версии 2 с сохранением истории отправленных событий.

RSS-зависимости больше не используются. После деплоя проверьте `/health`, затем выполните `/checknow` от имени администратора.

При интервале проверки 30 минут один канал использует примерно 96 единиц квоты в сутки после первоначального получения uploads playlist. Фактическое потребление зависит от количества каналов и запусков `/checknow`.

---

## 📄 Лицензия

Проект является проприетарным программным обеспечением.

YouTube Alerts разработан исключительно для проекта **Technology Universe**.

Использование в сторонних проектах, Telegram‑каналах, группах или сервисах строго запрещено без письменного разрешения правообладателя.

Все права защищены.

---

*YouTube Alerts — internal monitoring infrastructure of Technology Universe.*

## Version 2.0.1 — production delivery

The VPS runtime supports `BOT_MODE=polling` or `webhook`. `STATE_FILE=/data/state.prod.json` must be persistent. Schema 2 is migrated to schema 3 without replaying historical notifications.

Events are stored in `outbox` before sending. Confirmed sends record `message_id` in `history` and update processed flags. Rate limits defer queued events; ambiguous network failures become `uncertain`, definite rejections become `failed`. Interrupted `sending` jobs become `uncertain` on restart. Inspect these jobs against Telegram history before manual recovery with the bot stopped. Never reset state to force a retry.

`/status` and `/health` show monitoring freshness and queue counts. Loopback `/health` returns 503 when monitoring has never succeeded, the last successful check is older than 65 minutes, or the latest monitoring run failed. A healthy process alone does not prove successful YouTube monitoring. Monitoring remains every 30 minutes.

Run tests with fixture environment: `PYTHONPATH=. python -m unittest discover -s tests`.
