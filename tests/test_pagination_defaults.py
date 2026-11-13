"""Default page size and server pagination retain all shared/audit records."""
from conftest import login


def test_records_and_audit_default_to_ten_and_keep_later_pages(client):
    login(client)
    for index in range(25):
        result=client.post('/api/workspace/records',json={'title':f'任务{index}','content':'分页验收'})
        assert result.status_code==201
    for path in ['/api/workspace/records','/api/audit-events']:
        first=client.get(path).json();second=client.get(path,params={'offset':10,'limit':20}).json()
        assert len(first['items'])==10
        assert first['total']>=25
        assert len(second['items'])==min(20,first['total']-10)
        assert not ({r['id'] for r in first['items']}&{r['id'] for r in second['items']})
        assert client.get(path,params={'offset':-1}).status_code==422
