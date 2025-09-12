# TasksBot Deploy (bottg.online)

Быстрый деплой Telegram-бота через **Docker + Caddy** (авто-HTTPS).

## Шаги

### 1) На сервере: клонировать и подготовить код
```bash
git clone https://github.com/<your_account>/<your_repo>.git ~/tasksbot
cd ~/tasksbot
mkdir -p app
unzip ~/tasksbot_min.zip -d app   # распакуй сюда свой минимальный архив
```

### 2) Создать .env
```bash
cp env/.env.example .env
nano .env
```
Заполни: `TELEGRAM_TOKEN`, `DATABASE_URL`, `OPENAI_API_KEY (опц.)`.
`WEBHOOK_BASE` уже выставлен на `https://bottg.online` (можешь поменять).

### 3) Запуск
```bash
docker compose up -d --build
```

### 4) Проверка
```bash
curl -I https://bottg.online/healthz
```

### 5) Выставить webhook
```bash
bash scripts/set_webhook.sh
```

---

## Полезные команды
```bash
docker compose logs -f app
docker compose up -d --build
```

### Development

В репо добавлен простой `Makefile`:

```bash
make fmt   # форматирование (black)
make lint  # статический анализ (ruff)
make test  # pytest с покрытием
```

Конфиги инструментов находятся в `pyproject.toml`.
