import os
import threading
from flask import Flask
from bot import main as run_bot

# Создаём Flask приложение
app = Flask(__name__)


@app.route('/')
def home():
    return "Bot is running!", 200


@app.route('/health')
def health():
    """Эндпоинт для проверки здоровья (и для пинга от cron-job.org)"""
    return "OK", 200


def run_bot_thread():
    """Запускает бота в отдельном потоке"""
    run_bot()


if __name__ == "__main__":
    # Запускаем бота в фоне
    bot_thread = threading.Thread(target=run_bot_thread, daemon=True)
    bot_thread.start()

    # Запускаем Flask сервер на порту из окружения Render
    port = int(os.environ.get('PORT', 10000))
    app.run(host='0.0.0.0', port=port)
