import pytest
from app.config import load_settings


def test_load_settings_defaults():
    s = load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "42", "ENCRYPTION_KEY": "k"})
    assert s.admin_id == 42
    assert s.db_path == "/data/bot.db"
    assert s.check_interval == 300
    assert s.invite_ttl == 86400


def test_check_interval_floor():
    s = load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "1", "ENCRYPTION_KEY": "k", "CHECK_INTERVAL": "5"})
    assert s.check_interval == 60


def test_missing_required():
    with pytest.raises(RuntimeError, match="ENCRYPTION_KEY"):
        load_settings({"BOT_TOKEN": "t", "ADMIN_TELEGRAM_ID": "1"})
