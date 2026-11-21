"""Run optional PyVRP generation on an isolated measured quote group."""

from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from fractions import Fraction
from importlib.metadata import PackageNotFoundError, version
import json
from math import isfinite, lcm
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic


_VERSION = '0.14.0'
_WIRE_VERSION = 1
_MAX_CLIENTS = 1000
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_MODEL_VALUE = 10**9
_POLL_SECONDS = .02
_MAX_RETURN_RESERVE_SECONDS = .25
_SKIPS = {'TIME_BUDGET', 'PYVRP_UNAVAILABLE', 'PYVRP_VERSION_MISMATCH',
          'PYVRP_WORKER_ERROR', 'PYVRP_UNSUPPORTED_INPUT'}


class _StageExpired(Exception):
    pass


def _seconds_since(value, departure):
    seconds = (datetime.fromisoformat(value) - departure).total_seconds()
    if not seconds.is_integer():
        raise ValueError('PyVRP requires whole-second time windows.')
    return int(seconds)


def group_facts(data):
    """Return model facts for an eligible warehouse and pure kilometre quote."""
    from app.routing_engine import common_fee

    try:
        tasks = data['tasks']
        if not tasks or len({(row['warehouse_id'], row['signature']) for row in tasks.values()}) != 1:
            raise ValueError('PyVRP requires one nonempty compatible quote group.')
        task = next(iter(tasks.values()))
        quote = task['quote']
        if not quote or task['signature'] is None or quote['mode'] != 'KM' or quote.get('region_surcharge'):
            raise ValueError('PyVRP requires a pure kilometre quote.')
        warehouse = data['warehouses'][task['warehouse_id']]
        departure = datetime.fromisoformat(warehouse['earliest_departure_at'])
        rate_per_meter = Fraction(Decimal(quote['per_km'])) / 1000
        scale = lcm(100, rate_per_meter.denominator)
        if rate_per_meter < 0 or scale > _MAX_MODEL_VALUE:
            raise ValueError('PyVRP requires a bounded exact currency scale.')
        priced = {}
        for key, row in tasks.items():
            opening = max(0, _seconds_since(row['open_at'], departure))
            close = _seconds_since(row['close_at'], departure)
            latest = _seconds_since(row['latest_arrival_at'], departure) if row.get('latest_arrival_at') else close
            unload = row['unload_minutes']
            if min(close, latest) < opening or type(unload) is not int or unload < 0:
                raise ValueError('The time windows or unloading duration require a different model.')
            shared = common_fee(row['pieces'], row['common_unit_price'])
            prize = int(Fraction(Decimal(shared)) * scale) if shared is not None else None
            priced[key] = (opening, min(close, latest), unload * 60, prize)
        by_capacity = defaultdict(list)
        for vehicle_id, row in sorted(data['vehicles'].items()):
            if row['available'] and row['warehouse_id'] == warehouse['id'] and row['max_pieces'] > 0:
                by_capacity[row['max_pieces']].append(vehicle_id)
        if not by_capacity:
            raise ValueError('PyVRP requires a compatible physical vehicle.')
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise ValueError('Unsupported PyVRP group facts.') from exc
    return warehouse, int(rate_per_meter * scale), scale, priced, sorted(by_capacity.items())


def _integer(value, upper=_MAX_MODEL_VALUE - 1):
    return type(value) is int and 0 <= value <= upper


