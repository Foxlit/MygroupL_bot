#!/bin/bash
# start.sh

echo "🚀 Запуск Render entrypoint..."

# Создаём папку для БД если нет
mkdir -p shared-data

# Пытаемся скачать БД из GitHub
echo "🔄 Попытка загрузки базы данных из GitHub..."
python -c "
import os
from git_db_sync import GitDatabaseSync
from pathlib import Path

db_sync = GitDatabaseSync(
    repo_path=os.path.dirname(__file__),
    db_path=Path('shared-data') / 'bot_data.db',
    branch='data'
)
if db_sync.download_db():
    print('✅ База данных загружена из GitHub')
else:
    print('⚠️ База данных не найдена в GitHub, будет создана новая')
    # Создаём новую БД
    import subprocess
    subprocess.run(['python', 'scripts/init_db.py'])
"

# Запускаем основное приложение
echo "🚀 Запуск приложения..."
python app.py
