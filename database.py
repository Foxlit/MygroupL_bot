"""
Модуль для работы с SQLite базой данных
Автоматически сгенерирован скриптом init_db.py
"""

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

DB_PATH = Path(__file__).parent / "shared-data" / "bot_data.db"


class Database:
    """Класс для работы с базой данных"""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self._ensure_db_exists()

    def _ensure_db_exists(self):
        """Проверяет существование БД"""
        if not self.db_path.exists():
            raise FileNotFoundError(f"База данных не найдена: {self.db_path}")

    def _get_connection(self):
        """Возвращает подключение к БД"""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    # ===== РАБОТА С ВЕБИНАРАМИ =====

    def save_webinar_link(self, par_name: str, link: str) -> int:
        """Сохраняет ссылку на вебинар"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO links (par_name, link) VALUES (?, ?)",
                (par_name, link)
            )
            conn.commit()
            return cursor.lastrowid

    def get_pending_webinars(self) -> List[Dict]:
        """Получает неотправленные ссылки на вебинары"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM links 
                WHERE notified = 0 
                ORDER BY parsed_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def mark_webinar_notified(self, link_id: int):
        """Отмечает ссылку как отправленную"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE links SET notified = 1 WHERE id = ?",
                (link_id,)
            )
            conn.commit()

    def get_today_webinars(self) -> List[Dict]:
        """Получает сегодняшние вебинары"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM links 
                WHERE date(parsed_at) = date('now')
                ORDER BY parsed_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    # ===== РАБОТА С ПОЛЬЗОВАТЕЛЯМИ =====

    def add_user(self, user_id: int, username: str = None, first_name: str = None):
        """Добавляет или обновляет пользователя"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_active)
                VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_active = CURRENT_TIMESTAMP
            """, (user_id, username, first_name))
            conn.commit()

    def get_all_users(self, only_subscribed=True) -> List[int]:
        """Получает список всех пользователей"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if only_subscribed:
                cursor.execute("SELECT user_id FROM users WHERE subscribed = 1")
            else:
                cursor.execute("SELECT user_id FROM users")
            return [row[0] for row in cursor.fetchall()]

    def unsubscribe_user(self, user_id: int):
        """Отписывает пользователя от уведомлений"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET subscribed = 0 WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()

    # ===== ЛОГИРОВАНИЕ =====

    def add_log(self, level: str, message: str):
        """Добавляет запись в лог"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO logs (level, message) VALUES (?, ?)",
                (level, message)
            )
            conn.commit()

    def get_recent_logs(self, limit: int = 100) -> List[Dict]:
        """Получает последние записи логов"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM logs ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]


# Создаем глобальный экземпляр для удобства
db = Database()


# Функции для обратной совместимости
def save_webinar_link(par_name, link):
    return db.save_webinar_link(par_name, link)


def get_pending_webinars():
    return db.get_pending_webinars()


def mark_webinar_notified(link_id):
    db.mark_webinar_notified(link_id)


def add_user(user_id, username=None, first_name=None):
    db.add_user(user_id, username, first_name)


def get_all_users():
    return db.get_all_users()


def unsubscribe_user(user_id):
    db.unsubscribe_user(user_id)


def add_log(level, message):
    db.add_log(level, message)
