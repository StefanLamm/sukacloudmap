from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import os
import subprocess
import sys

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.config import Settings
from app.database import make_engine, migrate
from app.main import create_app
from app.models import Account, AuditEvent, Base, LoginSession, LoginThrottle
from app.security import digest
from conftest import ADMIN_PASSWORD, USER_PASSWORD, create_member, login


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/auth/me", None), ("GET", "/api/accounts", None),
    ("GET", "/api/workspace/records", None), ("GET", "/api/audit-events", None),
    ("POST", "/api/workspace/records", {"title": "a", "content": "b"}),
    ("POST", "/api/accounts", {"username": "someone", "display_name": "成员", "password": USER_PASSWORD}),
    ("PATCH", "/api/accounts/any", {"active": False, "expected_revision": 1}),
    ("POST", "/api/accounts/any/reset-password", {"password": USER_PASSWORD, "expected_revision": 1}),
])
def test_anonymous_business_api_denied(client, method, path, body):
    r = client.request(method, path, json=body)
    assert r.status_code == 401
    assert r.json()["code"] == "AUTH_REQUIRED"
    assert r.json()["request_id"] == r.headers["x-request-id"]


@pytest.mark.parametrize("path", ["/", "/accounts"])
def test_anonymous_page_redirects_to_login(client, path):
    r = client.get(path, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_password_session_hashes_and_no_secrets_in_responses(client, app):
    r = login(client)
    token = client.cookies.get("suka_session")
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=lax" in r.headers["set-cookie"]
    with app.state.sessions() as db:
        a = db.scalar(select(Account).where(Account.username == "admin"))
        assert a.password_hash.startswith("$argon2id$") and ADMIN_PASSWORD not in a.password_hash
        session = db.scalar(select(LoginSession))
        assert session.token_hash == digest(token) and session.token_hash != token
        assert session.csrf_hash != r.json()["csrf_token"]
    for path in ["/api/auth/me", "/api/accounts", "/api/audit-events"]:
        text = client.get(path).text
        for secret in [ADMIN_PASSWORD, token, "$argon2", "password_hash", "csrf_hash", "AMAP_WEB_SERVICE_KEY"]:
            assert secret not in text


def test_wrong_unknown_and_inactive_credentials_have_same_response(client):
    wrong = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    unknown = client.post("/api/auth/login", json={"username": "missing", "password": "wrong"})
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["message"] == unknown.json()["message"]


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/accounts", None), ("GET", "/api/audit-events", None),
    ("POST", "/api/accounts", {"username": "other", "display_name": "成员", "password": USER_PASSWORD}),
    ("PATCH", "/api/accounts/any", {"active": False, "expected_revision": 1}),
    ("POST", "/api/accounts/any/reset-password", {"password": USER_PASSWORD, "expected_revision": 1}),
])
def test_business_member_cannot_manage_accounts(client, method, path, body):
    login(client); create_member(client); login(client, "member", USER_PASSWORD)
    r = client.request(method, path, json=body)
    assert r.status_code == 403 and r.json()["code"] == "ADMIN_REQUIRED"
    assert client.get("/accounts", follow_redirects=False).headers["location"] == "/"


def test_logout_revokes_captured_cookie(client):
    login(client); captured = client.cookies.get("suka_session")
    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    client.cookies.set("suka_session", captured)
    assert client.get("/api/auth/me").status_code == 401


def test_disable_and_reenable_revoke_sessions(client, app):
    login(client); member = create_member(client)
    with TestClient(app) as user:
        login(user, "member", USER_PASSWORD)
        old_cookie = user.cookies.get("suka_session")
        r = client.patch(f"/api/accounts/{member['id']}", json={"active": False, "expected_revision": member['revision']})
        assert r.status_code == 200
        assert user.get("/api/auth/me").status_code == 401
        assert user.post("/api/auth/login", json={"username": "member", "password": USER_PASSWORD}).status_code == 401
        r = client.patch(f"/api/accounts/{member['id']}", json={"active": True, "expected_revision": r.json()['revision']})
        assert r.status_code == 200
        user.cookies.set("suka_session", old_cookie)
        assert user.get("/api/auth/me").status_code == 401
        login(user, "member", USER_PASSWORD)


