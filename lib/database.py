import base64
import binascii
import logging
import os
import tempfile
from contextlib import closing
from pathlib import Path

from sqlcipher3 import dbapi2 as sqlcipher

_log = logging.getLogger(__name__)

SQLITE_HEADER = b"SQLite format 3\x00"
KEY_SIZE_BYTES = 32


class DatabaseEncryptionError(RuntimeError):
    """Raised when the encrypted database cannot be opened or migrated safely."""


def load_database_encryption_key() -> bytes:
    """Load a 256-bit, base64-encoded database key from a secret or environment."""
    key_file = os.getenv("DATABASE_ENCRYPTION_KEY_FILE")
    key_value = os.getenv("DATABASE_ENCRYPTION_KEY")

    if key_file and key_value:
        raise DatabaseEncryptionError(
            "Set only one of DATABASE_ENCRYPTION_KEY_FILE or DATABASE_ENCRYPTION_KEY."
        )

    if key_file:
        try:
            key_value = Path(key_file).read_text(encoding="ascii").strip()
        except OSError as error:
            raise DatabaseEncryptionError(
                "Could not read DATABASE_ENCRYPTION_KEY_FILE."
            ) from error

    if not key_value:
        raise DatabaseEncryptionError(
            "Database encryption is mandatory. Set DATABASE_ENCRYPTION_KEY_FILE "
            "or DATABASE_ENCRYPTION_KEY to a base64-encoded 32-byte key."
        )

    try:
        key = base64.b64decode(key_value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise DatabaseEncryptionError(
            "The database encryption key must be valid base64."
        ) from error

    if len(key) != KEY_SIZE_BYTES:
        raise DatabaseEncryptionError(
            "The database encryption key must decode to exactly 32 bytes."
        )

    return key


class EncryptedDatabase:
    """Small SQLCipher database wrapper with safe one-time plaintext migration."""

    def __init__(
        self,
        path: str,
        encryption_key: bytes,
        *,
        enforce_permissions: bool = True,
    ):
        if not path:
            raise DatabaseEncryptionError("PATH_TO_DB must not be empty.")
        if len(encryption_key) != KEY_SIZE_BYTES:
            raise DatabaseEncryptionError(
                "The database encryption key must contain exactly 32 bytes."
            )

        self.path = Path(path)
        self._enforce_permissions = enforce_permissions
        self._key_hex = encryption_key.hex()
        self._key_pragma = f"PRAGMA key = \"x'{self._key_hex}'\""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._prepare_database()

    def _apply_key(self, connection):
        cipher_version = connection.execute("PRAGMA cipher_version").fetchone()
        if not cipher_version or not cipher_version[0]:
            raise DatabaseEncryptionError(
                "The SQLite driver does not support SQLCipher."
            )

        connection.execute(self._key_pragma)
        connection.execute("PRAGMA cipher_memory_security = ON")

    def _connect(self, path: Path | None = None):
        connection = sqlcipher.connect(str(path or self.path), timeout=30)
        try:
            self._apply_key(connection)
            connection.execute("SELECT count(*) FROM sqlite_master").fetchone()
        except Exception:
            connection.close()
            raise
        return connection

    @staticmethod
    def _integrity_check(connection, schema: str = "main"):
        result = connection.execute(f"PRAGMA {schema}.integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise DatabaseEncryptionError(
                f"Database integrity check failed for schema {schema}."
            )

    @staticmethod
    def _table_counts(connection, schema: str = "main") -> dict[str, int]:
        table_rows = connection.execute(
            f"SELECT name FROM {schema}.sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        counts = {}
        for (table_name,) in table_rows:
            quoted_name = table_name.replace('"', '""')
            counts[table_name] = connection.execute(
                f'SELECT count(*) FROM {schema}."{quoted_name}"'
            ).fetchone()[0]
        return counts

    def _prepare_database(self):
        if not self.path.exists() or self.path.stat().st_size == 0:
            with closing(self._connect()):
                if self._enforce_permissions:
                    os.chmod(self.path, 0o600)
                return

        with self.path.open("rb") as database_file:
            is_plaintext = database_file.read(len(SQLITE_HEADER)) == SQLITE_HEADER

        if is_plaintext:
            if not self._enforce_permissions:
                raise DatabaseEncryptionError(
                    "Expected an encrypted SQLCipher database, but found plaintext."
                )
            self._migrate_plaintext_database()
            return

        try:
            with closing(self._connect()) as connection:
                self._integrity_check(connection)
            if self._enforce_permissions:
                os.chmod(self.path, 0o600)
        except Exception as error:
            raise DatabaseEncryptionError(
                "The database is encrypted with a different key or is corrupt."
            ) from error

    def _migrate_plaintext_database(self):
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.encrypting-",
            dir=self.path.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        try:
            with closing(sqlcipher.connect(str(self.path), timeout=30)) as source:
                cipher_version = source.execute("PRAGMA cipher_version").fetchone()
                if not cipher_version or not cipher_version[0]:
                    raise DatabaseEncryptionError(
                        "The SQLite driver does not support SQLCipher."
                    )

                self._integrity_check(source)
                source_counts = self._table_counts(source)
                user_version = source.execute("PRAGMA user_version").fetchone()[0]
                application_id = source.execute("PRAGMA application_id").fetchone()[0]

                source.execute(
                    f"ATTACH DATABASE ? AS encrypted KEY \"x'{self._key_hex}'\"",
                    (str(temporary_path),),
                )
                source.execute("SELECT sqlcipher_export('encrypted')")
                source.execute(f"PRAGMA encrypted.user_version = {user_version}")
                source.execute(f"PRAGMA encrypted.application_id = {application_id}")
                source.commit()
                source.execute("DETACH DATABASE encrypted")

            with closing(self._connect(temporary_path)) as encrypted:
                self._integrity_check(encrypted)
                if self._table_counts(encrypted) != source_counts:
                    raise DatabaseEncryptionError(
                        "Encrypted database verification found mismatched table data."
                    )

            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
            self._fsync_database_and_directory()
            self._remove_plaintext_sidecars()
            _log.info("Migrated the existing plaintext database to SQLCipher.")
        finally:
            temporary_path.unlink(missing_ok=True)

    def _fsync_database_and_directory(self):
        with self.path.open("rb") as database_file:
            os.fsync(database_file.fileno())

        directory_descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)

    def _remove_plaintext_sidecars(self):
        for suffix in ("-journal", "-shm", "-wal"):
            Path(f"{self.path}{suffix}").unlink(missing_ok=True)

    def run(self, query: str, params: tuple = ()):
        with closing(self._connect()) as connection:
            connection.execute(query, params)
            connection.commit()

    def run_and_get_id(self, query: str, params: tuple = ()) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(query, params)
            connection.commit()
            return cursor.lastrowid

    def run_many(self, query: str, rows: list[tuple]):
        with closing(self._connect()) as connection:
            connection.executemany(query, rows)
            connection.commit()

    def get(self, query: str, params: tuple = ()) -> list[tuple]:
        with closing(self._connect()) as connection:
            return connection.execute(query, params).fetchall()

    def get_one(self, query: str, params: tuple = ()) -> tuple | None:
        with closing(self._connect()) as connection:
            return connection.execute(query, params).fetchone()

    def backup(self, destination: str):
        destination_path = Path(destination)
        if destination_path.resolve() == self.path.resolve():
            raise DatabaseEncryptionError(
                "Backup destination must differ from the database."
            )
        if destination_path.exists() and destination_path.stat().st_size:
            raise FileExistsError(f"Refusing to overwrite {destination_path}.")
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        with (
            closing(self._connect()) as source,
            closing(self._connect(destination_path)) as target,
        ):
            source.backup(target)
            self._integrity_check(target)
        os.chmod(destination_path, 0o600)

    def export_plaintext(self, destination: str):
        """Export a decrypted copy for an administrator's local use."""
        destination_path = Path(destination)
        if destination_path.exists():
            raise FileExistsError(f"Refusing to overwrite {destination_path}.")
        destination_path.parent.mkdir(parents=True, exist_ok=True)

        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination_path.name}.decrypting-",
            dir=destination_path.parent,
        )
        os.close(file_descriptor)
        temporary_path = Path(temporary_name)

        try:
            with closing(self._connect()) as source:
                source_counts = self._table_counts(source)
                user_version = source.execute("PRAGMA user_version").fetchone()[0]
                application_id = source.execute("PRAGMA application_id").fetchone()[0]

                source.execute(
                    "ATTACH DATABASE ? AS plaintext KEY ''",
                    (str(temporary_path),),
                )
                source.execute("SELECT sqlcipher_export('plaintext')")
                source.execute(f"PRAGMA plaintext.user_version = {user_version}")
                source.execute(f"PRAGMA plaintext.application_id = {application_id}")
                source.commit()
                source.execute("DETACH DATABASE plaintext")

            with closing(
                sqlcipher.connect(str(temporary_path), timeout=30)
            ) as plaintext:
                self._integrity_check(plaintext)
                if self._table_counts(plaintext) != source_counts:
                    raise DatabaseEncryptionError(
                        "Decrypted database verification found mismatched table data."
                    )

            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, destination_path)
        finally:
            temporary_path.unlink(missing_ok=True)
