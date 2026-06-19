import re
import time
import sys
import os
import tomllib
from playwright.sync_api import Playwright, sync_playwright

# --- ЗАГРУЗКА НАСТРОЕК ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.toml")

if not os.path.exists(CONFIG_PATH):
    print(f"\n❌ ОШИБКА: Конфигурационный файл '{CONFIG_PATH}' не найден!")
    sys.exit(1)

with open(CONFIG_PATH, "rb") as f:
    config = tomllib.load(f)

# Извлечение общих настроек
URL = config.get("url", "https://forms.yandex.ru/u/696a31421f1eb52eecdbc9df/")
DEPARTMENT = config.get("department", "ОППО")
EMPLOYEE_NAME = config.get("employee_name", "Шведов Максим")
BROWSER_MODE = config.get("browser_mode", "local").lower()
BROWSERLESS_TOKEN = config.get("browserless_token", "")
BROWSERLESS_ENDPOINT = config.get("browserless_endpoint", "wss://chrome.browserless.io").rstrip("/")
SESSION_REPLAY = config.get("session_replay", False)

# Извлечение трудозатрат
workload = config.get("workload", {})

# Динамическое создание переменных в глобальном пространстве имен для обратной совместимости 
# (или можно использовать словарь workload напрямую, что чище)
globals().update(workload)

# Список всех ключей трудозатрат для проверки суммы
WORKLOAD_KEYS = [
    "BUGS_PROCESSING", "CLIENT_SUPPORT", "INTERNAL_SUPPORT", "WORKAROUNDS",
    "INFRASTRUCTURE", "BACKUPS", "DOCUMENTATION", "INTERNAL_TRAINING",
    "EXTERNAL_TRAINING", "REPORTING", "ANALYTICS", "TASK_TRACKER",
    "QUALITY_CONTROL", "MANAGEMENT", "CRM_DEVELOPMENT", "NAUMEN_ADMIN",
    "SITES_TECH_SUPPORT", "SITES_ADMIN", "SITES_DEVELOPMENT"
]

TOTAL_SUM = sum(workload.get(key, 0) for key in WORKLOAD_KEYS)

if TOTAL_SUM != 100:
    print(f"\n❌ ОШИБКА: Сумма трудозатрат в config.toml равна {TOTAL_SUM}, а должна быть ровно 100!")
    print("Пожалуйста, исправьте значения в [workload].")
    sys.exit(1)


# --- ПАПКА ДЛЯ СКРИНШОТОВ ---
VERIFIED_DIR = os.path.join(BASE_DIR, "verified")
os.makedirs(VERIFIED_DIR, exist_ok=True)

MAX_RETRIES = 5
STEP_DELAY = 0.05  # секунды между действиями (можно изменить)


def step(action):
    """Выполняет действие и делает паузу STEP_DELAY секунд."""
    result = action()
    time.sleep(STEP_DELAY)
    return result


def save_screenshot(page, name, full_page=False):
    """Сохраняет скриншот. В удаленном режиме пропускает промежуточные скриншоты для экономии времени."""
    if BROWSER_MODE == "remote" and name in ["01_start_page.png", "02_name_page.png", "03_confirm_page.png"]:
        return
    
    path = os.path.join(VERIFIED_DIR, name)
    try:
        page.screenshot(path=path, full_page=full_page)
    except Exception as e:
        print(f"⚠️ Не удалось сохранить скриншот {name}: {e}")


