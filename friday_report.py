import re
import time
import sys
import os
import tomllib
import json
from playwright.sync_api import Playwright, sync_playwright

# Force UTF-8 encoding for standard output and error on Windows to prevent encoding errors
if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

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
DEBUG = config.get("debug", True)

# Извлечение трудозатрат
WORKLOAD_PATH = os.path.join(BASE_DIR, "workload.json")
if not os.path.exists(WORKLOAD_PATH):
    print(f"\n❌ ОШИБКА: Файл трудозатрат '{WORKLOAD_PATH}' не найден!")
    sys.exit(1)

with open(WORKLOAD_PATH, "r", encoding="utf-8") as f:
    workload_data = json.load(f)
workload = {k: v["value"] for k, v in workload_data.items()}

# Динамическое создание переменных в глобальном пространстве имен для обратной совместимости 
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
    print(f"\n❌ ОШИБКА: Сумма трудозатрат в workload.json равна {TOTAL_SUM}, а должна быть ровно 100!")
    print("Пожалуйста, исправьте значения в workload.json.")
    sys.exit(1)


# --- ПАПКА ДЛЯ СКРИНШОТОВ ---
VERIFIED_DIR = os.path.join(BASE_DIR, "verified")
os.makedirs(VERIFIED_DIR, exist_ok=True)

MAX_RETRIES = 5
STEP_DELAY = 0.05  # секунды между действиями (можно изменить)


def step(action, description=None):
    """Выполняет действие и делает паузу STEP_DELAY секунд."""
    t0 = time.time()
    result = action()
    if DEBUG and description:
        print(f"⏱️ [{description}] выполнено за {time.time() - t0:.2f} сек.")
    time.sleep(STEP_DELAY)
    return result


def save_screenshot(page, name, full_page=False):
    """Сохраняет скриншот."""
    t0 = time.time()
    path = os.path.join(VERIFIED_DIR, name)
    try:
        page.screenshot(path=path, full_page=full_page)
        if DEBUG:
            print(f"📸 Скриншот {name} сохранен за {time.time() - t0:.2f} сек.")
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
        t_goto = time.time()
        page.goto(URL, wait_until="commit")
        if DEBUG:
            print(f"⏱️ Загрузка страницы (goto) выполнена за {time.time() - t_goto:.2f} сек.")
        try:
            # Быстрая проверка загрузки формы (ожидаем селектор первой страницы)
            t_wait = time.time()
            page.wait_for_selector("#answer_choices_68039958", timeout=15_000)
            print(f"Попытка {attempt}: форма загружена" + (f" (wait_for_selector за {time.time() - t_wait:.2f} сек.)" if DEBUG else ""))
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

    step(lambda: page.locator("#answer_choices_68039958").evaluate("el => el.click()"), "выбор отдела: клик по полю")
    step(lambda: page.get_by_role("option", name=DEPARTMENT, exact=True).evaluate("el => el.click()"), f"выбор отдела: клик по '{DEPARTMENT}'")
    step(lambda: page.get_by_role("button", name="Календарь").evaluate("el => el.click()"), "выбор даты: открытие календаря")
    # Выбираем текущую дату по CSS-классу (подсвеченная кнопка сегодняшнего дня)
    step(lambda: page.locator(".g-date-calendar__button_current").first.evaluate("el => el.click()"), "выбор даты: клик по текущему дню")
    save_screenshot(page, "01_start_page.png")
    step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"), "клик 'Далее' (первый шаг)")

    step(lambda: page.locator("#answer_choices_68042447").evaluate("el => el.click()"), "выбор сотрудника: клик по полю")
    step(lambda: page.get_by_role("option", name=EMPLOYEE_NAME, exact=True).evaluate("el => el.click()"), f"выбор сотрудника: клик по '{EMPLOYEE_NAME}'")
    save_screenshot(page, "02_name_page.png")
    step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"), "клик 'Далее' (второй шаг)")

    step(lambda: page.get_by_text("Нет").evaluate("el => el.click()"), "подтверждение отсутствия изменений: выбор 'Нет'")
    save_screenshot(page, "03_confirm_page.png")
    step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"), "клик 'Далее' (третий шаг)")

    # Ждем загрузки полей ввода трудозатрат
    t_wait_fields = time.time()
    page.wait_for_selector("#id-question-68085646", timeout=15_000)
    if DEBUG:
        print(f"⏱️ Ожидание полей ввода трудозатрат заняло {time.time() - t_wait_fields:.2f} сек.")

    # Заполняем все поля одним JS-вызовом (критично для remote-режима — экономит время сессии)
    # ID полей зафиксированы на основе реальной структуры формы
    t_fill = time.time()
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
    if DEBUG:
        print(f"⏱️ Заполнение полей формы (evaluate) выполнено за {time.time() - t_fill:.2f} сек.")

    # Скриншот заполненного финала
    save_screenshot(page, "04_workload_filled.png", full_page=True)

    if not DEBUG:
        # Нажимаем кнопку "Отправить"
        step(lambda: page.get_by_role("button", name="Отправить").click(), "отправка формы")
        # Короткая пауза, чтобы страница успела обновиться после клика
        time.sleep(2)
        print("\n✅ Форма отправлена! Скриншоты сохранены в папке 'verified'.")
    else:
        print("\n⚠️ Режим отладки (debug = true): отправка формы пропущена. Скриншоты сохранены в папке 'verified'.")

    # Делаем финальный скриншот
    save_screenshot(page, "05_final_page.png", full_page=True)

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


if __name__ == "__main__":
    with sync_playwright() as playwright:
        run(playwright)
