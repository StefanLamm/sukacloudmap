"""Price a returned native allocation before processing ordinary route rows.

Only identities come from the native model. Compact prices use current measured
roads and original quotes. A complete actual improvement is proved once as a
whole, rather than materializing every improving prefix of an ordinary batch.
"""
from decimal import Decimal
from time import monotonic

from app.route_metrics import evaluate_candidate
from app.routing_engine import StopSearch


def _complete(result):
    return (not result['unassigned'] and result['fees_complete'] and
            all(issue['code']=='DRIVING_REFERENCE' for issue in result['check']['issues']))


def assignment_callback(search, *, scope_ids=None, after_accept=None):
    """Create the optional host callback; never installed in legacy mode."""
    def callback(specs):
        try:
            record=price_complete_assignment(search,specs,scope_ids=scope_ids)
        except StopSearch as exc:
            # A selector may have proved a complete win immediately before its
            # local slice stopped. Give a surrounding thawed-scope host a chance
            # to retain that proved result; normal publication controls still
            # prevent network work after cancellation/the global wall deadline.
            record=search.assignment_pricing_diagnostics[-1]
            exc.assignment_pricing_record=record
            if (record['accepted_complete'] and after_accept is not None
                    and exc.reason not in {'CANCELLED','WALL_BUDGET'}):
                try:after_accept()
                except StopSearch as publish_exc:
                    publish_exc.assignment_pricing_record=record
                    # A local selector stop must not conceal a later global
                    # cancellation or wall deadline encountered publishing it.
                    if publish_exc.reason in {'CANCELLED','WALL_BUDGET'}:
                        raise
            raise
        if record['accepted_complete'] and after_accept is not None:
            try:after_accept()
            except StopSearch as exc:
                exc.assignment_pricing_record=record
                raise
        return record
    return callback


