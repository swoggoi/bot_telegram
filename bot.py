"""
Telegram-бот для продажи цифровых видео через Telegram Stars.
Реферальная система со скидками, админ-панель для управления товарами.
"""

import os
import sys
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

import aiosqlite
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode, ChatType
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    CallbackQuery,
    LabeledPrice,
    PreCheckoutQuery,
    SuccessfulPayment,
    InputMediaVideo,
)
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramNetworkError,
)

# ---------------------------------------------------------------------------
# Конфигурация
# ---------------------------------------------------------------------------

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
ADMIN_ID: int = int(os.getenv("ADMIN_ID", "0"))
DATABASE_NAME: str = "bot_database.db"

if not BOT_TOKEN:
    logging.critical("BOT_TOKEN не найден! Установите переменную окружения BOT_TOKEN.")
    sys.exit(1)

if ADMIN_ID == 0:
    logging.critical("ADMIN_ID не найден! Установите переменную окружения ADMIN_ID.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Инициализация бота и диспетчера
# ---------------------------------------------------------------------------

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# ---------------------------------------------------------------------------
# FSM-состояния для админ-панели
# ---------------------------------------------------------------------------


class AdminAddVideo(StatesGroup):
    """Состояния загрузки нового видео."""

    waiting_title = State()
    waiting_description = State()
    waiting_price = State()
    waiting_video = State()


class AdminChangePrice(StatesGroup):
    """Состояния изменения цены товара."""

    waiting_product_id = State()
    waiting_new_price = State()


class AdminDeleteProduct(StatesGroup):
    """Состояния удаления товара."""

    waiting_product_id = State()
    waiting_confirm = State()


# ---------------------------------------------------------------------------
# Работа с базой данных
# ---------------------------------------------------------------------------


async def init_db() -> None:
    """Создание таблиц базы данных при первом запуске."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute("PRAGMA journal_mode=WAL;")
        await db.execute("PRAGMA foreign_keys=ON;")

        # Таблица пользователей
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                referrer_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id)
            )
            """
        )

        # Таблица товаров
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS products (
                product_id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                telegram_file_id TEXT NOT NULL,
                telegram_file_unique_id TEXT NOT NULL,
                price_stars INTEGER NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Таблица рефералов
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                referral_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                first_successful_order_id INTEGER,
                created_at TEXT NOT NULL,
                activated_at TEXT,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_user_id) REFERENCES users(user_id),
                UNIQUE(referrer_id, referred_user_id)
            )
            """
        )

        # Таблица заказов
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                base_price INTEGER NOT NULL,
                discount_percent INTEGER NOT NULL DEFAULT 0,
                final_price INTEGER NOT NULL,
                payload TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                telegram_payment_charge_id TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT,
                refunded_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (product_id) REFERENCES products(product_id)
            )
            """
        )

        # Таблица покупок
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS purchases (
                purchase_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'delivered',
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (product_id) REFERENCES products(product_id),
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
            """
        )

        # Таблица вознаграждений (реферальные комиссии)
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
                reward_id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'accrued',
                created_at TEXT NOT NULL,
                reversed_at TEXT,
                FOREIGN KEY (referrer_id) REFERENCES users(user_id),
                FOREIGN KEY (referred_user_id) REFERENCES users(user_id),
                FOREIGN KEY (order_id) REFERENCES orders(order_id)
            )
            """
        )

        # Индексы
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_charge "
            "ON orders(telegram_payment_charge_id) WHERE telegram_payment_charge_id IS NOT NULL"
        )
        await db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_purchases_user_product "
            "ON purchases(user_id, product_id)"
        )

        await db.commit()
        logger.info("База данных инициализирована.")


async def ensure_user(user: types.User, referrer_id: Optional[int] = None) -> None:
    """Регистрация или обновление данных пользователя."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,))
        existing = await cursor.fetchone()

        if existing is None:
            # Определяем реферера
            effective_referrer = referrer_id
            if effective_referrer == user.id:
                effective_referrer = None  # самореферал

            await db.execute(
                """
                INSERT INTO users (user_id, username, first_name, referrer_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user.id, user.username, user.first_name, effective_referrer, now, now),
            )
            # Если есть реферер — создаём запись о реферале
            if effective_referrer:
                await db.execute(
                    """
                    INSERT OR IGNORE INTO referrals (referrer_id, referred_user_id, status, created_at)
                    VALUES (?, ?, 'pending', ?)
                    """,
                    (effective_referrer, user.id, now),
                )
        else:
            await db.execute(
                "UPDATE users SET username = ?, first_name = ?, updated_at = ? WHERE user_id = ?",
                (user.username, user.first_name, now, user.id),
            )

        await db.commit()


async def get_user(user_id: int) -> Optional[dict]:
    """Получить данные пользователя."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_active_products() -> list[dict]:
    """Получить все активные товары."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM products WHERE is_active = 1 ORDER BY created_at DESC"
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_product(product_id: int) -> Optional[dict]:
    """Получить товар по ID."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM products WHERE product_id = ?", (product_id,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def add_product(
    title: str,
    description: str,
    file_id: str,
    file_unique_id: str,
    price: int,
    created_by: int,
) -> int:
    """Добавить новый товар, вернуть ID."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            """
            INSERT INTO products (title, description, telegram_file_id, telegram_file_unique_id,
                                  price_stars, is_active, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (title, description, file_id, file_unique_id, price, created_by, now, now),
        )
        await db.commit()
        return cursor.lastrowid


