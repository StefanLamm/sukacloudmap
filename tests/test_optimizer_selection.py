from types import SimpleNamespace
import pytest
from app.routing_engine import optimize
from app.config import Settings
from test_route_search import clustered_problem


@pytest.mark.parametrize('profile,expected,options',[
 ('HYBRID','app.route_portfolio.optimize_hybrid',{}),('ALNS','app.route_portfolio.optimize_alns',{}),
 ('V3_REFERENCE','app.route_baseline.optimize_joint',{}),
 ('GUIDED_LOCAL_SEARCH','app.route_ortools.optimize_ortools',{'strategy':'GUIDED_LOCAL_SEARCH','common_penalty_multiplier':2}),
 ('SIMULATED_ANNEALING','app.route_ortools.optimize_ortools',{'strategy':'SIMULATED_ANNEALING','common_penalty_multiplier':2}),
 ('TABU_SEARCH','app.route_ortools.optimize_ortools',{'strategy':'TABU_SEARCH','common_penalty_multiplier':2})])
def test_studied_profile_can_be_applied_without_changing_price_inputs(monkeypatch,profile,expected,options):
 body,road=clustered_problem();road.settings=SimpleNamespace(planning_optimizer_profile=profile,planning_common_penalty_multiplier=2)
 original_tasks=[dict(t) for t in body['tasks']];seen=[]
 def selected(*args,**kwargs):seen.append((args,kwargs));return {'selected':profile}
 monkeypatch.setattr(expected,selected)
 assert optimize(body,road,20,7)=={'selected':profile}
 assert seen[0][0][:4]==(body,road,20,7)
 for name,value in options.items():assert seen[0][1][name]==value
 assert body['tasks']==original_tasks


@pytest.mark.parametrize('value',[float('inf'),float('nan'),0,-1,10.1])
def test_invalid_generation_multiplier_cannot_change_live_cost_model(value):
 with pytest.raises(ValueError):Settings(planning_common_penalty_multiplier=value)
