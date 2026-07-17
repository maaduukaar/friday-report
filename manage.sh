#!/usr/bin/env bash

# Управление Friday Report из любого каталога. По умолчанию корнем проекта
# считается каталог, в котором расположен этот скрипт.
set -uo pipefail

resolve_script_dir() {
    local source="${BASH_SOURCE[0]}"
    local directory

    while [ -h "$source" ]; do
        directory="$(cd -P "$(dirname "$source")" && pwd)"
        source="$(readlink "$source")"
        [[ "$source" != /* ]] && source="$directory/$source"
    done

    cd -P "$(dirname "$source")" && pwd
}

SCRIPT_DIR="$(resolve_script_dir)"
BOT_DIR="${FRIDAY_REPORT_DIR:-$SCRIPT_DIR}"
if ! BOT_DIR="$(cd -P "$BOT_DIR" 2>/dev/null && pwd)"; then
    echo "Не удалось найти каталог проекта: ${FRIDAY_REPORT_DIR:-$SCRIPT_DIR}" >&2
    exit 1
fi

make_project_path() {
    local path="$1"

    if [[ "$path" = /* ]]; then
        printf '%s\n' "$path"
    else
        printf '%s/%s\n' "$BOT_DIR" "$path"
    fi
}

BOT_SCRIPT="$(make_project_path "${FRIDAY_REPORT_SCRIPT:-telegram_bot.py}")"
STATE_DIR="$(make_project_path "${FRIDAY_REPORT_STATE_DIR:-.run}")"
PID_FILE="$(make_project_path "${FRIDAY_REPORT_PID_FILE:-$STATE_DIR/bot.pid}")"
LOG_FILE="$(make_project_path "${FRIDAY_REPORT_LOG_FILE:-$STATE_DIR/bot.log}")"

STOP_TIMEOUT="${FRIDAY_REPORT_STOP_TIMEOUT:-10}"

usage() {
    cat <<EOF
Использование: $(basename "$0") {start|stop|restart|status|logs}

Переменные окружения:
  FRIDAY_REPORT_DIR           Каталог проекта (по умолчанию: каталог manage.sh)
  FRIDAY_REPORT_SCRIPT        Путь к telegram_bot.py (по умолчанию: <project>/telegram_bot.py)
  FRIDAY_REPORT_PYTHON        Путь или имя Python-интерпретатора
  FRIDAY_REPORT_STATE_DIR     Каталог PID- и log-файлов (по умолчанию: <project>/.run)
  FRIDAY_REPORT_PID_FILE      Явный путь к PID-файлу
  FRIDAY_REPORT_LOG_FILE      Явный путь к журналу
  FRIDAY_REPORT_STOP_TIMEOUT  Время ожидания остановки в секундах (по умолчанию: 10)
EOF
}

resolve_python() {
    local candidate

    if [ -n "${FRIDAY_REPORT_PYTHON:-}" ]; then
        candidate="$FRIDAY_REPORT_PYTHON"
        if [ -x "$candidate" ]; then
            printf '%s\n' "$candidate"
            return 0
        fi
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
        echo "Не найден Python из FRIDAY_REPORT_PYTHON: $candidate" >&2
        return 1
    fi

    if [ -x "$BOT_DIR/.venv/bin/python" ]; then
        printf '%s\n' "$BOT_DIR/.venv/bin/python"
        return 0
    fi

    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done

    echo "Python не найден. Создайте .venv или задайте FRIDAY_REPORT_PYTHON." >&2
    return 1
}

ensure_runtime_paths() {
    if [ ! -f "$BOT_SCRIPT" ]; then
        echo "Файл бота не найден: $BOT_SCRIPT" >&2
        return 1
    fi
    if ! [[ "$STOP_TIMEOUT" =~ ^[1-9][0-9]*$ ]]; then
        echo "FRIDAY_REPORT_STOP_TIMEOUT должен быть положительным целым числом." >&2
        return 1
    fi
    mkdir -p "$(dirname "$PID_FILE")" "$(dirname "$LOG_FILE")"
}

get_pid() {
    [ -f "$PID_FILE" ] && tr -d '[:space:]' < "$PID_FILE"
}

is_bot_process() {
    local pid="$1"
    local command_line

    kill -0 "$pid" 2>/dev/null || return 1
    if ! command -v ps >/dev/null 2>&1; then
        return 0
    fi
    command_line="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    [[ "$command_line" == *"$BOT_SCRIPT"* ]]
}

is_running() {
    local pid
    pid="$(get_pid)"
    [ -n "$pid" ] && [[ "$pid" =~ ^[1-9][0-9]*$ ]] && is_bot_process "$pid"
}

remove_stale_pid_file() {
    if [ -f "$PID_FILE" ] && ! is_running; then
        rm -f "$PID_FILE"
    fi
}

start_bot() {
    local python_bin
    local pid

    ensure_runtime_paths || return 1
    if is_running; then
        echo "⚠️ Бот уже запущен с PID $(get_pid)."
        return 0
    fi
    remove_stale_pid_file
    python_bin="$(resolve_python)" || return 1

    echo "🚀 Запуск бота в фоновом режиме..."
    (
        cd "$BOT_DIR" || exit 1
        exec nohup "$python_bin" -u "$BOT_SCRIPT" </dev/null >>"$LOG_FILE" 2>&1
    ) &
    pid=$!
    printf '%s\n' "$pid" > "$PID_FILE"

    sleep 1
    if is_running; then
        echo "🟢 Бот успешно запущен. PID: $pid"
        echo "📄 Журнал: $LOG_FILE"
    else
        rm -f "$PID_FILE"
        echo "❌ Бот не запустился. Проверьте журнал: $LOG_FILE" >&2
        return 1
    fi
}

stop_bot() {
    local pid
    local attempt

    if ! is_running; then
        remove_stale_pid_file
        echo "⚠️ Бот не запущен."
        return 0
    fi

    pid="$(get_pid)"
    echo "🛑 Останавливаю бота (PID: $pid)..."
    kill "$pid"

    for ((attempt = 1; attempt <= STOP_TIMEOUT; attempt++)); do
        if ! is_running; then
            rm -f "$PID_FILE"
            echo "🟢 Бот остановлен."
            return 0
        fi
        sleep 1
    done

    echo "⚠️ Штатная остановка не завершилась; отправляю SIGKILL..."
    kill -9 "$pid"
    rm -f "$PID_FILE"
    echo "🛑 Бот принудительно остановлен."
}

show_status() {
    if is_running; then
        echo "🟢 Бот работает в фоне. PID: $(get_pid)"
        echo "📄 Журнал: $LOG_FILE"
        if [ -f "$LOG_FILE" ]; then
            echo "--- Последние 5 строк ---"
            tail -n 5 "$LOG_FILE"
        fi
    else
        remove_stale_pid_file
        echo "🔴 Бот остановлен."
    fi
}

show_logs() {
    ensure_runtime_paths || return 1
    if [ ! -f "$LOG_FILE" ]; then
        echo "Журнал ещё не создан: $LOG_FILE" >&2
        return 1
    fi
    echo "📋 Журнал бота (Ctrl+C для выхода):"
    tail -f -n 50 "$LOG_FILE"
}

case "${1:-}" in
    start)
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot && start_bot
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    help|-h|--help)
        usage
        ;;
    *)
        usage >&2
        exit 1
        ;;
esac
