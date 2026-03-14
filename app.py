import os
import sys
import asyncio
import threading
from flask import Flask

# Создаём Flask приложение
app = Flask(__name__)


@app.route('/')
@app.route('/health')
def health():
    return "OK", 200


def run_flask():
    """Запускает Flask сервер в отдельном потоке"""
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port, threaded=True)


def init_database():
    """Создаёт базу данных через init_db.py"""
    print("🚀 Проверяю базу данных...")

    import subprocess
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
    return True


if __name__ == "__main__":
    # Сначала создаём БД
    if not init_database():
        sys.exit(1)

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Даём Flask время запуститься
    import time

    time.sleep(2)

    # Запускаем бота в главном потоке (здесь можно работать с сигналами)
    print("🚀 Запуск бота в главном потоке...")

    try:
        from bot import main as bot_main

        bot_main()
    except Exception as e:
        print(f"❌ Ошибка в боте: {e}")
        import traceback

        traceback.print_exc()