async def update_product_price(product_id: int, new_price: int) -> bool:
    """Изменить цену товара."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            "UPDATE products SET price_stars = ?, updated_at = ? WHERE product_id = ? AND is_active = 1",
            (new_price, now, product_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def deactivate_product(product_id: int) -> bool:
    """Деактивировать товар (пометить как удалённый)."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            "UPDATE products SET is_active = 0, updated_at = ? WHERE product_id = ? AND is_active = 1",
            (now, product_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def is_already_purchased(user_id: int, product_id: int) -> bool:
    """Проверить, купил ли пользователь товар ранее."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            "SELECT 1 FROM purchases WHERE user_id = ? AND product_id = ?",
            (user_id, product_id),
        )
        return (await cursor.fetchone()) is not None


async def get_discount_percent(user_id: int) -> int:
    """Рассчитать процент скидки по числу активных рефералов."""
    active_count = await get_active_referral_count(user_id)
    if active_count >= 20:
        return 50
    elif active_count >= 10:
        return 30
    elif active_count >= 5:
        return 20
    elif active_count >= 2:
        return 10
    return 0


async def get_active_referral_count(user_id: int) -> int:
    """Число активных (платящих) рефералов пользователя."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'active'",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_next_discount_info(user_id: int) -> str:
    """Информация о ближайшем уровне скидки."""
    active = await get_active_referral_count(user_id)
    levels = [
        (2, 10, "10%"),
        (5, 20, "20%"),
        (10, 30, "30%"),
        (20, 50, "50%"),
    ]
    for threshold, pct, label in levels:
        if active < threshold:
            remaining = threshold - active
            return (
                f"Текущая скидка: {await get_discount_percent(user_id)}%\n"
                f"Следующий уровень: {label} (нужно ещё {remaining} реферал(ов))"
            )
    return "У вас максимальная скидка — 50%!"


async def create_order(
    user_id: int, product_id: int, base_price: int, discount_percent: int
) -> int:
    """Создать заказ, вернуть order_id."""
    final_price = base_price * (100 - discount_percent) // 100
    if final_price < 1:
        final_price = 1
    now = datetime.now(timezone.utc).isoformat()
    payload = f"order_{user_id}_{product_id}_{now}"
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            """
            INSERT INTO orders (user_id, product_id, base_price, discount_percent,
                                final_price, payload, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
            """,
            (user_id, product_id, base_price, discount_percent, final_price, payload, now),
        )
        await db.commit()
        return cursor.lastrowid


async def get_order(order_id: int) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM orders WHERE order_id = ?", (order_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_order_by_payload(payload: str) -> Optional[dict]:
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE payload = ?", (payload,)
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def complete_order(order_id: int, payment_charge_id: str) -> None:
    """Отметить заказ как оплаченный, создать запись покупки."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        # Обновляем заказ
        await db.execute(
            """
            UPDATE orders
            SET status = 'paid', telegram_payment_charge_id = ?, paid_at = ?
            WHERE order_id = ?
            """,
            (payment_charge_id, now, order_id),
        )
        # Получаем данные заказа для создания покупки
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order_id,)
        )
        order = await cursor.fetchone()

        if order:
            # Создаём запись покупки
            await db.execute(
                """
                INSERT OR IGNORE INTO purchases (user_id, product_id, order_id, created_at, delivery_status)
                VALUES (?, ?, ?, ?, 'delivered')
                """,
                (order["user_id"], order["product_id"], order_id, now),
            )

        await db.commit()


async def process_referral_reward(order_id: int, buyer_id: int) -> None:
    """Начислить рефереру комиссию за покупку реферала."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row

        # Получаем информацию о покупателе — кто его пригласил
        cursor = await db.execute(
            "SELECT referrer_id FROM referrals WHERE referred_user_id = ? AND status = 'active'",
            (buyer_id,),
        )
        referral = await cursor.fetchone()
        if not referral:
            return

        referrer_id = referral["referrer_id"]

        # Проверяем, не начисляли ли уже за этот заказ
        cursor = await db.execute(
            "SELECT 1 FROM rewards WHERE order_id = ? AND referrer_id = ?",
            (order_id, referrer_id),
        )
        if await cursor.fetchone():
            return

        # Получаем сумму заказа
        cursor = await db.execute(
            "SELECT final_price FROM orders WHERE order_id = ?", (order_id,)
        )
        order_row = await cursor.fetchone()
        if not order_row:
            return

        amount = order_row["final_price"]

        # Начисляем
        await db.execute(
            """
            INSERT INTO rewards (referrer_id, referred_user_id, order_id, amount, status, created_at)
            VALUES (?, ?, ?, ?, 'accrued', ?)
            """,
            (referrer_id, buyer_id, order_id, amount, now),
        )

        await db.commit()
        logger.info("Начислена комиссия %d звёзд рефереру %d за заказ %d", amount, referrer_id, order_id)


