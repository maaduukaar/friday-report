#!/usr/bin/env bash

# Path configuration
BOT_DIR="/home/ubuntu/.hermes/tools/friday-report"
PYTHON_BIN="/home/ubuntu/.hermes/hermes-agent/venv/bin/python"
BOT_SCRIPT="$BOT_DIR/telegram_bot.py"
PID_FILE="$BOT_DIR/bot.pid"
LOG_FILE="$BOT_DIR/bot.log"

get_pid() {
    if [ -f "$PID_FILE" ]; then
        echo "$(cat "$PID_FILE")"
    else
        echo ""
    fi
}

is_running() {
    local pid=$(get_pid)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        return 0 # True
    else
        return 1 # False
    fi
}

start_bot() {
    if is_running; then
        echo "⚠️  Бот уже запущен с PID $(get_pid)."
        exit 0
    fi

    echo "🚀 Запуск бота в фоновом режиме..."
    nohup "$PYTHON_BIN" -u "$BOT_SCRIPT" > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"
    
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
        echo "🟢 Бот успешно запущен в фоне! PID: $pid"
        echo "📝 Логи сохраняются в: $LOG_FILE"
    else
        echo "❌ Ошибка при запуске. Проверьте логи в $LOG_FILE"
    fi
}

stop_bot() {
    if ! is_running; then
        echo "⚠️  Бот не запущен."
        [ -f "$PID_FILE" ] && rm "$PID_FILE"
        return
    fi

    local pid=$(get_pid)
    echo "🛑 Останавливаем бота (PID: $pid)..."
    kill "$pid"
    
    # Wait for the process to stop
    for i in {1..5}; do
        if ! kill -0 "$pid" 2>/dev/null; then
            rm "$PID_FILE"
            echo "🟢 Бот успешно остановлен."
            return
        fi
        sleep 1
    done

    echo "⚠️  Бот не остановился штатно. Принудительное завершение..."
    kill -9 "$pid"
    rm "$PID_FILE"
    echo "🛑 Бот принудительно остановлен (SIGKILL)."
}

show_status() {
    if is_running; then
        echo "🟢 Бот РАБОТАЕТ в фоне. PID: $(get_pid)"
        echo "📝 Путь к логам: $LOG_FILE"
        echo "--- Последние 5 строк логов ---"
        tail -n 5 "$LOG_FILE"
    else
        echo "🔴 Бот ОСТАНОВЛЕН."
    fi
}

show_logs() {
    echo "📋 Вывод логов бота (нажмите Ctrl+C для выхода):"
    tail -f -n 50 "$LOG_FILE"
}

case "$1" in
    start)
        start_bot
        ;;
    stop)
        stop_bot
        ;;
    restart)
        stop_bot
        sleep 1
        start_bot
        ;;
    status)
        show_status
        ;;
    logs)
        show_logs
        ;;
    *)
        echo "Использование: $0 {start|stop|restart|status|logs}"
        exit 1
        ;;
esac
