#!/bin/bash
set -e

python3 -m venv .venv
.venv/bin/pip install --upgrade pip -q
.venv/bin/pip install -r requirements.txt

if [ ! -f .env ]; then
    cp .env.example .env
    echo "Создан .env — заполните переменные перед запуском."
fi

echo "Готово. Запуск: .venv/bin/python bot.py"
