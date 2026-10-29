import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from backup_db import backup_database


class DatabaseBackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(
            prefix='igri-eltoi5-backup-test-'
        )
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / 'site.sqlite3'
        self.backup_directory = self.root / 'backups'
        with closing(sqlite3.connect(self.database_path)) as connection:
            connection.execute('CREATE TABLE sample (value TEXT NOT NULL)')
            connection.execute('INSERT INTO sample (value) VALUES (?)', ('saved',))
            connection.commit()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_backup_uses_sqlite_backup_and_passes_integrity_check(self):
        backup_path = backup_database(
            destination_dir=self.backup_directory,
            database_url=f'sqlite:///{self.database_path.as_posix()}',
        )

        self.assertTrue(backup_path.is_file())
        self.assertFalse(backup_path.is_relative_to(Path(__file__).resolve().parents[1]))
        with closing(sqlite3.connect(backup_path)) as connection:
            value = connection.execute('SELECT value FROM sample').fetchone()[0]
            integrity = connection.execute('PRAGMA integrity_check').fetchone()[0]
        self.assertEqual(value, 'saved')
        self.assertEqual(integrity, 'ok')

    def test_backup_retains_only_requested_number_of_copies(self):
        for value in ('first', 'second', 'third'):
            with closing(sqlite3.connect(self.database_path)) as connection:
                connection.execute('INSERT INTO sample (value) VALUES (?)', (value,))
                connection.commit()
            backup_database(
                destination_dir=self.backup_directory,
                keep=2,
                database_url=f'sqlite:///{self.database_path.as_posix()}',
            )

        backups = list(self.backup_directory.glob('drevo_zhizni_*.sqlite3'))
        self.assertEqual(len(backups), 2)

    def test_backup_refuses_destination_inside_project(self):
        with self.assertRaisesRegex(ValueError, 'вне папки проекта'):
            backup_database(
                destination_dir=Path(__file__).resolve().parents[1] / 'instance' / 'backups',
                database_url=f'sqlite:///{self.database_path.as_posix()}',
            )


if __name__ == '__main__':
    unittest.main()