def _request(data, measured_rows, deadline, seed, check):
    task_ids = sorted(data['tasks'])
    if not task_ids or len(task_ids) > _MAX_CLIENTS or type(seed) is not int:
        raise ValueError('The group or seed is outside the PyVRP worker input range.')
    _, meter_cost, scale, priced, capacities = group_facts(data)
    check()
    clients = []
    for key in task_ids:
        check()
        task = data['tasks'][key]
        x, y = float(task['longitude']), float(task['latitude'])
        if not isfinite(x) or not isfinite(y):
            raise ValueError('Invalid PyVRP client location.')
        clients.append([task['pieces'], x, y, *priced[key]])
    groups = [[capacity, len(members)] for capacity, members in capacities]
    indices = {task: index for index, task in enumerate(task_ids)}
    directions = {}
    for index, (origin, destination, meters, duration) in enumerate(measured_rows):
        if index % 32 == 0:
            check()
        side = (-1 if origin is None else indices[origin], indices[destination])
        if side[0] == side[1] or not _integer(meters) or not _integer(duration):
            raise ValueError('Invalid measured direction.')
        if directions.setdefault(side, (meters, duration)) != (meters, duration):
            raise ValueError('Conflicting frozen measured directions.')
    payload = {'schema': _WIRE_VERSION, 'deadline': deadline, 'seed': seed % (2**32),
               'meter_cost': meter_cost, 'scale': scale, 'clients': clients,
               'groups': groups, 'edges': [[*side, *metrics] for side, metrics in directions.items()]}
    return payload, task_ids, capacities, directions


def _write_json(path, value):
    raw = json.dumps(value, separators=(',', ':'), allow_nan=False).encode('utf-8')
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError('PyVRP worker envelope exceeds its byte budget.')
    with path.open('xb') as stream:
        stream.write(raw)


def _read_json(path):
    with path.open('rb') as stream:
        raw = stream.read(_MAX_JSON_BYTES + 1)
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError('PyVRP worker envelope exceeds its byte budget.')
    value = json.loads(raw)
    if type(value) is not dict or type(value.get('schema')) is not int or value['schema'] != _WIRE_VERSION:
        raise ValueError('Invalid PyVRP worker protocol.')
    return value


def _worker_env():
    allowed = ('PATH', 'SYSTEMROOT', 'WINDIR', 'TEMP', 'TMP', 'TMPDIR', 'LANG', 'LC_ALL', 'LC_CTYPE')
    env = {name: os.environ[name] for name in allowed if name in os.environ}
    installed = [str(Path(path).resolve()) for path in os.environ.get('PYTHONPATH', '').split(os.pathsep) if path]
    if installed:
        env['PYTHONPATH'] = os.pathsep.join(installed)
    env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1', PYTHONHASHSEED='0')
    return env


def _run_worker(directory, check, deadline):
    command = [sys.executable, '-B', str(Path(__file__).resolve()), '--worker', str(directory)]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=_worker_env(), close_fds=True)
    try:
        while True:
            check()
            try:
                status = process.wait(timeout=min(_POLL_SECONDS, max(0, deadline - monotonic())))
                check()
                return status
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait()


def _decode_response(response, task_ids, capacities, directions, clients, check):
    rows, diagnostics = response['proposals'], response['diagnostics']
    if (type(rows) is not list or type(diagnostics) is not dict or len(diagnostics) > 24 or
            any(type(value) not in (str, int, float, bool, type(None)) or
                (type(value) is float and not isfinite(value)) or (type(value) is str and len(value) > 160)
                for value in diagnostics.values())):
        raise ValueError('Invalid PyVRP worker response.')
    reason = diagnostics['stop_reason']
    if reason in _SKIPS and not rows:
        return [], diagnostics
    if reason != 'SEARCH_COMPLETED' or diagnostics.get('version') != _VERSION:
        raise ValueError('Invalid PyVRP worker result status.')
    seen = set()
    proposals = []
    for group, member, order in rows:
        check()
        if (not _integer(group, len(capacities) - 1) or not _integer(member, len(capacities[group][1]) - 1)
                or type(order) is not list or not order or len(order) > len(task_ids)
                or not all(_integer(index, len(task_ids) - 1) for index in order) or len(set(order)) != len(order)
                or any(side not in directions for side in zip([-1, *order], order))
                or sum(clients[index][0] for index in order) > capacities[group][0]):
            raise ValueError('Invalid physical or measured PyVRP proposal.')
        key = group, member, tuple(order)
        if key in seen:
            raise ValueError('A PyVRP proposal was repeated on the wire.')
        seen.add(key)
        proposals.append({'vehicle_id': capacities[group][1][member], 'task_ids': [task_ids[index] for index in order]})
    return proposals, diagnostics


