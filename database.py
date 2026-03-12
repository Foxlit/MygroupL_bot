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

    # ===== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ =====

    def format_date(self, date_str):
        """Преобразует дату из ISO в европейский формат"""
        if not date_str:
            return "неизвестно"
        try:
            if 'T' in date_str:
                date_part = date_str.split('T')[0]
                time_part = date_str.split('T')[1][:5] if len(date_str.split('T')) > 1 else ''
                year, month, day = date_part.split('-')
                return f"{day}.{month}.{year} {time_part}"
            elif ' ' in date_str:
                parts = date_str.split(' ')
                if len(parts) >= 2:
                    date_part, time_part = parts[0], parts[1]
                    year, month, day = date_part.split('-')
                    return f"{day}.{month}.{year} {time_part[:5]}"
                else:
                    return date_str
            else:
                return date_str
        except:
            return date_str

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

    def remove_from_whitelist(self, user_id: int):
        """Удаляет пользователя из белого списка"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM whitelist WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()

    def is_user_authorized(self, user_id: int) -> bool:
        """Проверяет, авторизован ли пользователь"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT is_authorized FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            return bool(row and row['is_authorized'])

    def authorize_user(self, user_id: int):
        """Авторизует пользователя"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE users 
                SET is_authorized = 1, auth_date = CURRENT_TIMESTAMP 
                WHERE user_id = ?
            """, (user_id,))
            conn.commit()

    def get_user_subscription(self, user_id: int) -> bool:
        """Проверяет подписку пользователя"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT subscribed FROM users WHERE user_id = ?",
                (user_id,)
            )
            row = cursor.fetchone()
            return bool(row and row['subscribed'])

    def toggle_subscription(self, user_id: int) -> bool:
        """Переключает подписку пользователя"""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Получаем текущий статус
            cursor.execute("SELECT subscribed FROM users WHERE user_id = ?", (user_id,))
            current = cursor.fetchone()

            if not current:
                # Если пользователя нет, создаём с подпиской
                cursor.execute("""
                    INSERT INTO users (user_id, subscribed) 
                    VALUES (?, 1)
                """, (user_id,))
                conn.commit()
                return True

            # Переключаем
            new_status = not current['subscribed']
            cursor.execute(
                "UPDATE users SET subscribed = ? WHERE user_id = ?",
                (new_status, user_id)
            )
            conn.commit()
            return new_status

    def get_subscribed_users(self) -> List[int]:
        """Получает список подписанных пользователей"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id FROM users 
                WHERE subscribed = 1 AND is_authorized = 1
            """)
            return [row[0] for row in cursor.fetchall()]

    def get_authorized_users(self) -> List[int]:
        """Получает список авторизованных пользователей"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE is_authorized = 1")
            return [row[0] for row in cursor.fetchall()]

    # ===== РАБОТА С БЕЛЫМ СПИСКОМ =====

    def is_in_whitelist(self, user_id: int) -> bool:
        """Проверяет, есть ли пользователь в белом списке"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM whitelist WHERE user_id = ?",
                (user_id,)
            )
            return cursor.fetchone() is not None

    def add_to_whitelist(self, user_id: int, added_by: int, comment: str = ""):
        """Добавляет пользователя в белый список"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR IGNORE INTO whitelist (user_id, added_by, comment)
                VALUES (?, ?, ?)
            """, (user_id, added_by, comment))
            conn.commit()

    def remove_from_whitelist(self, user_id: int):
        """Удаляет пользователя из белого списка"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "DELETE FROM whitelist WHERE user_id = ?",
                (user_id,)
            )
            conn.commit()

    def get_whitelist(self) -> List[Dict]:
        """Получает весь белый список"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM whitelist ORDER BY added_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    # ===== РАБОТА СО ССЫЛКАМИ =====

    def save_link(self, par_name: str, link: str) -> int:
        """Сохраняет ссылку"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO links (par_name, link) VALUES (?, ?)",
                (par_name, link)
            )
            conn.commit()
            return cursor.lastrowid

    def get_pending_links(self) -> List[Dict]:
        """Получает неотправленные ссылки"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM links 
                WHERE notified = 0 
                ORDER BY parsed_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]

    def mark_link_notified(self, link_id: int):
        """Отмечает ссылку как отправленную"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE links SET notified = 1 WHERE id = ?",
                (link_id,)
            )
            conn.commit()

    def get_today_links(self) -> List[Dict]:
        """Получает сегодняшние ссылки"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM links 
                WHERE date(parsed_at) = date('now')
                ORDER BY parsed_at DESC
            """)
            return [dict(row) for row in cursor.fetchall()]
    
    def format_username(self, username):
        """Форматирует username с @ или 'нет'"""
        if username and username != 'нет' and username != 'None':
            return f"@{username}"
        return "нет"

    # ===== ЛОГИРОВАНИЕ =====

    def add_log(self, user_id: int, action: str, level: str, message: str):
        """Добавляет запись в лог"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO logs (user_id, action, level, message) VALUES (?, ?, ?, ?)",
                (user_id, action, level, message)
            )
            conn.commit()


# Создаем глобальный экземпляр для удобства
db = Database()


# ===== ФУНКЦИИ ДЛЯ БЫСТРОГО ИМПОРТА =====
def add_user(user_id, username=None, first_name=None):
    db.add_user(user_id, username, first_name)


def remove_from_whitelist(user_id):
    db.remove_from_whitelist(user_id)


def is_authorized(user_id):
    return db.is_user_authorized(user_id)


def authorize_user(user_id):
    db.authorize_user(user_id)


def get_user_subscription(user_id):
    return db.get_user_subscription(user_id)


def toggle_subscription(user_id):
    return db.toggle_subscription(user_id)


def get_subscribed_users():
    return db.get_subscribed_users()


def is_in_whitelist(user_id):
    return db.is_in_whitelist(user_id)


def add_to_whitelist(user_id, added_by, comment=""):
    db.add_to_whitelist(user_id, added_by, comment)


def get_whitelist():
    return db.get_whitelist()


def get_pending_links():
    return db.get_pending_links()


def mark_link_notified(link_id):
    db.mark_link_notified(link_id)


def get_today_links():
    return db.get_today_links()


def add_log(user_id, action, level, message):
    db.add_log(user_id, action, level, message)
