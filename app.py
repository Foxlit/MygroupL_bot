import os
import threading
import subprocess
import sys
from flask import Flask
from bot import main as run_bot

# Создаём Flask приложение
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is running!", 200


@app.route('/health')
def health():
    return "OK", 200


def run_bot_thread():
    """Запускает бота в отдельном потоке"""
    run_bot()


if __name__ == "__main__":
    # Сначала создаём базу данных, если её нет
    print("🚀 Проверяю базу данных...")

    # Запускаем init_db.py как отдельный процесс
    result = subprocess.run([sys.executable, "scripts/init_db.py"],
                            capture_output=True, text=True)

    if result.returncode == 0:
        print("✅ База данных готова")
        if result.stdout:
            print(result.stdout)
    else:
        print("❌ Ошибка при создании БД:")
        print(result.stderr)
        sys.exit(1)

    # Запускаем бота в фоне
    bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
    bot_thread.start()

    # Запускаем Flask сервер
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
