#!/usr/bin/env python3
"""
Точка входа для Render
Сначала создаёт БД, потом импортирует и запускает бота
"""

import os
import sys
import subprocess
import threading
from pathlib import Path

# Добавляем текущую папку в путь
sys.path.insert(0, os.path.dirname(__file__))


def init_database():
    """Создаёт базу данных через init_db.py"""
    print("🚀 Инициализация базы данных...")

    # Запускаем init_db.py
    result = subprocess.run(
        [sys.executable, "scripts/init_db.py"],
        capture_output=True,
        text=True,
        cwd=os.path.dirname(__file__)
    )

    if result.returncode != 0:
        print("❌ Ошибка при создании БД:")
        print(result.stderr)
        return False

    print("✅ База данных готова")
    if result.stdout:
        print(result.stdout)
    return True


def run_bot():
    """Импортирует и запускает бота (после создания БД)"""
    print("🚀 Запуск бота...")

    # Теперь импортируем бота
    from bot import main as bot_main
    bot_main()


if __name__ == "__main__":
    # Сначала создаём БД
    if not init_database():
        sys.exit(1)

    # Запускаем бота в отдельном потоке
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Запускаем Flask для health check
    from flask import Flask

    app = Flask(__name__)


    @app.route('/')
    @app.route('/health')
    def health():
        return "OK", 200


    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