def test_reset_revokes_all_sessions_and_old_password(client, app):
    login(client); member = create_member(client)
    with TestClient(app) as user1, TestClient(app) as user2:
        login(user1, "member", USER_PASSWORD); login(user2, "member", USER_PASSWORD)
        new_password = "Changed-Test-Member-7791!"
        r = client.post(f"/api/accounts/{member['id']}/reset-password", json={"password": new_password, "expected_revision": member['revision']})
        assert r.status_code == 200
        assert user1.get("/api/auth/me").status_code == user2.get("/api/auth/me").status_code == 401
        assert user1.post("/api/auth/login", json={"username": "member", "password": USER_PASSWORD}).status_code == 401
        login(user1, "member", new_password)
        unchanged = client.post(f"/api/accounts/{member['id']}/reset-password", json={"password": new_password, "expected_revision": r.json()['revision']})
        assert unchanged.status_code == 422 and unchanged.json()["code"] == "PASSWORD_UNCHANGED"


def test_revoked_admin_cannot_use_preexisting_session(client, app):
    login(client); other = create_member(client, "admin_two", is_admin=True)
    with TestClient(app) as user:
        login(user, "admin_two", USER_PASSWORD)
        assert user.get("/api/accounts").status_code == 200
        assert client.patch(f"/api/accounts/{other['id']}", json={"is_admin": False, "expected_revision": 1}).status_code == 200
        assert user.get("/api/accounts").status_code == 401
        login(user, "admin_two", USER_PASSWORD)
        assert user.get("/api/accounts").status_code == 403


def test_last_admin_and_account_version_conflict(client):
    me = login(client).json()['account']
    denied = client.patch(f"/api/accounts/{me['id']}", json={"active": False, "expected_revision": 1})
    assert denied.status_code == 409 and denied.json()['code'] == 'LAST_ADMIN'
    member = create_member(client)
    assert client.patch(f"/api/accounts/{member['id']}", json={"display_name": "已更新", "expected_revision": 1}).status_code == 200
    stale = client.post(f"/api/accounts/{member['id']}/reset-password", json={"password": "Stale-Test-Password-123!", "expected_revision": 1})
    assert stale.status_code == 409 and stale.json()['code'] == 'VERSION_CONFLICT'


def test_two_members_share_data_and_conflicts_preserve_first_write(client, app):
    login(client); create_member(client, 'member_one'); create_member(client, 'member_two')
    with TestClient(app) as user1, TestClient(app) as user2:
        login(user1, 'member_one', USER_PASSWORD); login(user2, 'member_two', USER_PASSWORD)
        r = user1.post('/api/workspace/records', json={'title':'配送提醒','content':'今天核对门店件数'})
        assert r.status_code == 201
        record = r.json()
        assert user2.get('/api/workspace/records').json()['items'][0]['id'] == record['id']
        r = user2.patch('/api/workspace/records/'+record['id'], json={'title':'已核对','content':'件数已核对','expected_revision':1})
        assert r.status_code == 200
        conflict = user1.patch('/api/workspace/records/'+record['id'], json={'title':'过期编辑','content':'不能覆盖','expected_revision':1})
        assert conflict.status_code == 409
        assert user1.get('/api/workspace/records').json()['items'][0]['content'] == '件数已核对'


def test_csrf_and_foreign_origin(client):
    login(client); token = client.headers.pop('x-csrf-token')
    assert client.post('/api/workspace/records',json={'title':'x','content':'y'}).status_code == 403
    client.headers['x-csrf-token'] = token
    r = client.post('/api/workspace/records', json={'title':'x','content':'y'}, headers={'Origin':'https://foreign.example'})
    assert r.status_code == 403 and r.json()['code'] == 'ORIGIN_REJECTED'
    assert client.post('/api/auth/login',json={'username':'admin','password':ADMIN_PASSWORD},headers={'Origin':'null'}).status_code == 403
    me = client.get('/api/auth/me').json()
    client.headers['x-csrf-token'] = me['csrf_token']
    assert client.post('/api/workspace/records',json={'title':'x','content':'y'}).status_code == 201


def test_expiry_and_login_throttling(client, app):
    login(client)
    with app.state.sessions() as db:
        session = db.scalar(select(LoginSession))
        session.expires_at = 1
        db.commit()
    assert client.get('/api/auth/me').status_code == 401
    for _ in range(10):
        assert client.post('/api/auth/login',json={'username':'admin','password':'wrong'}).status_code == 401
    assert client.post('/api/auth/login',json={'username':'admin','password':ADMIN_PASSWORD}).status_code == 429


