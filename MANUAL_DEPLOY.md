# 🚀 РУЧНОЕ РАЗВЕРТЫВАНИЕ HELP CLEANERS BOT НА VPS

## Шаг 1: Подключение к серверу
```bash
ssh root@5.129.246.62
```

## Шаг 2: Переход в директорию проекта
```bash
cd /opt/help_cleaners
```

## Шаг 3: Создание виртуального окружения
```bash
python3 -m venv venv
```

## Шаг 4: Активация виртуального окружения
```bash
source venv/bin/activate
```

## Шаг 5: Установка зависимостей
```bash
pip install --upgrade pip
pip install aiogram==3.4.1
pip install sqlalchemy==2.0.25
pip install python-dotenv==1.0.0
pip install apscheduler==3.10.4
```

## Шаг 6: Создание файла конфигурации
```bash
nano .env
```

Добавьте в файл:
```
BOT_TOKEN=ВАШ_РЕАЛЬНЫЙ_ТОКЕН_БОТА
```

## Шаг 7: Настройка systemd сервиса
```bash
cp help_cleaners_bot.service /etc/systemd/system/
systemctl daemon-reload
```

## Шаг 8: Запуск бота
```bash
systemctl start help_cleaners_bot
systemctl enable help_cleaners_bot
```

## Шаг 9: Проверка статуса
```bash
systemctl status help_cleaners_bot
```

## Шаг 10: Просмотр логов
```bash
journalctl -u help_cleaners_bot -f
```

## 🔧 Управление ботом

### Остановка бота:
```bash
systemctl stop help_cleaners_bot
```

### Перезапуск бота:
```bash
systemctl restart help_cleaners_bot
```

### Просмотр логов:
```bash
journalctl -u help_cleaners_bot -f
```

### Отключение автозапуска:
```bash
systemctl disable help_cleaners_bot
```

## 📁 Структура файлов на сервере
```
/opt/help_cleaners/
├── main.py                    # Основной файл бота
├── requirements.txt           # Зависимости Python
├── .env                      # Конфигурация (токен бота)
├── help_cleaners_bot.service # Systemd сервис
├── venv/                     # Виртуальное окружение
└── app/                      # Код приложения
    ├── config.py
    ├── handlers/
    ├── db/
    └── ...
```

## ⚠️ Важные замечания

1. **Токен бота**: Обязательно замените `YOUR_BOT_TOKEN_HERE` на реальный токен вашего бота
2. **Права доступа**: Убедитесь, что файлы имеют правильные права доступа
3. **Логи**: Всегда проверяйте логи при возникновении проблем
4. **Обновления**: Для обновления бота просто скопируйте новые файлы и перезапустите сервис

## 🆘 Решение проблем

### Бот не запускается:
```bash
journalctl -u help_cleaners_bot --no-pager
```

### Проблемы с зависимостями:
```bash
source venv/bin/activate
pip install -r requirements.txt
```

### Проблемы с правами доступа:
```bash
chown -R root:root /opt/help_cleaners
chmod +x /opt/help_cleaners/main.py
```
