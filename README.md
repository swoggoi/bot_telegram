# Telegram Auto-Accept Bot

Бот для автоматического одобрения заявок на вступление в Telegram-канал.

## Что делает бот

- Автоматически одобряет заявки на вступление в канал (ChatJoinRequest)
- Отправляет приветственное сообщение в ЛС после одобрения
- Админ-команды: `/status`, `/chat_id`, `/approve_pending`

## Запуск локально

```bash
pip install -r requirements.txt
cp .env.example .env
# Отредактируйте .env — вставьте BOT_TOKEN
python bot.py
```

## Деплой на Amvera Cloud

1. Перейдите на https://cloud.amvera.ru/
2. Создайте проект, подключите GitHub-репозиторий
3. Amvera автоматически соберёт Docker-образ по `Dockerfile`
4. В настройках проекта задайте переменные окружения:
   - `BOT_TOKEN` — токен от @BotFather
   - `ADMIN_ID` — ваш Telegram ID
   - `CHANNEL_ID` — ID канала
5. Запустите — бот будет работать 24/7

## Права бота

Бот должен быть **администратором канала** с правами:
- **Приглашать пользователей** (`can_invite_users`)
- **Управление чатом** (`can_manage_chat`)

## Структура проекта

```
bot.py            — основной файл бота
requirements.txt  — зависимости
.env.example      — шаблон переменных окружения
Dockerfile        — для деплоя в контейнере
.gitignore        — игнорируемые файлы
```
