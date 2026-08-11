import base64
import gzip
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.database import (
    SQLITE_HEADER,
    DatabaseEncryptionError,
    EncryptedDatabase,
    load_database_encryption_key,
)
from scripts.database_crypto import (
    copy_plaintext_database,
    decrypt_database,
    preflight_plaintext_migration,
    verify_encrypted_database,
)


class EncryptedDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.key = bytes(range(32))
        self.encoded_key = base64.b64encode(self.key).decode("ascii")

    def tearDown(self):
        self.temporary_directory.cleanup()

    def create_plaintext_database(self, path: Path):
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA user_version = 17")
        connection.execute("PRAGMA application_id = 123456")
        connection.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        connection.executemany(
            "INSERT INTO users(name) VALUES (?)",
            [("alice",), ("bob",)],
        )
        connection.execute("CREATE VIRTUAL TABLE documents USING fts5(body)")
        connection.executemany(
            "INSERT INTO documents(body) VALUES (?)",
            [("hello world",), ("encrypted data",)],
        )
        connection.commit()
        connection.close()

    def test_plaintext_database_is_migrated_and_verified(self):
        database_path = self.directory / "db.sqlite3"
        self.create_plaintext_database(database_path)
        os.chmod(database_path, 0o644)

        database = EncryptedDatabase(str(database_path), self.key)

        self.assertFalse(database_path.read_bytes().startswith(SQLITE_HEADER))
        self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            database.get("SELECT * FROM users ORDER BY id"),
            [(1, "alice"), (2, "bob")],
        )
        self.assertEqual(
            database.get(
                "SELECT body FROM documents WHERE documents MATCH 'encrypted'"
            ),
            [("encrypted data",)],
        )
        self.assertEqual(database.get_one("PRAGMA user_version"), (17,))
        self.assertEqual(database.get_one("PRAGMA application_id"), (123456,))

    def test_preflight_migrates_a_copy_without_changing_the_source(self):
        source_path = self.directory / "source.sqlite3"
        destination_path = self.directory / "preflight.sqlite3"
        self.create_plaintext_database(source_path)

        with patch.dict(
            os.environ,
            {"DATABASE_ENCRYPTION_KEY": self.encoded_key},
            clear=True,
        ):
            preflight_plaintext_migration(source_path, destination_path)

        self.assertTrue(source_path.read_bytes().startswith(SQLITE_HEADER))
        self.assertFalse(destination_path.read_bytes().startswith(SQLITE_HEADER))
        migrated_copy = EncryptedDatabase(str(destination_path), self.key)
        self.assertEqual(migrated_copy.get_one("SELECT count(*) FROM users"), (2,))

    def test_plaintext_snapshot_is_consistent_and_private(self):
        source_path = self.directory / "source.sqlite3"
        snapshot_path = self.directory / "snapshot.sqlite3"
        self.create_plaintext_database(source_path)

        copy_plaintext_database(source_path, snapshot_path)

        self.assertTrue(snapshot_path.read_bytes().startswith(SQLITE_HEADER))
        self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)
        snapshot = sqlite3.connect(snapshot_path)
        try:
            self.assertEqual(
                snapshot.execute("SELECT count(*) FROM users").fetchone(),
                (2,),
            )
        finally:
            snapshot.close()

    def test_fresh_database_crud_and_permissions(self):
        database_path = self.directory / "fresh.sqlite3"
        database = EncryptedDatabase(str(database_path), self.key)
        database.run("CREATE TABLE values_table (id INTEGER PRIMARY KEY, value TEXT)")
        first_id = database.run_and_get_id(
            "INSERT INTO values_table(value) VALUES (?)",
            ("one",),
        )
        database.run_many(
            "INSERT INTO values_table(value) VALUES (?)",
            [("two",), ("three",)],
        )

        self.assertEqual(first_id, 1)
        self.assertEqual(database.get_one("SELECT count(*) FROM values_table"), (3,))
        self.assertFalse(database_path.read_bytes().startswith(SQLITE_HEADER))
        self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)

    def test_backup_is_encrypted_consistent_and_decryptable(self):
        database_path = self.directory / "db.sqlite3"
        self.create_plaintext_database(database_path)
        database = EncryptedDatabase(str(database_path), self.key)
        backup_path = self.directory / "backup.sqlite3"

        database.backup(str(backup_path))

        self.assertFalse(backup_path.read_bytes().startswith(SQLITE_HEADER))
        self.assertEqual(backup_path.stat().st_mode & 0o777, 0o600)
        backup = EncryptedDatabase(str(backup_path), self.key)
        self.assertEqual(backup.get_one("SELECT count(*) FROM users"), (2,))

        compressed_path = self.directory / "backup.sqlite3.gz"
        with (
            backup_path.open("rb") as source,
            gzip.open(compressed_path, "wb") as destination,
        ):
            destination.write(source.read())

        plaintext_path = self.directory / "decrypted.sqlite3"
        with patch.dict(
            os.environ,
            {"DATABASE_ENCRYPTION_KEY": self.encoded_key},
            clear=True,
        ):
            decrypt_database(compressed_path, plaintext_path)

        self.assertTrue(plaintext_path.read_bytes().startswith(SQLITE_HEADER))
        plaintext = sqlite3.connect(plaintext_path)
        try:
            self.assertEqual(
                plaintext.execute("SELECT count(*) FROM documents").fetchone(),
                (2,),
            )
        finally:
            plaintext.close()

    def test_wrong_key_is_rejected(self):
        database_path = self.directory / "db.sqlite3"
        database = EncryptedDatabase(str(database_path), self.key)
        database.run("CREATE TABLE data (value TEXT)")

        with self.assertRaises(DatabaseEncryptionError):
            EncryptedDatabase(str(database_path), bytes(range(1, 33)))

    def test_read_only_verification_does_not_change_permissions(self):
        database_path = self.directory / "db.sqlite3"
        database = EncryptedDatabase(str(database_path), self.key)
        database.run("CREATE TABLE data (value TEXT)")
        os.chmod(database_path, 0o640)

        with patch.dict(
            os.environ,
            {"DATABASE_ENCRYPTION_KEY": self.encoded_key},
            clear=True,
        ):
            verify_encrypted_database(database_path)

        self.assertEqual(database_path.stat().st_mode & 0o777, 0o640)

    def test_key_loading_requires_exactly_one_valid_32_byte_key(self):
        key_file = self.directory / "database-key"
        key_file.write_text(self.encoded_key, encoding="ascii")

        with patch.dict(
            os.environ,
            {"DATABASE_ENCRYPTION_KEY_FILE": str(key_file)},
            clear=True,
        ):
            self.assertEqual(load_database_encryption_key(), self.key)

        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(DatabaseEncryptionError),
        ):
            load_database_encryption_key()

        with (
            patch.dict(
                os.environ,
                {"DATABASE_ENCRYPTION_KEY": base64.b64encode(b"too short").decode()},
                clear=True,
            ),
            self.assertRaises(DatabaseEncryptionError),
        ):
            load_database_encryption_key()


if __name__ == "__main__":
    unittest.main()
