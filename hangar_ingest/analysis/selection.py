"""Observation selection, exclusion accounting and gap segmentation."""

from decimal import Decimal

from hangar_ingest.types import from_micros, micros
from hangar_ingest.quality import devices
from hangar_ingest.runs import windows

WEIGHT_CAP_FRACTION = 0.25
WEIGHT_POLICY = 'trapezoidal_time_support_half_distance_to_neighbours_capped_at_quarter_segment_duration'


def quality_codes(row):
    return sorted({issue.get('code') for issue in row['quality'] if issue.get('code')})


def observation_ledger(row, codes):
    return {
        'observation_id': row['observation_id'],
        'source': row['source'],
        'external_event': row['external_event'],
        'raw_time': row['raw_time'],
        'raw_value': row['raw_value'],
        'raw_unit': row['raw_unit'],
        'event_time': row['event_time'],
        'value': row['value'],
        'unit': row['unit'],
        'calibration_id': row['calibration_id'],
        'calibration_revision': row['calibration_revision'],
        'quality_codes': codes,
        'batch_id': row['batch_id'],
    }


def load(db, device_id, start, end, *, exclude_quality_codes=(), duplicate_time_policy='mean'):
    """Select observations and build the retained/excluded provenance ledger."""
    exclude = set(exclude_quality_codes)
    rows = windows.select(db, device_id, start, end)
    retained = []
    excluded = []
    for row in rows:
        codes = quality_codes(row)
        ledger = observation_ledger(row, codes)
        matched = sorted(set(codes) & exclude)
        if matched:
            excluded.append({**ledger, 'reasons': [{'code': 'quality_flag', 'flags': matched}]})
        else:
            retained.append(ledger)

    groups = {}
    for row in retained:
        groups.setdefault(row['event_time'], []).append(row)
    merged = []
    duplicate_groups = []
    first = duplicate_time_policy == 'first'
    for event_time in sorted(groups):
        members = groups[event_time]
        values = [Decimal(member['value']) for member in members]
        if len(members) == 1:
            selected = values[0]
        else:
            if first:
                # windows.select already orders by observation_id, so the retained
                # "first" observation is deterministic.
                selected = values[0]
            else:
                selected = sum(values) / len(values)
            duplicate_groups.append({
                'event_time': event_time,
                'policy': duplicate_time_policy,
                'observation_ids': [member['observation_id'] for member in members],
                'values': [member['value'] for member in members],
                'selected_value': str(selected),
                'conflicting_values': len({member['value'] for member in members}) > 1,
            })
        merged.append({
            'event_time': event_time,
            'time_us': micros(event_time),
            'value': selected,
            'observation_ids': [member['observation_id'] for member in members],
            'calibration_revisions': [[member['calibration_id'], member['calibration_revision']] for member in members],
        })
    return retained, excluded, merged, duplicate_groups


def maintenance_intervals(db, device_id, start, end):
    device = devices.device(db, device_id)
    rows = db.all(
        "SELECT order_id,opened_at,closed_at FROM work_orders WHERE aircraft=? AND component=? "
        "AND state!='cancelled' AND opened_at<? AND closed_at>? ORDER BY opened_at,order_id",
        (device['aircraft'], device['component'], end, start),
    )
    lower = micros(start)
    upper = micros(end)
    result = []
    for row in rows:
        left = max(lower, micros(row['opened_at']))
        right = min(upper, micros(row['closed_at']))
        if left < right:
            result.append({
                'order_id': row['order_id'],
                'start': from_micros(left),
                'end': from_micros(right),
                'start_us': left,
                'end_us': right,
            })
    return result


def exclude_maintenance(points, intervals):
    kept = []
    removed = []
    for point in points:
        matches = [interval for interval in intervals if interval['start_us'] <= point['time_us'] < interval['end_us']]
        if matches:
            removed.append({
                'event_time': point['event_time'],
                'observation_ids': point['observation_ids'],
                'order_ids': [item['order_id'] for item in matches],
            })
        else:
            kept.append(point)
    return kept, removed


def split_segments(points, max_gap_us, intervals):
    """Cut at gaps strictly larger than ``max_gap_us`` or across maintenance."""
    if not points:
        return []
    segments = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        gap = current['time_us'] - previous['time_us']
        cut = gap > max_gap_us
        if not cut:
            for interval in intervals:
                if interval['start_us'] <= previous['time_us'] and interval['end_us'] > previous['time_us']:
                    cut = True
                    break
                if interval['start_us'] > previous['time_us'] and interval['start_us'] < current['time_us']:
                    cut = True
                    break
        if cut:
            segments.append([current])
        else:
            segments[-1].append(current)
    return segments


def segment_weights(times, duration):
    """Time-support weights bounded so a dense burst cannot dominate a segment.

    The weight is half the distance to the neighbouring samples, capped at a
    quarter of the segment duration and normalized so the segment total equals
    its duration (time already spent), not its report count.
    """
    count = len(times)
    cap = WEIGHT_CAP_FRACTION * duration
    supports = []
    for index in range(count):
        if index == 0:
            support = (times[1] - times[0]) / 2.0
        elif index == count - 1:
            support = (times[-1] - times[-2]) / 2.0
        else:
            support = (times[index + 1] - times[index - 1]) / 2.0
        supports.append(min(support, cap))
    total = sum(supports)
    if total <= 0.0 or duration <= 0.0:
        return None
    return [support * duration / total for support in supports]
