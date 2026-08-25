"""
Telegram-бот для автоматического одобрения заявок на вступление в канал.

Использует aiogram 3.x и long polling.
Получает ChatJoinRequest — автоматически одобряет и отправляет
приветственное сообщение в личные сообщения пользователя.
"""

import asyncio
import io
import logging
import os
import sys

from aiogram import Bot, Dispatcher, Router
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)
from aiogram.filters import Command
from aiogram.types import (
    ChatJoinRequest,
    ChatMemberUpdated,
    Message,
)
from dotenv import load_dotenv

# ===========================================================================
# 1. ЛОГИРОВАНИЕ — настраиваем ДО любых проверок
# ===========================================================================

_stream = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=_stream,
)
logger = logging.getLogger(__name__)

# ===========================================================================
# 2. ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ — загружаем из .env и проверяем
# ===========================================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

if not BOT_TOKEN:
    logger.critical(
        "BOT_TOKEN не задан. Создайте файл .env и укажите BOT_TOKEN=ваш_токен."
    )
    sys.exit(1)

if not ADMIN_ID:
    logger.warning(
        "ADMIN_ID не задан. Админ-команды (/status, /chat_id, /approve_pending) "
        "будут недоступны."
    )
else:
    ADMIN_ID = int(ADMIN_ID)

if not CHANNEL_ID:
    logger.critical(
        "CHANNEL_ID не задан. Укажите ID канала: CHANNEL_ID=-100XXXXXXXXXX."
    )
    sys.exit(1)

CHANNEL_ID = int(CHANNEL_ID)

# ===========================================================================
# 3. ИНИЦИАЛИЗАЦИЯ БОТА, ДИСПЕТЧЕРА, РОУТЕРА
# ===========================================================================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
router = Router()

# Множество user_id уже обработанных заявок — защита от повторной обработки
_processed_requests = set()

# ===========================================================================
# 4. АДМИН-КОМАНДЫ
# ===========================================================================