def generate(data, measured_rows, *, seconds, seed, control):
    """Return route identities. The caller independently reprices and checks them."""
    started = monotonic()

    def result(reason):
        return [], {'stop_reason': reason, 'elapsed_seconds': round(monotonic() - started, 6)}

    control()
    if type(seconds) not in (float, int) or not isfinite(seconds) or seconds <= 0:
        return result('TIME_BUDGET')
    deadline = started + seconds

    def check():
        control()
        if monotonic() >= deadline:
            raise _StageExpired

    try:
        try:
            request, task_ids, capacities, directions = _request(data, measured_rows, deadline, seed, check)
        except (KeyError, TypeError, ValueError, ArithmeticError):
            check()
            return result('PYVRP_UNSUPPORTED_INPUT')
        with TemporaryDirectory(prefix='suka-pyvrp-') as temporary:
            directory = Path(temporary)
            _write_json(directory / 'request.json', request)
            check()
            if _run_worker(directory, check, deadline) != 0:
                return result('PYVRP_WORKER_ERROR')
            check()
            response = _read_json(directory / 'result.json')
            proposals, diagnostics = _decode_response(response, task_ids, capacities, directions, request['clients'], check)
        check()
    except _StageExpired:
        return result('TIME_BUDGET')
    except (OSError, KeyError, ValueError, TypeError, RecursionError, ArithmeticError):
        control()
        return result('TIME_BUDGET' if monotonic() >= deadline else 'PYVRP_WORKER_ERROR')
    return proposals, {**diagnostics, 'elapsed_seconds': round(monotonic() - started, 6)}


def _validate_request(request):
    if set(request) != {'schema', 'deadline', 'seed', 'meter_cost', 'scale', 'clients', 'groups', 'edges'}:
        raise ValueError('Invalid PyVRP worker request fields.')
    deadline, clients, groups, edges = (request[k] for k in ('deadline', 'clients', 'groups', 'edges'))
    if (type(deadline) not in (int, float) or not isfinite(deadline)
            or not _integer(request['seed'], 2**32 - 1) or not _integer(request['meter_cost'])
            or not _integer(request['scale'], _MAX_MODEL_VALUE) or request['scale'] == 0
            or type(clients) is not list or not 0 < len(clients) <= _MAX_CLIENTS
            or type(groups) is not list or not groups or type(edges) is not list or len(edges) > len(clients)**2):
        raise ValueError('Invalid PyVRP request dimensions.')
    for pieces, x, y, opening, close, unload, prize in clients:
        if (not _integer(pieces) or not all(type(coordinate) in (int, float) and isfinite(coordinate) for coordinate in (x, y))
                or not all(_integer(value) for value in (opening, close, unload)) or opening > close
                or (prize is not None and not _integer(prize))):
            raise ValueError('Invalid PyVRP client facts.')
    for capacity, count in groups:
        if not _integer(capacity) or capacity == 0 or not _integer(count) or count == 0:
            raise ValueError('Invalid PyVRP group capacity.')
    seen = set()
    for origin, destination, meters, duration in edges:
        if (type(origin) is not int or not -1 <= origin < len(clients)
                or not _integer(destination, len(clients) - 1) or origin == destination
                or not _integer(meters) or not _integer(duration) or (origin, destination) in seen):
            raise ValueError('Invalid frozen PyVRP edge.')
        seen.add((origin, destination))


