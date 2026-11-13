"""The user's 50×400-piece fleet cannot silently carry 24,000 pieces."""
from copy import deepcopy
from app.routing_engine import optimize
from scripts.benchmark_planning import GridRoad,scale_input


def test_400_piece_capacity_shortage_is_explicit_without_common_prices():
    _,body,points=scale_input();body=deepcopy(body)
    for v in body['vehicles']:v['max_pieces']=400
    for q in body['quotes']:body['quotes'][q]={'mode':'KM','per_km':'5.8'}
    for t in body['tasks']:t['common_unit_price']=None
    result=optimize(body,GridRoad(points,{}),budget_seconds=3)
    assert sum(r['pieces'] for r in result['routes'])<=20000
    assert sum(r['pieces'] for r in result['unassigned'])>=4000
    assert not result['check']['valid'] and not result['confirmable']
    assert result['check']['total_pieces']==24000 and result['check']['task_count']==800
    assert result['check']['used_vehicles']<=50
    assert not result['algorithm']['infeasibility_proven']