@router.message(Command("status"))
async def cmd_status(message: Message) -> None:
    """Команда /status — показывает работоспособность бота (только админ)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return

    text = (
        f"Бот запущен и работает\n\n"
        f"ID канала: {CHANNEL_ID}\n"
        f"Режим: автоматическое одобрение заявок\n"
        f"Обработано за сессию: {len(_processed_requests)}"
    )
    await message.answer(text)
    logger.info("Команда /status от администратора %s", message.from_user.id)


@router.message(Command("chat_id"))
async def cmd_chat_id(message: Message) -> None:
    """Команда /chat_id — показывает ID текущего чата (только админ)."""
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return

    chat_id = message.chat.id
    chat_type = message.chat.type
    chat_title = (
        message.chat.title or message.chat.full_name or "Личные сообщения"
    )

    text = (
        f"Информация о чате:\n"
        f"ID чата: {chat_id}\n"
        f"Тип: {chat_type}\n"
        f"Название: {chat_title}"
    )
    await message.answer(text)
    logger.info(
        "Команда /chat_id — chat_id=%s, тип=%s", chat_id, chat_type,
    )


@router.message(Command("approve_pending"))
async def cmd_approve_pending(message: Message) -> None:
    """
    Команда /approve_pending — одобряет старые заявки по user_id.

    Telegram Bot API НЕ предоставляет метод для получения списка
    ожидающих заявок. Поэтому этот механизм работает только для
    конкретных user_id, переданных командой.

    Формат: /approve_pending user_id1 [user_id2 ...]
    Пример: /approve_pending 123456789
    """
    if not ADMIN_ID or message.from_user.id != ADMIN_ID:
        return

    parts = message.text.split()
    if len(parts) < 2:
        await message.answer(
            "Использование: /approve_pending user_id1 [user_id2 ...]\n"
            "Пример: /approve_pending 123456789\n\n"
            "Telegram Bot API не позволяет получить список заявок, "
            "поэтому нужно указать ID пользователей явно."
        )
        return

    approved = 0
    failed = 0

    for arg in parts[1:]:
        try:
            uid = int(arg)
        except ValueError:
            await message.answer(f"'{arg}' не является валидным ID.")
            continue

        try:
            await bot.approve_chat_join_request(
                chat_id=CHANNEL_ID,
                user_id=uid,
            )
            approved += 1
            _processed_requests.add(uid)
            logger.info(
                "Заявка ID:%s одобрена командой /approve_pending", uid,
            )

            try:
                await bot.send_message(
                    chat_id=uid,
                    text=(
                        "Привет! Рад видеть тебя в канале. "
                        "Твоя заявка одобрена!"
                    ),
                )
            except Exception as send_err:
                logger.warning(
                    "Не удалось отправить ЛС ID:%s: %s", uid, send_err,
                )

        except TelegramBadRequest as e:
            failed += 1
            logger.error(
                "TelegramBadRequest при одобрении ID:%s: %s", uid, e,
            )
        except TelegramForbiddenError as e:
            failed += 1
            logger.error("TelegramForbiddenError ID:%s: %s", uid, e)
        except Exception as e:
            failed += 1
            logger.error("Ошибка при одобрении ID:%s: %s", uid, e)

    await message.answer(
        f"Результат:\n  Одобрено: {approved}\n  Ошибок: {failed}"
    )


# ===========================================================================
# 5. ОБРАБОТЧИК ChatJoinRequest — ОСНОВНАЯ ЛОГИКА
# ===========================================================================


@router.chat_join_request()
async def handle_chat_join_request(event: ChatJoinRequest) -> None:
    """
    Обрабатывает каждую входящую заявку на вступление в канал.

    Алгоритм:
      1. Проверяем chat.id канала.
      2. Проверяем дубли.
      3. Одобряем заявку.
      4. Отправляем уведомление в ЛС.
      5. Ошибка отправки НЕ отменяет одобрение.
    """

    user_id = event.from_user.id
    username = event.from_user.username or "bez_nika"
    user_full_name = event.from_user.full_name or "Bez imeni"

    # Шаг 1: Проверка ID канала
    if event.chat.id != CHANNEL_ID:
        logger.warning(
            "Заявка в другой канал (chat_id=%s), ожидался %s. Пропуск.",
            event.chat.id, CHANNEL_ID,
        )
        return

    # Шаг 2: Защита от дублей
    if user_id in _processed_requests:
        logger.info("Заявка ID:%s уже обработана. Пропуск.", user_id)
        return

    logger.info(
        "Новая заявка: %s (ID: %s, @%s)",
        user_full_name, user_id, username,
    )

    # Шаг 3: Одобряем заявку
    try:
        await bot.approve_chat_join_request(
            chat_id=event.chat.id,
            user_id=user_id,
        )
        _processed_requests.add(user_id)
        logger.info(
            "Заявка %s (ID: %s) ОДОБРЕНА.",
            user_full_name, user_id,
        )
    except TelegramBadRequest as e:
        logger.error(
            "TelegramBadRequest при одобрении %s (ID: %s): %s",
            user_full_name, user_id, e,
        )
        return
    except TelegramForbiddenError as e:
        logger.error(
            "TelegramForbiddenError — бот не имеет прав. "
            "Проверьте can_invite_users: %s", e,
        )
        return
    except TelegramNetworkError as e:
        logger.error(
            "Сетевая ошибка %s (ID: %s): %s",
            user_full_name, user_id, e,
        )
        return
    except Exception as e:
        logger.error(
            "Непредвиденная ошибка %s (ID: %s): %s",
            user_full_name, user_id, e,
        )
        return

    # Шаг 4: Отправляем уведомление в ЛС
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "Привет! Рад видеть тебя в канале. "
                "Твоя заявка одобрена!"
            ),
        )
        logger.info(
            "Уведомление отправлено %s (ID: %s).",
            user_full_name, user_id,
        )
    except TelegramForbiddenError:
        logger.warning(
            "Не удалось отправить ЛС %s (ID: %s) — блокировка. "
            "Заявка уже одобрена.",
            user_full_name, user_id,
        )
    except TelegramBadRequest as e:
        logger.warning(
            "TelegramBadRequest ЛС %s (ID: %s): %s. Заявка уже одобрена.",
            user_full_name, user_id, e,
        )
    except TelegramNetworkError as e:
        logger.warning(
            "Сетевая ошибка ЛС %s (ID: %s): %s. Заявка уже одобрена.",
            user_full_name, user_id, e,
        )
    except Exception as e:
        logger.warning(
            "Непредвиденная ошибка ЛС %s (ID: %s): %s. Заявка уже одобрена.",
            user_full_name, user_id, e,
        )


# ===========================================================================
# 6. ЛОГИРОВАНИЕ ИЗМЕНЕНИЯ СТАТУСА БОТА В ЧАТЕ
# ===========================================================================


@router.my_chat_member()
async def handle_bot_chat_member(event: ChatMemberUpdated) -> None:
    """Логируем добавление/удаление бота из канала/группы."""
    chat = event.chat
    new_status = event.new_chat_member.status
    old_status = event.old_chat_member.status
    logger.info(
        "Статус бота в '%s' (ID: %s): %s -> %s",
        chat.title, chat.id, old_status, new_status,
    )


# ===========================================================================
# 7. ТОЧКА ВХОДА — ЗАПУСК БОТА
# ===========================================================================


async def main() -> None:
    """Главная функция: сброс webhook, проверка прав, запуск polling."""

    bot_info = await bot.get_me()
    logger.info("Бот: @%s (ID: %s)", bot_info.username, bot_info.id)
    logger.info("ID канала: %s", CHANNEL_ID)
    if ADMIN_ID:
        logger.info("ID администратора: %s", ADMIN_ID)

    # Проверяем права бота в канале при запуске
    try:
        bot_member = await bot.get_chat_member(
            chat_id=CHANNEL_ID,
            user_id=bot_info.id,
        )
        logger.info(
            "Права бота: статус=%s, can_invite_users=%s, can_manage_chat=%s",
            bot_member.status,
            getattr(bot_member, "can_invite_users", "N/A"),
            getattr(bot_member, "can_manage_chat", "N/A"),
        )
    except Exception as e:
        logger.error(
            "Не удалось проверить права бота в канале %s: %s",
            CHANNEL_ID, e,
        )

    # Сбрасываем вебхук и удаляем накопленные обновления
    await bot.delete_webhook(drop_pending_updates=True)

    # Подключаем роутер
    dp.include_router(router)

    # Запускаем long polling
    # ВАЖНО: allowed_updates должен содержать "chat_join_request",
    # иначе бот НЕ будет получать заявки на вступление!
    try:
        await dp.start_polling(
            bot,
            allowed_updates=["message", "chat_join_request"],
        )
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    finally:
        await bot.session.close()
        logger.info("Сессия закрыта.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception as e:
        logger.critical("Критическая ошибка: %s", e, exc_info=True)
        sys.exit(1)
