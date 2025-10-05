# 🚀 Инструкция по деплою бота на VPS (timeweb.cloud)

## 📋 Что тебе понадобится:
- SSH доступ к серверу (Root-пароль из скриншота)
- IP адрес сервера
- Telegram Bot Token

---

## 🔧 ШАГ 1: Подключение к серверу

### Через SSH (Windows PowerShell или Terminal):
```bash
ssh root@ВАШ_IP_АДРЕС
```
Введи пароль, который ты скопировал на скриншоте.

---

## 🐍 ШАГ 2: Установка Python и зависимостей

```bash
# Обновляем систему
apt update && apt upgrade -y

# Устанавливаем Python 3.11+
apt install python3 python3-pip python3-venv git -y

# Проверяем версию
python3 --version
```

---

## 📦 ШАГ 3: Загрузка бота на сервер

### Вариант 1: Через Git (если есть репозиторий)
```bash
cd /opt
git clone ВАШ_РЕПОЗИТОРИЙ help_cleaners
cd help_cleaners
```

### Вариант 2: Через SCP (с твоего ПК)
На твоем ПК в PowerShell:
```powershell
cd C:\Users\restr\OneDrive\Документы\help_cleaners
scp -r . root@ВАШ_IP:/opt/help_cleaners
```

---

## 🔐 ШАГ 4: Настройка переменных окружения

На сервере:
```bash
cd /opt/help_cleaners

# Создаем .env файл
nano .env
```

Вставь это содержимое (замени на свои данные):
```env
BOT_TOKEN=твой_токен_от_BotFather
DATABASE_PATH=./database.db
OWNER_ID=6405212136
```

Сохрани: `Ctrl+O`, `Enter`, `Ctrl+X`

---

## 🔨 ШАГ 5: Установка зависимостей

```bash
# Создаем виртуальное окружение
python3 -m venv venv

# Активируем
source venv/bin/activate

# Устанавливаем зависимости
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 🤖 ШАГ 6: Тестовый запуск

```bash
python3 help_cleaners/main.py
```

Если бот запустился без ошибок - отлично! Нажми `Ctrl+C` для остановки.

---

## ⚙️ ШАГ 7: Создание systemd сервиса (автозапуск)

```bash
# Создаем сервис
nano /etc/systemd/system/help_cleaners_bot.service
```

Вставь это содержимое:
```ini
[Unit]
Description=Help Cleaners Telegram Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/help_cleaners
Environment="PATH=/opt/help_cleaners/venv/bin"
ExecStart=/opt/help_cleaners/venv/bin/python3 /opt/help_cleaners/help_cleaners/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Сохрани: `Ctrl+O`, `Enter`, `Ctrl+X`

---

## 🚀 ШАГ 8: Запуск бота как сервис

```bash
# Перезагружаем systemd
systemctl daemon-reload

# Включаем автозапуск
systemctl enable help_cleaners_bot

# Запускаем бота
systemctl start help_cleaners_bot

# Проверяем статус
systemctl status help_cleaners_bot
```

---

## 📊 ПОЛЕЗНЫЕ КОМАНДЫ:

### Проверить статус бота:
```bash
systemctl status help_cleaners_bot
```

### Посмотреть логи:
```bash
journalctl -u help_cleaners_bot -f
```

### Остановить бота:
```bash
systemctl stop help_cleaners_bot
```

### Перезапустить бота:
```bash
systemctl restart help_cleaners_bot
```

### Отключить автозапуск:
```bash
systemctl disable help_cleaners_bot
```

---

## 🔄 ОБНОВЛЕНИЕ БОТА:

Когда нужно обновить код:

```bash
# Останови бота
systemctl stop help_cleaners_bot

# Обнови код (git pull или загрузи новые файлы)
cd /opt/help_cleaners
git pull  # или scp новые файлы

# Обнови зависимости (если нужно)
source venv/bin/activate
pip install -r requirements.txt

# Запусти бота
systemctl start help_cleaners_bot
```

---

## ✅ ПРОВЕРКА РАБОТЫ:

1. Отправь боту `/help` в Telegram
2. Попробуй команду `/register`
3. Проверь `/add_shift`

Если всё работает - бот успешно задеплоен! 🎉

---

## ⚠️ TROUBLESHOOTING:

### Бот не запускается:
```bash
# Проверь логи
journalctl -u help_cleaners_bot -n 50

# Проверь .env файл
cat /opt/help_cleaners/.env
```

### База данных не создается:
```bash
# Проверь права
chmod 777 /opt/help_cleaners
```

### Ошибки с зависимостями:
```bash
source /opt/help_cleaners/venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt --force-reinstall
```

---

## 📝 ЗАМЕТКИ:

- ✅ Бот автоматически запустится после перезагрузки сервера
- ✅ Логи сохраняются в systemd
- ✅ База данных SQLite будет в `/opt/help_cleaners/database.db`
- ✅ Все команды работают так же, как на твоем ПК

**ГОТОВО! Твой бот теперь работает 24/7 онлайн!** 🚀

