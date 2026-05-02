from aiogram import Router

from app.bot.handlers.commands import commands_router
from app.bot.handlers.tasks import tasks_router


main_router = Router(name="main")

# Порядок важен: команды регистрируются раньше catch-all текстового хендлера
main_router.include_router(commands_router)
main_router.include_router(tasks_router)
