import pytest

from app.routing_engine import normalize
from test_stage4 import DAY, basis, commit, day_state, detail, previewed


def configured(basis, **hours):
    client, warehouse, quote, store, second, vehicle = basis
    response = client.patch('/api/stores/' + store['id'], json={
        'expected_revision': store['revision'], 'latest_arrival_at': None, **hours})
    assert response.status_code == 200, response.text
    return client, response.json()


@pytest.mark.parametrize('day,hours,expected', [
    (DAY, {}, DAY + 'T18:00:00+08:00'),
    (DAY, {'business_open':'20:00','business_close':'02:00','close_day_offset':1}, '2026-11-12T02:00:00+08:00'),
    ('2026-12-31', {'business_open':'20:00','business_close':'02:00','close_day_offset':1}, '2027-01-01T02:00:00+08:00'),
    ('2028-02-28', {'business_open':'20:00','business_close':'02:00','close_day_offset':1}, '2028-02-29T02:00:00+08:00'),
    ('2028-02-29', {'business_open':'20:00','business_close':'02:00','close_day_offset':1}, '2028-03-01T02:00:00+08:00'),
])
def test_deadline_defaults_to_closing_on_the_correct_beijing_date(basis,day,hours,expected):
    client, store = configured(basis, **hours)
    row = next(s for s in client.get('/api/master-data/catalog?delivery_date='+day).json()['stores'] if s['id']==store['id'])
    assert row['latest_arrival_at']==row['business_close_at']==expected
    assert row['latest_arrival_source']=='BUSINESS_CLOSE' and row['day_revision']==0
    # Calculating a dated default does not add a date to the master record.
    master=next(s for s in client.get('/api/master-data/management').json()['stores'] if s['id']==store['id'])
    assert master['latest_arrival_at'] is None


def test_day_rows_snapshots_and_solver_share_the_same_closing_deadline(basis):
    client, store = configured(basis, business_open='20:00',business_close='02:00',close_day_offset=1)
    receipt=commit(client,previewed(client,[[store['name'],7,store['code']]])).json()
    row=day_state(client)['items'][0];body=detail(client,receipt['snapshot_id'])['input']
    assert row['latest_arrival_at']=='2026-11-12T02:00:00+08:00' and row['latest_arrival_source']=='BUSINESS_CLOSE'
    assert body['tasks'][0]['latest_arrival_at']==body['stores'][0]['business_close_at']==row['latest_arrival_at']
    task=normalize(body)['tasks'][store['id']]
    assert task['latest_arrival_at']==task['close_at']==row['latest_arrival_at']


def test_special_daily_deadline_can_be_cleared_back_to_live_closing_time(basis):
    client,store=configured(basis);path='/api/planning/days/'+DAY+'/stores/'+store['id']
    custom=client.put(path,json={'expected_revision':0,'latest_arrival_at':DAY+'T12:30:00+08:00'})
    assert custom.status_code==200 and custom.json()['latest_arrival_source']=='DAY'
    current=client.get('/api/master-data/catalog?delivery_date='+DAY).json()['stores'][0]
    assert current['latest_arrival_at']==DAY+'T12:30:00+08:00'
    cleared=client.put(path,json={'expected_revision':1,'latest_arrival_at':None})
    assert cleared.status_code==200 and cleared.json()['latest_arrival_at']==DAY+'T18:00:00+08:00'
    assert cleared.json()['latest_arrival_source']=='BUSINESS_CLOSE'
    assert client.patch('/api/stores/'+store['id'],json={'expected_revision':store['revision'],'business_close':'19:00'}).status_code==200
    assert client.get('/api/master-data/catalog?delivery_date='+DAY).json()['stores'][0]['latest_arrival_at']==DAY+'T19:00:00+08:00'


def test_closing_change_requires_new_snapshot_and_preserves_frozen_deadline(basis):
    client,store=configured(basis)
    receipt=commit(client,previewed(client,[[store['name'],2,store['code']]])).json();old=detail(client,receipt['snapshot_id'])
    assert client.patch('/api/stores/'+store['id'],json={'expected_revision':store['revision'],'business_close':'19:30'}).status_code==200
    state=day_state(client)
    assert state['items'][0]['latest_arrival_at']==DAY+'T19:30:00+08:00'
    assert state['latest_snapshot']['input_changed']
    assert detail(client,receipt['snapshot_id'])['input']==old['input']
    response=client.post('/api/planning/days/'+DAY+'/snapshots',json={'expected_revision':state['revision'],'expected_fingerprint':state['input_fingerprint']})
    assert response.status_code==200
    fresh=detail(client,response.json()['id'])
    assert fresh['version']==2 and fresh['input']['tasks'][0]['latest_arrival_at']==DAY+'T19:30:00+08:00'
    assert old['input']['tasks'][0]['latest_arrival_at']==DAY+'T18:00:00+08:00'


def test_legacy_explicit_deadline_is_preserved_until_user_returns_to_closing(basis):
    client,warehouse,quote,store,second,vehicle=basis
    current=client.get('/api/master-data/catalog?delivery_date='+DAY).json()['stores'][0]
    assert current['latest_arrival_source']=='LEGACY' and current['latest_arrival_at']==DAY+'T09:00:00+08:00'
    response=client.put('/api/planning/days/'+DAY+'/stores/'+store['id'],json={'expected_revision':0,'latest_arrival_at':None})
    assert response.status_code==200 and response.json()['latest_arrival_source']=='BUSINESS_CLOSE'
    assert response.json()['latest_arrival_at']==DAY+'T18:00:00+08:00'