def _solve(request):
    from pyvrp import IteratedLocalSearchCallbacks, IteratedLocalSearchParams, Model, SolveParams

    started = monotonic()
    reserve = min(_MAX_RETURN_RESERVE_SECONDS, max(.05, (request['deadline'] - started) * .05))
    deadline = request['deadline'] - reserve
    clients, groups, edges = (request[k] for k in ('clients', 'groups', 'edges'))
    def stop(_objective=None):
        return monotonic() >= deadline

    def check():
        if stop():
            raise _StageExpired

    check()
    horizon = max(close + unload for _, _, _, _, close, unload, _ in clients)
    meter_cost = request['meter_cost']
    maximum = max(horizon, *(prize or 0 for _, _, _, _, _, _, prize in clients))
    for index, (_, _, meters, duration) in enumerate(edges):
        if index % 32 == 0:
            check()
        maximum = max(maximum, meter_cost * meters, duration)
    if maximum >= _MAX_MODEL_VALUE or (maximum + 1) * len(clients) >= 2**50:
        raise ValueError('The graph is outside the bounded PyVRP integer model.')

    model = Model()
    depot_location, sink_location = model.add_location(x=0, y=0), model.add_location(x=0, y=0)
    depot = model.add_depot(depot_location, tw_early=0, tw_late=0)
    sink = model.add_depot(sink_location, tw_early=0, tw_late=horizon)
    model.add_edge(depot_location, sink_location, distance=0, duration=0)
    locations = []
    for pieces, x, y, opening, close, unload, prize in clients:
        check()
        location = model.add_location(x=x, y=y)
        locations.append(location)
        model.add_client(location, delivery=pieces, service_duration=unload,
                         tw_early=opening, tw_late=close, required=prize is None, prize=prize or 0)
        model.add_edge(location, sink_location, distance=0, duration=0)
    for capacity, count in groups:
        check()
        model.add_vehicle_type(num_available=count, capacity=capacity, start_depot=depot, end_depot=sink,
                               start_late=0, tw_early=0, tw_late=horizon, shift_duration=horizon)
    for index, (origin, destination, meters, duration) in enumerate(edges):
        if index % 32 == 0:
            check()
        model.add_edge(depot_location if origin == -1 else locations[origin], locations[destination],
                       distance=meter_cost * meters, duration=duration)
    pending = {}
    best_events = 0

    class CandidateObserver(IteratedLocalSearchCallbacks):
        def on_best(self, best):
            nonlocal best_events
            if monotonic() >= request['deadline']:
                return
            best_events += 1
            members = [iter(range(count)) for _, count in groups]
            for route in best.routes():
                if monotonic() >= request['deadline']:
                    return
                order = tuple(activity.idx for activity in route if activity.is_client())
                if order and route.is_feasible():
                    group = route.vehicle_type()
                    pending.setdefault((group, next(members[group]), order), None)

    observer = CandidateObserver()
    check()
    options = SolveParams(ils=IteratedLocalSearchParams(callbacks=observer))
    result = model.solve(stop, seed=request['seed'], collect_stats=False, display=False,
                         params=options, missing_value=maximum + 1)
    if result.best.is_feasible():
        observer.on_best(result.best)
    return list(pending), {'stop_reason': 'SEARCH_COMPLETED', 'version': _VERSION,
        'iterations': result.num_iterations, 'native_feasible': result.best.is_feasible(),
        'observed_feasible_best_events': best_events, 'worker_solve_elapsed_seconds': round(monotonic() - started, 6),
        'native_seconds': result.runtime, 'measured_directions': len(edges), 'missing_directions': len(clients)**2 - len(edges),
        'unknown_time_barrier': maximum + 1, 'currency_units_per_yuan': request['scale'], 'return_reserve_seconds': reserve,
        'receipt_pricing_in_callbacks': False, 'graph_scope': 'SINGLE_WAREHOUSE_COMPATIBLE_KM_QUOTE'}


def _worker(directory):
    proposals = []
    try:
        request = _read_json(directory / 'request.json')
        _validate_request(request)
        installed = version('pyvrp')
        if installed == _VERSION:
            proposals, diagnostics = _solve(request)
        else:
            diagnostics = {'stop_reason': 'PYVRP_VERSION_MISMATCH', 'version': installed}
    except (PackageNotFoundError, ImportError):
        diagnostics = {'stop_reason': 'PYVRP_UNAVAILABLE'}
    except _StageExpired:
        diagnostics = {'stop_reason': 'TIME_BUDGET'}
    except (OSError, RuntimeError, KeyError, ValueError, TypeError, ArithmeticError, RecursionError) as exc:
        diagnostics = {'stop_reason': 'PYVRP_WORKER_ERROR', 'error_type': type(exc).__name__}
    _write_json(directory / 'result.json', {'schema': _WIRE_VERSION, 'proposals': proposals, 'diagnostics': diagnostics})


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker':
        raise SystemExit(2)
    _worker(Path(sys.argv[2]))