async def activate_referral(referred_user_id: int, order_id: int) -> None:
    """Активировать реферала после первой покупки."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute(
            """
            UPDATE referrals
            SET status = 'active', first_successful_order_id = ?, activated_at = ?
            WHERE referred_user_id = ? AND status = 'pending'
            """,
            (order_id, now, referred_user_id),
        )
        await db.commit()


async def refund_order(order_id: int) -> bool:
    """Отметить заказ как возврат."""
    now = datetime.now(timezone.utc).isoformat()
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            """
            UPDATE orders SET status = 'refunded', refunded_at = ?
            WHERE order_id = ? AND status = 'paid'
            """,
            (now, order_id),
        )
        # Отменяем начисление рефереру
        await db.execute(
            """
            UPDATE rewards SET status = 'reversed', reversed_at = ?
            WHERE order_id = ? AND status = 'accrued'
            """,
            (now, order_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_user_purchases(user_id: int) -> list[dict]:
    """Список купленных видео."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT p.*, pr.title, pr.description, pr.telegram_file_id, pr.price_stars
            FROM purchases p
            JOIN products pr ON p.product_id = pr.product_id
            WHERE p.user_id = ?
            ORDER BY p.created_at DESC
            """,
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_referrer_balance(referrer_id: int) -> int:
    """Баланс реферальных комиссий."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            """
            SELECT COALESCE(SUM(amount), 0) FROM rewards
            WHERE referrer_id = ? AND status = 'accrued'
            """,
            (referrer_id,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0


async def get_admin_stats() -> dict:
    """Общая статистика для администратора."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        stats = {}

        cursor = await db.execute("SELECT COUNT(*) FROM users")
        stats["total_users"] = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM products WHERE is_active = 1")
        stats["active_products"] = (await cursor.fetchone())[0]

        cursor = await db.execute("SELECT COUNT(*) FROM orders WHERE status = 'paid'")
        stats["total_orders"] = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COALESCE(SUM(final_price), 0) FROM orders WHERE status = 'paid'"
        )
        stats["total_revenue"] = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE status = 'active'"
        )
        stats["active_referrals"] = (await cursor.fetchone())[0]

        return stats


async def get_user_stats(user_id: int) -> dict:
    """Личная статистика пользователя."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        stats = {}

        cursor = await db.execute(
            "SELECT COUNT(*) FROM purchases WHERE user_id = ?", (user_id,)
        )
        stats["purchases"] = (await cursor.fetchone())[0]

        cursor = await db.execute(
            "SELECT COUNT(*) FROM referrals WHERE referrer_id = ? AND status = 'active'",
            (user_id,),
        )
        stats["active_referrals"] = (await cursor.fetchone())[0]

        stats["discount"] = await get_discount_percent(user_id)
        stats["balance"] = await get_referrer_balance(user_id)

        return stats


# ---------------------------------------------------------------------------
# Клавиатуры
# ---------------------------------------------------------------------------


def main_menu_keyboard() -> InlineKeyboardMarkup:
    """Главное меню пользователя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📹 Каталог видео", callback_data="catalog")],
            [InlineKeyboardButton(text="🎬 Мои видео", callback_data="my_videos")],
            [InlineKeyboardButton(text="🔗 Реферальная ссылка", callback_data="referrals")],
            [InlineKeyboardButton(text="🏷 Моя скидка", callback_data="discount")],
            [InlineKeyboardButton(text="📊 Моя статистика", callback_data="user_stats")],
        ]
    )


def admin_menu_keyboard() -> InlineKeyboardMarkup:
    """Админ-панель."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить видео", callback_data="admin_add")],
            [InlineKeyboardButton(text="📋 Список товаров", callback_data="admin_list")],
            [InlineKeyboardButton(text="💰 Изменить цену", callback_data="admin_price")],
            [InlineKeyboardButton(text="🗑 Удалить товар", callback_data="admin_delete")],
            [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_cancel")],
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_action")]
        ]
    )


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------


def is_admin(user_id: int) -> bool:
    """Проверка на администратора."""
    return user_id == ADMIN_ID


async def safe_send_message(chat_id: int, text: str, **kwargs) -> None:
    """Безопасная отправка сообщения с обработкой ошибок."""
    try:
        await bot.send_message(chat_id, text, **kwargs)
    except TelegramForbiddenError:
        logger.warning("Пользователь %d заблокировал бота.", chat_id)
    except TelegramBadRequest as e:
        logger.error("Ошибка отправки сообщения %d: %s", chat_id, e)
    except TelegramNetworkError as e:
        logger.error("Сетевая ошибка при отправке %d: %s", chat_id, e)
    except Exception as e:
        logger.error("Непредвиденная ошибка отправки %d: %s", chat_id, e)


async def safe_answer_callback(callback: CallbackQuery, text: str = "", show_alert: bool = False) -> None:
    """Безопасный ответ на callback."""
    try:
        await callback.answer(text=text, show_alert=show_alert)
    except TelegramBadRequest:
        pass
    except Exception as e:
        logger.error("Ошибка ответа callback: %s", e)


# ---------------------------------------------------------------------------
# Обработчики команд
# ---------------------------------------------------------------------------


@router.message(CommandStart())
async def cmd_start(message: Message, command: Command) -> None:
    """Обработка /start с поддержкой реферальных ссылок."""
    referrer_id = None
    if command.args and command.args.startswith("ref_"):
        try:
            referrer_id = int(command.args[4:])
        except ValueError:
            referrer_id = None

    await ensure_user(message.from_user, referrer_id)

    await safe_send_message(
        message.chat.id,
        (
            f"👋 Добро пожаловать, <b>{message.from_user.first_name}</b>!\n\n"
            "Я продаю качественные обучающие видео.\n\n"
            "Используйте меню ниже для навигации:"
        ),
        reply_markup=main_menu_keyboard(),
    )


@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    """Админ-панель."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        return

    await safe_send_message(
        message.chat.id,
        "🔧 <b>Панель администратора</b>",
        reply_markup=admin_menu_keyboard(),
    )


