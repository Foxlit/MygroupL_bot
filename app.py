import os
import asyncio
import threading
import time
from pathlib import Path
from flask import Flask, make_response
from git_db_sync import GitDatabaseSync

# Создаём Flask приложение
app = Flask(__name__)

# Глобальный объект для синхронизации
backup_thread_running = False
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


def scheduled_backup():
    """Автоматическое сохранение БД каждые 5 минут"""
    global backup_thread_running
    backup_thread_running = True

    while backup_thread_running:
        time.sleep(300)  # 5 минут
        if db_sync:
            print(f"⏰ Автосохранение базы данных в {time.strftime('%H:%M')}")
            try:
                db_sync.upload_db(commit_message=f"Auto-backup {time.strftime('%Y-%m-%d %H:%M')}")
                print("✅ Автосохранение выполнено")
            except Exception as e:
                print(f"❌ Ошибка автосохранения: {e}")


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

    # Всегда скачиваем свежую версию
    return db_sync.download_db()


def save_database(commit_message=None):
    """Сохраняет базу данных в GitHub"""
    global db_sync
    if db_sync:
        if not commit_message:
            commit_message = f"Database update at {time.strftime('%Y-%m-%d %H:%M')}"
        print(f"💾 Сохраняю базу данных в GitHub...")
        db_sync.upload_db(commit_message=commit_message)


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
        save_database("Bot shutdown")


if __name__ == "__main__":
    # Загружаем базу из GitHub
    if not init_database():
        print("⚠️ Не удалось загрузить БД, будет создана новая")

    # Запускаем автосохранение
    if db_sync:
        backup_thread = threading.Thread(target=scheduled_backup, daemon=True)
        backup_thread.start()
        print("✅ Автосохранение запущено (каждые 5 минут)")

    # Запускаем Flask в отдельном потоке
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    time.sleep(2)

    print("🚀 Запуск бота...")
    try:
        run_bot()
    except KeyboardInterrupt:
        print("🛑 Получен сигнал завершения")
        backup_thread_running = False
        save_database("Manual shutdown")
