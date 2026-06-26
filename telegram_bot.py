import os
import re
import sys
import json
import time
import subprocess
import threading
from datetime import datetime
import pytz
import tomllib
import telebot
from telebot import types

# Force UTF-8 encoding for standard output and error on Windows to prevent encoding errors
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

# --- НАСТРОЙКИ ПУТЕЙ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")
WORKLOAD_PATH = os.path.join(BASE_DIR, "workload.json")
STATE_FILE = os.path.join(BASE_DIR, "bot_state.json")

# Дефолтные трудозатраты (для сброса)
DEFAULT_WORKLOAD = {
    "SITES_ADMIN": 8,
    "SITES_DEVELOPMENT": 82,
    "REPORTING": 10,
    "DOCUMENTATION": 0,
    "SITES_TECH_SUPPORT": 0,
    "BUGS_PROCESSING": 0,
    "CLIENT_SUPPORT": 0,
    "INTERNAL_SUPPORT": 0,
    "WORKAROUNDS": 0,
    "INFRASTRUCTURE": 0,
    "BACKUPS": 0,
    "INTERNAL_TRAINING": 0,
    "EXTERNAL_TRAINING": 0,
    "ANALYTICS": 0,
    "TASK_TRACKER": 0,
    "QUALITY_CONTROL": 0,
    "MANAGEMENT": 0,
    "CRM_DEVELOPMENT": 0,
    "NAUMEN_ADMIN": 0
}

USER_STATE = {}

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---

def load_telegram_token():
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
            return cfg.get("telegram", {}).get("token", "")
    except Exception as e:
        print(f"Ошибка загрузки токена из config.toml: {e}")
        return ""

def load_allowed_chats():
    chats = set()
    # 1. Из config.toml
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
            config_chats = cfg.get("telegram", {}).get("allowed_chat_ids", [])
            for c in config_chats:
                chats.add(int(c))
    except Exception as e:
        print(f"Ошибка загрузки config.toml: {e}")
        
    # 2. Из bot_state.json
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                state_chats = state.get("allowed_chat_ids", [])
                for c in state_chats:
                    chats.add(int(c))
        except Exception as e:
            print(f"Ошибка загрузки bot_state.json: {e}")
            
    return list(chats)

def save_allowed_chat(chat_id):
    chats = set(load_allowed_chats())
    chats.add(int(chat_id))
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"allowed_chat_ids": list(chats)}, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка сохранения bot_state.json: {e}")

def clean_category_name(name):
    if not name:
        return ""
    name = name.strip().rstrip(",")
    open_count = name.count("(")
    close_count = name.count(")")
    if open_count > close_count:
        name += ")"
    return name

