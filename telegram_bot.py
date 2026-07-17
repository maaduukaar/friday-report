"""Multi-user Telegram interface for Friday Report.

Profiles, workloads and report jobs live in SQLite.  ``config.toml`` now holds
only deployment-wide settings and secrets; it is never rewritten by users.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import telebot
import tomllib
from telebot import types

from storage import (
    InvalidSetting,
    ProfileIncomplete,
    ReportInProgress,
    Storage,
    StorageError,
    User,
    UserLimitReached,
    WeeklyLimitReached,
)


if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
DATA_DIR = BASE_DIR / "data"
DATABASE_PATH = DATA_DIR / "friday_report.db"
REPORTS_DIR = DATA_DIR / "report-runs"
MAX_USERS = 300
STATE_TTL_SECONDS = 15 * 60


# Keep the form categories in one fixed whitelist.  Callback data is checked
# against it before any value is written to the database.
WORKLOAD_CATEGORIES: dict[str, tuple[str, str]] = {
    "SITES_ADMIN": ("Администрирование сайтов", "my"),
    "SITES_DEVELOPMENT": ("Доработка сайтов support", "my"),
    "REPORTING": ("Ведение (подготовка) отчётности", "my"),
    "DOCUMENTATION": ("Работа с внутренней документацией", "my"),
    "SITES_TECH_SUPPORT": ("Тех. сопровождение сайтов", "my"),
    "BUGS_PROCESSING": ("Обработка багов и заявок", "other"),
    "CLIENT_SUPPORT": ("Техподдержка клиентов", "other"),
    "INTERNAL_SUPPORT": ("Техподдержка внутренних пользователей", "other"),
    "WORKAROUNDS": ("Поиск обходных решений", "other"),
    "INFRASTRUCTURE": ("Поддержка внутренней инфраструктуры", "other"),
    "BACKUPS": ("Работа с резервными копиями", "other"),
    "INTERNAL_TRAINING": ("Внутреннее обучение", "other"),
    "EXTERNAL_TRAINING": ("Внешнее обучение", "other"),
    "ANALYTICS": ("Аналитика (Naumen)", "other"),
    "TASK_TRACKER": ("Актуализация задач в трекере", "other"),
    "QUALITY_CONTROL": ("Контроль качества", "other"),
    "MANAGEMENT": ("Менеджмент", "other"),
    "CRM_DEVELOPMENT": ("Доработка CRM", "other"),
    "NAUMEN_ADMIN": ("Администрирование Naumen", "other"),
}

DEFAULT_WORKLOAD = {
    category: value
    for category, value in {
        "SITES_ADMIN": 8,
        "SITES_DEVELOPMENT": 82,
        "REPORTING": 10,
    }.items()
}
DEFAULT_WORKLOAD = {category: DEFAULT_WORKLOAD.get(category, 0) for category in WORKLOAD_CATEGORIES}


def _read_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError(f"Конфигурационный файл не найден: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as config_file:
        return tomllib.load(config_file)


def _parse_id_list(value: object, source: str) -> set[int]:
    if value is None:
        return set()
    if isinstance(value, str):
        items = [part.strip() for part in value.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise RuntimeError(f"{source} должен быть списком Telegram user ID")

    result: set[int] = set()
    for item in items:
        try:
            parsed = int(item)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"Некорректный Telegram user ID в {source}") from error
        if parsed <= 0:
            raise RuntimeError(f"Некорректный Telegram user ID в {source}")
        result.add(parsed)
    return result


def _legacy_default_workload(config: dict[str, Any]) -> dict[str, int]:
    configured = config.get("workload", {})
    if not isinstance(configured, dict):
        return DEFAULT_WORKLOAD.copy()
    candidate: dict[str, int] = {}
    for category in WORKLOAD_CATEGORIES:
        value = configured.get(category, DEFAULT_WORKLOAD[category])
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
            return DEFAULT_WORKLOAD.copy()
        candidate[category] = value
    return candidate if sum(candidate.values()) == 100 else DEFAULT_WORKLOAD.copy()


def _load_settings() -> dict[str, Any]:
    config = _read_config()
    telegram_config = config.get("telegram", {})
    if not isinstance(telegram_config, dict):
        raise RuntimeError("Секция [telegram] в config.toml имеет неверный формат")

    token = os.getenv("TELEGRAM_BOT_TOKEN") or str(telegram_config.get("token", "")).strip()
    if not token:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN или telegram.token в config.toml")

    bootstrap_source = os.getenv("FRIDAY_REPORT_BOOTSTRAP_ADMIN_IDS")
    bootstrap_ids = _parse_id_list(
        bootstrap_source if bootstrap_source is not None else telegram_config.get("bootstrap_admin_ids", []),
        "FRIDAY_REPORT_BOOTSTRAP_ADMIN_IDS / telegram.bootstrap_admin_ids",
    )
    timezone_name = str(telegram_config.get("business_timezone", "Europe/Moscow"))
    try:
        business_timezone = ZoneInfo(timezone_name)
    except Exception as error:
        raise RuntimeError(f"Неизвестный business_timezone: {timezone_name}") from error

    try:
        workers = int(os.getenv("FRIDAY_REPORT_MAX_WORKERS", telegram_config.get("report_workers", 1)))
    except (TypeError, ValueError) as error:
        raise RuntimeError("report_workers должен быть целым числом") from error
    workers = max(1, min(workers, 3))

    try:
        report_timeout = int(os.getenv("FRIDAY_REPORT_TIMEOUT_SECONDS", "600"))
    except ValueError as error:
        raise RuntimeError("FRIDAY_REPORT_TIMEOUT_SECONDS должен быть целым числом") from error
    report_timeout = max(60, min(report_timeout, 30 * 60))
    debug = config.get("debug", True)
    if not isinstance(debug, bool):
        raise RuntimeError("debug должен быть true или false")

    return {
        "token": token,
        "bootstrap_admin_ids": bootstrap_ids,
        "business_timezone": business_timezone,
        "debug": debug,
        "default_workload": _legacy_default_workload(config),
        "legacy_employee_name": str(config.get("employee_name", "")).strip() or None,
        "legacy_department": str(config.get("department", "")).strip() or None,
        "workers": workers,
        "report_timeout": report_timeout,
    }


SETTINGS = _load_settings()
STORAGE = Storage(DATABASE_PATH, max_active_users=MAX_USERS)
STORAGE.initialize()
STORAGE.recover_interrupted_runs()
if not STORAGE.has_admins() and not SETTINGS["bootstrap_admin_ids"]:
    raise RuntimeError(
        "Нет администратора. Укажите telegram.bootstrap_admin_ids в config.toml "
        "или FRIDAY_REPORT_BOOTSTRAP_ADMIN_IDS до первого запуска."
    )

bot = telebot.TeleBot(SETTINGS["token"], threaded=True)
REPORT_EXECUTOR = ThreadPoolExecutor(
    max_workers=SETTINGS["workers"], thread_name_prefix="friday-report"
)
# Do not accept an unbounded burst of expensive browser processes.  Slots include
# both running and waiting jobs; per-user reservations still stop duplicate clicks.
REPORT_QUEUE = threading.BoundedSemaphore(SETTINGS["workers"] * 3)
USER_STATE: dict[int, dict[str, Any]] = {}
USER_STATE_LOCK = threading.RLock()


def _private_chat(message_or_call: Any) -> bool:
    message = getattr(message_or_call, "message", message_or_call)
    return bool(getattr(getattr(message, "chat", None), "type", None) == "private")


def _user_label(user: User) -> str:
    if user.employee_name:
        return user.employee_name[:80]
    if user.username:
        return f"@{user.username}"[:80]
    return " ".join(part for part in [user.first_name or "", user.last_name or ""] if part).strip()[:80] or str(user.telegram_user_id)


def _button_label(value: str, max_bytes: int = 48) -> str:
    """Trim text by UTF-8 bytes for Telegram's inline-button limit."""

    result = ""
    for character in value:
        if len((result + character).encode("utf-8")) > max_bytes:
            return result.rstrip() + "…"
        result += character
    return result


