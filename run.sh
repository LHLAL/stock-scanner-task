#!/bin/bash

cd "$(dirname "$0")"

PID_FILE=".monitor.pid"
LOG_FILE="stock-monitor.log"

start() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "📈 监控已在运行中 (PID: $PID)"
            return 0
        fi
        rm -f "$PID_FILE"
    fi

    echo "📈 A股持仓监控 启动中..."
    if [ ! -d "venv" ]; then
        echo "📦 首次运行，创建虚拟环境..."
        python3 -m venv venv
        source venv/bin/activate
        pip3 install -r requirements.txt
    fi
    source venv/bin/activate

    nohup python3 stock_monitor.py > "$LOG_FILE" 2>&1 &
    PID=$!
    echo $PID > "$PID_FILE"
    echo "✅ 监控已启动 (PID: $PID)，日志: $LOG_FILE"
}

stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "🛑 监控未运行"
        return 1
    fi

    PID=$(cat "$PID_FILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "✅ 监控已停止 (PID: $PID)"
    else
        echo "⚠️ 进程已不存在 (PID: $PID)"
    fi
    rm -f "$PID_FILE"
}

restart() {
    stop
    sleep 1
    start
}

status() {
    if [ -f "$PID_FILE" ]; then
        PID=$(cat "$PID_FILE")
        if kill -0 "$PID" 2>/dev/null; then
            echo "📈 监控运行中 (PID: $PID)"
            echo "   日志: $LOG_FILE"
            return 0
        else
            echo "⚠️ PID 文件存在但进程已退出 (PID: $PID)"
            rm -f "$PID_FILE"
            return 1
        fi
    else
        echo "🛑 监控未运行"
        return 1
    fi
}

case "${1:-start}" in
    start)
        start
        ;;
    stop)
        stop
        ;;
    restart)
        restart
        ;;
    status)
        status
        ;;
    *)
        echo "用法: $0 {start|stop|restart|status}"
        exit 1
        ;;
esac
