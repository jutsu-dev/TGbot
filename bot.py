import os
import asyncio
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiohttp import web
from dotenv import load_dotenv   # ✅ добавили

# загружаем переменные из файла .env
load_dotenv()

# читаем токен и настройки
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
MIN_WITHDRAW = os.getenv("MIN_WITHDRAW", "100")

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не найден! Проверь файл .env рядом с bot.py")


BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set (Render → Environment)")

router = Router()

@router.message(CommandStart())
async def start_cmd(msg: Message):
    await msg.answer("👋 Бот на Render запущен! /start ок")

async def run_bot():
    bot = Bot(BOT_TOKEN, parse_mode=None)
    dp = Dispatcher()
    dp.include_router(router)
    # TODO: сюда подключай свои роутеры из большого кода
    await dp.start_polling(bot)

async def start_http_server():
    async def ok(_):
        return web.Response(text="ok")
    app = web.Application()
    app.router.add_get("/", ok)
    app.router.add_get("/healthz", ok)
    port = int(os.getenv("PORT", "10000"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()

async def main():
    await asyncio.gather(
        start_http_server(),
        run_bot(),
    )

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
