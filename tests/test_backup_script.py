import os
import subprocess
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from lib.database import SQLITE_HEADER


class EncryptedBackupScriptTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary_directory.name)
        self.backup_directory = self.directory / "backups"
        self.backup_directory.mkdir()
        self.fake_bin = self.directory / "bin"
        self.fake_bin.mkdir()
        self.fake_docker = self.fake_bin / "docker"
        self.fake_docker.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                import sys
                from pathlib import Path

                if sys.argv[1] == "inspect":
                    print("true" if os.environ.get("FAKE_CONTAINER_RUNNING") == "1" else "false")
                    raise SystemExit(0)

                if sys.argv[1] == "exec":
                    destination = Path(os.environ["FAKE_BACKUP_DIR"]) / Path(sys.argv[-1]).name
                    if os.environ.get("FAKE_BACKUP_PLAINTEXT") == "1":
                        destination.write_bytes(b"SQLite format 3\\x00" + b"plaintext")
                    else:
                        destination.write_bytes(b"encrypted-sqlcipher-backup")
                    raise SystemExit(0)

                raise SystemExit(2)
                """
            ),
            encoding="utf-8",
        )
        self.fake_docker.chmod(0o755)
        self.script = Path("scripts/backup_database.sh").resolve()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def environment(self, **overrides):
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{self.fake_bin}:{environment['PATH']}",
                "FAKE_BACKUP_DIR": str(self.backup_directory),
                "FAKE_CONTAINER_RUNNING": "1",
                "TMW_BACKUP_DIR": str(self.backup_directory),
                "TMW_CONTAINER_BACKUP_DIR": "/app/backups",
            }
        )
        environment.update(overrides)
        return environment

    def run_script(self, **environment_overrides):
        return subprocess.run(
            [str(self.script)],
            env=self.environment(**environment_overrides),
            capture_output=True,
            text=True,
            check=False,
        )

    def test_creates_private_encrypted_backup_and_retains_30(self):
        old_timestamp = time.time() - 86400
        for index in range(30):
            backup = self.backup_directory / f"db_2026-01-{index + 1:02d}_0400.sqlite3"
            backup.write_bytes(b"old-encrypted-backup")
            os.utime(backup, (old_timestamp - index, old_timestamp - index))

        result = self.run_script()

        self.assertEqual(result.returncode, 0, result.stderr)
        backups = list(self.backup_directory.glob("db_*.sqlite3"))
        self.assertEqual(len(backups), 30)
        newest = max(backups, key=lambda path: path.stat().st_mtime)
        self.assertFalse(newest.read_bytes().startswith(SQLITE_HEADER))
        self.assertEqual(newest.stat().st_mode & 0o777, 0o600)

    def test_rejects_and_removes_plaintext_output(self):
        result = self.run_script(FAKE_BACKUP_PLAINTEXT="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("plaintext SQLite header", result.stderr)
        self.assertEqual(list(self.backup_directory.iterdir()), [])

    def test_refuses_to_run_when_container_is_stopped(self):
        result = self.run_script(FAKE_CONTAINER_RUNNING="0")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not running", result.stderr)
        self.assertEqual(list(self.backup_directory.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
