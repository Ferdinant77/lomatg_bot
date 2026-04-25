#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import logging
import sqlite3
import sys
from typing import Dict, List, Tuple, Optional

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telegram.request import HTTPXRequest

# ==================== НАСТРОЙКИ ====================
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise ValueError("❌ Переменная окружения BOT_TOKEN не установлена!")

SUPER_ADMIN_IDS = [923942388]  # Ваш ID
DB_NAME = "poll_bot.db"

NO_ACTIVE_POLL_MSG = "❌ Нет активного опроса."
PERMISSION_DENIED = "⛔ Недостаточно прав."
UNKNOWN_OPTION = "неизвестно"
ERROR_OCCURRED = "⚠️ Произошла ошибка. Попробуйте позже."

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS polls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            question TEXT NOT NULL,
            created_by INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_active BOOLEAN DEFAULT 1
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS options (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            poll_id INTEGER NOT NULL,
            text TEXT NOT NULL,
            added_by INTEGER,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(poll_id) REFERENCES polls(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            poll_id INTEGER NOT NULL,
            option_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            nickname TEXT,
            voted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (poll_id, user_id),
            FOREIGN KEY(option_id) REFERENCES options(id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            nickname TEXT,
            registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY,
            added_by INTEGER NOT NULL,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.commit()
    conn.close()
    logger.info("База данных инициализирована")

def get_db_connection():
    return sqlite3.connect(DB_NAME)

# ---------- Админы ----------
def is_super_admin(user_id: int) -> bool:
    return user_id in SUPER_ADMIN_IDS

def is_admin(user_id: int) -> bool:
    if is_super_admin(user_id):
        return True
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
        return cur.fetchone() is not None

def add_admin(admin_id: int, added_by: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.cursor()
        try:
            cur.execute("INSERT INTO admins (user_id, added_by) VALUES (?, ?)", (admin_id, added_by))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False

def remove_admin(admin_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM admins WHERE user_id = ?", (admin_id,))
        conn.commit()
        return cur.rowcount > 0

def get_all_admins() -> List[Dict]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, added_by, added_at FROM admins ORDER BY added_at")
        rows = cur.fetchall()
        return [{"user_id": r[0], "added_by": r[1], "added_at": r[2]} for r in rows]

# ---------- Пользователи ----------
def set_user_nickname(user_id: int, nickname: str) -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO users (user_id, nickname) VALUES (?, ?)", (user_id, nickname))
        conn.commit()

def get_user_nickname(user_id: int) -> Optional[str]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT nickname FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        return row[0] if row else None

def get_user_vote_count(user_id: int) -> int:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM votes WHERE user_id = ?", (user_id,))
        return cur.fetchone()[0]

# ---------- Опросы ----------
def deactivate_all_polls() -> None:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE polls SET is_active = 0")
        conn.commit()

def create_poll(question: str, created_by: int, options: List[str]) -> int:
    with get_db_connection() as conn:
        cur = conn.cursor()
        deactivate_all_polls()
        cur.execute("INSERT INTO polls (question, created_by, is_active) VALUES (?, ?, 1)", (question, created_by))
        poll_id = cur.lastrowid
        for opt in options:
            cur.execute("INSERT INTO options (poll_id, text, added_by) VALUES (?, ?, ?)", (poll_id, opt, created_by))
        conn.commit()
        logger.info(f"Создан опрос {poll_id}: {question}")
        return poll_id

def get_active_poll() -> Optional[Dict]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, question FROM polls WHERE is_active = 1 LIMIT 1")
        poll = cur.fetchone()
        if not poll:
            return None
        poll_id, question = poll
        cur.execute("SELECT id, text FROM options WHERE poll_id = ? ORDER BY id", (poll_id,))
        options = [{"id": row[0], "text": row[1]} for row in cur.fetchall()]
        return {"id": poll_id, "question": question, "options": options}

def get_poll_results(poll_id: int) -> Tuple[List[str], List[int]]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT text FROM options WHERE poll_id = ? ORDER BY id", (poll_id,))
        option_texts = [row[0] for row in cur.fetchall()]
        cur.execute("""
            SELECT option_id, COUNT(*) FROM votes
            WHERE poll_id = ?
            GROUP BY option_id
        """, (poll_id,))
        counts = dict.fromkeys(range(1, len(option_texts) + 1), 0)
        for opt_id, cnt in cur.fetchall():
            counts[opt_id] = cnt
        votes = [counts[i] for i in range(1, len(option_texts) + 1)]
        return option_texts, votes

def get_poll_history() -> List[Dict]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT p.id, p.question, p.created_at,
                   COUNT(v.user_id) as total_votes
            FROM polls p
            LEFT JOIN votes v ON p.id = v.poll_id
            GROUP BY p.id
            ORDER BY p.created_at DESC
        """)
        rows = cur.fetchall()
        return [{"id": row[0], "question": row[1], "created_at": row[2], "votes": row[3]} for row in rows]

def cast_vote(poll_id: int, option_id: int, user_id: int, nickname: Optional[str] = None) -> bool:
    try:
        with get_db_connection() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM votes WHERE poll_id = ? AND user_id = ?", (poll_id, user_id))
            cur.execute("INSERT INTO votes (poll_id, option_id, user_id, nickname) VALUES (?, ?, ?, ?)",
                        (poll_id, option_id, user_id, nickname))
            conn.commit()
            return True
    except Exception as e:
        logger.error(f"Ошибка голосования: {e}")
        return False

def close_poll(poll_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE polls SET is_active = 0 WHERE id = ?", (poll_id,))
        conn.commit()
        return cur.rowcount > 0

def get_poll_by_id(poll_id: int) -> Optional[Dict]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id, question, is_active FROM polls WHERE id = ?", (poll_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("SELECT id, text FROM options WHERE poll_id = ?", (poll_id,))
        options = [{"id": r[0], "text": r[1]} for r in cur.fetchall()]
        return {"id": row[0], "question": row[1], "is_active": bool(row[2]), "options": options}

def add_option_to_poll(poll_id: int, option_text: str, added_by: int) -> Optional[int]:
    with get_db_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM options WHERE poll_id = ? AND text = ?", (poll_id, option_text))
        if cur.fetchone():
            return None
        cur.execute("INSERT INTO options (poll_id, text, added_by) VALUES (?, ?, ?)",
                    (poll_id, option_text, added_by))
        conn.commit()
        return cur.lastrowid

def format_results_text(poll_id: int) -> str:
    options, votes = get_poll_results(poll_id)
    total = sum(votes)
    if total == 0:
        return "😔 Пока никто не проголосовал."
    max_votes = max(votes) if votes else 1
    lines = [f"📊 *Результаты опроса* (всего голосов: {total})\n"]
    for opt, v in zip(options, votes):
        percent = v / total * 100
        bar_length = int(20 * v / max_votes) if max_votes > 0 else 0
        bar = "█" * bar_length + "░" * (20 - bar_length)
        lines.append(f"• {opt}:\n  {bar} {v} ({percent:.1f}%)")
    return "\n".join(lines)

# ==================== МЕНЮ И ПРОФИЛЬ ====================
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    nickname = get_user_nickname(user_id) or "Аноним"
    vote_count = get_user_vote_count(user_id)
    role = "Вожатый" if is_admin(user_id) else "Студент"
    text = (f"👤 *Профиль*\n\n"
            f"🔹 ID: `{user_id}`\n"
            f"🔹 Ник: {nickname}\n"
            f"🔹 Роль: {role}\n"
            f"🔹 Голосов: {vote_count}")
    await update.message.reply_text(text, parse_mode="Markdown")

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    keyboard = [
        [InlineKeyboardButton("🗳 Голосовать", callback_data="menu_vote")],
        [InlineKeyboardButton("📊 Результаты", callback_data="menu_results")],
        [InlineKeyboardButton("📜 История", callback_data="menu_history")],
        [InlineKeyboardButton("✏️ Ник", callback_data="menu_setname")],
        [InlineKeyboardButton("👤 Профиль", callback_data="menu_profile")]
    ]
    if is_admin(user_id):
        keyboard.append([InlineKeyboardButton("🆕 Новый опрос", callback_data="menu_new_poll")])
        keyboard.append([InlineKeyboardButton("🔒 Закрыть опрос", callback_data="menu_close_poll")])
    if is_super_admin(user_id):
        keyboard.append([InlineKeyboardButton("👥 Админы", callback_data="menu_admins")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    # Исправлено: если это callback_query, редактируем сообщение, иначе отправляем новое
    if update.callback_query:
        await update.callback_query.message.edit_text(
            "🏠 *Главное меню*", reply_markup=reply_markup, parse_mode="Markdown"
        )
    else:
        await update.message.reply_text(
            "🏠 *Главное меню*", reply_markup=reply_markup, parse_mode="Markdown"
        )

async def show_admins_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    keyboard = [
        [InlineKeyboardButton("➕ Добавить", callback_data="admin_add")],
        [InlineKeyboardButton("➖ Удалить", callback_data="admin_remove")],
        [InlineKeyboardButton("📋 Список", callback_data="admin_list")],
        [InlineKeyboardButton("◀️ Назад", callback_data="menu_back")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.callback_query.message.edit_text(
        "👥 *Управление админами*", reply_markup=reply_markup, parse_mode="Markdown"
    )

async def show_poll_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, poll: Dict) -> None:
    keyboard = []
    opts = poll["options"]
    for i in range(0, len(opts), 2):
        row = [InlineKeyboardButton(opts[i]["text"], callback_data=f"vote_{poll['id']}_{opts[i]['id']}")]
        if i+1 < len(opts):
            row.append(InlineKeyboardButton(opts[i+1]["text"], callback_data=f"vote_{poll['id']}_{opts[i+1]['id']}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("➕ Свой вариант", callback_data=f"add_option_{poll['id']}")])
    keyboard.append([InlineKeyboardButton("📊 Результаты", callback_data=f"results_{poll['id']}")])
    keyboard.append([InlineKeyboardButton("◀️ Назад", callback_data="menu_back")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = f"🎯 *{poll['question']}*\n\nВыберите вариант:"
    if update.callback_query:
        await update.callback_query.message.edit_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")

# ==================== ОБРАБОТЧИКИ КОМАНД ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    args = context.args
    if args and args[0].startswith("poll_"):
        try:
            poll_id = int(args[0].split("_")[1])
            poll = get_poll_by_id(poll_id)
            if poll:
                await show_poll_to_user(update, context, poll)
            else:
                await update.message.reply_text("❌ Опрос не найден.")
        except (IndexError, ValueError):
            await update.message.reply_text("❌ Неверная ссылка.")
    else:
        await update.message.reply_text(
            "👋 Добро пожаловать!\n/menu – главное меню\n/setname <ник> – установить имя"
        )
        await show_main_menu(update, context)

async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_main_menu(update, context)

async def setname(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not context.args:
        await update.message.reply_text("📝 Использование: /setname <ваш_ник>")
        return
    nickname = " ".join(context.args).strip()[:30]
    set_user_nickname(user_id, nickname)
    await update.message.reply_text(f"✅ Ник установлен: {nickname}")

async def vote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text(NO_ACTIVE_POLL_MSG)
        return
    await show_poll_to_user(update, context, poll)

async def results_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text(NO_ACTIVE_POLL_MSG)
        return
    text = format_results_text(poll["id"])
    await update.message.reply_text(text, parse_mode="Markdown")

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    history = get_poll_history()
    if not history:
        await update.message.reply_text("📭 История пуста.")
        return
    text = "📜 *История опросов:*\n\n"
    keyboard = []
    for p in history:
        date_str = p['created_at'][:10] if p['created_at'] else UNKNOWN_OPTION
        text += f"• *{p['question']}* (голосов: {p['votes']}, {date_str})\n"
        keyboard.append([InlineKeyboardButton(f"📊 {p['question'][:30]}", callback_data=f"show_poll_{p['id']}")])
    await update.message.reply_text(text, parse_mode="Markdown")
    await update.message.reply_text("Нажмите на кнопку:", reply_markup=InlineKeyboardMarkup(keyboard))

# ---------- АДМИН-КОМАНДЫ ----------
async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_super_admin(user_id):
        await update.message.reply_text(PERMISSION_DENIED)
        return
    if not context.args:
        await update.message.reply_text("📝 /add_admin <user_id>")
        return
    try:
        new_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return
    if is_admin(new_id):
        await update.message.reply_text(f"⚠️ {new_id} уже админ.")
        return
    if add_admin(new_id, user_id):
        await update.message.reply_text(f"✅ Админ {new_id} добавлен.")
    else:
        await update.message.reply_text("❌ Ошибка добавления.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_super_admin(user_id):
        await update.message.reply_text(PERMISSION_DENIED)
        return
    if not context.args:
        await update.message.reply_text("📝 /remove_admin <user_id>")
        return
    try:
        admin_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ ID должен быть числом.")
        return
    if admin_id in SUPER_ADMIN_IDS:
        await update.message.reply_text("❌ Нельзя удалить главного.")
        return
    if remove_admin(admin_id):
        await update.message.reply_text(f"✅ Админ {admin_id} удалён.")
    else:
        await update.message.reply_text("❌ Не найден.")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_super_admin(user_id):
        await update.message.reply_text(PERMISSION_DENIED)
        return
    admins = get_all_admins()
    if not admins:
        await update.message.reply_text("📭 Список пуст.")
        return
    text = "👥 *Вожатые:*\n"
    for a in admins:
        text += f"• `{a['user_id']}` (добавлен {a['added_at'][:10]})\n"
    await update.message.reply_text(text, parse_mode="Markdown")

# ---------- СОЗДАНИЕ ОПРОСА ----------
async def new_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(PERMISSION_DENIED)
        return
    context.user_data["creating_poll"] = True
    context.user_data["poll_question"] = None
    context.user_data["poll_options"] = []
    await update.message.reply_text("📝 Введите вопрос:")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()
    user_id = update.effective_user.id

    if context.user_data.get("waiting_for_custom_option"):
        await _handle_custom_option(update, context, text, user_id)
        return

    if context.user_data.get("creating_poll"):
        await _handle_poll_creation(update, context, text)
        return

async def _handle_custom_option(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, user_id: int) -> None:
    poll_id = context.user_data.get("pending_poll_id")
    if not poll_id:
        await update.message.reply_text("❌ Ошибка.")
        context.user_data.clear()
        return
    poll = get_poll_by_id(poll_id)
    if not poll or not poll["is_active"]:
        await update.message.reply_text("❌ Опрос закрыт.")
        context.user_data.clear()
        return
    if len(text) > 100:
        await update.message.reply_text("❌ Слишком длинный вариант.")
        return
    if text in [opt["text"] for opt in poll["options"]]:
        await update.message.reply_text("❌ Уже есть.")
        await show_poll_to_user(update, context, poll)
        context.user_data.clear()
        return
    add_option_to_poll(poll_id, text, user_id)
    await update.message.reply_text(f"✅ Вариант «{text}» добавлен!")
    context.user_data.clear()
    updated_poll = get_active_poll()
    if updated_poll and updated_poll["id"] == poll_id:
        await show_poll_to_user(update, context, updated_poll)
    else:
        await show_poll_to_user(update, context, poll)

async def _handle_poll_creation(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    if context.user_data["poll_question"] is None:
        context.user_data["poll_question"] = text
        await update.message.reply_text(
            "✅ Вопрос сохранён. Вводите варианты по одному.\n/done – закончить"
        )
    else:
        if len(text) > 100:
            await update.message.reply_text("❌ Слишком длинный.")
            return
        context.user_data["poll_options"].append(text)
        await update.message.reply_text(f"✅ Вариант «{text}» добавлен. Ещё или /done")

async def done_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get("creating_poll"):
        await update.message.reply_text("❌ Нет активного создания.")
        return
    question = context.user_data.get("poll_question")
    options = context.user_data.get("poll_options", [])
    if not question:
        await update.message.reply_text("❌ Нет вопроса.")
        context.user_data.clear()
        return
    try:
        poll_id = create_poll(question, update.effective_user.id, options)
        bot_info = await context.bot.get_me()
        deep_link = f"https://t.me/{bot_info.username}?start=poll_{poll_id}"
        await update.message.reply_text(
            f"✅ Опрос создан!\n🔗 {deep_link}\nТеперь студенты могут голосовать."
        )
        poll = get_active_poll()
        if poll:
            await show_poll_to_user(update, context, poll)
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await update.message.reply_text(ERROR_OCCURRED)
    finally:
        context.user_data.clear()

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("creating_poll"):
        context.user_data.clear()
        await update.message.reply_text("❌ Создание отменено.")
    elif context.user_data.get("waiting_for_custom_option"):
        context.user_data.clear()
        await update.message.reply_text("❌ Добавление отменено.")
        poll = get_active_poll()
        if poll:
            await show_poll_to_user(update, context, poll)
    else:
        await update.message.reply_text("❌ Нет активной операции.")

async def close_poll_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text(PERMISSION_DENIED)
        return
    poll = get_active_poll()
    if not poll:
        await update.message.reply_text(NO_ACTIVE_POLL_MSG)
        return
    close_poll(poll["id"])
    await update.message.reply_text(f"🔒 Опрос «{poll['question']}» закрыт.")

async def add_custom_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("add_option_"):
        poll_id = int(data.split("_")[2])
        poll = get_poll_by_id(poll_id)
        if not poll or not poll["is_active"]:
            await query.edit_message_text("❌ Опрос не активен.")
            return
        context.user_data["waiting_for_custom_option"] = True
        context.user_data["pending_poll_id"] = poll_id
        await query.edit_message_text("✏️ Введите свой вариант:\n/cancel – отмена")

# ---------- CALLBACK-ОБРАБОТЧИКИ ----------
async def menu_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id

    if data == "menu_back":
        await show_main_menu(update, context)
    elif data == "menu_vote":
        poll = get_active_poll()
        if poll:
            await show_poll_to_user(update, context, poll)
        else:
            await query.edit_message_text(NO_ACTIVE_POLL_MSG)
    elif data == "menu_results":
        poll = get_active_poll()
        if poll:
            text = format_results_text(poll["id"])
            await query.edit_message_text(text, parse_mode="Markdown")
        else:
            await query.edit_message_text(NO_ACTIVE_POLL_MSG)
    elif data == "menu_history":
        hist = get_poll_history()
        if not hist:
            await query.edit_message_text("📭 История пуста.")
            return
        text = "📜 *История:*\n"
        keyboard = []
        for p in hist:
            text += f"• {p['question']} (голосов: {p['votes']}, {p['created_at'][:10]})\n"
            keyboard.append([InlineKeyboardButton(f"📊 {p['question'][:30]}", callback_data=f"show_poll_{p['id']}")])
        await query.edit_message_text(text, parse_mode="Markdown")
        await query.message.reply_text("Результаты:", reply_markup=InlineKeyboardMarkup(keyboard))
    elif data == "menu_setname":
        await query.edit_message_text("📝 /setname <ник>")
    elif data == "menu_profile":
        nick = get_user_nickname(user_id) or "Аноним"
        votes = get_user_vote_count(user_id)
        role = "Вожатый" if is_admin(user_id) else "Студент"
        text = f"👤 *Профиль*\nID: `{user_id}`\nНик: {nick}\nРоль: {role}\nГолосов: {votes}"
        await query.edit_message_text(text, parse_mode="Markdown")
    elif data == "menu_new_poll":
        if not is_admin(user_id):
            await query.edit_message_text(PERMISSION_DENIED)
            return
        await query.edit_message_text("📝 Команда /new_poll")
    elif data == "menu_close_poll":
        if not is_admin(user_id):
            await query.edit_message_text(PERMISSION_DENIED)
            return
        poll = get_active_poll()
        if not poll:
            await query.edit_message_text(NO_ACTIVE_POLL_MSG)
            return
        close_poll(poll["id"])
        await query.edit_message_text(f"🔒 Опрос закрыт.")
    elif data == "menu_admins":
        if not is_super_admin(user_id):
            await query.edit_message_text(PERMISSION_DENIED)
            return
        await show_admins_menu(update, context)
    elif data == "admin_add":
        await query.edit_message_text("📝 /add_admin <id>")
    elif data == "admin_remove":
        await query.edit_message_text("📝 /remove_admin <id>")
    elif data == "admin_list":
        admins = get_all_admins()
        if not admins:
            await query.edit_message_text("📭 Нет дополнительных.")
            return
        text = "👥 *Вожатые:*\n"
        for a in admins:
            text += f"• `{a['user_id']}` (добавлен {a['added_at'][:10]})\n"
        await query.edit_message_text(text, parse_mode="Markdown")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = query.data
    user_id = query.from_user.id

    if data.startswith("vote_"):
        await query.answer()
        try:
            _, pid, oid = data.split("_")
            pid, oid = int(pid), int(oid)
        except ValueError:
            await query.edit_message_text("❌ Ошибка.")
            return
        poll = get_poll_by_id(pid)
        if not poll or not poll["is_active"]:
            await query.edit_message_text("❌ Опрос неактивен.")
            return
        nick = get_user_nickname(user_id)
        if cast_vote(pid, oid, user_id, nick):
            await query.edit_message_text("✅ Голос учтён! /menu")
        else:
            await query.edit_message_text("❌ Ошибка.")
    elif data.startswith("results_"):
        await query.answer()
        pid = int(data.split("_")[1])
        await query.edit_message_text(format_results_text(pid), parse_mode="Markdown")
    elif data.startswith("show_poll_"):
        await query.answer()
        pid = int(data.split("_")[2])
        await query.edit_message_text(format_results_text(pid), parse_mode="Markdown")
    else:
        await menu_callback_handler(update, context)

# ==================== ЗАПУСК ====================
def main():
    init_db()
    request = HTTPXRequest(
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(TOKEN).request(request).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_command))
    app.add_handler(CommandHandler("help", menu_command))
    app.add_handler(CommandHandler("setname", setname))
    app.add_handler(CommandHandler("vote", vote_command))
    app.add_handler(CommandHandler("results", results_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("close_poll", close_poll_command))
    app.add_handler(CommandHandler("new_poll", new_poll))
    app.add_handler(CommandHandler("done", done_command))
    app.add_handler(CommandHandler("stop", done_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("add_admin", add_admin_command))
    app.add_handler(CommandHandler("remove_admin", remove_admin_command))
    app.add_handler(CommandHandler("list_admins", list_admins_command))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(add_custom_option_callback, pattern="^add_option_"))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🚀 Бот успешно запущен!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Бот остановлен.")
    except Exception as e:
        logger.critical(f"Критическая ошибка: {e}")
        sys.exit(1)
