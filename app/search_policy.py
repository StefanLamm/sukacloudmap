"""Bound production search by the job deadline, never evaluated-candidate totals."""
import math
import time


def production_search_control(road, control=None, *, cancelled=None):
    """Keep research behavior; require an absolute bound for production callers.

    PlanningRunner already supplies its compute/wall/cancel control. The second
    deadline check also bounds direct helper use and all-cache enumeration;
    checking only HTTP would leave those loops unbounded after removing caps.
    This wrapper does not inspect enabled or issue HTTP, so disabled-network
    planning can still use existing measured evidence.
    """
    if not getattr(road, 'production_planning', False):
        return control
    initial_deadline = getattr(road, 'wall_deadline', None)
    if (isinstance(initial_deadline, bool) or initial_deadline is None
            or not math.isfinite(float(initial_deadline))):
        raise ValueError('PRODUCTION_SEARCH_REQUIRES_ABSOLUTE_WALL_DEADLINE')
    timer = getattr(road, 'timer', time.monotonic)
    initial_deadline = float(initial_deadline)

    def bounded():
        from app.routing_engine import StopSearch
        if control is not None:
            control()
        if ((cancelled is not None and cancelled())
                or (callable(getattr(road, 'cancelled', None)) and road.cancelled())):
            raise StopSearch('CANCELLED')
        current = getattr(road, 'wall_deadline', initial_deadline)
        # A caller can tighten an active deadline; it cannot extend this scope.
        deadline = initial_deadline
        if current is not None and not isinstance(current, bool) and math.isfinite(float(current)):
            deadline = min(deadline, float(current))
        if timer() >= deadline:
            raise StopSearch('WALL_BUDGET')

    return bounded
