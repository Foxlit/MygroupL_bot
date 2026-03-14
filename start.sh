#!/bin/bash
# start.sh

echo "🚀 Запуск Render entrypoint..."

# Создаём папку для БД если нет
mkdir -p shared-data

# Получаем абсолютный путь к проекту
PROJECT_DIR="$(pwd)"
echo "📁 Проект в: $PROJECT_DIR"

# Пытаемся скачать БД из GitHub
echo "🔄 Попытка загрузки базы данных из GitHub..."
python -c "
import os
import sys
sys.path.insert(0, '$PROJECT_DIR')
from git_db_sync import GitDatabaseSync
from pathlib import Path

print('🔄 Инициализация синхронизатора...')
db_sync = GitDatabaseSync(
    repo_path='$PROJECT_DIR',
    db_path=Path('$PROJECT_DIR') / 'shared-data' / 'bot_data.db',
    branch='data'
)
print('✅ Синхронизатор создан')

if db_sync.download_db():
    print('✅ База данных загружена из GitHub')
else:
    print('⚠️ База данных не найдена в GitHub, будет создана новая')
    # Создаём новую БД
    import subprocess
    subprocess.run([sys.executable, 'scripts/init_db.py'], cwd='$PROJECT_DIR')
"

# Запускаем основное приложение
echo "🚀 Запуск приложения..."
python app.py