def run(playwright: Playwright) -> None:
    if BROWSER_MODE == "remote":
        if not BROWSERLESS_TOKEN:
            print("\n❌ ОШИБКА: Токен 'browserless_token' не указан в config.toml для удаленного режима!")
            sys.exit(1)
        ws_url = f"{BROWSERLESS_ENDPOINT}?token={BROWSERLESS_TOKEN}"
        if SESSION_REPLAY:
            ws_url += "&replay=true"
            print("🎥 Session Replay включен, сессия будет записана в дашборде browserless.io")
        print(f"🌐 Подключение к удаленному браузеру {BROWSERLESS_ENDPOINT}...")
        browser = playwright.chromium.connect_over_cdp(ws_url)
    else:
        print("💻 Запуск локального браузера...")
        browser = playwright.chromium.launch(headless=False)

    context = browser.new_context()
    page = context.new_page()

    # --- Загрузка страницы с перезагрузкой при ошибке ---
    for attempt in range(1, MAX_RETRIES + 1):
        page.goto(URL)
        try:
            # Быстрая проверка загрузки формы (ожидаем селектор первой страницы)
            page.wait_for_selector("#answer_choices_68039958", timeout=15_000)
            print(f"Попытка {attempt}: форма загружена")
            break
        except Exception:
            if page.get_by_text("Что-то пошло не так").count() > 0:
                print(f"Попытка {attempt}: ошибка — перезагружаю...")
                if attempt == MAX_RETRIES:
                    raise RuntimeError("Форма недоступна после нескольких попыток")
                continue
            # Если форма все же загрузилась (селектор по какой-то причине не сработал), продолжаем
            if page.locator("#answer_choices_68039958").count() > 0:
                print(f"Попытка {attempt}: форма загружена")
                break
    # --- Конец блока загрузки ---

    step(lambda: page.locator("#answer_choices_68039958").click())
    step(lambda: page.locator("div").filter(has_text=re.compile(fr"^{re.escape(DEPARTMENT)}$")).nth(2).click())
    step(lambda: page.get_by_role("button", name="Календарь").click())
    # Выбираем текущую дату по CSS-классу (подсвеченная кнопка сегодняшнего дня)
    step(lambda: page.locator(".g-date-calendar__button_current").first.click())
    save_screenshot(page, "01_start_page.png")
    step(lambda: page.get_by_role("button", name="Далее").click())

    step(lambda: page.locator("#answer_choices_68042447").click())
    step(lambda: page.locator("div").filter(has_text=re.compile(fr"^{re.escape(EMPLOYEE_NAME)}$")).nth(2).click())
    save_screenshot(page, "02_name_page.png")
    step(lambda: page.get_by_role("button", name="Далее").click())

    step(lambda: page.get_by_text("Нет").click())
    save_screenshot(page, "03_confirm_page.png")
    step(lambda: page.get_by_role("button", name="Далее").click())

    # Ждем загрузки полей ввода трудозатрат
    page.wait_for_selector("#id-question-68085646", timeout=15_000)

    # Заполняем все поля одним JS-вызовом (критично для remote-режима — экономит время сессии)
    # ID полей зафиксированы на основе реальной структуры формы
    page.evaluate("""
    (fields) => {
        const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
        for (const [id, val] of Object.entries(fields)) {
            const el = document.getElementById(id);
            if (!el) { console.warn('NOT FOUND: ' + id); continue; }
            setter.call(el, String(val));
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }
    }
    """, {
        "id-question-68085646": workload.get("BUGS_PROCESSING", 0),
        "id-question-68085807": workload.get("CLIENT_SUPPORT", 0),
        "id-question-68087510": workload.get("INTERNAL_SUPPORT", 0),
        "id-question-105349989": workload.get("WORKAROUNDS", 0),
        "id-question-68085835": workload.get("INFRASTRUCTURE", 0),
        "id-question-68085924": workload.get("BACKUPS", 0),
        "id-question-68085987": workload.get("DOCUMENTATION", 0),
        "id-question-68086033": workload.get("INTERNAL_TRAINING", 0),
        "id-question-68086144": workload.get("EXTERNAL_TRAINING", 0),
        "id-question-68086044": workload.get("REPORTING", 0),
        "id-question-68086063": workload.get("ANALYTICS", 0),
        "id-question-68086072": workload.get("TASK_TRACKER", 0),
        "id-question-105348749": workload.get("QUALITY_CONTROL", 0),
        "id-question-68086078": workload.get("MANAGEMENT", 0),
        "id-question-68091795": workload.get("CRM_DEVELOPMENT", 0),
        "id-question-75367696": workload.get("NAUMEN_ADMIN", 0),
        "id-question-68092331": workload.get("SITES_TECH_SUPPORT", 0),
        "id-question-68092354": workload.get("SITES_ADMIN", 0),
        "id-question-68092384": workload.get("SITES_DEVELOPMENT", 0),
    })

    # Скриншот заполненного финала
    save_screenshot(page, "04_workload_filled.png", full_page=True)

    # Нажимаем кнопку "Отправить"
    # step(lambda: page.get_by_role("button", name="Отправить").click())

    # Короткая пауза, чтобы страница успела обновиться после клика
    time.sleep(2)

    # Делаем финальный скриншот
    save_screenshot(page, "05_final_page.png", full_page=True)

    print("\n✅ Форма отправлена! Скриншоты сохранены в папке 'verified'.")

    # Останавливаем запись Session Replay через CDP
    if BROWSER_MODE == "remote" and SESSION_REPLAY:
        try:
            cdp_session = context.new_cdp_session(page)
            cdp_session.send("Browserless.stopSessionRecording")
            print("🎥 Session Replay загружен в дашборд browserless.io")
        except Exception as e:
            print(f"⚠️ Не удалось остановить Session Replay: {e}")

    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
