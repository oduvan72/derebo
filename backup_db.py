import argparse
import os
import sqlite3
import tempfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.engine import make_url


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE_URL = 'sqlite:///drevo_zhizni.db'
BACKUP_PREFIX = 'drevo_zhizni_'
BACKUP_SUFFIX = '.sqlite3'


def resolve_database_path(database_url=None):
    url = make_url(database_url or os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL))
    if url.get_backend_name() != 'sqlite':
        raise ValueError('Резервное копирование поддерживает только SQLite.')
    if not url.database or url.database == ':memory:':
        raise ValueError('В DATABASE_URL должна быть указана файловая база SQLite.')

    database_path = Path(url.database)
    if not database_path.is_absolute():
        database_path = PROJECT_ROOT / 'instance' / database_path
    return database_path.resolve()


def _is_inside(path, directory):
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def backup_database(destination_dir=None, keep=7, database_url=None):
    if keep < 1:
        raise ValueError('Количество хранимых резервных копий должно быть не меньше 1.')

    source_path = resolve_database_path(database_url)
    if not source_path.is_file():
        raise FileNotFoundError(f'Файл базы данных не найден: {source_path}')

    backup_dir = Path(destination_dir or Path.home() / 'igri-eltoi5-backups').resolve()
    if _is_inside(backup_dir, PROJECT_ROOT):
        raise ValueError(
            'Резервные копии должны храниться вне папки проекта.'
        )
    if backup_dir == source_path:
        raise ValueError('Папка резервных копий не может быть файлом базы данных.')
    backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup_path = backup_dir / f'{BACKUP_PREFIX}{timestamp}{BACKUP_SUFFIX}'
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f'.{BACKUP_PREFIX}',
        suffix='.tmp',
        dir=backup_dir,
    )
    os.close(descriptor)
    temporary_path = Path(temporary_name)

    try:
        with closing(sqlite3.connect(source_path, timeout=30)) as source:
            with closing(sqlite3.connect(temporary_path, timeout=30)) as destination:
                source.backup(destination)
                integrity = destination.execute('PRAGMA integrity_check').fetchone()
                if integrity is None or integrity[0] != 'ok':
                    raise sqlite3.DatabaseError(
                        f'Проверка резервной копии не пройдена: {integrity}'
                    )
        os.replace(temporary_path, backup_path)
    finally:
        temporary_path.unlink(missing_ok=True)

    existing_backups = sorted(
        backup_dir.glob(f'{BACKUP_PREFIX}*{BACKUP_SUFFIX}'),
        key=lambda path: path.name,
        reverse=True,
    )
    for old_backup in existing_backups[keep:]:
        old_backup.unlink()
    return backup_path


def main():
    parser = argparse.ArgumentParser(
        description='Создать и проверить резервную копию SQLite вне папки проекта.'
    )
    parser.add_argument(
        '--destination',
        type=Path,
        default=Path.home() / 'igri-eltoi5-backups',
        help='Каталог для копий (по умолчанию: ~/igri-eltoi5-backups).',
    )
    parser.add_argument(
        '--keep',
        type=int,
        default=7,
        help='Сколько последних копий хранить (по умолчанию: 7).',
    )
    args = parser.parse_args()
    backup_path = backup_database(args.destination, args.keep)
    print(f'Резервная копия создана и проверена: {backup_path}')


if __name__ == '__main__':
    main()
