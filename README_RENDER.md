# Render Deploy — TG Bot Starter

Минимальный каркас для деплоя Telegram-бота на Render Free (Web Service).

## Шаги деплоя
1) Создай репозиторий на GitHub и залей эти файлы.
2) Render → New → Web Service → выбери репозиторий.
3) Region: Europe (Frankfurt) → Instance: Free.
4) Environment Vars:
   - BOT_TOKEN = токен из @BotFather
   - (опц.) OWNER_ID, MIN_WITHDRAW — если используешь в своём коде
5) Start Command: `python bot.py`
6) Deploy → в логах появится запуск aiogram и HTTP-сервера.
7) Напиши боту `/start` → должен ответить.

## Подключение твоего большого кода
В `bot.py` есть комментарий:
```
# TODO: сюда подключай свои роутеры из большого кода
```
Импортируй и подключи свои роутеры/хэндлеры, оставив HTTP-сервер и main() как есть.
