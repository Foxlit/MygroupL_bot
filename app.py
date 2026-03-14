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


def run_bot():
    """Запускает бота в отдельном потоке с собственным event loop"""
    # Создаём новый event loop для этого потока
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        # Импортируем бота ПОСЛЕ настройки event loop
        from bot import main as bot_main
        bot_main()
    except Exception as e:
        print(f"❌ Ошибка в боте: {e}")
        import traceback
        traceback.print_exc()
    finally:
        loop.close()


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

    # Запускаем бота в отдельном потоке с правильным event loop
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    # Даём боту время на запуск
    import time

    time.sleep(2)

    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
