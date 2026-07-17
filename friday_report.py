"""Playwright runner for one immutable Friday Report profile.

The Telegram bot invokes this module with a JSON snapshot and a unique output
directory.  Running it without arguments remains supported for a single local
profile in ``config.toml``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from playwright.sync_api import Playwright, sync_playwright


if sys.platform.startswith("win"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except AttributeError:
        pass


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.toml"
WORKLOAD_KEYS = [
    "BUGS_PROCESSING",
    "CLIENT_SUPPORT",
    "INTERNAL_SUPPORT",
    "WORKAROUNDS",
    "INFRASTRUCTURE",
    "BACKUPS",
    "DOCUMENTATION",
    "INTERNAL_TRAINING",
    "EXTERNAL_TRAINING",
    "REPORTING",
    "ANALYTICS",
    "TASK_TRACKER",
    "QUALITY_CONTROL",
    "MANAGEMENT",
    "CRM_DEVELOPMENT",
    "NAUMEN_ADMIN",
    "SITES_TECH_SUPPORT",
    "SITES_ADMIN",
    "SITES_DEVELOPMENT",
]

# These values are populated exactly once by load_runtime_config() before run().
URL = ""
DEPARTMENT = ""
EMPLOYEE_NAME = ""
BROWSER_MODE = "local"
BROWSERLESS_TOKEN = ""
BROWSERLESS_ENDPOINT = "wss://chrome.browserless.io"
SESSION_REPLAY = False
DEBUG = True
workload: dict[str, int] = {}
VERIFIED_DIR = BASE_DIR / "verified"

MAX_RETRIES = 5
STEP_DELAY = 0.05


def progress(event: str) -> None:
    """Emit machine-readable progress without exposing profile details."""

    print(f"PROGRESS:{event}", flush=True)


def _read_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise RuntimeError("config.toml не найден")
    with CONFIG_PATH.open("rb") as config_file:
        return tomllib.load(config_file)


def _read_profile(path: str | None, config: dict[str, Any]) -> dict[str, Any]:
    if path is None:
        return {
            "employee_name": config.get("employee_name", ""),
            "department": config.get("department", ""),
            "workload": config.get("workload", {}),
        }
    profile_path = Path(path)
    with profile_path.open("r", encoding="utf-8") as profile_file:
        profile = json.load(profile_file)
    if not isinstance(profile, dict):
        raise ValueError("Снимок профиля имеет неверный формат")
    return profile


def _validated_workload(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError("Трудозатраты профиля имеют неверный формат")
    result: dict[str, int] = {}
    for key in WORKLOAD_KEYS:
        percentage = value.get(key, 0)
        if isinstance(percentage, bool) or not isinstance(percentage, int) or not 0 <= percentage <= 100:
            raise ValueError("Значения трудозатрат должны быть целыми числами от 0 до 100")
        result[key] = percentage
    if sum(result.values()) != 100:
        raise ValueError("Сумма трудозатрат должна быть ровно 100%")
    return result


def _profile_text(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} профиля должен быть строкой")
    value = value.strip()
    if not 1 <= len(value) <= 120 or any(ord(character) < 32 for character in value):
        raise ValueError(f"{field_name} профиля имеет неверное значение")
    return value


def load_runtime_config(profile_file: str | None = None, output_dir: str | None = None) -> None:
    """Load global deployment settings and one user-owned immutable snapshot."""

    global URL, DEPARTMENT, EMPLOYEE_NAME, BROWSER_MODE, BROWSERLESS_TOKEN
    global BROWSERLESS_ENDPOINT, SESSION_REPLAY, DEBUG, workload, VERIFIED_DIR

    config = _read_config()
    profile = _read_profile(profile_file, config)
    URL = str(config.get("url", "")).strip()
    if not URL:
        raise ValueError("Не задан url формы")
    DEPARTMENT = _profile_text(profile.get("department"), "Подразделение")
    EMPLOYEE_NAME = _profile_text(profile.get("employee_name"), "ФИО сотрудника")
    workload = _validated_workload(profile.get("workload"))
    BROWSER_MODE = str(config.get("browser_mode", "local")).lower()
    if BROWSER_MODE not in {"local", "remote"}:
        raise ValueError("browser_mode должен быть local или remote")
    BROWSERLESS_TOKEN = str(config.get("browserless_token", ""))
    BROWSERLESS_ENDPOINT = str(config.get("browserless_endpoint", "wss://chrome.browserless.io")).rstrip("/")
    session_replay = config.get("session_replay", False)
    debug = config.get("debug", True)
    if not isinstance(session_replay, bool) or not isinstance(debug, bool):
        raise ValueError("debug и session_replay должны быть true или false")
    SESSION_REPLAY = session_replay
    DEBUG = debug
    VERIFIED_DIR = Path(output_dir).resolve() if output_dir else BASE_DIR / "verified"
    VERIFIED_DIR.mkdir(parents=True, exist_ok=True)


def step(action: Any, description: str | None = None) -> Any:
    started_at = time.monotonic()
    result = action()
    if DEBUG and description:
        print(f"DEBUG_STEP:{description}:{time.monotonic() - started_at:.2f}", flush=True)
    time.sleep(STEP_DELAY)
    return result


def save_screenshot(page: Any, name: str, full_page: bool = False) -> None:
    path = VERIFIED_DIR / name
    page.screenshot(path=str(path), full_page=full_page)


def _open_browser(playwright: Playwright) -> Any:
    if BROWSER_MODE == "remote":
        if not BROWSERLESS_TOKEN:
            raise RuntimeError("Для удалённого режима не задан browserless_token")
        ws_url = f"{BROWSERLESS_ENDPOINT}?token={BROWSERLESS_TOKEN}"
        if SESSION_REPLAY:
            ws_url += "&replay=true"
        return playwright.chromium.connect_over_cdp(ws_url)
    return playwright.chromium.launch(headless=False)


def _wait_for_form(page: Any) -> None:
    for attempt in range(1, MAX_RETRIES + 1):
        page.goto(URL, wait_until="commit")
        try:
            page.wait_for_selector("#answer_choices_68039958", timeout=15_000)
            return
        except Exception:
            if attempt == MAX_RETRIES:
                raise RuntimeError("Форма недоступна после нескольких попыток")
            time.sleep(0.5)


def run(playwright: Playwright) -> None:
    browser = None
    context = None
    try:
        browser = _open_browser(playwright)
        progress("browser_ready")
        context = browser.new_context()
        page = context.new_page()

        _wait_for_form(page)
        progress("form_loaded")

        step(lambda: page.locator("#answer_choices_68039958").evaluate("el => el.click()"))
        step(lambda: page.get_by_role("option", name=DEPARTMENT, exact=True).evaluate("el => el.click()"))
        step(lambda: page.get_by_role("button", name="Календарь").evaluate("el => el.click()"))
        step(lambda: page.locator(".g-date-calendar__button_current").first.evaluate("el => el.click()"))
        save_screenshot(page, "01_start_page.png")
        step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"))

        step(lambda: page.locator("#answer_choices_68042447").evaluate("el => el.click()"))
        step(lambda: page.get_by_role("option", name=EMPLOYEE_NAME, exact=True).evaluate("el => el.click()"))
        save_screenshot(page, "02_name_page.png")
        step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"))
        progress("profile_filled")

        step(lambda: page.get_by_text("Нет").evaluate("el => el.click()"))
        save_screenshot(page, "03_confirm_page.png")
        step(lambda: page.get_by_role("button", name="Далее").evaluate("el => el.click()"))

        page.wait_for_selector("#id-question-68085646", timeout=15_000)
        page.evaluate(
            """
            (fields) => {
                const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                for (const [id, val] of Object.entries(fields)) {
                    const el = document.getElementById(id);
                    if (!el) { throw new Error('NOT_FOUND:' + id); }
                    setter.call(el, String(val));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }
            """,
            {
                "id-question-68085646": workload["BUGS_PROCESSING"],
                "id-question-68085807": workload["CLIENT_SUPPORT"],
                "id-question-68087510": workload["INTERNAL_SUPPORT"],
                "id-question-105349989": workload["WORKAROUNDS"],
                "id-question-68085835": workload["INFRASTRUCTURE"],
                "id-question-68085924": workload["BACKUPS"],
                "id-question-68085987": workload["DOCUMENTATION"],
                "id-question-68086033": workload["INTERNAL_TRAINING"],
                "id-question-68086144": workload["EXTERNAL_TRAINING"],
                "id-question-68086044": workload["REPORTING"],
                "id-question-68086063": workload["ANALYTICS"],
                "id-question-68086072": workload["TASK_TRACKER"],
                "id-question-105348749": workload["QUALITY_CONTROL"],
                "id-question-68086078": workload["MANAGEMENT"],
                "id-question-68091795": workload["CRM_DEVELOPMENT"],
                "id-question-75367696": workload["NAUMEN_ADMIN"],
                "id-question-68092331": workload["SITES_TECH_SUPPORT"],
                "id-question-68092354": workload["SITES_ADMIN"],
                "id-question-68092384": workload["SITES_DEVELOPMENT"],
            },
        )
        save_screenshot(page, "04_workload_filled.png", full_page=True)
        progress("workload_filled")

        if DEBUG:
            progress("dry_run")
        else:
            step(lambda: page.get_by_role("button", name="Отправить").click())
            time.sleep(2)
            progress("form_submitted")

        save_screenshot(page, "05_final_page.png", full_page=True)

        if BROWSER_MODE == "remote" and SESSION_REPLAY:
            try:
                cdp_session = context.new_cdp_session(page)
                cdp_session.send("Browserless.stopSessionRecording")
            except Exception:
                # Session Replay is optional and must not change a report result.
                pass
    finally:
        if context is not None:
            try:
                context.close()
            finally:
                if browser is not None:
                    browser.close()
        elif browser is not None:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one Friday Report profile")
    parser.add_argument("--profile-file", help="JSON snapshot generated for one report job")
    parser.add_argument("--output-dir", help="Directory for this job's screenshots")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        load_runtime_config(args.profile_file, args.output_dir)
        with sync_playwright() as playwright:
            run(playwright)
    except Exception as error:
        # Do not print the exception text: browser URLs and automation errors can
        # contain profile data or secrets.  The bot stores a generic status only.
        print(f"RUN_FAILED:{type(error).__name__}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
