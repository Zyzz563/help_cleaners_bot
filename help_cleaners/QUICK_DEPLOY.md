# ⚡ БЫСТРЫЙ ДЕПЛОЙ (5 минут)

## 🔑 ШАГ 1: Подключись к серверу

```bash
ssh root@ВАШ_IP_АДРЕС
```
*(Используй пароль со скриншота)*

---

## 📦 ШАГ 2: Скачай бота на сервер

### Способ А: Загрузка с твоего ПК (рекомендуется)

**На твоем ПК (PowerShell):**
```powershell
cd C:\Users\restr\OneDrive\Документы
scp -r help_cleaners root@ВАШ_IP:/opt/
```

### Способ Б: Через Git (если есть репозиторий)
**На сервере:**
```bash
cd /opt
git clone https://github.com/твой-репо/help_cleaners.git
```

---

## 🔧 ШАГ 3: Настрой переменные окружения

**На сервере:**
```bash
cd /opt/help_cleaners

# Создай .env файл
nano .env
```

**Вставь:**
```env
BOT_TOKEN=твой_токен_от_BotFather
DATABASE_PATH=./database.db
OWNER_ID=6405212136
```

Сохрани: `Ctrl+O`, `Enter`, `Ctrl+X`

---

## 🚀 ШАГ 4: Запусти автоматический деплой

```bash
cd /opt/help_cleaners
chmod +x deploy.sh
./deploy.sh
```

---

## ✅ ШАГ 5: Проверь работу

```bash
# Посмотри статус
systemctl status help_cleaners_bot

# Посмотри логи
journalctl -u help_cleaners_bot -f
```

В Telegram напиши боту `/help` - должен ответить!

---

## 🎉 ГОТОВО!

Бот работает 24/7 онлайн! 🚀

### Полезные команды:
- `systemctl restart help_cleaners_bot` - перезапустить
- `systemctl stop help_cleaners_bot` - остановить
- `journalctl -u help_cleaners_bot -n 100` - последние 100 строк логов

---

## 🔄 Обновление бота:

```bash
# Останови бота
systemctl stop help_cleaners_bot

# Загрузи новые файлы (через scp с ПК)
# или
# git pull  (если через git)

# Запусти бота
systemctl start help_cleaners_bot
```

---

## ⚠️ Если что-то не работает:

```bash
# Проверь логи
journalctl -u help_cleaners_bot -n 50

# Проверь .env
cat /opt/help_cleaners/.env

# Попробуй запустить вручную
cd /opt/help_cleaners
source venv/bin/activate
python3 help_cleaners/main.py
```

**Напиши мне, если возникнут проблемы!** 💬

