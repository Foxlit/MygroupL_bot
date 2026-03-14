#!/bin/bash
# start.sh

echo "🚀 Запуск Render entrypoint..."

# Создаём папку для БД если нет
mkdir -p shared-data

# Запускаем инициализацию БД
python scripts/init_db.py

# Запускаем основное приложение
python app.py