@router.message(Command("catalog"))
@router.message(Command("buy"))
async def cmd_catalog(message: Message) -> None:
    """Каталог видео."""
    await show_catalog(message.chat.id, message.from_user.id)


@router.message(Command("referrals"))
async def cmd_referrals(message: Message) -> None:
    """Реферальная ссылка и статистика."""
    await show_referrals(message.from_user.id)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Личная статистика."""
    await show_user_stats(message.from_user.id)


@router.message(Command("my_videos"))
async def cmd_my_videos(message: Message) -> None:
    """Список купленных видео."""
    await show_my_videos(message.chat.id, message.from_user.id)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    """Отмена текущего действия."""
    current = await state.get_state()
    if current is not None:
        await state.clear()
        await safe_send_message(message.chat.id, "✅ Действие отменено.", reply_markup=main_menu_keyboard())
    else:
        await safe_send_message(message.chat.id, "Нечего отменять.")


# ---------------------------------------------------------------------------
# Каталог
# ---------------------------------------------------------------------------


async def show_catalog(chat_id: int, user_id: int) -> None:
    """Показать каталог активных товаров."""
    products = await get_active_products()

    if not products:
        await safe_send_message(chat_id, "🎬 Каталог пока пуст.", reply_markup=main_menu_keyboard())
        return

    discount = await get_discount_percent(user_id)

    text_lines = ["📹 <b>Каталог видео</b>\n"]
    if discount > 0:
        text_lines.append(f"🏷 Ваша скидка: <b>{discount}%</b>\n")

    keyboard_rows = []
    for p in products:
        base = p["price_stars"]
        if discount > 0:
            final_price = base * (100 - discount) // 100
            if final_price < 1:
                final_price = 1
            price_text = f"⭐ {final_price} (было {base})"
        else:
            final_price = base
            price_text = f"⭐ {base}"

        already = await is_already_purchased(user_id, p["product_id"])
        status = " ✅ куплено" if already else ""

        text_lines.append(
            f"<b>{p['title']}</b> — {price_text}{status}\n"
            f"{p['description']}\n"
        )

        if not already:
            keyboard_rows.append(
                [InlineKeyboardButton(
                    text=f"💳 {p['title']} — {price_text}",
                    callback_data=f"buy_{p['product_id']}",
                )]
            )

    keyboard_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])

    await safe_send_message(
        chat_id,
        "\n".join(text_lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


# ---------------------------------------------------------------------------
# Мои видео
# ---------------------------------------------------------------------------


async def show_my_videos(chat_id: int, user_id: int) -> None:
    """Показать список купленных видео."""
    purchases = await get_user_purchases(user_id)

    if not purchases:
        await safe_send_message(
            chat_id,
            "🎬 У вас пока нет купленных видео.",
            reply_markup=main_menu_keyboard(),
        )
        return

    text = "🎬 <b>Мои видео</b>\n\n"
    keyboard_rows = []

    for pur in purchases:
        text += f"• <b>{pur['title']}</b>\n"
        keyboard_rows.append(
            [InlineKeyboardButton(
                text=f"▶ {pur['title']}",
                callback_data=f"watch_{pur['product_id']}",
            )]
        )

    keyboard_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_main")])

    await safe_send_message(
        chat_id,
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard_rows),
    )


# ---------------------------------------------------------------------------
# Реферальная система
# ---------------------------------------------------------------------------


async def show_referrals(user_id: int) -> None:
    """Показать реферальную ссылку и статистику."""
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start=ref_{user_id}"
    active_count = await get_active_referral_count(user_id)
    discount_info = await get_next_discount_info(user_id)
    balance = await get_referrer_balance(user_id)

    text = (
        f"🔗 <b>Реферальная программа</b>\n\n"
        f"Ваша ссылка:\n<code>{ref_link}</code>\n\n"
        f"Активных рефералов: <b>{active_count}</b>\n\n"
        f"{discount_info}\n\n"
        f"💰 Баланс реферальных комиссий: <b>{balance} ⭐</b>\n\n"
        f"<i>Внутренний баланс не является автоматическим переводом Stars. "
        f"Выплаты производятся вручную администратором.</i>"
    )

    await safe_send_message(user_id, text, reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Личная статистика
# ---------------------------------------------------------------------------


async def show_user_stats(user_id: int) -> None:
    """Показать личную статистику."""
    stats = await get_user_stats(user_id)
    discount_info = await get_next_discount_info(user_id)

    text = (
        f"📊 <b>Ваша статистика</b>\n\n"
        f"Куплено видео: <b>{stats['purchases']}</b>\n"
        f"Активных рефералов: <b>{stats['active_referrals']}</b>\n"
        f"Текущая скидка: <b>{stats['discount']}%</b>\n"
        f"Баланс комиссий: <b>{stats['balance']} ⭐</b>\n\n"
        f"{discount_info}"
    )

    await safe_send_message(user_id, text, reply_markup=main_menu_keyboard())


# ---------------------------------------------------------------------------
# Inline callback-обработчики (пользователи)
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "back_main")
async def cb_back_main(callback: CallbackQuery) -> None:
    """Возврат в главное меню."""
    await safe_answer_callback(callback)
    await safe_send_message(
        callback.message.chat.id,
        "🏠 <b>Главное меню</b>",
        reply_markup=main_menu_keyboard(),
    )


@router.callback_query(F.data == "catalog")
async def cb_catalog(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback)
    await show_catalog(callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "my_videos")
async def cb_my_videos(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback)
    await show_my_videos(callback.message.chat.id, callback.from_user.id)


@router.callback_query(F.data == "referrals")
async def cb_referrals(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback)
    await show_referrals(callback.from_user.id)


@router.callback_query(F.data == "discount")
async def cb_discount(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback)
    await show_referrals(callback.from_user.id)


@router.callback_query(F.data == "user_stats")
async def cb_user_stats(callback: CallbackQuery) -> None:
    await safe_answer_callback(callback)
    await show_user_stats(callback.from_user.id)


@router.callback_query(F.data.startswith("watch_"))
async def cb_watch(callback: CallbackQuery) -> None:
    """Отправить купленное видео."""
    try:
        product_id = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        await safe_answer_callback(callback, "❌ Ошибка данных.", show_alert=True)
        return

    user_id = callback.from_user.id

    # Проверяем покупку
    if not await is_already_purchased(user_id, product_id):
        await safe_answer_callback(callback, "❌ Вы не покупали это видео.", show_alert=True)
        return

    product = await get_product(product_id)
    if not product or not product["is_active"]:
        await safe_answer_callback(callback, "❌ Товар больше не доступен.", show_alert=True)
        return

    await safe_answer_callback(callback)

    try:
        await bot.send_video(
            chat_id=user_id,
            video=product["telegram_file_id"],
            caption=f"🎬 <b>{product['title']}</b>\n\n{product['description']}",
        )
    except TelegramBadRequest as e:
        logger.error("Ошибка отправки видео пользователю %d: %s", user_id, e)
        await safe_send_message(user_id, "❌ Не удалось отправить видео. Обратитесь к администратору.")
    except TelegramForbiddenError:
        logger.warning("Пользователь %d заблокировал бота.", user_id)
    except Exception as e:
        logger.error("Непредвиденная ошибка отправки видео %d: %s", user_id, e)
        await safe_send_message(user_id, "❌ Произошла ошибка при отправке видео.")


@router.callback_query(F.data.startswith("buy_"))
async def cb_buy(callback: CallbackQuery) -> None:
    """Начать процесс покупки — создать инвойс."""
    try:
        product_id = int(callback.data.split("_")[1])
    except (ValueError, IndexError):
        await safe_answer_callback(callback, "❌ Ошибка данных.", show_alert=True)
        return

    user_id = callback.from_user.id

    # Проверяем повторную покупку
    if await is_already_purchased(user_id, product_id):
        await safe_answer_callback(callback, "❌ Вы уже купили это видео.", show_alert=True)
        return

    product = await get_product(product_id)
    if not product or not product["is_active"]:
        await safe_answer_callback(callback, "❌ Товар недоступен.", show_alert=True)
        return

    # Скидка — берём из БД, НЕ из callback_data
    base_price = product["price_stars"]
    discount = await get_discount_percent(user_id)
    final_price = base_price * (100 - discount) // 100
    if final_price < 1:
        final_price = 1

    # Создаём заказ
    order_id = await create_order(user_id, product_id, base_price, discount)

    await safe_answer_callback(callback)

    try:
        await bot.send_invoice(
            chat_id=user_id,
            title=product["title"],
            description=product["description"],
            payload=f"order_{order_id}",
            currency="XTR",
            prices=[LabeledPrice(label=product["title"], amount=final_price)],
            provider_token="",
        )
    except TelegramBadRequest as e:
        logger.error("Ошибка создания инвойса для %d: %s", user_id, e)
        await safe_send_message(user_id, "❌ Не удалось создать счёт. Попробуйте позже.")
    except Exception as e:
        logger.error("Непредвиденная ошибка инвойса %d: %s", user_id, e)
        await safe_send_message(user_id, "❌ Произошла ошибка. Попробуйте позже.")


# ---------------------------------------------------------------------------
# Платежи
# ---------------------------------------------------------------------------


@router.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery) -> None:
    """Обработка pre_checkout_query — подтверждение оплаты."""
    payload = query.invoice_payload
    user_id = query.from_user.id

    # Ожидаемый формат payload: "order_{order_id}"
    if not payload.startswith("order_"):
        await query.answer(ok=False, error_message="Неверные данные заказа.")
        return

    try:
        order_id = int(payload.split("_")[1])
    except (ValueError, IndexError):
        await query.answer(ok=False, error_message="Неверный формат заказа.")
        return

    order = await get_order(order_id)
    if not order:
        await query.answer(ok=False, error_message="Заказ не найден.")
        return

    # Проверяем, что пользователь — владелец заказа
    if order["user_id"] != user_id:
        await query.answer(ok=False, error_message="Это не ваш заказ.")
        return

    # Проверяем статус
    if order["status"] != "pending":
        await query.answer(ok=False, error_message="Заказ уже обработан.")
        return

    # Проверяем сумму — цену берём из БД, не доверяем сумме из запроса
    if query.total_amount != order["final_price"]:
        await query.answer(ok=False, error_message="Сумма заказа изменилась.")
        return

    await query.answer(ok=True)


@router.message(F.successful_payment)
async def successful_payment(message: Message) -> None:
    """Обработка успешного платежа — выдача видео."""
    payment: SuccessfulPayment = message.successful_payment
    payload = payment.invoice_payload
    payment_charge_id = payment.telegram_payment_charge_id
    user_id = message.from_user.id

    # Двойная защита от повторной обработки
    async with aiosqlite.connect(DATABASE_NAME) as db:
        cursor = await db.execute(
            "SELECT 1 FROM orders WHERE telegram_payment_charge_id = ?",
            (payment_charge_id,),
        )
        if await cursor.fetchone():
            logger.info("Платёж %s уже обработан, пропускаем.", payment_charge_id)
            return

    # Парсим payload
    if not payload.startswith("order_"):
        logger.error("Неверный payload: %s", payload)
        return

    try:
        order_id = int(payload.split("_")[1])
    except (ValueError, IndexError):
        logger.error("Не удалось распарсить order_id из payload: %s", payload)
        return

    order = await get_order(order_id)
    if not order:
        logger.error("Заказ %d не найден.", order_id)
        return

    # Завершаем заказ
    await complete_order(order_id, payment_charge_id)

    # Активируем реферала (если его первый платёж)
    await activate_referral(user_id, order_id)

    # Начисляем рефереру комиссию
    await process_referral_reward(order_id, user_id)

    # Отправляем видео
    product = await get_product(order["product_id"])
    if product:
        try:
            await bot.send_video(
                chat_id=user_id,
                video=product["telegram_file_id"],
                caption=f"🎬 <b>{product['title']}</b>\n\n{product['description']}\n\n"
                        f"✅ Оплата прошла успешно! Приятного просмотра!",
            )
            logger.info("Видео %d отправлено пользователю %d.", product["product_id"], user_id)
        except TelegramBadRequest as e:
            logger.error("Ошибка отправки видео %d: %s", user_id, e)
            await safe_send_message(
                user_id,
                "✅ Оплата прошла успешно, но не удалось отправить видео.\n"
                "Обратитесь к администратору или используйте /my_videos.",
            )
        except TelegramForbiddenError:
            logger.warning("Пользователь %d заблокировал бота.", user_id)
        except Exception as e:
            logger.error("Непредвиденная ошибка отправки видео: %s", e)
            await safe_send_message(
                user_id,
                "✅ Оплата прошла, но возникла ошибка при отправке.\n"
                "Используйте /my_videos для получения видео.",
            )
    else:
        logger.error("Товар %d не найден после оплаты.", order["product_id"])
        await safe_send_message(
            user_id,
            "✅ Оплата прошла, но видео не найдено.\nОбратитесь к администратору.",
        )


# ---------------------------------------------------------------------------
# Админ-панель — callback
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "admin_cancel")
async def cb_admin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer_callback(callback)
    if is_admin(callback.from_user.id):
        await state.clear()
        await safe_send_message(
            callback.message.chat.id,
            "✅ Действие отменено.",
            reply_markup=admin_menu_keyboard(),
        )


@router.callback_query(F.data == "admin_add")
async def cb_admin_add(callback: CallbackQuery, state: FSMContext) -> None:
    """Начало добавления видео."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await safe_answer_callback(callback)
    await state.set_state(AdminAddVideo.waiting_title)
    await safe_send_message(
        callback.message.chat.id,
        "📝 <b>Добавление нового видео</b>\n\n"
        "Введите название видео:",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_list")
async def cb_admin_list(callback: CallbackQuery) -> None:
    """Список товаров."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await safe_answer_callback(callback)

    products = await get_active_products()
    if not products:
        await safe_send_message(
            callback.message.chat.id,
            "📋 Список товаров пуст.",
            reply_markup=admin_menu_keyboard(),
        )
        return

    text = "📋 <b>Активные товары:</b>\n\n"
    for p in products:
        text += (
            f"ID: <code>{p['product_id']}</code> | "
            f"<b>{p['title']}</b> | ⭐ {p['price_stars']} | "
            f"Добавлен: {p['created_at'][:10]}\n"
        )

    await safe_send_message(callback.message.chat.id, text, reply_markup=admin_menu_keyboard())


@router.callback_query(F.data == "admin_price")
async def cb_admin_price(callback: CallbackQuery, state: FSMContext) -> None:
    """Изменение цены."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await safe_answer_callback(callback)
    await state.set_state(AdminChangePrice.waiting_product_id)
    await safe_send_message(
        callback.message.chat.id,
        "💰 <b>Изменение цены</b>\n\n"
        "Введите ID товара:",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_delete")
async def cb_admin_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """Удаление товара."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await safe_answer_callback(callback)
    await state.set_state(AdminDeleteProduct.waiting_product_id)
    await safe_send_message(
        callback.message.chat.id,
        "🗑 <b>Удаление товара</b>\n\n"
        "Введите ID товара для удаления:",
        reply_markup=cancel_keyboard(),
    )


@router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery) -> None:
    """Статистика."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await safe_answer_callback(callback)
    stats = await get_admin_stats()

    text = (
        f"📊 <b>Статистика бота</b>\n\n"
        f"Пользователей: <b>{stats['total_users']}</b>\n"
        f"Активных товаров: <b>{stats['active_products']}</b>\n"
        f"Оплаченных заказов: <b>{stats['total_orders']}</b>\n"
        f"Общая выручка: <b>{stats['total_revenue']} ⭐</b>\n"
        f"Активных рефералов: <b>{stats['active_referrals']}</b>"
    )

    await safe_send_message(callback.message.chat.id, text, reply_markup=admin_menu_keyboard())


# ---------------------------------------------------------------------------
# Админ FSM — добавление видео
# ---------------------------------------------------------------------------


@router.message(AdminAddVideo.waiting_title)
async def admin_title(message: Message, state: FSMContext) -> None:
    """Получение названия видео."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    title = message.text.strip()
    if not title:
        await safe_send_message(message.chat.id, "❌ Название не может быть пустым. Введите снова:")
        return

    await state.update_data(title=title)
    await state.set_state(AdminAddVideo.waiting_description)
    await safe_send_message(
        message.chat.id,
        f"✅ Название: <b>{title}</b>\n\n📝 Введите описание видео:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminAddVideo.waiting_description)
async def admin_description(message: Message, state: FSMContext) -> None:
    """Получение описания видео."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    description = message.text.strip()
    if not description:
        await safe_send_message(message.chat.id, "❌ Описание не может быть пустым. Введите снова:")
        return

    await state.update_data(description=description)
    await state.set_state(AdminAddVideo.waiting_price)
    await safe_send_message(
        message.chat.id,
        f"✅ Описание: <b>{description}</b>\n\n💰 Введите цену в Telegram Stars (целое число):",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminAddVideo.waiting_price)
async def admin_price_input(message: Message, state: FSMContext) -> None:
    """Получение цены видео."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    try:
        price = int(message.text.strip())
        if price < 1:
            raise ValueError
    except ValueError:
        await safe_send_message(
            message.chat.id,
            "❌ Цена должна быть целым числом >= 1. Введите снова:",
        )
        return

    await state.update_data(price=price)
    await state.set_state(AdminAddVideo.waiting_video)
    await safe_send_message(
        message.chat.id,
        f"✅ Цена: <b>{price} ⭐</b>\n\n"
        "📹 Теперь отправьте видео (как видео-файл, не как документ):",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminAddVideo.waiting_video)
async def admin_video_input(message: Message, state: FSMContext) -> None:
    """Получение видео от администратора."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    # Проверяем, что сообщение содержит видео
    if not message.video:
        await safe_send_message(
            message.chat.id,
            "❌ Это не видео. Пожалуйста, отправьте видео-файл.\n"
            "Используйте кнопку прикрепления → Видео/Фильм.",
        )
        return

    data = await state.get_data()
    title = data["title"]
    description = data["description"]
    price = data["price"]

    file_id = message.video.file_id
    file_unique_id = message.video.file_unique_id

    product_id = await add_product(
        title=title,
        description=description,
        file_id=file_id,
        file_unique_id=file_unique_id,
        price=price,
        created_by=message.from_user.id,
    )

    await state.clear()

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    card = (
        f"✅ <b>Видео успешно добавлено!</b>\n\n"
        f"🆔 ID товара: <code>{product_id}</code>\n"
        f"📝 Название: <b>{title}</b>\n"
        f"💰 Цена: <b>{price} ⭐</b>\n"
        f"📄 Описание: {description}\n"
        f"📅 Дата добавления: {now}"
    )

    await safe_send_message(
        message.chat.id,
        card,
        reply_markup=admin_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Админ FSM — изменение цены
# ---------------------------------------------------------------------------


@router.message(AdminChangePrice.waiting_product_id)
async def admin_change_price_id(message: Message, state: FSMContext) -> None:
    """Получение ID товара для изменения цены."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    try:
        product_id = int(message.text.strip())
    except ValueError:
        await safe_send_message(message.chat.id, "❌ ID должен быть числом. Введите снова:")
        return

    product = await get_product(product_id)
    if not product or not product["is_active"]:
        await safe_send_message(message.chat.id, "❌ Товар не найден или неактивен.")
        return

    await state.update_data(product_id=product_id)
    await state.set_state(AdminChangePrice.waiting_new_price)
    await safe_send_message(
        message.chat.id,
        f"📦 Товар: <b>{product['title']}</b>\n"
        f"💰 Текущая цена: <b>{product['price_stars']} ⭐</b>\n\n"
        f"Введите новую цену:",
        reply_markup=cancel_keyboard(),
    )


@router.message(AdminChangePrice.waiting_new_price)
async def admin_change_price_value(message: Message, state: FSMContext) -> None:
    """Получение новой цены."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    try:
        new_price = int(message.text.strip())
        if new_price < 1:
            raise ValueError
    except ValueError:
        await safe_send_message(
            message.chat.id, "❌ Цена должна быть числом >= 1. Введите снова:"
        )
        return

    data = await state.get_data()
    product_id = data["product_id"]

    success = await update_product_price(product_id, new_price)
    await state.clear()

    if success:
        product = await get_product(product_id)
        await safe_send_message(
            message.chat.id,
            f"✅ Цена изменена!\n\n"
            f"📦 {product['title']}\n"
            f"💰 Новая цена: <b>{new_price} ⭐</b>",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await safe_send_message(
            message.chat.id, "❌ Не удалось изменить цену.", reply_markup=admin_menu_keyboard()
        )


# ---------------------------------------------------------------------------
# Админ FSM — удаление товара
# ---------------------------------------------------------------------------


@router.message(AdminDeleteProduct.waiting_product_id)
async def admin_delete_id(message: Message, state: FSMContext) -> None:
    """Получение ID товара для удаления."""
    if not is_admin(message.from_user.id):
        await safe_send_message(message.chat.id, "🚫 Доступ запрещён.")
        await state.clear()
        return

    try:
        product_id = int(message.text.strip())
    except ValueError:
        await safe_send_message(message.chat.id, "❌ ID должен быть числом. Введите снова:")
        return

    product = await get_product(product_id)
    if not product or not product["is_active"]:
        await safe_send_message(message.chat.id, "❌ Товар не найден или уже удалён.")
        await state.clear()
        return

    await state.update_data(product_id=product_id)
    await state.set_state(AdminDeleteProduct.waiting_confirm)

    confirm_kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete"),
                InlineKeyboardButton(text="❌ Нет, отмена", callback_data="cancel_delete"),
            ]
        ]
    )

    await safe_send_message(
        message.chat.id,
        f"⚠️ <b>Вы уверены?</b>\n\n"
        f"Товар: <b>{product['title']}</b>\n"
        f"ID: <code>{product_id}</code>\n\n"
        f"Товар будет деактивирован. История покупок сохранится.",
        reply_markup=confirm_kb,
    )


@router.callback_query(F.data == "confirm_delete")
async def cb_confirm_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """Подтверждение удаления товара."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    data = await state.get_data()
    product_id = data.get("product_id")
    await state.clear()

    if not product_id:
        await safe_answer_callback(callback, "❌ Ошибка: ID товара не найден.", show_alert=True)
        return

    success = await deactivate_product(product_id)
    await safe_answer_callback(callback)

    if success:
        await safe_send_message(
            callback.message.chat.id,
            f"✅ Товар <code>{product_id}</code> деактивирован.",
            reply_markup=admin_menu_keyboard(),
        )
    else:
        await safe_send_message(
            callback.message.chat.id,
            "❌ Не удалось удалить товар.",
            reply_markup=admin_menu_keyboard(),
        )


@router.callback_query(F.data == "cancel_delete")
async def cb_cancel_delete(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена удаления товара."""
    if not is_admin(callback.from_user.id):
        await safe_answer_callback(callback, "🚫 Доступ запрещён.", show_alert=True)
        return

    await state.clear()
    await safe_answer_callback(callback)
    await safe_send_message(
        callback.message.chat.id,
        "✅ Удаление отменено.",
        reply_markup=admin_menu_keyboard(),
    )


# ---------------------------------------------------------------------------
# Обработка неизвестных callback
# ---------------------------------------------------------------------------


@router.callback_query()
async def cb_unknown(callback: CallbackQuery) -> None:
    """Обработка неизвестных callback-данных."""
    await safe_answer_callback(callback, "❓ Неизвестная команда.")


# ---------------------------------------------------------------------------
# Обработка неизвестных сообщений
# ---------------------------------------------------------------------------


@router.message()
async def on_unknown_message(message: Message, state: FSMContext) -> None:
    """Обработка неизвестных сообщений."""
    current_state = await state.get_state()
    if current_state:
        # Если пользователь в FSM, пропускаем (FSM-обработчики решат)
        return

    if message.chat.type == ChatType.PRIVATE:
        await safe_send_message(
            message.chat.id,
            "Я не понимаю эту команду. Используйте /start для начала.",
            reply_markup=main_menu_keyboard(),
        )


# ---------------------------------------------------------------------------
# Запуск бота
# ---------------------------------------------------------------------------


async def on_startup() -> None:
    """Действия при запуске бота."""
    await init_db()
    logger.info("Бот запущен и готов к работе.")


async def on_shutdown() -> None:
    """Действия при остановке бота."""
    logger.info("Бот остановлен.")


async def main() -> None:
    """Главная функция запуска."""
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    logger.info("Запуск бота через long polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
