#!/bin/bash

# Скрипт развертывания бота на сервере
echo "🚀 Начинаем развертывание Help Cleaners Bot..."

# Переходим в директорию проекта
cd /opt/help_cleaners

# Создаем виртуальное окружение если его нет
if [ ! -d "venv" ]; then
    echo "📦 Создаем виртуальное окружение..."
    python3 -m venv venv
fi

# Активируем виртуальное окружение
echo "🔧 Активируем виртуальное окружение..."
source venv/bin/activate

# Устанавливаем зависимости
echo "📥 Устанавливаем зависимости..."
pip install --upgrade pip
pip install aiogram==3.4.1
pip install sqlalchemy==2.0.25
pip install python-dotenv==1.0.0
pip install apscheduler==3.10.4

# Создаем файл .env если его нет
if [ ! -f ".env" ]; then
    echo "⚙️ Создаем файл конфигурации..."
    echo "BOT_TOKEN=YOUR_BOT_TOKEN_HERE" > .env
    echo "⚠️ ВАЖНО: Замените YOUR_BOT_TOKEN_HERE на реальный токен бота!"
fi

# Устанавливаем права на выполнение
chmod +x main.py

# Создаем systemd сервис
echo "🔧 Настраиваем systemd сервис..."
cp help_cleaners_bot.service /etc/systemd/system/

# Перезагружаем systemd
systemctl daemon-reload

echo "✅ Развертывание завершено!"
echo ""
echo "📋 Следующие шаги:"
echo "1. Отредактируйте файл .env и укажите реальный BOT_TOKEN"
echo "2. Запустите бота: systemctl start help_cleaners_bot"
echo "3. Включите автозапуск: systemctl enable help_cleaners_bot"
echo "4. Проверьте статус: systemctl status help_cleaners_bot"
echo "5. Просмотрите логи: journalctl -u help_cleaners_bot -f"
