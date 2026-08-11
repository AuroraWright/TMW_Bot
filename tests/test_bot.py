import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lib.bot import TMWBot

os.environ.setdefault("AUTHORIZED_USERS", "1")
os.environ.setdefault("DEBUG_USER", "1")


class BotConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def test_intents_and_all_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            ready_file = Path(directory) / "bot-ready"
            with patch.dict(os.environ, {"READY_FILE": str(ready_file)}):
                bot = TMWBot(
                    command_prefix="%",
                    database_encryption_key=bytes(range(32)),
                    path_to_db=str(Path(directory) / "db.sqlite3"),
                )
            try:
                self.assertTrue(bot.intents.members)
                self.assertTrue(bot.intents.message_content)
                self.assertFalse(bot.intents.presences)

                await bot.load_cogs("*")
                expected_extensions = len(list(Path("cogs").glob("*.py")))
                self.assertEqual(len(bot.extensions), expected_extensions)

                await bot.on_ready()
                self.assertEqual(ready_file.read_text(encoding="ascii"), "ready\n")
            finally:
                for extension in list(bot.extensions):
                    await bot.unload_extension(extension)
                await bot.close()


if __name__ == "__main__":
    unittest.main()
