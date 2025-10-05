#!/bin/bash

# 🚀 Скрипт автоматического деплоя Help Cleaners Bot на VPS

echo "🤖 Деплой Help Cleaners Bot"
echo "================================"

# Проверка Python
echo "📦 Проверка Python..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 не установлен. Устанавливаем..."
    apt update
    apt install python3 python3-pip python3-venv -y
fi

# Создание директории
echo "📁 Создание директорий..."
mkdir -p /opt/help_cleaners
cd /opt/help_cleaners

# Создание виртуального окружения
echo "🐍 Создание виртуального окружения..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# Активация виртуального окружения
echo "⚡ Активация виртуального окружения..."
source venv/bin/activate

# Установка зависимостей
echo "📦 Установка зависимостей..."
pip install --upgrade pip
pip install -r requirements.txt

# Проверка .env файла
if [ ! -f ".env" ]; then
    echo "⚠️  .env файл не найден! Создай его вручную."
    echo "Пример:"
    echo "BOT_TOKEN=your_token"
    echo "DATABASE_PATH=./database.db"
    echo "OWNER_ID=6405212136"
    exit 1
fi

# Создание systemd сервиса
echo "⚙️  Создание systemd сервиса..."
cp help_cleaners_bot.service /etc/systemd/system/

# Перезагрузка systemd
echo "🔄 Перезагрузка systemd..."
systemctl daemon-reload

# Включение автозапуска
echo "🚀 Включение автозапуска..."
systemctl enable help_cleaners_bot

# Запуск бота
echo "▶️  Запуск бота..."
systemctl restart help_cleaners_bot

# Проверка статуса
echo "📊 Проверка статуса..."
sleep 2
systemctl status help_cleaners_bot --no-pager

echo ""
echo "✅ Деплой завершен!"
echo ""
echo "📋 Полезные команды:"
echo "  • systemctl status help_cleaners_bot   - проверить статус"
echo "  • journalctl -u help_cleaners_bot -f   - посмотреть логи"
echo "  • systemctl restart help_cleaners_bot  - перезапустить"
echo ""

