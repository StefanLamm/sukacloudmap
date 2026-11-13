"""Bounded small-group intensification using measured metrics only.

A large day's total size must not hide a tiny compatible warehouse/quote group.
Enumerate its ordered subsets within the host's phase budget, then let the host
compare the columns against the shared physical fleet and common delivery.
Large compatible groups may contribute small, explicitly named city subgroups;
city labels prioritize candidate generation and never prohibit cross-city routes.
Unknown roads remain unknown. Only the host may materialize and accept a route.
"""
from decimal import Decimal
import unicodedata

from app.route_metrics import evaluate_candidate
from app.routing_engine import RoadError


def _measured_orders(ids, count, warehouse, neighbors, pieces, capacity, control, diagnostics):
    """Enumerate only measured directed paths; never invent missing bridges.

    Pruning a missing edge or excess load is safe for every continuation. Time
    and regional-price decisions stay with the shared route evaluator: a later
    stop can, for example, resolve an uncertain once-per-route regional fee.
    """
    def walk(order, remaining, load):
        control()
        if len(order) == count:
            yield tuple(order)
            return
        origin = order[-1] if order else warehouse
        for item in remaining:
            control()
            if item not in neighbors.get(origin, ()):
                diagnostics['pruned_missing_prefixes'] += 1
                continue
            next_load = load + pieces[item]
            if next_load > capacity:
                diagnostics['pruned_capacity_prefixes'] += 1
                continue
            yield from walk(order + [item], [i for i in remaining if i != item], next_load)
    yield from walk([], ids, 0)


def _rank(row):
    return Decimal(row['fee']), row['meters'], row['seconds'], tuple(row['task_ids'])


def _city_identity(task):
    """Conservative namespaced labels, never a delivery compatibility rule.

    Do not infer an administrative city from a county code: province-direct
    counties and municipalities do not share one universal prefix convention.
    Both province and city must be explicit; equal city names in different
    provinces stay separate. Missing labels do not create an "unknown city".
    """
    labels = []
    unknown = {'', '-', '未知', '不详', '未填写', '未设置', 'unknown', 'n/a'}
    for field in ('province', 'city'):
        value = task.get(field)
        if not isinstance(value, str):
            return None
        label = ' '.join(unicodedata.normalize('NFKC', value).split()).casefold()
        if label in unknown:
            return None
        labels.append(label)
    return tuple(labels)


def seed_small_groups(search, max_stores=7):
    search.control()
    if not callable(getattr(search.road, 'known_segment', None)):
        return {'eligible_groups': 0, 'completed_groups': 0, 'orders_checked': 0,
                'stop_reason': 'MATRIX_UNAVAILABLE'}
    compatible = {}
    cars_by_warehouse = {}
    for car in search.cars:
        cars_by_warehouse.setdefault(car['warehouse_id'], []).append(car)
    for item, task in search.tasks.items():
        if task['signature']:
            compatible.setdefault((task['warehouse_id'], task['signature']), []).append(item)
    groups = []
    seen = set()
    scope = {'compatible_groups_seen': len(compatible), 'full_small_groups': 0,
             'large_compatible_groups': 0, 'city_subgroups': 0,
             'unclassified_city_tasks': 0, 'singleton_city_groups': 0,
             'city_subgroups_over_limit': 0, 'groups_without_measured_departure': 0}
    def add(key, ids, kind):
        ordered = sorted(ids)
        identity = key, tuple(ordered)
        if identity not in seen:
            seen.add(identity)
            groups.append((key, ordered))
            scope[kind] += 1
    for key, ids in compatible.items():
        search.control()
        if key[0] not in cars_by_warehouse:
            continue
        if len(ids) <= max_stores:
            # Preserve full cross-city enumeration for an already small group.
            add(key, ids, 'full_small_groups')
            continue
        scope['large_compatible_groups'] += 1
        cities = {}
        for item in ids:
            search.control()
            city = _city_identity(search.tasks[item])
            if city is None:
                scope['unclassified_city_tasks'] += 1
            else:
                cities.setdefault(city, []).append(item)
        for members in cities.values():
            if len(members) == 1:
                scope['singleton_city_groups'] += 1
            elif len(members) <= max_stores:
                add(key, members, 'city_subgroups')
            else:
                scope['city_subgroups_over_limit'] += 1
    groups.sort(key=lambda row: (len(row[1]), row[0][0], row[1]))
    diagnostics = {'eligible_groups': len(groups), 'completed_groups': 0,
                   'orders_checked': 0, 'missing_metric_orders': 0,
                   'pruned_missing_prefixes': 0, 'pruned_capacity_prefixes': 0,
                   'max_group_stores': max_stores, 'global_optimality_proven': False,
                   'city_subgroup_min_stores': 2, 'city_scope_is_candidate_only': True, **scope}
    for (warehouse, _), ids in groups:
        cars = cars_by_warehouse[warehouse]
        max_capacity = max(v['max_pieces'] for v in cars)
        pieces_by_id = {i: search.tasks[i]['pieces'] for i in ids}
        sorted_pieces = sorted(pieces_by_id.values())
        origins = {warehouse: search.data['warehouses'][warehouse], **{i: search.tasks[i] for i in ids}}
        neighbors = {}
        for origin, point in origins.items():
            for item in ids:
                search.control()
                if origin == item:
                    continue
                try:
                    known = search.road.known_segment(point, search.tasks[item])
                except (KeyError, ValueError, TypeError, RoadError):
                    known = None
                if known is not None:
                    neighbors.setdefault(origin, set()).add(item)
            if origin == warehouse and not neighbors.get(warehouse):
                # A missing measured departure makes every continuation absent.
                # Avoid building the rest of this city's matrix or packing an
                # unchanged pool, especially when many city groups are present.
                diagnostics['groups_without_measured_departure'] += 1
                diagnostics['pruned_missing_prefixes'] += len(ids)
                break
        if not neighbors.get(warehouse):
            diagnostics['completed_groups'] += 1
            continue
        # Full coverage is tried first, so a short budget can still discover a
        # one-car alternative before enumerating all cheaper partial columns.
        for count in range(len(ids), 0, -1):
            if sum(sorted_pieces[:count]) > max_capacity:
                continue
            for order in _measured_orders(ids, count, warehouse, neighbors, pieces_by_id,
                                          max_capacity, search.control, diagnostics):
                search.control()
                pieces = sum(search.tasks[i]['pieces'] for i in order)
                mask = sum(search.indices[i] for i in order)
                for car in cars:
                    search.control()
                    if pieces > car['max_pieces']:
                        continue
                    diagnostics['orders_checked'] += 1
                    search.evaluations += 1
                    value = evaluate_candidate(search.data, car['id'], order, search.road, search.control)
                    if value is None:
                        diagnostics['missing_metric_orders'] += 1
                        continue
                    if not search.route_ok(value):
                        continue
                    prior = search.pool.get((car['id'], mask))
                    if prior is not None and _rank(value) >= _rank(prior):
                        continue
                    candidate = search.remember(value)
                    search.quick_exchange(candidate)
        diagnostics['completed_groups'] += 1
        if search.pool:
            search.pack()
    return diagnostics