def parse_workload_details(filepath=None):
    try:
        with open(WORKLOAD_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Ошибка загрузки workload.json: {e}")
        return {}

def update_config_value(key, value):
    try:
        with open(WORKLOAD_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if key in data:
            data[key]["value"] = value
        with open(WORKLOAD_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Ошибка обновления workload.json для {key}: {e}")

def get_workload_summary_and_sum():
    details = parse_workload_details(WORKLOAD_PATH)
    active_lines = []
    total_sum = 0
    for key, data in details.items():
        val = data["value"]
        name = data["name"]
        total_sum += val
        if val > 0:
            active_lines.append(f"- **{name}**: {val}%")
            
    if not active_lines:
        summary = "⚠️ _Нет активных трудозатрат_"
    else:
        summary = "\n".join(active_lines)
        
    return summary, total_sum

def get_main_menu_text():
    # Загружаем настройки
    try:
        with open(CONFIG_PATH, "rb") as f:
            cfg = tomllib.load(f)
            employee_name = cfg.get("employee_name", "Не указан")
            department = cfg.get("department", "Не указан")
            browser_mode = cfg.get("browser_mode", "local")
            debug = cfg.get("debug", True)
    except Exception as e:
        print(f"Ошибка загрузки общих настроек: {e}")
        employee_name = "Ошибка"
        department = "Ошибка"
        browser_mode = "local"
        debug = True

    summary, total_sum = get_workload_summary_and_sum()
    
    debug_status = "⚠️ Вкл (отправка отключена)" if debug else "🟢 Выкл (отчет БУДЕТ отправлен!)"
    browser_status = "🌐 Remote (Browserless.io)" if browser_mode == "remote" else "💻 Local (Локальный браузер)"
    
    text = (
        "📊 **Панель управления пятничным отчётом**\n\n"
        f"👤 **Сотрудник:** {employee_name}\n"
        f"🏢 **Подразделение:** {department}\n"
        f"🔧 **Режим браузера:** {browser_status}\n"
        f"⚙️ **Режим отладки (debug):** {debug_status}\n\n"
        "📅 **Текущее распределение (в сумме должно быть 100%):**\n"
        f"{summary}\n\n"
        f"Итого сумма: **{total_sum}%** " + ("✅" if total_sum == 100 else "❌") + "\n"
    )
    if total_sum != 100:
        text += "\n⚠️ *Внимание: Сумма трудозатрат должна быть ровно 100% для отправки!*"
        
    return text, total_sum

def clean_screenshots():
    verified_dir = os.path.join(BASE_DIR, "verified")
    if os.path.exists(verified_dir):
        for f in os.listdir(verified_dir):
            if f.endswith(".png"):
                try:
                    os.remove(os.path.join(verified_dir, f))
                except Exception as e:
                    print(f"Ошибка удаления скриншота {f}: {e}")

# --- АВТОРИЗАЦИЯ ДЕКОРАТОРЫ ---

def authorized_only(func):
    def wrapper(message, *args, **kwargs):
        chat_id = message.chat.id
        allowed = load_allowed_chats()
        if not allowed:
            save_allowed_chat(chat_id)
            allowed = [chat_id]
            
        if chat_id in allowed:
            return func(message, *args, **kwargs)
        else:
            bot.send_message(chat_id, "🚫 Доступ ограничен. Вы не являетесь авторизованным пользователем.")
    return wrapper

def authorized_only_callback(func):
    def wrapper(call, *args, **kwargs):
        chat_id = call.message.chat.id
        allowed = load_allowed_chats()
        if chat_id in allowed:
            return func(call, *args, **kwargs)
        else:
            bot.answer_callback_query(call.id, "🚫 Доступ ограничен.", show_alert=True)
    return wrapper

# --- КЛАВИАТУРЫ ---

def make_main_keyboard(total_sum):
    markup = types.InlineKeyboardMarkup()
    if total_sum == 100:
        markup.add(types.InlineKeyboardButton("🚀 Отправить отчёт", callback_data="send_report"))
    else:
        markup.add(types.InlineKeyboardButton(f"⚠️ Нельзя отправить (Сумма {total_sum}%)", callback_data="sum_warning"))
    markup.add(types.InlineKeyboardButton("✏️ Настроить трудозатраты", callback_data="menu_edit"))
    markup.add(types.InlineKeyboardButton("🔄 Обновить статус", callback_data="menu_main"))
    return markup

def make_edit_keyboard(details):
    markup = types.InlineKeyboardMarkup(row_width=1)
    
    # 1. Мои трудозатраты (показываем всегда)
    for key, data in details.items():
        if data["group"] == "my":
            markup.add(types.InlineKeyboardButton(f"💻 {data['name']} ({data['value']}%)", callback_data=f"edit_cat:{key}"))
            
    # 2. Активные из Остального (>0)
    for key, data in details.items():
        if data["group"] == "other" and data["value"] > 0:
            markup.add(types.InlineKeyboardButton(f"➕ {data['name']} ({data['value']}%)", callback_data=f"edit_cat:{key}"))
            
    markup.add(types.InlineKeyboardButton("➕ Добавить из 'Остальное'", callback_data="add_other_menu"))
    markup.add(types.InlineKeyboardButton("🔄 Сбросить к стандартным", callback_data="reset_default"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_main"))
    return markup

def make_cat_edit_keyboard(key):
    markup = types.InlineKeyboardMarkup(row_width=3)
    markup.row(
        types.InlineKeyboardButton("-10%", callback_data=f"adjust:{key}:-10"),
        types.InlineKeyboardButton("-5%", callback_data=f"adjust:{key}:-5"),
        types.InlineKeyboardButton("-1%", callback_data=f"adjust:{key}:-1")
    )
    markup.row(
        types.InlineKeyboardButton("+1%", callback_data=f"adjust:{key}:1"),
        types.InlineKeyboardButton("+5%", callback_data=f"adjust:{key}:5"),
        types.InlineKeyboardButton("+10%", callback_data=f"adjust:{key}:10")
    )
    markup.row(
        types.InlineKeyboardButton("🗑️ В ноль (0%)", callback_data=f"adjust:{key}:zero"),
        types.InlineKeyboardButton("✏️ Точное значение", callback_data=f"set_exact:{key}")
    )
    markup.row(
        types.InlineKeyboardButton("⬅️ К списку категорий", callback_data="menu_edit")
    )
    return markup

def make_add_other_keyboard(details):
    markup = types.InlineKeyboardMarkup(row_width=1)
    for key, data in details.items():
        if data["group"] == "other" and data["value"] == 0:
            markup.add(types.InlineKeyboardButton(data["name"], callback_data=f"edit_cat:{key}"))
    markup.add(types.InlineKeyboardButton("⬅️ Назад", callback_data="menu_edit"))
    return markup

# --- ХЕНДЛЕРЫ ---

token = load_telegram_token()
if not token:
    print("❌ КРИТИЧЕСКАЯ ОШИБКА: Токен Telegram не найден в config.toml!")
    sys.exit(1)

bot = telebot.TeleBot(token)

def clear_user_state(chat_id):
    state = USER_STATE.get(chat_id)
    if state:
        if "prompt_msg_id" in state:
            try:
                bot.delete_message(chat_id, state["prompt_msg_id"])
            except Exception:
                pass
        USER_STATE.pop(chat_id, None)

@bot.message_handler(commands=["start"])
def handle_start(message):
    chat_id = message.chat.id
    allowed = load_allowed_chats()
    
    is_new = False
    if not allowed:
        save_allowed_chat(chat_id)
        allowed = [chat_id]
        is_new = True
        
    if chat_id in allowed:
        text, total_sum = get_main_menu_text()
        welcome = "👋 **Добро пожаловать в Friday Report Bot!**\n\n"
        if is_new:
            welcome = "🆕 **Вы автоматически зарегистрированы как владелец бота!**\n\n" + welcome
        bot.send_message(chat_id, welcome + text, reply_markup=make_main_keyboard(total_sum), parse_mode="Markdown")
    else:
        bot.send_message(chat_id, "🚫 Доступ ограничен. Вы не являетесь авторизованным пользователем.")

@bot.message_handler(commands=["status", "menu"])
@authorized_only
def handle_status_command(message):
    chat_id = message.chat.id
    clear_user_state(chat_id)
    text, total_sum = get_main_menu_text()
    bot.send_message(chat_id, text, reply_markup=make_main_keyboard(total_sum), parse_mode="Markdown")

# --- КОЛБЭКИ ---

@bot.callback_query_handler(func=lambda call: call.data == "menu_main")
@authorized_only_callback
def handle_menu_main(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    text, total_sum = get_main_menu_text()
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_main_keyboard(total_sum), parse_mode="Markdown")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "menu_edit")
@authorized_only_callback
def handle_menu_edit(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    details = parse_workload_details(WORKLOAD_PATH)
    total_sum = sum(d["value"] for d in details.values())
    text = (
        "✏️ **Настройка трудозатрат**\n"
        "Выберите категорию для изменения или добавьте дополнительные.\n\n"
        f"Текущая сумма: **{total_sum}%** " + ("✅" if total_sum == 100 else "❌")
    )
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_edit_keyboard(details), parse_mode="Markdown")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_cat:"))
@authorized_only_callback
def handle_edit_cat(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    _, key = call.data.split(":")
    details = parse_workload_details(WORKLOAD_PATH)
    if key not in details:
        bot.answer_callback_query(call.id, "Категория не найдена.")
        return
        
    cat_name = details[key]["name"]
    val = details[key]["value"]
    total_sum = sum(d["value"] for d in details.values())
    
    text = (
        f"⚙️ **Категория:** {cat_name}\n"
        f"Текущее значение: **{val}%**\n"
        f"Сумма всех трудозатрат: **{total_sum}%**\n\n"
        "Измените процент с помощью кнопок или введите число в чат:"
    )
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_cat_edit_keyboard(key), parse_mode="Markdown")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "add_other_menu")
@authorized_only_callback
def handle_add_other_menu(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    details = parse_workload_details(WORKLOAD_PATH)
    text = (
        "➕ **Добавление категорий из раздела 'Остальное'**\n"
        "Выберите категорию ниже, чтобы настроить её процент:"
    )
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_add_other_keyboard(details), parse_mode="Markdown")
    except Exception:
        pass
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "reset_default")
@authorized_only_callback
def handle_reset_default(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    
    for key, val in DEFAULT_WORKLOAD.items():
        update_config_value(key, val)
        
    details = parse_workload_details(WORKLOAD_PATH)
    text = (
        "✏️ **Настройка трудозатрат**\n"
        "Сброшено к стандартным настройкам!\n\n"
        "Текущая сумма: **100%** ✅"
    )
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_edit_keyboard(details), parse_mode="Markdown")
    except Exception:
        pass
    bot.answer_callback_query(call.id, "Трудозатраты сброшены к стандартным", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "sum_warning")
@authorized_only_callback
def handle_sum_warning(call):
    bot.answer_callback_query(call.id, "Сумма трудозатрат должна быть ровно 100%! Пожалуйста, отрегулируйте проценты.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("adjust:"))
@authorized_only_callback
def handle_adjust(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    
    _, key, delta_str = call.data.split(":")
    details = parse_workload_details(WORKLOAD_PATH)
    if key not in details:
        bot.answer_callback_query(call.id, "Категория не найдена.")
        return
        
    current_val = details[key]["value"]
    
    if delta_str == "zero":
        new_val = 0
    else:
        try:
            delta = int(delta_str)
            new_val = current_val + delta
            if new_val < 0:
                new_val = 0
            elif new_val > 100:
                new_val = 100
        except ValueError:
            new_val = current_val
            
    if new_val != current_val:
        update_config_value(key, new_val)
        details = parse_workload_details(WORKLOAD_PATH)
        
    cat_name = details[key]["name"]
    total_sum = sum(d["value"] for d in details.values())
    
    text = (
        f"⚙️ **Категория:** {cat_name}\n"
        f"Текущее значение: **{new_val}%**\n"
        f"Сумма всех трудозатрат: **{total_sum}%**\n\n"
        "Измените процент с помощью кнопок или введите число в чат:"
    )
    try:
        bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=make_cat_edit_keyboard(key), parse_mode="Markdown")
        bot.answer_callback_query(call.id, f"Значение изменено на {new_val}%")
    except Exception:
        bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("set_exact:"))
@authorized_only_callback
def handle_set_exact_callback(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    
    _, key = call.data.split(":")
    details = parse_workload_details(WORKLOAD_PATH)
    if key not in details:
        bot.answer_callback_query(call.id, "Категория не найдена.")
        return
        
    cat_name = details[key]["name"]
    
    prompt = bot.send_message(
        chat_id,
        f"✏️ Введите целое число от 0 до 100 для категории *'{cat_name}'*:",
        parse_mode="Markdown"
    )
    
    USER_STATE[chat_id] = {
        "action": "wait_exact",
        "key": key,
        "menu_msg_id": call.message.message_id,
        "prompt_msg_id": prompt.message_id
    }
    bot.answer_callback_query(call.id)

@bot.callback_query_handler(func=lambda call: call.data == "send_report")
@authorized_only_callback
def handle_send_report(call):
    chat_id = call.message.chat.id
    clear_user_state(chat_id)
    
    _, total_sum = get_workload_summary_and_sum()
    if total_sum != 100:
        bot.answer_callback_query(call.id, "Сумма трудозатрат изменилась и не равна 100%!", show_alert=True)
        return
        
    bot.answer_callback_query(call.id, "Запуск отправки...")
    
    threading.Thread(
        target=run_report_script,
        args=(bot, chat_id, call.message.message_id),
        daemon=True
    ).start()

# --- ТЕКСТОВЫЕ СООБЩЕНИЯ ---

@bot.message_handler(func=lambda message: True)
@authorized_only
def handle_text_messages(message):
    chat_id = message.chat.id
    state = USER_STATE.get(chat_id)
    
    if state and state.get("action") == "wait_exact":
        key = state["key"]
        text = message.text.strip()
        
        try:
            bot.delete_message(chat_id, message.message_id)
        except Exception:
            pass
            
        try:
            bot.delete_message(chat_id, state["prompt_msg_id"])
        except Exception:
            pass
            
        try:
            val = int(text)
            if 0 <= val <= 100:
                update_config_value(key, val)
                
                details = parse_workload_details(WORKLOAD_PATH)
                cat_name = details[key]["name"]
                total_sum = sum(d["value"] for d in details.values())
                
                new_text = (
                    f"⚙️ **Категория:** {cat_name}\n"
                    f"Текущее значение: **{val}%**\n"
                    f"Сумма всех трудозатрат: **{total_sum}%**\n\n"
                    "Измените процент с помощью кнопок или введите число в чат:"
                )
                
                bot.edit_message_text(
                    new_text, 
                    chat_id, 
                    state["menu_msg_id"], 
                    reply_markup=make_cat_edit_keyboard(key),
                    parse_mode="Markdown"
                )
                USER_STATE.pop(chat_id, None)
            else:
                raise ValueError()
        except ValueError:
            details = parse_workload_details(WORKLOAD_PATH)
            cat_name = details[key]["name"]
            prompt = bot.send_message(
                chat_id,
                f"❌ **Некорректное значение!**\nПожалуйста, введите целое число от 0 до 100 для категории *'{cat_name}'*.\nДля отмены нажмите кнопку 'Назад' в меню выше.",
                parse_mode="Markdown"
            )
            USER_STATE[chat_id]["prompt_msg_id"] = prompt.message_id
    else:
        text, total_sum = get_main_menu_text()
        bot.send_message(chat_id, text, reply_markup=make_main_keyboard(total_sum), parse_mode="Markdown")

# --- ВЫПОЛНЕНИЕ СКРИПТА ОТПРАВКИ ---

def send_result_screenshots(bot, chat_id):
    verified_dir = os.path.join(BASE_DIR, "verified")
    if not os.path.exists(verified_dir):
        bot.send_message(chat_id, "⚠️ Папка со скриншотами не найдена.")
        return
        
    files = sorted([f for f in os.listdir(verified_dir) if f.endswith(".png")])
    if not files:
        bot.send_message(chat_id, "⚠️ Скриншоты не были созданы.")
        return
        
    bot.send_message(chat_id, "📸 **Скриншоты процесса заполнения и отправки отчёта:**", parse_mode="Markdown")
    
    media = []
    for f in files:
        file_path = os.path.join(verified_dir, f)
        caption = f.replace(".png", "").replace("_", " ").title()
        try:
            with open(file_path, "rb") as photo:
                media.append(types.InputMediaPhoto(photo.read(), caption=caption))
        except Exception as e:
            print(f"Ошибка чтения файла {f}: {e}")
            
    if media:
        for i in range(0, len(media), 10):
            try:
                bot.send_media_group(chat_id, media[i:i+10])
            except Exception as e:
                bot.send_message(chat_id, f"⚠️ Не удалось отправить скриншоты: {e}")

def run_report_script(bot, chat_id, message_id):
    clean_screenshots()
    
    steps = [
        {"name": "Инициализация браузера", "status": "pending"},
        {"name": "Загрузка формы Яндекс", "status": "pending"},
        {"name": "Заполнение личных данных", "status": "pending"},
        {"name": "Ввод трудозатрат", "status": "pending"},
        {"name": "Отправка отчёта", "status": "pending"}
    ]
    
    def get_progress_text(error_msg=None):
        text = "🔄 **Отчет в процессе отправки...**\n\n"
        for step in steps:
            status_icon = "⏳" if step["status"] == "pending" else "🔄" if step["status"] == "running" else "✅" if step["status"] == "done" else "❌"
            text += f"{status_icon} {step['name']}\n"
        
        if error_msg:
            text += f"\n❌ **Произошла ошибка!**\n`{error_msg}`"
        elif steps[-1]["status"] == "done":
            text += "\n🎉 **Отчёт успешно заполнен и отправлен!**"
        return text
    
    steps[0]["status"] = "running"
    try:
        bot.edit_message_text(get_progress_text(), chat_id, message_id, parse_mode="Markdown")
    except Exception:
        pass
        
    proc = subprocess.Popen(
        [sys.executable, os.path.join(BASE_DIR, "friday_report.py")],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1
    )
    
    error_output = []
    last_update_time = time.time()
    
    for line in iter(proc.stdout.readline, ''):
        print(f"[PLAYWRIGHT]: {line.strip()}")
        error_output.append(line)
        changed = False
        
        if "Запуск локального браузера" in line or "Подключение к удаленному браузеру" in line:
            if steps[0]["status"] != "done":
                steps[0]["status"] = "running"
                changed = True
                
        elif "форма загружена" in line:
            if steps[0]["status"] != "done":
                steps[0]["status"] = "done"
            steps[1]["status"] = "running"
            changed = True
            
        elif "клик 'Далее' (второй шаг)" in line or "клик 'Далее' (третий шаг)" in line:
            if steps[1]["status"] != "done":
                steps[1]["status"] = "done"
            steps[2]["status"] = "running"
            changed = True
            
        elif "Заполнение полей формы (evaluate)" in line:
            if steps[2]["status"] != "done":
                steps[2]["status"] = "done"
            steps[3]["status"] = "running"
            changed = True
            
        elif "Форма отправлена" in line or "отправка формы пропущена" in line:
            if steps[3]["status"] != "done":
                steps[3]["status"] = "done"
            steps[4]["status"] = "running"
            changed = True
            
        if changed or (time.time() - last_update_time > 1.5):
            try:
                bot.edit_message_text(get_progress_text(), chat_id, message_id, parse_mode="Markdown")
                last_update_time = time.time()
            except Exception:
                pass
                
    proc.wait()
    
    if proc.returncode == 0:
        for s in steps:
            s["status"] = "done"
        try:
            bot.edit_message_text(get_progress_text(), chat_id, message_id, parse_mode="Markdown")
        except Exception:
            pass
        send_result_screenshots(bot, chat_id)
    else:
        for s in steps:
            if s["status"] == "running":
                s["status"] = "failed"
            elif s["status"] == "pending":
                s["status"] = "pending"
                
        err_msg = "".join(error_output[-5:]).strip()
        if not err_msg:
            err_msg = f"Процесс завершился с кодом {proc.returncode}"
            
        try:
            bot.edit_message_text(get_progress_text(err_msg), chat_id, message_id, parse_mode="Markdown")
        except Exception:
            pass

# --- ПЛАНИРОВЩИК (SCHEDULER) ---

def send_friday_reminder(bot, chat_id):
    summary, total_sum = get_workload_summary_and_sum()
    text = (
        "🔔 **Напоминание: Время отправить пятничный отчёт о трудозатратах!**\n\n"
        f"**Текущее распределение:**\n{summary}\n\n"
        f"Итого сумма: **{total_sum}%**\n\n"
        "Вы можете отправить отчёт прямо сейчас или скорректировать его, нажав на кнопку ниже."
    )
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown", reply_markup=make_main_keyboard(total_sum))
    except Exception as e:
        print(f"Ошибка напоминания для {chat_id}: {e}")

def run_scheduler(bot):
    msk_tz = pytz.timezone('Europe/Moscow')
    last_reminder_date = None
    
    print("⏰ Планировщик напоминаний запущен.")
    while True:
        try:
            now = datetime.now(msk_tz)
            # Пятница = 4 (Monday = 0)
            if now.weekday() == 4:
                # 17:50 по МСК
                current_date = now.date()
                if now.hour == 17 and now.minute >= 50 and last_reminder_date != current_date:
                    chats = load_allowed_chats()
                    if chats:
                        print(f"⏰ [Напоминание] Отправка уведомлений {len(chats)} пользователям...")
                        for chat_id in chats:
                            send_friday_reminder(bot, chat_id)
                        last_reminder_date = current_date
        except Exception as e:
            print(f"Ошибка в цикле планировщика: {e}")
        time.sleep(30)

# --- ЗАПУСК БОТА ---

if __name__ == "__main__":
    # Запускаем планировщик в фоновом потоке
    scheduler_thread = threading.Thread(target=run_scheduler, args=(bot,), daemon=True)
    scheduler_thread.start()
    
    print("🤖 Telegram бот запущен и готов к работе!")
    while True:
        try:
            bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception as e:
            print(f"Бот упал с ошибкой: {e}. Перезапуск через 5 секунд...")
            time.sleep(5)
