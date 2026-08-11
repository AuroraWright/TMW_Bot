import argparse
import gzip
import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path

from lib.database import (
    SQLITE_HEADER,
    DatabaseEncryptionError,
    EncryptedDatabase,
    load_database_encryption_key,
)


@contextmanager
def uncompressed_database(source_path: Path):
    if source_path.suffix != ".gz":
        yield source_path
        return

    with tempfile.NamedTemporaryFile(
        prefix="tmw-db-decrypt-",
        suffix=".sqlcipher.sqlite3",
    ) as temporary_file:
        with gzip.open(source_path, "rb") as source:
            shutil.copyfileobj(source, temporary_file)
        temporary_file.flush()
        yield Path(temporary_file.name)


def decrypt_database(source_path: Path, destination_path: Path):
    with uncompressed_database(source_path) as database_path:
        with database_path.open("rb") as database_file:
            if database_file.read(len(SQLITE_HEADER)) == SQLITE_HEADER:
                raise DatabaseEncryptionError(
                    "The input database is already plaintext."
                )

        database = EncryptedDatabase(
            str(database_path),
            load_database_encryption_key(),
        )
        database.export_plaintext(str(destination_path))


def create_backup(source_path: Path, destination_path: Path):
    database = EncryptedDatabase(
        str(source_path),
        load_database_encryption_key(),
    )
    database.backup(str(destination_path))


def copy_plaintext_database(source_path: Path, destination_path: Path):
    if destination_path.exists():
        raise FileExistsError(f"Refusing to overwrite {destination_path}.")
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    with source_path.open("rb") as source_file:
        if source_file.read(len(SQLITE_HEADER)) != SQLITE_HEADER:
            raise DatabaseEncryptionError(
                "Preflight migration source is not a plaintext SQLite database."
            )

    try:
        with (
            closing(
                sqlite3.connect(
                    f"{source_path.resolve().as_uri()}?mode=ro",
                    uri=True,
                )
            ) as source,
            closing(sqlite3.connect(destination_path)) as target,
        ):
            source.backup(target)
            integrity_result = target.execute("PRAGMA integrity_check").fetchone()
            if not integrity_result or integrity_result[0] != "ok":
                raise DatabaseEncryptionError(
                    "Plaintext database snapshot failed its integrity check."
                )
        destination_path.chmod(0o600)
    except Exception:
        destination_path.unlink(missing_ok=True)
        raise


def preflight_plaintext_migration(source_path: Path, destination_path: Path):
    copy_plaintext_database(source_path, destination_path)

    EncryptedDatabase(
        str(destination_path),
        load_database_encryption_key(),
    )


def verify_encrypted_database(source_path: Path):
    with source_path.open("rb") as source_file:
        if source_file.read(len(SQLITE_HEADER)) == SQLITE_HEADER:
            raise DatabaseEncryptionError(
                "Expected an encrypted SQLCipher database, but found plaintext."
            )

    EncryptedDatabase(
        str(source_path),
        load_database_encryption_key(),
        enforce_permissions=False,
    )


def migrate_backup_directory(backup_directory: Path):
    encryption_key = load_database_encryption_key()
    backup_paths = sorted(backup_directory.glob("db_*.sqlite3"))
    if not backup_paths:
        print(f"No database backups found in {backup_directory}")
        return

    for backup_path in backup_paths:
        EncryptedDatabase(str(backup_path), encryption_key)
        print(f"Encrypted and verified {backup_path}")

    print(f"Encrypted and verified {len(backup_paths)} database backups")


def main():
    parser = argparse.ArgumentParser(
        description="Manage TMW Bot SQLCipher databases and backups."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    decrypt_parser = subparsers.add_parser(
        "decrypt",
        help="Create a plaintext copy of an encrypted database export.",
    )
    decrypt_parser.add_argument(
        "source", type=Path, help="Encrypted .sqlite3 or .sqlite3.gz"
    )
    decrypt_parser.add_argument(
        "destination", type=Path, help="New plaintext .sqlite3 path"
    )

    backup_parser = subparsers.add_parser(
        "backup",
        help="Create a consistent encrypted database backup.",
    )
    backup_parser.add_argument("source", type=Path, help="Encrypted live database")
    backup_parser.add_argument(
        "destination", type=Path, help="New encrypted backup path"
    )

    migrate_parser = subparsers.add_parser(
        "migrate-backups",
        help="Encrypt and verify db_*.sqlite3 files in place.",
    )
    migrate_parser.add_argument("directory", type=Path, help="Backup directory")

    preflight_parser = subparsers.add_parser(
        "preflight-migration",
        help="Test plaintext migration against a consistent disposable copy.",
    )
    preflight_parser.add_argument(
        "source", type=Path, help="Read-only plaintext source database"
    )
    preflight_parser.add_argument(
        "destination", type=Path, help="Disposable encrypted destination"
    )

    snapshot_parser = subparsers.add_parser(
        "snapshot-plaintext",
        help="Create a consistent plaintext rollback snapshot.",
    )
    snapshot_parser.add_argument(
        "source", type=Path, help="Read-only plaintext source database"
    )
    snapshot_parser.add_argument(
        "destination", type=Path, help="New plaintext snapshot path"
    )

    verify_parser = subparsers.add_parser(
        "verify",
        help="Verify an encrypted database using the configured key.",
    )
    verify_parser.add_argument("source", type=Path, help="Encrypted database")

    args = parser.parse_args()

    if args.command == "decrypt":
        decrypt_database(args.source, args.destination)
        print(f"Wrote plaintext database to {args.destination}")
    elif args.command == "backup":
        create_backup(args.source, args.destination)
        print(f"Wrote encrypted database backup to {args.destination}")
    elif args.command == "migrate-backups":
        migrate_backup_directory(args.directory)
    elif args.command == "preflight-migration":
        preflight_plaintext_migration(args.source, args.destination)
        print("Plaintext migration preflight passed")
    elif args.command == "snapshot-plaintext":
        copy_plaintext_database(args.source, args.destination)
        print(f"Wrote plaintext rollback snapshot to {args.destination}")
    elif args.command == "verify":
        verify_encrypted_database(args.source)
        print(f"Verified encrypted database {args.source}")


if __name__ == "__main__":
    main()