def _bootstrap_profile_for(user_id: int) -> tuple[str | None, str | None]:
    # A legacy one-user profile is migrated only to the first configured admin,
    # never copied into every administrator's profile.
    bootstrap_ids = sorted(SETTINGS["bootstrap_admin_ids"])
    if bootstrap_ids and user_id == bootstrap_ids[0]:
        return SETTINGS["legacy_employee_name"], SETTINGS["legacy_department"]
    return None, None


def _notify_admins_about_pending(pending_user: User) -> None:
    for admin in STORAGE.list_active_users():
        if not admin.is_admin:
            continue
        try:
            bot.send_message(
                admin.chat_id,
                f"Новая заявка от пользователя {_user_label(pending_user)} (ID {pending_user.telegram_user_id}).\n"
                "Проверьте её через /admin.",
            )
        except Exception:
            # Telegram may have no active private dialog with an administrator yet.
            pass


def _identify_message_user(message: Any, register_unknown: bool = True) -> User | None:
    """Return the canonical Telegram user, never a group chat identity."""

    if not _private_chat(message):
        try:
            bot.send_message(message.chat.id, "Для защиты персональных данных бот работает только в личном чате.")
        except Exception:
            pass
        return None
    sender = getattr(message, "from_user", None)
    if sender is None:
        return None
    user_id = int(sender.id)
    chat_id = int(message.chat.id)
    if user_id in SETTINGS["bootstrap_admin_ids"]:
        employee_name, department = _bootstrap_profile_for(user_id)
        return STORAGE.ensure_bootstrap_admin(
            user_id,
            chat_id,
            getattr(sender, "username", None),
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
            employee_name,
            department,
            SETTINGS["default_workload"],
        )

    existing = STORAGE.get_user(user_id)
    if existing is not None:
        updated, _ = STORAGE.register_pending_user(
            user_id,
            chat_id,
            getattr(sender, "username", None),
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
        )
        return updated
    if not register_unknown:
        return None
    try:
        pending_user, created = STORAGE.register_pending_user(
            user_id,
            chat_id,
            getattr(sender, "username", None),
            getattr(sender, "first_name", None),
            getattr(sender, "last_name", None),
        )
    except UserLimitReached:
        bot.send_message(chat_id, "Достигнут лимит в 300 зарегистрированных пользователей. Обратитесь к администратору.")
        return None
    if created:
        _notify_admins_about_pending(pending_user)
    return pending_user


def _identify_callback_user(call: Any) -> User | None:
    if not _private_chat(call):
        bot.answer_callback_query(call.id, "Бот работает только в личном чате.", show_alert=True)
        return None
    sender = getattr(call, "from_user", None)
    if sender is None:
        return None
    user = STORAGE.get_user(int(sender.id))
    if user is None or user.chat_id != int(call.message.chat.id):
        bot.answer_callback_query(call.id, "Сессия устарела. Откройте /start в личном чате.", show_alert=True)
        return None
    if user.role == "blocked":
        bot.answer_callback_query(call.id, "Доступ ограничен.", show_alert=True)
        return None
    return user


def _send_access_status(chat_id: int, user: User) -> None:
    if user.role == "pending":
        profile_hint = " Заполните профиль через /profile." if not user.profile_complete else ""
        bot.send_message(chat_id, f"Заявка ожидает подтверждения администратора.{profile_hint}")
    elif user.role == "blocked":
        bot.send_message(chat_id, "Доступ к боту ограничен.")


