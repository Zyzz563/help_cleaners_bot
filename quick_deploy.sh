#!/bin/bash

# Быстрое развертывание Help Cleaners Bot
echo "🚀 Быстрое развертывание бота..."

cd /opt/help_cleaners

# Создаем виртуальное окружение
python3 -m venv venv
source venv/bin/activate

# Устанавливаем зависимости
pip install aiogram sqlalchemy python-dotenv apscheduler

# Создаем .env файл
echo "BOT_TOKEN=YOUR_BOT_TOKEN_HERE" > .env

# Настраиваем systemd
cp help_cleaners_bot.service /etc/systemd/system/
systemctl daemon-reload

echo "✅ Готово! Теперь:"
echo "1. Отредактируйте .env файл с реальным токеном"
echo "2. Запустите: systemctl start help_cleaners_bot"
echo "3. Включите автозапуск: systemctl enable help_cleaners_bot"
