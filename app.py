import os
import sys
import asyncio
import threading
import time
from pathlib import Path
from flask import Flask, make_response
from git_db_sync import GitDatabaseSync

# Создаём Flask приложение
app = Flask(__name__)

# Глобальный объект для синхронизации
db_sync = None


@app.route('/')
def home():
    return "Bot is running!", 200


@app.route('/health')
@app.route('/ping')
def health():
    response = make_response("OK", 200)
    response.headers['Content-Type'] = 'text/plain'
    return response


def run_flask():
    """Запускает Flask сервер в отдельном потоке"""
    port = int(os.environ.get('PORT', 10000))
    import logging
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.ERROR)
    app.run(host='0.0.0.0', port=port, threaded=True)


def init_database():
    """Инициализирует базу данных из GitHub"""
    global db_sync

    print("🔄 Синхронизация базы данных с GitHub...")

    # Проверяем переменные окружения
    if not os.environ.get('GITHUB_TOKEN'):
        print("❌ GITHUB_TOKEN не задан")
        return False

    if not os.environ.get('GITHUB_REPO'):
        print("❌ GITHUB_REPO не задан (формат: username/repo)")
        return False

    # Создаём папку shared-data, если её нет
    Path(__file__).parent.joinpath("shared-data").mkdir(exist_ok=True)

    # Создаём синхронизатор
    db_sync = GitDatabaseSync(
        repo_path=os.path.dirname(__file__),
        db_path=Path(__file__).parent / "shared-data" / "bot_data.db",
        branch='data'
    )

    # Скачиваем базу
    return db_sync.download_db()


def save_database():
    """Сохраняет базу данных в GitHub"""
    global db_sync
    if db_sync:
        print("💾 Сохраняю базу данных в GitHub...")
        db_sync.upload_db()
        db_sync.cleanup()


def run_bot():
    """Запускает бота"""
    # Создаём event loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        from bot import main as bot_main
        bot_main()
    except Exception as e:
        print(f"❌ Ошибка в боте: {e}")
        import traceback
        traceback.print_exc()
    finally:
        loop.close()
        # При завершении сохраняем БД
        save_database()


if __name__ == "__main__":
    # Загружаем базу из GitHub
    if not init_database():
        print("⚠️ Не удалось загрузить БД, будет создана новая")

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    time.sleep(2)

    print("🚀 Запуск бота...")
    try:
        run_bot()
    except KeyboardInterrupt:
        print("🛑 Получен сигнал завершения")
        save_database()
