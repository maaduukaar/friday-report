import re
import time
import sys
import os
from playwright.sync_api import Playwright, sync_playwright

# --- НАСТРОЙКИ ТРУДОЗАТРАТ (в сумме должно быть ровно 100) ---
# Мои трудозатраты
SITES_ADMIN = 0         # Администрирование сайтов
SITES_DEVELOPMENT = 0   # Доработка сайтов support
REPORTING = 0           # Ведение (подготовка) отчетности
DOCUMENTATION = 0       # Работа с внутренней документацией и базой знаний
SITES_TECH_SUPPORT = 0  # Тех. сопровождение сайтов

# Остальное
BUGS_PROCESSING = 0      # Обработка багов и заявок на доработку
CLIENT_SUPPORT = 0       # Техподдержка клиентов
INTERNAL_SUPPORT = 0     # Техподдержка внутренних пользователей
WORKAROUNDS = 0          # Поиск обходных решений
INFRASTRUCTURE = 0      # Поддержка внутренней инфраструктуры (прод, препрод, разработка, тестирование)
BACKUPS = 0             # Работа с резервными копиями
INTERNAL_TRAINING = 0   # Внутреннее обучение других сотрудников
EXTERNAL_TRAINING = 0   # Обучение, повышение квалификации
ANALYTICS = 0           # Аналитика (Naumen)
TASK_TRACKER = 0        # Актуализиция информации по тикетам, работа в таск трекере (Youtrack
QUALITY_CONTROL = 0     # Контроль качества (аудит) информации
MANAGEMENT = 0          # Менеджмент (в т.ч. внутри подразделения, бизнес-процессы, смежные подразделения,
CRM_DEVELOPMENT = 0     # Доработка системы CRM
NAUMEN_ADMIN = 0        # Администрирование Naumen

TOTAL_SUM = (
    BUGS_PROCESSING + CLIENT_SUPPORT + INTERNAL_SUPPORT + WORKAROUNDS +
    INFRASTRUCTURE + BACKUPS + DOCUMENTATION + INTERNAL_TRAINING +
    EXTERNAL_TRAINING + REPORTING + ANALYTICS + TASK_TRACKER +
    QUALITY_CONTROL + MANAGEMENT + CRM_DEVELOPMENT + NAUMEN_ADMIN +
    SITES_TECH_SUPPORT + SITES_ADMIN + SITES_DEVELOPMENT
)

if TOTAL_SUM != 100:
    print(f"\n❌ ОШИБКА: Сумма трудозатрат равна {TOTAL_SUM}, а должна быть ровно 100!")
    print("Пожалуйста, исправьте значения переменных в начале скрипта.")
    sys.exit(1)


# --- ПАПКА ДЛЯ СКРИНШОТОВ ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
VERIFIED_DIR = os.path.join(BASE_DIR, "verified")
os.makedirs(VERIFIED_DIR, exist_ok=True)


URL = "URL"
MAX_RETRIES = 5
STEP_DELAY = 0.05  # секунды между действиями (можно изменить)


def step(action):
    """Выполняет действие и делает паузу STEP_DELAY секунд."""
    result = action()
    time.sleep(STEP_DELAY)
    return result


