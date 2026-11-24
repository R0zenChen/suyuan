import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mysql_store


class MySQLConfigTests(unittest.TestCase):
    def test_rejects_remote_host_and_database_name_injection(self):
        base = {"host": "127.0.0.1", "port": 3306, "database": "suyuan", "user": "demo", "password": "test"}
        with self.assertRaises(ValueError):
            mysql_store.validate_config({**base, "host": "example.com"})
        with self.assertRaises(ValueError):
            mysql_store.validate_config({**base, "database": "suyuan;DROP TABLE news_articles"})

    def test_public_config_never_returns_saved_password(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mysql-config.json"
            path.write_text(json.dumps({"host": "127.0.0.1", "port": 3306, "database": "suyuan", "user": "demo", "password": "secret"}), encoding="utf-8")
            with patch.object(mysql_store, "CONFIG_FILE", path):
                result = mysql_store.public_config()
        self.assertTrue(result["password_set"])
        self.assertNotIn("password", result)
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
