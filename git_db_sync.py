import os
import shutil
import tempfile
from pathlib import Path
from git import Repo
import logging

logger = logging.getLogger(__name__)


class GitDatabaseSync:
    """Синхронизирует SQLite базу данных с GitHub веткой data"""

    def __init__(self, repo_path, db_path, branch='data'):
        self.repo_path = Path(repo_path)
        self.db_path = Path(db_path)
        self.branch = branch
        self.repo = None
        self.temp_dir = None

    def clone_repo(self):
        """Клонирует репозиторий во временную папку"""
        self.temp_dir = tempfile.mkdtemp()

        # Отладка: выводим значение переменной
        repo_name_raw = os.environ.get('GITHUB_REPO', 'НЕ ЗАДАНО')
        print(f"🔍 GITHUB_REPO из окружения: '{repo_name_raw}'")

        # Очищаем название репозитория
        repo_name = repo_name_raw.rstrip('/').rstrip('.git')
        print(f"🔍 Очищенное название: '{repo_name}'")

        repo_url = f"https://{os.environ['GITHUB_TOKEN']}@github.com/{repo_name}.git"
        print(f"🔍 URL для клонирования: {repo_url.replace(os.environ['GITHUB_TOKEN'], '*****')}")

        logger.info(f"📥 Клонирую репозиторий {repo_name} в {self.temp_dir}")
        self.repo = Repo.clone_from(repo_url, self.temp_dir, branch=self.branch)
        return self.temp_dir

    def download_db(self):
        """Скачивает базу данных из GitHub"""
        try:
            temp_path = self.clone_repo()
            github_db = Path(temp_path) / "shared-data" / "bot_data.db"

            if github_db.exists():
                # Создаём папку назначения, если её нет
                self.db_path.parent.mkdir(exist_ok=True)
                # Копируем БД из GitHub в рабочую папку
                shutil.copy2(github_db, self.db_path)
                logger.info(f"✅ База данных загружена из GitHub: {github_db}")
                return True
            else:
                logger.warning("⚠️ База данных не найдена в GitHub, будет создана новая")
                return False

        except Exception as e:
            logger.error(f"❌ Ошибка при загрузке БД: {e}")
            return False

    def upload_db(self, commit_message="Auto-update database"):
        """Загружает базу данных в GitHub"""
        try:
            if not self.repo:
                self.clone_repo()

            # Создаём папку shared-data в репозитории, если её нет
            repo_db_dir = Path(self.temp_dir) / "shared-data"
            repo_db_dir.mkdir(exist_ok=True)

            # Копируем БД в репозиторий
            repo_db = repo_db_dir / "bot_data.db"
            shutil.copy2(self.db_path, repo_db)

            # Проверяем, есть ли изменения
            self.repo.index.add([str(repo_db.relative_to(self.temp_dir))])

            if self.repo.index.diff("HEAD") or self.repo.untracked_files:
                self.repo.index.commit(commit_message)
                self.repo.remote().push()
                logger.info(f"✅ База данных загружена в GitHub: {commit_message}")
            else:
                logger.info("📭 Нет изменений в базе данных")

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка при загрузке БД: {e}")
            return False

    def cleanup(self):
        """Очищает временные файлы"""
        if self.temp_dir and Path(self.temp_dir).exists():
            shutil.rmtree(self.temp_dir)
            logger.info("🧹 Временные файлы удалены")