def _require_active_message_user(message: Any) -> User | None:
    user = _identify_message_user(message)
    if user is None:
        return None
    if not user.is_active:
        _send_access_status(message.chat.id, user)
        return None
    return user


def _require_admin(user: User, call: Any | None = None) -> bool:
    if user.is_admin:
        return True
    if call is not None:
        bot.answer_callback_query(call.id, "Эта настройка доступна только администраторам.", show_alert=True)
    return False


def _get_state(user_id: int) -> dict[str, Any] | None:
    with USER_STATE_LOCK:
        state = USER_STATE.get(user_id)
        if state and time.monotonic() - state["created_at"] > STATE_TTL_SECONDS:
            USER_STATE.pop(user_id, None)
            return None
        return state.copy() if state else None


def _set_state(user_id: int, **state: Any) -> None:
    with USER_STATE_LOCK:
        state["created_at"] = time.monotonic()
        USER_STATE[user_id] = state


def _clear_state(user_id: int) -> None:
    with USER_STATE_LOCK:
        USER_STATE.pop(user_id, None)


def _workload_for(user_id: int) -> dict[str, int]:
    stored = STORAGE.get_workload(user_id)
    return {category: stored.get(category, 0) for category in WORKLOAD_CATEGORIES}


def _workload_total(user_id: int) -> int:
    return sum(_workload_for(user_id).values())


def _workload_summary(user_id: int) -> tuple[str, int]:
    workload = _workload_for(user_id)
    active = [
        f"• {WORKLOAD_CATEGORIES[category][0]}: {value}%"
        for category, value in workload.items()
        if value > 0
    ]
    total = sum(workload.values())
    return ("\n".join(active) if active else "Нет активных трудозатрат."), total


def _main_menu_text(user: User) -> tuple[str, int]:
    summary, total = _workload_summary(user.telegram_user_id)
    name = user.employee_name or "не заполнено"
    department = user.department or "не заполнено"
    profile_status = "готов" if user.profile_complete else "требует заполнения"
    text = (
        "Панель Friday Report\n\n"
        f"Сотрудник: {name}\n"
        f"Подразделение: {department}\n"
        f"Профиль: {profile_status}\n\n"
        "Текущие трудозатраты:\n"
        f"{summary}\n\n"
        f"Итого: {total}% {'✓' if total == 100 else '✗'}"
    )
    return text, total


def _main_keyboard(user: User, total: int) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=1)
    if user.profile_complete and total == 100:
        markup.add(types.InlineKeyboardButton("🚀 Отправить отчёт", callback_data="send_report"))
    elif not user.profile_complete:
        markup.add(types.InlineKeyboardButton("⚠️ Заполните профиль", callback_data="profile"))
    else:
        markup.add(types.InlineKeyboardButton(f"⚠️ Сумма: {total}%", callback_data="sum_warning"))
    markup.add(types.InlineKeyboardButton("✏️ Настроить трудозатраты", callback_data="menu_edit"))
    markup.add(types.InlineKeyboardButton("👤 Профиль", callback_data="profile"))
    markup.add(types.InlineKeyboardButton("🔄 Обновить", callback_data="menu_main"))
    if user.is_admin:
        markup.add(types.InlineKeyboardButton("🔐 Администрирование", callback_data="admin_menu"))
    return markup


