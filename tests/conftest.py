from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import make_engine, migrate, session_factory
from app.main import create_app
from app.models import Account
from app.security import hash_password

# Test-only credentials. No default account/password is created by the app.
ADMIN_PASSWORD = "Test-Only-Admin-9385!"
USER_PASSWORD = "Test-Only-Member-5827!"


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, environment="test", database_url=f"sqlite:///{tmp_path / 'test.db'}", allowed_hosts=["testserver", "127.0.0.1"], public_origin="http://testserver", map_auto_geocode_enabled=False, map_provider='OPEN')


@pytest.fixture(autouse=True)
def isolated_amap_pacing(tmp_path, monkeypatch):
    # Even tests constructing Settings directly must never touch the live pacer.
    monkeypatch.setenv('SUKA_AMAP_RATE_LIMIT_PATH', str(tmp_path / 'amap-rate.db'))


@pytest.fixture
def app(settings):
    engine = make_engine(settings)
    migrate(engine)
    with session_factory(engine)() as db:
        db.add(Account(username="admin", display_name="测试管理员", password_hash=hash_password(ADMIN_PASSWORD), is_admin=True))
        db.commit()
    engine.dispose()
    return create_app(settings)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def login(client, username="admin", password=ADMIN_PASSWORD):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    client.headers["x-csrf-token"] = response.json()["csrf_token"]
    return response


def create_member(client, username="member", is_admin=False):
    response = client.post("/api/accounts", json={"username": username, "display_name": "成员" + username,
                           "password": USER_PASSWORD, "is_admin": is_admin})
    assert response.status_code == 201, response.text
    return response.json()

