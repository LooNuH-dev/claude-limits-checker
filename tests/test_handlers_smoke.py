from aiogram import Router

import app.main as main
from app.bot import add_account, admin, menu, start
from app.db import User


def test_routers_exist():
    for mod in (start, admin, add_account, menu):
        assert isinstance(mod.router, Router)


async def test_is_admin_filter():
    is_admin = admin.IsAdmin()
    assert await is_admin(event=None, user=None) is False
    assert await is_admin(event=None, user=User(1, None, "user", False)) is False
    assert await is_admin(event=None, user=User(1, None, "admin", False)) is True


def test_main_importable():
    assert callable(main.main)
