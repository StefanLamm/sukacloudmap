from datetime import datetime, timezone

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.models import Account, AuditEvent, LoginSession, SharedRecord
from conftest import USER_PASSWORD, create_member, login


def add_task(client, day, title='团队任务'):
    response = client.post('/api/workspace/records', json={'title': title, 'content': '核对配送安排', 'task_date': day})
    assert response.status_code == 201, response.text
    return response.json()


def test_delete_account_revokes_sessions_preserves_authored_records_and_audit(client, app):
    login(client)
    member = create_member(client)
    with TestClient(app) as user:
        login(user, 'member', USER_PASSWORD)
        record = add_task(user, '2026-11-12')
        response = client.delete('/api/accounts/'+member['id'], params={'expected_revision': 1})
        assert response.status_code == 204
        assert user.get('/api/auth/me').status_code == 401
        assert user.post('/api/auth/login', json={'username':'member','password':USER_PASSWORD}).status_code == 401
    assert member['id'] not in [row['id'] for row in client.get('/api/accounts').json()['items']]
    assert client.get('/api/workspace/records').json()['items'][0]['id'] == record['id']
    with app.state.sessions() as db:
        deleted = db.get(Account, member['id'])
        assert deleted.deleted_at and not deleted.active
        assert db.scalar(select(LoginSession).where(LoginSession.account_id == member['id'])) is None
        assert db.scalar(select(AuditEvent).where(AuditEvent.action == 'ACCOUNT_DELETED')) is not None
        assert db.get(SharedRecord,record['id']).created_by == member['id']
    assert client.patch('/api/accounts/'+member['id'],json={'active':True,'expected_revision':2}).status_code == 404
    collision = client.post('/api/accounts', json={'username':'member','display_name':'新用户','password':USER_PASSWORD})
    assert collision.status_code == 409 and '历史账号' in collision.json()['message']


def test_delete_account_guards_last_admin_self_stale_and_permissions(client, app):
    me = login(client).json()['account']
    last = client.delete('/api/accounts/'+me['id'],params={'expected_revision':1})
    assert last.status_code == 409 and last.json()['code'] == 'LAST_ADMIN'
    create_member(client, 'second_admin', True)
    self_delete = client.delete('/api/accounts/'+me['id'],params={'expected_revision':1})
    assert self_delete.status_code == 409 and self_delete.json()['code'] == 'DELETE_SELF'
    member = create_member(client)
    assert client.delete('/api/accounts/'+member['id'],params={'expected_revision':99}).status_code == 409
    with TestClient(app) as user:
        login(user,'member',USER_PASSWORD)
        assert user.delete('/api/accounts/'+me['id'],params={'expected_revision':1}).status_code == 403
    assert client.get('/api/auth/me').status_code == 200


def test_tasks_complete_archive_and_beijing_midnight_overdue(client, app, monkeypatch):
    import app.api as api_module
    instant = [int(datetime(2026,11,12,15,59,tzinfo=timezone.utc).timestamp())]
    monkeypatch.setattr(api_module,'now',lambda:instant[0])
    login(client)
    today = add_task(client,'2026-11-12','当日配送')
    overdue = add_task(client,'2026-11-11','昨日核查')
    future = add_task(client,'2026-11-14','后日预约')
    rows = client.get('/api/workspace/records?status=pending&task_date=2026-11-12').json()
    assert [r['id'] for r in rows['items']] == [today['id']]
    assert rows['counts'] == {'pending':2,'completed':0,'overdue':1}
    done = client.post('/api/workspace/records/'+overdue['id']+'/completion',json={'expected_revision':1,'completed':True})
    assert done.status_code == 200 and done.json()['status'] == 'COMPLETED'
    assert done.json()['completed_at'] and done.json()['completed_by']
    assert client.get('/api/workspace/records?status=overdue').json()['total'] == 0
    completed = client.get('/api/workspace/records?status=completed').json()
    assert [r['id'] for r in completed['items']] == [overdue['id']]
    instant[0] += 61
    midnight = client.get('/api/workspace/records?status=overdue').json()
    assert [r['id'] for r in midnight['items']] == [today['id']]
    assert midnight['counts'] == {'pending':1,'completed':1,'overdue':1}
    reopened = client.post('/api/workspace/records/'+overdue['id']+'/completion',json={'expected_revision':done.json()['revision'],'completed':False})
    assert reopened.status_code == 200 and reopened.json()['status'] == 'OVERDUE'
    assert reopened.json()['completed_at'] is None
    assert client.get('/api/workspace/records?task_date=2026-11-14').json()['items'][0]['id'] == future['id']


def test_completion_admin_only_and_version_conflicts_preserve_first_change(client, app):
    login(client);create_member(client)
    record = add_task(client,'2026-11-12')
    with TestClient(app) as user:
        login(user,'member',USER_PASSWORD)
        assert user.post('/api/workspace/records/'+record['id']+'/completion',json={'expected_revision':1}).status_code == 403
    assert client.post('/api/workspace/records/'+record['id']+'/completion',json={'expected_revision':1}).status_code == 200
    conflict=client.patch('/api/workspace/records/'+record['id'],json={'expected_revision':1,'title':'不能覆盖','content':'旧编辑','task_date':'2026-11-14'})
    assert conflict.status_code == 409
    result=client.get('/api/workspace/records?status=completed').json()['items'][0]
    assert result['title'] == record['title'] and result['task_date'] == record['task_date']


@pytest.mark.parametrize('day',['2026-02-30','2026-13-01','2026-1-1','not-a-date',''])
def test_invalid_task_dates_are_rejected(client, day):
    login(client)
    assert client.post('/api/workspace/records',json={'title':'日期','content':'校验','task_date':day}).status_code == 422
    assert client.get('/api/workspace/records',params={'task_date':day}).status_code == 422


def test_legacy_record_create_defaults_today_and_update_preserves_task_date(client):
    login(client)
    response=client.post('/api/workspace/records',json={'title':'兼容旧客户端','content':'无需丢失历史日期'})
    assert response.status_code == 201 and len(response.json()['task_date']) == 10
    record=add_task(client,'2026-12-31')
    updated=client.patch('/api/workspace/records/'+record['id'],json={'title':'旧客户端更新','content':'日期保留','expected_revision':1})
    assert updated.status_code == 200 and updated.json()['task_date'] == '2026-12-31'