def price_complete_assignment(search, specs, *, scope_ids=None):
    """One compact pricing pass, then one consider/physical-selection call.

    The complete native allocation is independent of every ordinary pool cap.
    A quote-group scope can compete for cars used outside that group; those
    conflicts go through the host's global physical selector. No reference or
    historical fee is accepted. Search/cancellation exceptions propagate after
    the diagnostic record has been completed.
    """
    started=monotonic();compute_started=search.compute_elapsed()
    calls_before=search.road.stats().get('calls',0)
    complete_before=_complete(search.best)
    before_fee=search.best['total_fee']
    record={'scope':'RETURNED_NATIVE_ASSIGNMENT_CURRENT_ACTUAL_PRICING',
        'input_routes':len(specs),'priced_routes':0,'valid_routes':0,'rejected_routes':0,
        'kept_mandatory_routes':0,'kept_economic_optional_routes':0,'dropped_optional_routes':0,
        'missing_metric_routes':0,'consider_calls':0,'global_selection_calls':0,
        'complete_before':complete_before,'complete_after':complete_before,
        'before_fee':before_fee,'after_fee':before_fee,'direct_fee_reduction':'0.00',
        'accepted_complete':False,'stop_reason':'SEARCH_COMPLETED','rejection_codes':{},
        'ordinary_candidate_cap_applied':False,'new_network_calls_for_compact_pricing':0}
    if not hasattr(search,'assignment_pricing_diagnostics'):
        search.assignment_pricing_diagnostics=[]
    search.assignment_pricing_diagnostics.append(record)
    trace=getattr(search,'stage_trace',None)
    scope=trace.scope('ASSIGNMENT_PRICE') if trace is not None else None
    if trace is not None:trace.mark('ASSIGNMENT_PRICE',status='ENTERED',scope=scope)
    interrupted=False
    allowed=set(search.tasks) if scope_ids is None else set(scope_ids)
    def reject(code):
        record['rejection_codes'][code]=record['rejection_codes'].get(code,0)+1
    try:
        search.control()
        # Validate the allocation as a unit before admitting even its first
        # column. Physical-car reuse and overlapping tickets are never fixed by
        # silently accepting just one of the conflicting source rows.
        cars=set();covered=set()
        for spec in specs:
            search.control()
            if not isinstance(spec,dict) or not isinstance(spec.get('task_ids'),(list,tuple)) or not spec['task_ids']:
                reject('INVALID_ORDER');continue
            vid=spec.get('vehicle_id');ids=spec['task_ids']
            if not isinstance(vid,str) or any(not isinstance(item,str) for item in ids):
                reject('INVALID_ORDER');continue
            if vid not in search.data['vehicles']:
                reject('UNKNOWN_VEHICLE')
            if vid in cars:reject('VEHICLE_REUSED')
            if len(set(ids))!=len(ids) or covered.intersection(ids):reject('TASK_OVERLAP')
            if not set(ids)<=allowed:reject('OUTSIDE_SCOPE')
            cars.add(vid);covered.update(ids)
        if record['rejection_codes']:
            record['stop_reason']='INVALID_PHYSICAL_ASSIGNMENT'
            return record
        chosen=[]
        for spec in specs:
            search.control()
            record['priced_routes']+=1
            search.evaluations+=1
            telemetry=getattr(search.road,'search_diagnostics',None)
            if telemetry is not None:telemetry.counts['evaluated_candidates']+=1
            row=evaluate_candidate(search.data,spec['vehicle_id'],list(spec['task_ids']),search.road,search.control)
            if row is None:
                record['missing_metric_routes']+=1;record['rejected_routes']+=1
                reject('MISSING_METRICS')
                continue
            if row['errors'] or row['fee'] is None:
                record['rejected_routes']+=1
                for issue in row['errors']:reject(issue['code'])
                if trace is not None:
                    trace.route('ASSIGNMENT_PRICE',row,status='REJECTED',
                        reason='|'.join(sorted({issue['code'] for issue in row['errors']})),scope=scope)
                continue
            value=Decimal(row['fee'])
            if not value.is_finite() or value<0:
                record['rejected_routes']+=1;reject('INVALID_ACTUAL_FEE');continue
            record['valid_routes']+=1
            compact=search.remember(row)
            if trace is not None:trace.route('ASSIGNMENT_PRICE',row,status='VALID',fee=row['fee'],scope=scope)
            if compact['_hard']:
                chosen.append(compact);record['kept_mandatory_routes']+=1
            elif compact['_net']<0:
                chosen.append(compact);record['kept_economic_optional_routes']+=1
            else:record['dropped_optional_routes']+=1
        # Existing complete external lines remain available. A car taken by the
        # new scope cannot simultaneously retain its external incumbent line.
        used_cars={row['vehicle_id'] for row in chosen}
        kept=[row for row in search.best_routes if not allowed.intersection(row['task_ids'])
              and row['vehicle_id'] not in used_cars]
        proposed=[*kept,*chosen]
        assigned={item for row in proposed for item in row['task_ids']}
        record['uncovered_required_after_native_filter']=len(search.mandatory-assigned)
        record['external_car_conflicts']=sum(not allowed.intersection(row['task_ids']) and row['vehicle_id'] in used_cars
                                             for row in search.best_routes)
        if search.mandatory<=assigned:
            record['consider_calls']=1
            search.consider(proposed)
        else:
            # An incomplete native scope, a failed required route, or an
            # external car conflict must not replace the complete incumbent.
            # Keep valid columns and let one full physical-fleet selection mix
            # them with that incumbent's required coverage.
            record['global_selection_calls']=1
            search.pack()
        record['stop_reason']='PRICED_ASSIGNMENT_CONSIDERED'
    except StopSearch as exc:
        record['stop_reason']=exc.reason
        interrupted=True
        raise
    except Exception as exc:
        record['stop_reason']='ASSIGNMENT_PRICING_ERROR'
        record['exception_type']=type(exc).__name__
        interrupted=True
        raise
    finally:
        record.update(after_fee=search.best['total_fee'],complete_after=_complete(search.best),
            elapsed_seconds=round(monotonic()-started,6),
            compute_seconds=round(search.compute_elapsed()-compute_started,6),
            proof_network_calls=max(0,search.road.stats().get('calls',0)-calls_before))
        record['accepted_complete']=record['complete_after'] and (not complete_before or Decimal(record['after_fee'])<Decimal(before_fee))
        if complete_before and record['complete_after']:
            record['direct_fee_reduction']=str(Decimal(before_fee)-Decimal(record['after_fee']))
        if trace is not None:
            trace.mark('ASSIGNMENT_PRICE',status='INTERRUPTED' if interrupted
                else 'COMPLETE',reason=record['stop_reason'],scope=scope)
    return record
