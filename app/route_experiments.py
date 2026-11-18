"""Immutable, per-job switches for controlled delivery-cost experiments."""
from dataclasses import dataclass, fields


@dataclass(frozen=True)
class DeepExperiments:
    diagnostics: bool = True
    candidate_retention: str = 'first'
    complete_assignment_pricing: bool = False
    fragment_merging: bool = False
    stage_budget_reservation: bool = False
    native_candidate_retention: str = 'first'
    joint_reoptimization: bool = False
    scalable_joint_search: bool = False
    quote_group_rotation: bool = False
    generation_selection_loop: bool = False
    gain_time_allocation: bool = False
    critical_edge_collection: bool = False

    def __post_init__(self):
        for name in ('candidate_retention', 'native_candidate_retention'):
            if getattr(self, name) not in {'first', 'batch_dedup', 'diversity'}:
                raise ValueError('Unsupported candidate retention policy')
        for item in fields(self):
            if item.name not in {'candidate_retention', 'native_candidate_retention'} and not isinstance(getattr(self, item.name), bool):
                raise ValueError(item.name + ' must be a boolean')

    @classmethod
    def parse(cls, value):
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        return cls(**value)
