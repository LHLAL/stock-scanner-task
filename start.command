#!/bin/bash
cd "$(dirname "$0")"

echo "📈 A股持仓监控 启动中..."
echo "🛑 按 Ctrl+C 停止监控..."

if [ ! -d "venv" ]; then
    echo "📦 首次运行，创建虚拟环境..."
    python3 -m venv venv
    source venv/bin/activate
    pip3 install -r requirements.txt
else
    source venv/bin/activate
fi

python3 stock_monitor.py

echo ""
echo "按回车键退出..."
read
