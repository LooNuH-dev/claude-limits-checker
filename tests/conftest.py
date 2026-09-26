import pytest
from app.crypto import Cipher, generate_key
from app.db import Repo


@pytest.fixture
def cipher():
    return Cipher(generate_key())


@pytest.fixture
async def repo(tmp_path, cipher):
    r = await Repo.open(str(tmp_path / "t.db"), cipher)
    yield r
    await r.close()
