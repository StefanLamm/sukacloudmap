"""Bounded, job-local telemetry. Observations never change route eligibility."""
from collections import Counter
from decimal import Decimal


class SearchDiagnostics:
    def __init__(self, budget, task_count=None, *, curve_limit=4096, edge_limit=2048):
        self.budget = budget
        self.task_count = task_count
        self.curve_limit, self.edge_limit = curve_limit, edge_limit
        self.first_complete = None
        self.best_fee = None
        self.curve = []
        self.omitted_improvements = 0
        self.counts = Counter()
        self.rejections = Counter()
        self.missing_edges = Counter()
        self.omitted_edge_observations = 0
        self.stage = 'INITIALIZATION'
        self.validation = None

    def plan(self, result, source):
        # Callers have already assembled and checked the allocation. Do not
        # rescan geometry or run a second proof in the incumbent hot path.
        if result.get('unassigned') or not result.get('fees_complete'):
            return
        if self.task_count is not None and result['check']['task_count'] != self.task_count:
            return
        if any(i['code'] != 'DRIVING_REFERENCE' for i in result['check']['issues']):
            return
        fee = Decimal(result['total_fee'])
        if self.best_fee is not None and fee >= self.best_fee:
            return
        point = {'elapsed_seconds': round(self.budget.elapsed(), 6),
                 'compute_seconds': round(self.budget.compute(), 6),
                 'total_fee': result['total_fee'], 'source': source, 'stage': self.stage}
        if self.first_complete is None:
            self.first_complete = dict(point)
        self.best_fee = fee
        if len(self.curve) < self.curve_limit:
            self.curve.append(point)
        else:
            self.curve[-1] = point
            self.omitted_improvements += 1

    def missing(self, origin, destination):
        self.counts['missing_metric_lookups'] += 1
        key = (origin['id'], destination['id'])
        if key in self.missing_edges or len(self.missing_edges) < self.edge_limit:
            self.missing_edges[key] += 1
        else:
            self.omitted_edge_observations += 1

    def reject(self, errors):
        self.counts['rejected_candidates'] += 1
        codes = {item['code'] for item in errors}
        self.rejections.update(codes)
        if any(code.startswith(('ROAD_', 'DRIVING_', 'TRUCK_', 'COORDINATES_')) for code in codes):
            self.counts['road_rejected_candidates'] += 1

    def snapshot(self):
        return {'first_complete': self.first_complete,
                'first_complete_elapsed_seconds': (self.first_complete or {}).get('elapsed_seconds'),
                'first_complete_compute_seconds': (self.first_complete or {}).get('compute_seconds'),
                'fee_curve': self.curve, 'omitted_improvements': self.omitted_improvements,
                'counts': dict(self.counts), 'rejection_codes': dict(sorted(self.rejections.items())),
                'missing_edges': [{'from_id': a, 'to_id': b, 'attempts': count}
                                  for (a, b), count in sorted(self.missing_edges.items(), key=lambda p: (-p[1], p[0]))],
                'omitted_edge_observations': self.omitted_edge_observations,
                'final_road_fee_change': self.validation,
                'clock_policy': 'COMPUTE_EXCLUDES_MEASURED_PROVIDER_IO; WALL_INCLUDES_ALL_WAIT',
                'curve_scope': 'CHECKED_COMPLETE_INCUMBENTS_ON_SEARCH_ROADS; FINAL_REBIND_REPORTED_SEPARATELY'}