def test_invalid_input_never_echoes_password(client):
    login(client)
    password = 'secret-sample'
    r = client.post('/api/accounts', json={'username':'x','display_name':'','password':password,'unexpected':password})
    assert r.status_code == 422 and r.json()['code'] == 'VALIDATION_ERROR'
    assert password not in r.text and 'input' not in r.text
    assert client.post('/api/accounts', json={'username':'empty','display_name':'  ','password':USER_PASSWORD}).status_code == 422


def test_case_insensitive_username_uniqueness(client):
    login(client); create_member(client, 'mixeduser')
    r=client.post('/api/accounts',json={'username':'MixedUser','display_name':'重名','password':USER_PASSWORD})
    assert r.status_code == 409
    login(client,'MIXEDUSER', USER_PASSWORD)


def test_secure_cookie_production_settings(settings):
    with pytest.raises(ValueError):
        Settings(_env_file=None, environment='production', public_origin='http://example.com', secure_cookie=False)


def test_production_cookie_flags_and_configuration_redaction(app, settings):
    prod=Settings(_env_file=None, environment='production', database_url=settings.database_url,
                  public_origin='https://testserver', allowed_hosts=['testserver'], secure_cookie=True)
    with TestClient(create_app(prod), base_url='https://testserver') as browser:
        r=login(browser)
        assert '__Host-suka_session=' in r.headers['set-cookie']
        assert 'Secure' in r.headers['set-cookie'] and 'HttpOnly' in r.headers['set-cookie']
        assert browser.get('/api/auth/me').status_code==200
    marker='must-not-appear-in-error-output'
    with pytest.raises(ValueError) as caught:
        Settings(_env_file=None, environment='production', secure_cookie=False, amap_web_service_key=marker)
    assert marker not in str(caught.value)


def test_unexpected_errors_are_redacted_in_response_and_logs(client, app, caplog):
    marker='fake-provider-secret-must-stay-private'
    @app.post('/test-only-error')
    def fail():
        raise ValueError(marker)
    r=client.post('/test-only-error')
    assert r.status_code==500 and r.json()['code']=='INTERNAL_ERROR'
    assert r.json()['request_id']==r.headers['x-request-id']
    assert marker not in r.text and marker not in caplog.text


def test_restart_keeps_accounts_sessions_records_and_migration(app, settings):
    with TestClient(app) as first:
        login(first); create_member(first)
        first.post('/api/workspace/records',json={'title':'重启验证','content':'资料和会话保留'})
        cookie=first.cookies.get('suka_session')
    fresh = create_app(settings)
    with TestClient(fresh) as second:
        second.cookies.set('suka_session',cookie)
        assert second.get('/api/auth/me').status_code == 200
        assert len(second.get('/api/accounts').json()['items']) == 2
        assert second.get('/api/workspace/records').json()['items'][0]['title']=='重启验证'
    engine=make_engine(settings)
    migrate(engine);migrate(engine)
    with engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection),Base.metadata)==[]
    engine.dispose()


def test_concurrent_last_admin_protection(client, app):
    first_admin=login(client).json()['account']; second_admin=create_member(client,'admin_two',True)
    with TestClient(app) as second:
        login(second,'admin_two',USER_PASSWORD)
        def disable_one(c, account):
            return c.patch('/api/accounts/'+account['id'],json={'active':False,'expected_revision':1}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            a=pool.submit(disable_one,client,second_admin)
            b=pool.submit(disable_one,second,first_admin)
            statuses=[a.result(),b.result()]
        assert statuses.count(200)==1 and (401 in statuses or 409 in statuses)
    with app.state.sessions() as db:
        remaining=list(db.scalars(select(Account).where(Account.active.is_(True), Account.is_admin.is_(True))))
        assert len(remaining)==1


def test_cli_initialization_does_not_overwrite_admin(tmp_path):
    env={**os.environ,'SUKA_DATABASE_URL':f'sqlite:///{tmp_path / "cli.db"}','SUKA_BOOTSTRAP_PASSWORD':ADMIN_PASSWORD}
    command=[sys.executable,'-m','app.cli','init-admin']
    one=subprocess.run(command,env=env,capture_output=True,text=True)
    two=subprocess.run(command,env=env,capture_output=True,text=True)
    assert one.returncode==0 and two.returncode==1
    assert ADMIN_PASSWORD not in one.stdout+one.stderr+two.stdout+two.stderr