def run(playwright: Playwright) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()

    # --- Загрузка страницы с перезагрузкой при ошибке ---
    for attempt in range(1, MAX_RETRIES + 1):
        page.goto(URL)
        try:
            page.wait_for_selector(
                "text=Трудозатраты ДИТ, text=Что-то пошло не так",
                timeout=10_000
            )
        except Exception:
            pass
        if page.get_by_text("Что-то пошло не так").count() > 0:
            print(f"Попытка {attempt}: ошибка — перезагружаю...")
            if attempt == MAX_RETRIES:
                raise RuntimeError("Форма недоступна после нескольких попыток")
            continue
        print(f"Попытка {attempt}: форма загружена")
        break
    # --- Конец блока загрузки ---

    # Ждём появления интерактивных элементов формы
    page.wait_for_selector("#answer_choices_68039958", timeout=30_000)

    step(lambda: page.locator("#answer_choices_68039958").click())
    step(lambda: page.locator("div").filter(has_text=re.compile(r"^ОППО$")).nth(2).click())
    step(lambda: page.get_by_role("button", name="Календарь").click())
    # Выбираем текущую дату по CSS-классу (подсвеченная кнопка сегодняшнего дня)
    step(lambda: page.locator(".g-date-calendar__button_current").first.click())
    page.screenshot(path=os.path.join(VERIFIED_DIR, "01_start_page.png"))
    step(lambda: page.get_by_role("button", name="Далее").click())

    step(lambda: page.locator("#answer_choices_68042447").click())
    step(lambda: page.locator("div").filter(has_text=re.compile(r"^Шведов Максим$")).nth(2).click())
    page.screenshot(path=os.path.join(VERIFIED_DIR, "02_name_page.png"))
    step(lambda: page.get_by_role("button", name="Далее").click())

    step(lambda: page.get_by_text("Нет").click())
    page.screenshot(path=os.path.join(VERIFIED_DIR, "03_confirm_page.png"))
    step(lambda: page.get_by_role("button", name="Далее").click())

    step(lambda: page.get_by_role("textbox", name="Обработка багов и заявок на доработку Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Обработка багов и заявок на доработку Обязательное поле").press("Insert"))
    step(lambda: page.get_by_role("textbox", name="Обработка багов и заявок на доработку Обязательное поле").press("NumLock"))
    step(lambda: page.get_by_role("textbox", name="Обработка багов и заявок на доработку Обязательное поле").fill(str(BUGS_PROCESSING)))

    step(lambda: page.get_by_role("textbox", name="Техподдержка клиентов Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Техподдержка клиентов Обязательное поле").fill(str(CLIENT_SUPPORT)))

    step(lambda: page.get_by_role("textbox", name="Техподдержка внутренних пользователей Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Техподдержка внутренних пользователей Обязательное поле").fill(str(INTERNAL_SUPPORT)))

    step(lambda: page.get_by_role("textbox", name="Поиск обходных решений Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Поиск обходных решений Обязательное поле").fill(str(WORKAROUNDS)))

    step(lambda: page.get_by_role("textbox", name="Поддержка внутренней инфраструктуры (прод, препрод, разработка, тестирование) Об").click())
    step(lambda: page.get_by_role("textbox", name="Поддержка внутренней инфраструктуры (прод, препрод, разработка, тестирование) Об").fill(str(INFRASTRUCTURE)))

    step(lambda: page.get_by_role("textbox", name="Работа с резервными копиями Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Работа с резервными копиями Обязательное поле").fill(str(BACKUPS)))

    step(lambda: page.get_by_role("textbox", name="Работа с внутренней документацией и базой знаний Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Работа с внутренней документацией и базой знаний Обязательное поле").fill(str(DOCUMENTATION)))

    step(lambda: page.get_by_role("textbox", name="Внутреннее обучение других сотрудников Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Внутреннее обучение других сотрудников Обязательное поле").fill(str(INTERNAL_TRAINING)))

    step(lambda: page.get_by_role("textbox", name="Обучение, повышение квалификации Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Обучение, повышение квалификации Обязательное поле").fill(str(EXTERNAL_TRAINING)))

    step(lambda: page.get_by_role("textbox", name="Ведение (подготовка) отчетности Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Ведение (подготовка) отчетности Обязательное поле").fill(str(REPORTING)))

    step(lambda: page.get_by_role("textbox", name="Аналитика (Naumen").click())
    step(lambda: page.get_by_role("textbox", name="Аналитика (Naumen").fill(str(ANALYTICS)))

    step(lambda: page.get_by_role("textbox", name="Актуализиция информации по тикетам, работа в таск трекере (Youtrack").click())
    step(lambda: page.get_by_role("textbox", name="Актуализиция информации по тикетам, работа в таск трекере (Youtrack").fill(str(TASK_TRACKER)))

    step(lambda: page.get_by_role("textbox", name="Контроль качества (аудит) информации Обязательное поле").click())
    step(lambda: page.get_by_role("textbox", name="Контроль качества (аудит) информации Обязательное поле").fill(str(QUALITY_CONTROL)))

    step(lambda: page.get_by_role("textbox", name="Менеджмент (в т.ч. внутри подразделения, бизнес-процессы, смежные подразделения,").click())
    step(lambda: page.get_by_role("textbox", name="Менеджмент (в т.ч. внутри подразделения, бизнес-процессы, смежные подразделения,").fill(str(MANAGEMENT)))

    step(lambda: page.get_by_role("textbox", name="Доработка системы CRM").click())
    step(lambda: page.get_by_role("textbox", name="Доработка системы CRM").fill(str(CRM_DEVELOPMENT)))

    step(lambda: page.get_by_role("textbox", name="Администрирование Naumen").click())
    step(lambda: page.get_by_role("textbox", name="Администрирование Naumen").fill(str(NAUMEN_ADMIN)))

    step(lambda: page.get_by_role("textbox", name="Тех. сопровождение сайтов").click())
    step(lambda: page.get_by_role("textbox", name="Тех. сопровождение сайтов").fill(str(SITES_TECH_SUPPORT)))

    step(lambda: page.get_by_role("textbox", name="Администрирование сайтов").click())
    step(lambda: page.get_by_role("textbox", name="Администрирование сайтов").fill(str(SITES_ADMIN)))

    step(lambda: page.get_by_role("textbox", name="Доработка сайтов support,").click())
    step(lambda: page.get_by_role("textbox", name="Доработка сайтов support,").fill(str(SITES_DEVELOPMENT)))

    # Скриншот заполненного финала (вся страница)
    page.screenshot(path=os.path.join(VERIFIED_DIR, "04_workload_filled.png"), full_page=True)

    # Нажимаем кнопку "Отправить"
    step(lambda: page.get_by_role("button", name="Отправить").click())

    # Короткая пауза, чтобы страница успела обновиться после клика
    time.sleep(2)

    # Делаем финальный скриншот
    page.screenshot(path=os.path.join(VERIFIED_DIR, "05_final_page.png"), full_page=True)

    print("\n✅ Форма отправлена! Финальный скриншот сохранен в папке 'verified'.")
    context.close()
    browser.close()


with sync_playwright() as playwright:
    run(playwright)