def _edit_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    workload = _workload_for(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for category, (name, group) in WORKLOAD_CATEGORIES.items():
        if group == "my":
            markup.add(
                types.InlineKeyboardButton(
                    f"💻 {name} ({workload[category]}%)", callback_data=f"edit_cat:{category}"
                )
            )
    for category, (name, group) in WORKLOAD_CATEGORIES.items():
        if group == "other" and workload[category] > 0:
            markup.add(
                types.InlineKeyboardButton(
                    f"➕ {name} ({workload[category]}%)", callback_data=f"edit_cat:{category}"
                )
            )
    markup.add(types.InlineKeyboardButton("➕ Добавить из «Остального»", callback_data="add_other"))
    markup.add(types.InlineKeyboardButton("🔄 Сбросить к значениям по умолчанию", callback_data="reset_workload"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup


def _category_keyboard(category: str) -> types.InlineKeyboardMarkup:
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(
        types.InlineKeyboardButton("-10%", callback_data=f"adjust:{category}:-10"),
        types.InlineKeyboardButton("-5%", callback_data=f"adjust:{category}:-5"),
        types.InlineKeyboardButton("-1%", callback_data=f"adjust:{category}:-1"),
    )
    markup.row(
        types.InlineKeyboardButton("+1%", callback_data=f"adjust:{category}:1"),
        types.InlineKeyboardButton("+5%", callback_data=f"adjust:{category}:5"),
        types.InlineKeyboardButton("+10%", callback_data=f"adjust:{category}:10"),
    )
    markup.row(
        types.InlineKeyboardButton("В ноль", callback_data=f"adjust:{category}:zero"),
        types.InlineKeyboardButton("Точное значение", callback_data=f"set_exact:{category}"),
    )
    markup.add(types.InlineKeyboardButton("⬅️ К списку категорий", callback_data="menu_edit"))
    return markup


def _other_categories_keyboard(user_id: int) -> types.InlineKeyboardMarkup:
    workload = _workload_for(user_id)
    markup = types.InlineKeyboardMarkup(row_width=1)
    for category, (name, group) in WORKLOAD_CATEGORIES.items():
        if group == "other" and workload[category] == 0:
            markup.add(types.InlineKeyboardButton(name, callback_data=f"edit_cat:{category}"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_edit"))
    return markup


def _admin_keyboard() -> types.InlineKeyboardMarkup:
    weekly_limit = STORAGE.get_weekly_send_limit()
    pending_count = len(STORAGE.list_pending_users())
    markup = types.InlineKeyboardMarkup(row_width=2)
    markup.add(types.InlineKeyboardButton(f"Лимит отправок в неделю: {weekly_limit}", callback_data="admin_limit_info"))
    markup.row(
        types.InlineKeyboardButton("−1", callback_data="admin_limit_delta:-1"),
        types.InlineKeyboardButton("+1", callback_data="admin_limit_delta:1"),
    )
    markup.add(types.InlineKeyboardButton("Задать значение", callback_data="admin_limit_input"))
    markup.add(types.InlineKeyboardButton(f"Заявки пользователей: {pending_count}", callback_data="admin_pending"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup


def _pending_page(page: int, page_size: int = 20) -> tuple[list[User], int, int]:
    pending_users = STORAGE.list_pending_users()
    page_count = max(1, (len(pending_users) + page_size - 1) // page_size)
    page = max(0, min(page, page_count - 1))
    start = page * page_size
    return pending_users[start : start + page_size], page, page_count


def _pending_keyboard(page: int = 0) -> types.InlineKeyboardMarkup:
    pending_users, page, page_count = _pending_page(page)
    markup = types.InlineKeyboardMarkup(row_width=2)
    for pending in pending_users:
        label = _button_label(_user_label(pending))
        markup.row(
            types.InlineKeyboardButton(f"✓ {label}", callback_data=f"admin_approve:{pending.telegram_user_id}"),
            types.InlineKeyboardButton("✕", callback_data=f"admin_block:{pending.telegram_user_id}"),
        )
    if page_count > 1:
        navigation: list[types.InlineKeyboardButton] = []
        if page > 0:
            navigation.append(types.InlineKeyboardButton("⬅️", callback_data=f"admin_pending_page:{page - 1}"))
        navigation.append(types.InlineKeyboardButton(f"{page + 1}/{page_count}", callback_data="admin_pending_info"))
        if page < page_count - 1:
            navigation.append(types.InlineKeyboardButton("➡️", callback_data=f"admin_pending_page:{page + 1}"))
        markup.row(*navigation)
    markup.add(types.InlineKeyboardButton("⬅️ К настройкам", callback_data="admin_menu"))
    return markup


def _pending_text(page: int = 0) -> tuple[str, int]:
    pending_users, page, page_count = _pending_page(page)
    if not pending_users:
        return "Нет ожидающих заявок.", page
    items = "\n".join(
        f"• ID {pending.telegram_user_id}: {_user_label(pending)}" for pending in pending_users
    )
    return (
        f"Ожидающие заявки, страница {page + 1}/{page_count}. "
        "Подтверждайте только проверенные учётные записи:\n\n"
        + items,
        page,
    )


def _safe_edit(call: Any, text: str, markup: types.InlineKeyboardMarkup | None = None) -> None:
    try:
        bot.edit_message_text(text, call.message.chat.id, call.message.message_id, reply_markup=markup)
    except Exception:
        # The message may be unchanged or too old to edit.  The next command
        # will still render a fresh menu.
        pass


def _show_main(call: Any, user: User) -> None:
    _clear_state(user.telegram_user_id)
    text, total = _main_menu_text(user)
    _safe_edit(call, text, _main_keyboard(user, total))


def _start_profile(chat_id: int, user: User) -> None:
    if user.role == "blocked":
        bot.send_message(chat_id, "Доступ ограничен.")
        return
    if user.is_active and user.profile_complete and not user.is_admin:
        bot.send_message(chat_id, "Профиль уже подтверждён. Для изменения обратитесь к администратору.")
        return
    _set_state(user.telegram_user_id, action="profile_employee")
    bot.send_message(chat_id, "Введите ФИО сотрудника точно как в форме. Для отмены: /cancel")


def _profile_value_is_plausible(value: str) -> bool:
    return 1 <= len(value.strip()) <= 120 and not any(ord(character) < 32 for character in value)


@bot.message_handler(commands=["start"])
def handle_start(message: Any) -> None:
    user = _identify_message_user(message)
    if user is None:
        return
    if not user.is_active:
        _send_access_status(message.chat.id, user)
        return
    text, total = _main_menu_text(user)
    bot.send_message(message.chat.id, text, reply_markup=_main_keyboard(user, total))


@bot.message_handler(commands=["status", "menu"])
def handle_status(message: Any) -> None:
    user = _require_active_message_user(message)
    if user is None:
        return
    _clear_state(user.telegram_user_id)
    text, total = _main_menu_text(user)
    bot.send_message(message.chat.id, text, reply_markup=_main_keyboard(user, total))


@bot.message_handler(commands=["profile"])
def handle_profile(message: Any) -> None:
    user = _identify_message_user(message)
    if user is not None:
        _start_profile(message.chat.id, user)


@bot.message_handler(commands=["admin"])
def handle_admin(message: Any) -> None:
    user = _require_active_message_user(message)
    if user is None:
        return
    if not user.is_admin:
        bot.send_message(message.chat.id, "Команда доступна только администраторам.")
        return
    bot.send_message(
        message.chat.id,
        "Административные настройки. Лимит применяется отдельно к каждому пользователю и ISO-неделе.",
        reply_markup=_admin_keyboard(),
    )


@bot.message_handler(commands=["cancel"])
def handle_cancel(message: Any) -> None:
    user = _identify_message_user(message, register_unknown=False)
    if user is None:
        return
    _clear_state(user.telegram_user_id)
    bot.send_message(message.chat.id, "Действие отменено.")


@bot.message_handler(commands=["users"])
def handle_users(message: Any) -> None:
    user = _require_active_message_user(message)
    if user is None:
        return
    if not user.is_admin:
        bot.send_message(message.chat.id, "Команда доступна только администраторам.")
        return
    users = STORAGE.list_active_users()
    if not users:
        bot.send_message(message.chat.id, "Нет активных пользователей.")
        return
    lines = [f"ID {item.telegram_user_id}: {_user_label(item)} ({item.role})" for item in users]
    for start in range(0, len(lines), 40):
        prefix = "Активные пользователи:\n" if start == 0 else "Продолжение:\n"
        bot.send_message(message.chat.id, prefix + "\n".join(lines[start : start + 40]))


@bot.message_handler(commands=["block"])
def handle_block(message: Any) -> None:
    user = _require_active_message_user(message)
    if user is None:
        return
    if not user.is_admin:
        bot.send_message(message.chat.id, "Команда доступна только администраторам.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Использование: /block <telegram_user_id>")
        return
    try:
        target_user_id = int(parts[1])
        STORAGE.block_user(user.telegram_user_id, target_user_id)
    except (StorageError, ValueError):
        bot.send_message(message.chat.id, "Не удалось ограничить доступ. Проверьте ID; себя заблокировать нельзя.")
        return
    bot.send_message(message.chat.id, f"Доступ пользователя {target_user_id} ограничен.")


@bot.message_handler(commands=["setprofile"])
def handle_set_profile(message: Any) -> None:
    user = _require_active_message_user(message)
    if user is None:
        return
    if not user.is_admin:
        bot.send_message(message.chat.id, "Команда доступна только администраторам.")
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) != 2:
        bot.send_message(message.chat.id, "Использование: /setprofile <telegram_user_id>")
        return
    try:
        target_user_id = int(parts[1])
        target = STORAGE.get_user(target_user_id)
        if target is None or target.role == "blocked":
            raise ValueError
    except ValueError:
        bot.send_message(message.chat.id, "Пользователь не найден или доступ к нему уже ограничен.")
        return
    _set_state(user.telegram_user_id, action="admin_profile_employee", target_user_id=target_user_id)
    bot.send_message(message.chat.id, f"Введите ФИО для пользователя {target_user_id}. Для отмены: /cancel")


def _category_text(user_id: int, category: str) -> str:
    workload = _workload_for(user_id)
    name = WORKLOAD_CATEGORIES[category][0]
    total = sum(workload.values())
    return f"Категория: {name}\nТекущее значение: {workload[category]}%\nСумма всех категорий: {total}%"


def _handle_send_report(call: Any, user: User) -> None:
    total = _workload_total(user.telegram_user_id)
    if not user.profile_complete:
        bot.answer_callback_query(call.id, "Сначала заполните профиль.", show_alert=True)
        return
    if total != 100:
        bot.answer_callback_query(call.id, "Сумма трудозатрат должна быть ровно 100%.", show_alert=True)
        return
    snapshot = {
        "employee_name": user.employee_name,
        "department": user.department,
        "workload": _workload_for(user.telegram_user_id),
    }
    consume_quota = not SETTINGS["debug"]
    try:
        reservation = STORAGE.reserve_report_run(
            user.telegram_user_id,
            snapshot,
            datetime.now(SETTINGS["business_timezone"]),
            consume_quota=consume_quota,
        )
    except WeeklyLimitReached as error:
        bot.answer_callback_query(
            call.id,
            f"Лимит отправок на эту неделю исчерпан ({error.limit}).",
            show_alert=True,
        )
        return
    except ReportInProgress:
        bot.answer_callback_query(call.id, "Ваш предыдущий отчёт ещё находится в очереди или выполняется.", show_alert=True)
        return
    except StorageError:
        bot.answer_callback_query(call.id, "Не удалось создать задание. Попробуйте ещё раз.", show_alert=True)
        return

    if not REPORT_QUEUE.acquire(blocking=False):
        STORAGE.finish_report_run(reservation.run_id, "failed", "Report queue is full before launch")
        bot.answer_callback_query(call.id, "Очередь занята. Повторите попытку позже.", show_alert=True)
        return

    try:
        REPORT_EXECUTOR.submit(
            _run_report_job,
            user,
            call.message.message_id,
            reservation.run_id,
            snapshot,
        )
    except Exception:
        REPORT_QUEUE.release()
        STORAGE.finish_report_run(reservation.run_id, "failed", "Could not submit report job")
        bot.answer_callback_query(call.id, "Не удалось поставить отчёт в очередь.", show_alert=True)
        return

    debug_note = " В режиме отладки лимит не расходуется." if SETTINGS["debug"] else ""
    bot.answer_callback_query(call.id, f"Отчёт поставлен в очередь.{debug_note}")


def _progress_text(steps: dict[str, str], final: str | None = None) -> str:
    names = {
        "browser": "Инициализация браузера",
        "form": "Загрузка формы",
        "profile": "Заполнение профиля",
        "workload": "Заполнение трудозатрат",
        "submit": "Отправка отчёта",
    }
    icons = {"pending": "⌛", "running": "🔄", "done": "✓", "failed": "✗"}
    lines = ["Отчёт в процессе:", ""]
    for key in ("browser", "form", "profile", "workload", "submit"):
        lines.append(f"{icons[steps[key]]} {names[key]}")
    if final:
        lines.extend(["", final])
    return "\n".join(lines)


def _reader_thread(stream: Any, output: queue.Queue[str | None]) -> None:
    try:
        for line in iter(stream.readline, ""):
            output.put(line)
    finally:
        output.put(None)


def _send_result_screenshots(chat_id: int, run_dir: Path) -> None:
    images = sorted(run_dir.glob("*.png"))
    if not images:
        return
    try:
        bot.send_message(chat_id, "Скриншоты выполнения отчёта:")
    except Exception:
        return
    media: list[types.InputMediaPhoto] = []
    for image in images:
        try:
            media.append(types.InputMediaPhoto(image.read_bytes(), caption=image.stem.replace("_", " ").title()))
        except OSError:
            continue
    for index in range(0, len(media), 10):
        try:
            bot.send_media_group(chat_id, media[index : index + 10])
        except Exception:
            pass


def _remove_run_directory(run_dir: Path) -> None:
    try:
        reports_root = REPORTS_DIR.resolve()
        resolved_run_dir = run_dir.resolve()
        resolved_run_dir.relative_to(reports_root)
        if resolved_run_dir != reports_root:
            shutil.rmtree(resolved_run_dir, ignore_errors=True)
    except (OSError, ValueError):
        pass


def _run_report_job(user: User, message_id: int, run_id: str, snapshot: dict[str, Any]) -> None:
    run_dir = REPORTS_DIR / run_id
    proc: subprocess.Popen[str] | None = None
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
        profile_path = run_dir / "profile.json"
        profile_path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        STORAGE.mark_run_running(run_id)

        steps = {key: "pending" for key in ("browser", "form", "profile", "workload", "submit")}
        steps["browser"] = "running"
        try:
            bot.edit_message_text(_progress_text(steps), user.chat_id, message_id)
        except Exception:
            pass

        proc = subprocess.Popen(
            [
                sys.executable,
                str(BASE_DIR / "friday_report.py"),
                "--profile-file",
                str(profile_path),
                "--output-dir",
                str(run_dir),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        output: queue.Queue[str | None] = queue.Queue()
        reader = threading.Thread(target=_reader_thread, args=(proc.stdout, output), daemon=True)
        reader.start()
        event_to_step = {
            "browser_ready": ("browser",),
            "form_loaded": ("browser", "form"),
            "profile_filled": ("form", "profile"),
            "workload_filled": ("profile", "workload"),
            "form_submitted": ("workload", "submit"),
        }
        saw_submission = False
        saw_dry_run = False
        timed_out = False
        stream_finished = False
        deadline = time.monotonic() + SETTINGS["report_timeout"]

        while not stream_finished or proc.poll() is None:
            if time.monotonic() > deadline:
                timed_out = True
                proc.kill()
                break
            try:
                line = output.get(timeout=0.5)
            except queue.Empty:
                continue
            if line is None:
                stream_finished = True
                continue
            if not line.startswith("PROGRESS:"):
                continue
            event = line.strip().partition(":")[2]
            if event == "dry_run":
                saw_dry_run = True
            if event == "form_submitted":
                saw_submission = True
            changed = False
            for step in event_to_step.get(event, ()):
                if step == event_to_step.get(event, ())[-1]:
                    new_status = "running"
                else:
                    new_status = "done"
                if steps[step] != new_status:
                    steps[step] = new_status
                    changed = True
            if changed:
                try:
                    bot.edit_message_text(_progress_text(steps), user.chat_id, message_id)
                except Exception:
                    pass

        if timed_out:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        else:
            proc.wait()

        if proc.returncode == 0:
            for step in steps:
                steps[step] = "done"
            if SETTINGS["debug"] or saw_dry_run:
                STORAGE.finish_report_run(run_id, "dry_run")
                final_message = "✓ Проверочный прогон завершён. Режим отладки: форма не была отправлена."
            else:
                STORAGE.finish_report_run(run_id, "submitted")
                final_message = "✓ Отчёт успешно заполнен и отправлен."
            try:
                bot.edit_message_text(_progress_text(steps, final_message), user.chat_id, message_id)
            except Exception:
                pass
            _send_result_screenshots(user.chat_id, run_dir)
        else:
            for step, status in steps.items():
                if status == "running":
                    steps[step] = "failed"
            if saw_submission:
                STORAGE.finish_report_run(run_id, "unknown", "Process failed after form submission marker")
                final_message = "⚠️ Статус отправки не подтверждён. Повторная отправка заблокирована, чтобы не создать дубликат; обратитесь к администратору."
            else:
                reason = "Report timeout" if timed_out else "Report process failed before submission"
                STORAGE.finish_report_run(run_id, "failed", reason)
                final_message = "✗ Отчёт не был отправлен. Проверьте настройки и повторите попытку."
            try:
                bot.edit_message_text(_progress_text(steps, final_message), user.chat_id, message_id)
            except Exception:
                pass
    except Exception:
        try:
            STORAGE.finish_report_run(run_id, "failed", "Unhandled report worker error")
        except Exception:
            pass
        try:
            bot.edit_message_text(
                "✗ Не удалось выполнить отчёт. Данные ошибки не отправлены в чат; повторите попытку или обратитесь к администратору.",
                user.chat_id,
                message_id,
            )
        except Exception:
            pass
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        _remove_run_directory(run_dir)
        REPORT_QUEUE.release()


def _send_friday_reminder(user: User) -> None:
    summary, total = _workload_summary(user.telegram_user_id)
    profile_note = "" if user.profile_complete else "\n\nСначала заполните профиль через /profile."
    text = (
        "🔔 Напоминание: время подготовить пятничный отчёт.\n\n"
        f"Текущие трудозатраты:\n{summary}\n\n"
        f"Итого: {total}%"
        f"{profile_note}"
    )
    try:
        bot.send_message(user.chat_id, text, reply_markup=_main_keyboard(user, total))
    except Exception:
        pass


def _run_scheduler() -> None:
    timezone = SETTINGS["business_timezone"]
    while True:
        try:
            now = datetime.now(timezone)
            if now.weekday() == 4 and (now.hour, now.minute) >= (17, 50):
                reminder_date = now.date().isoformat()
                for user in STORAGE.list_active_users():
                    if STORAGE.claim_reminder_delivery(user.telegram_user_id, reminder_date):
                        _send_friday_reminder(user)
                        time.sleep(0.05)
        except Exception:
            # The next scheduler loop retries only users that have not had a
            # reminder claimed, which prevents a restart from broadcasting again.
            pass
        time.sleep(30)


@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call: Any) -> None:
    user = _identify_callback_user(call)
    if user is None:
        return
    data = call.data or ""

    if data == "profile":
        bot.answer_callback_query(call.id)
        _start_profile(call.message.chat.id, user)
        return
    if not user.is_active:
        bot.answer_callback_query(call.id, "Заявка ещё не подтверждена.", show_alert=True)
        return

    if data == "menu_main":
        _show_main(call, user)
        bot.answer_callback_query(call.id)
        return
    if data == "menu_edit":
        _clear_state(user.telegram_user_id)
        total = _workload_total(user.telegram_user_id)
        _safe_edit(
            call,
            f"Настройка трудозатрат. Текущая сумма: {total}% {'✓' if total == 100 else '✗'}",
            _edit_keyboard(user.telegram_user_id),
        )
        bot.answer_callback_query(call.id)
        return
    if data == "add_other":
        _safe_edit(call, "Выберите дополнительную категорию:", _other_categories_keyboard(user.telegram_user_id))
        bot.answer_callback_query(call.id)
        return
    if data == "reset_workload":
        STORAGE.replace_workload(user.telegram_user_id, SETTINGS["default_workload"])
        _safe_edit(call, "Трудозатраты сброшены к значениям по умолчанию.", _edit_keyboard(user.telegram_user_id))
        bot.answer_callback_query(call.id)
        return
    if data == "sum_warning":
        bot.answer_callback_query(call.id, "Сумма трудозатрат должна быть ровно 100%.", show_alert=True)
        return
    if data == "send_report":
        _handle_send_report(call, user)
        return

    if data.startswith("edit_cat:"):
        category = data.partition(":")[2]
        if category not in WORKLOAD_CATEGORIES:
            bot.answer_callback_query(call.id, "Категория не найдена.", show_alert=True)
            return
        _clear_state(user.telegram_user_id)
        _safe_edit(call, _category_text(user.telegram_user_id, category), _category_keyboard(category))
        bot.answer_callback_query(call.id)
        return
    if data.startswith("adjust:"):
        _, category, delta_text = data.split(":", 2)
        if category not in WORKLOAD_CATEGORIES:
            bot.answer_callback_query(call.id, "Категория не найдена.", show_alert=True)
            return
        current = _workload_for(user.telegram_user_id)[category]
        if delta_text == "zero":
            value = 0
        else:
            try:
                value = max(0, min(100, current + int(delta_text)))
            except ValueError:
                bot.answer_callback_query(call.id, "Некорректное изменение.", show_alert=True)
                return
        STORAGE.set_workload_value(user.telegram_user_id, category, value)
        _safe_edit(call, _category_text(user.telegram_user_id, category), _category_keyboard(category))
        bot.answer_callback_query(call.id, f"Значение: {value}%")
        return
    if data.startswith("set_exact:"):
        category = data.partition(":")[2]
        if category not in WORKLOAD_CATEGORIES:
            bot.answer_callback_query(call.id, "Категория не найдена.", show_alert=True)
            return
        _set_state(
            user.telegram_user_id,
            action="workload_exact",
            category=category,
            menu_message_id=call.message.message_id,
        )
        bot.send_message(call.message.chat.id, "Введите целое число от 0 до 100. Для отмены: /cancel")
        bot.answer_callback_query(call.id)
        return

    if data == "admin_menu":
        if not _require_admin(user, call):
            return
        _safe_edit(
            call,
            "Административные настройки. Недельный лимит действует для каждого пользователя отдельно.",
            _admin_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return
    if data == "admin_limit_info":
        if _require_admin(user, call):
            bot.answer_callback_query(call.id, "Лимит учитывает подтверждённые и неопределённые отправки в текущей ISO-неделе.", show_alert=True)
        return
    if data.startswith("admin_limit_delta:"):
        if not _require_admin(user, call):
            return
        try:
            updated_limit = STORAGE.set_weekly_send_limit(
                user.telegram_user_id,
                STORAGE.get_weekly_send_limit() + int(data.partition(":")[2]),
            )
        except (ValueError, InvalidSetting):
            bot.answer_callback_query(call.id, "Лимит должен быть от 1 до 52.", show_alert=True)
            return
        _safe_edit(call, f"Недельный лимит обновлён: {updated_limit}.", _admin_keyboard())
        bot.answer_callback_query(call.id)
        return
    if data == "admin_limit_input":
        if not _require_admin(user, call):
            return
        _set_state(user.telegram_user_id, action="weekly_limit")
        bot.send_message(call.message.chat.id, "Введите лимит отправок на одного пользователя в неделю (от 1 до 52).")
        bot.answer_callback_query(call.id)
        return
    if data == "admin_pending":
        if not _require_admin(user, call):
            return
        text, page = _pending_text()
        _safe_edit(call, text, _pending_keyboard(page))
        bot.answer_callback_query(call.id)
        return
    if data.startswith("admin_pending_page:"):
        if not _require_admin(user, call):
            return
        try:
            requested_page = int(data.partition(":")[2])
        except ValueError:
            bot.answer_callback_query(call.id, "Некорректная страница.", show_alert=True)
            return
        text, page = _pending_text(requested_page)
        _safe_edit(call, text, _pending_keyboard(page))
        bot.answer_callback_query(call.id)
        return
    if data == "admin_pending_info":
        if _require_admin(user, call):
            bot.answer_callback_query(call.id, "Страница списка заявок.")
        return
    if data.startswith("admin_approve:"):
        if not _require_admin(user, call):
            return
        try:
            target_id = int(data.partition(":")[2])
            approved = STORAGE.approve_user(user.telegram_user_id, target_id, SETTINGS["default_workload"])
        except ProfileIncomplete:
            bot.answer_callback_query(call.id, "Пользователь должен сначала заполнить ФИО и подразделение через /profile.", show_alert=True)
            return
        except (StorageError, ValueError):
            bot.answer_callback_query(call.id, "Не удалось подтвердить заявку.", show_alert=True)
            return
        _safe_edit(call, f"Пользователь {_user_label(approved)} подтверждён.", _pending_keyboard())
        try:
            bot.send_message(approved.chat_id, "Ваша заявка подтверждена. Откройте /start для работы с ботом.")
        except Exception:
            pass
        bot.answer_callback_query(call.id)
        return
    if data.startswith("admin_block:"):
        if not _require_admin(user, call):
            return
        try:
            target_id = int(data.partition(":")[2])
            STORAGE.block_user(user.telegram_user_id, target_id)
        except (StorageError, ValueError):
            bot.answer_callback_query(call.id, "Не удалось отклонить заявку.", show_alert=True)
            return
        _safe_edit(call, "Заявка отклонена.", _pending_keyboard())
        bot.answer_callback_query(call.id)
        return

    bot.answer_callback_query(call.id, "Кнопка устарела. Откройте /menu.", show_alert=True)


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_text(message: Any) -> None:
    user = _identify_message_user(message)
    if user is None:
        return
    state = _get_state(user.telegram_user_id)
    if state is None:
        if user.is_active:
            text, total = _main_menu_text(user)
            bot.send_message(message.chat.id, text, reply_markup=_main_keyboard(user, total))
        else:
            _send_access_status(message.chat.id, user)
        return

    value = (message.text or "").strip()
    action = state.get("action")
    if action == "admin_profile_employee":
        if not user.is_admin:
            _clear_state(user.telegram_user_id)
            bot.send_message(message.chat.id, "Доступ к настройке ограничен.")
            return
        if not _profile_value_is_plausible(value):
            bot.send_message(message.chat.id, "Введите ФИО длиной от 1 до 120 символов без управляющих символов.")
            return
        _set_state(
            user.telegram_user_id,
            action="admin_profile_department",
            target_user_id=state["target_user_id"],
            employee_name=value,
        )
        bot.send_message(message.chat.id, "Введите подразделение для этого пользователя. Для отмены: /cancel")
        return
    if action == "admin_profile_department":
        if not user.is_admin:
            _clear_state(user.telegram_user_id)
            bot.send_message(message.chat.id, "Доступ к настройке ограничен.")
            return
        if not _profile_value_is_plausible(value):
            bot.send_message(message.chat.id, "Введите подразделение длиной от 1 до 120 символов без управляющих символов.")
            return
        try:
            updated = STORAGE.set_profile_for_user(
                user.telegram_user_id,
                int(state["target_user_id"]),
                str(state["employee_name"]),
                value,
            )
        except (StorageError, ValueError):
            bot.send_message(message.chat.id, "Не удалось обновить профиль пользователя.")
            return
        _clear_state(user.telegram_user_id)
        bot.send_message(message.chat.id, f"Профиль пользователя {updated.telegram_user_id} обновлён.")
        return
    if action == "profile_employee":
        if not _profile_value_is_plausible(value):
            bot.send_message(message.chat.id, "Введите ФИО длиной от 1 до 120 символов без управляющих символов.")
            return
        _set_state(user.telegram_user_id, action="profile_department", employee_name=value)
        bot.send_message(message.chat.id, "Введите подразделение точно как в форме. Для отмены: /cancel")
        return
    if action == "profile_department":
        if not _profile_value_is_plausible(value):
            bot.send_message(message.chat.id, "Введите подразделение длиной от 1 до 120 символов без управляющих символов.")
            return
        try:
            updated = STORAGE.set_profile(user.telegram_user_id, str(state["employee_name"]), value)
        except (StorageError, ValueError):
            bot.send_message(message.chat.id, "Не удалось сохранить профиль. Попробуйте ещё раз.")
            return
        _clear_state(user.telegram_user_id)
        if updated.is_active:
            text, total = _main_menu_text(updated)
            bot.send_message(message.chat.id, "Профиль сохранён.", reply_markup=_main_keyboard(updated, total))
        else:
            bot.send_message(message.chat.id, "Профиль сохранён. Теперь дождитесь подтверждения администратора.")
            _notify_admins_about_pending(updated)
        return
    if action == "workload_exact":
        category = str(state.get("category", ""))
        try:
            percentage = int(value)
            if category not in WORKLOAD_CATEGORIES or not 0 <= percentage <= 100:
                raise ValueError
            STORAGE.set_workload_value(user.telegram_user_id, category, percentage)
        except (StorageError, ValueError):
            bot.send_message(message.chat.id, "Введите целое число от 0 до 100.")
            return
        _clear_state(user.telegram_user_id)
        try:
            bot.edit_message_text(
                _category_text(user.telegram_user_id, category),
                message.chat.id,
                int(state["menu_message_id"]),
                reply_markup=_category_keyboard(category),
            )
        except Exception:
            pass
        return
    if action == "weekly_limit":
        if not user.is_admin:
            _clear_state(user.telegram_user_id)
            bot.send_message(message.chat.id, "Доступ к настройке ограничен.")
            return
        try:
            limit = STORAGE.set_weekly_send_limit(user.telegram_user_id, int(value))
        except (ValueError, InvalidSetting):
            bot.send_message(message.chat.id, "Введите целое число от 1 до 52.")
            return
        _clear_state(user.telegram_user_id)
        bot.send_message(message.chat.id, f"Недельный лимит обновлён: {limit}.", reply_markup=_admin_keyboard())
        return

    _clear_state(user.telegram_user_id)
    bot.send_message(message.chat.id, "Состояние устарело. Откройте /menu.")


def main() -> None:
    scheduler_thread = threading.Thread(target=_run_scheduler, name="friday-reminders", daemon=True)
    scheduler_thread.start()
    print("Telegram-бот Friday Report запущен.")
    try:
        bot.infinity_polling(skip_pending=True, timeout=20, long_polling_timeout=20)
    finally:
        REPORT_EXECUTOR.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
