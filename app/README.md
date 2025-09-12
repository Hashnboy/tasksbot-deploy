# TasksBot (minimal PostgreSQL-only)

## 1) Подготовка
```bash
sudo apt update && sudo apt install -y python3-venv python3-pip
git clone <ваш-репозиторий-или-scp>
cd tasksbot
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 2) ENV
Добавьте в `~/.bashrc` или /etc/environment:
```
export TELEGRAM_TOKEN=<ваш_токен>
export WEBHOOK_BASE=https://<ваш-домен-или-ip>
export DATABASE_URL=postgresql+psycopg2://user:pass@host:5432/dbname
export TZ=Europe/Moscow
# опционально:
export OPENAI_API_KEY=<ключ>
```
Перезайдите в оболочку или `source ~/.bashrc`.

## Направления

В бот встроена плагин‑система *Directions*. Каждое направление определяет свои
меню, шаблоны задач и политики. По умолчанию доступны четыре направления:

| key      | Название    | Кратко                   |
|----------|-------------|--------------------------|
| tobacco  | Табачка     | чек‑ин, задачи, приёмки   |
| coffee   | Кофейня     | чек‑ин, поставки, отчёты |
| wb       | Wildberries | контент, остатки, отчёты |
| personal | Личное      | личные задачи/напоминания|

Командой `/start` пользователь выбирает направление. Последний выбор
сохраняется в таблице `user_settings`.

## 3) Локальный тест
```bash
source .venv/bin/activate
python tasks_bot.py
# Остановите Ctrl+C
```

## 4) Gunicorn + systemd
Создайте `/etc/systemd/system/tasksbot.service`:
```
[Unit]
Description=TasksBot (Flask via gunicorn)
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/home/ubuntu/tasksbot
Environment="TELEGRAM_TOKEN=${TELEGRAM_TOKEN}"
Environment="WEBHOOK_BASE=${WEBHOOK_BASE}"
Environment="DATABASE_URL=${DATABASE_URL}"
Environment="OPENAI_API_KEY=${OPENAI_API_KEY}"
Environment="TZ=${TZ}"
ExecStart=/home/ubuntu/tasksbot/.venv/bin/gunicorn -w 2 -b 127.0.0.1:5000 tasks_bot:app
Restart=always

[Install]
WantedBy=multi-user.target
```
Затем:
```bash
sudo systemctl daemon-reload
sudo systemctl enable tasksbot
sudo systemctl start tasksbot
sudo systemctl status tasksbot
```

## 5) Nginx (рекомендация)
Проксируйте 443/80 → 127.0.0.1:5000.

```
server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

Перезапуск:
```bash
sudo systemctl restart nginx
```

## 6) Проверка
- Убедитесь, что ваш `/WEBHOOK_BASE/TELEGRAM_TOKEN` доступен из интернета.
- Отправьте `/start` боту.
```