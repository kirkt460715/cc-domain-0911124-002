"""Period search on irregular observations with explicit provenance.

The result separates three questions and keeps numeric evidence for each:

1. Which peaks does the fitted generalized Lomb-Scargle spectrum contain
   (period, amplitude, phase, and the time origin used for the phase)?
2. What does the *sampling* spectral window look like (which peaks can be
   produced by the observation times themselves, i.e. aliases)?
3. How likely is the strongest peak when the whole search is accounted for
   (block permutation inside contiguous segments keeps local correlation)?

No interpolation is performed: every retained observation enters the fit with
its actual timestamp, and observations are never connected across maintenance
windows or gaps larger than the configured maximum.
"""

import math
from decimal import Decimal, localcontext

from hangar_ingest.types import decimal_string, digest, fail, from_micros, identity, integer, instant, micros
from hangar_ingest.quality import devices, rules
from hangar_ingest.analysis import lomb, selection

VERSION = 1
MAXIMUM_FREQUENCY_HZ = 1000.0
MAXIMUM_FREQUENCY_POINTS = 2000
MINIMUM_FREQUENCY_POINTS = 4
MINIMUM_SAMPLES = 8
MINIMUM_SEGMENT_SAMPLES = 4
MINIMUM_SPAN_SECONDS = 4.0
DEFAULT_PERMUTATIONS = 99
MAXIMUM_PERMUTATIONS = 999
DEFAULT_BLOCK_LEVELS = 20
DEFAULT_TOP_PEAKS = 5


def _number(value, field):
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        fail('analysis_parameter', 'Expected a finite number', field=field, value=str(value))
    if not math.isfinite(parsed):
        fail('analysis_parameter', 'Expected a finite number', field=field, value=str(value))
    return parsed


def _frequency_range(minimum, maximum):
    low = _number(minimum, 'minimum_frequency_hz')
    high = _number(maximum, 'maximum_frequency_hz')
    if low <= 0.0 or high <= 0.0:
        fail('frequency_range', 'Frequencies must be positive')
    if low >= high:
        fail('frequency_range', 'Minimum frequency must be below maximum frequency')
    if high > MAXIMUM_FREQUENCY_HZ:
        fail('frequency_range', 'Frequency exceeds the supported maximum', maximum=MAXIMUM_FREQUENCY_HZ)
    return low, high


def _resolution(value):
    resolution = _number(value, 'resolution_hz')
    if resolution <= 0.0:
        fail('resolution', 'Resolution must be positive')
    return resolution


def _diagnostic(code, message, **context):
    return {'code': code, 'message': message, 'context': context}


def _decimal(value):
    """Render a finite float without scientific notation, using exact arithmetic."""
    if not isinstance(value, (int, float)):
        return value
    if not math.isfinite(value):
        fail('analysis_result', 'Computed value is not finite')
    with localcontext() as context:
        context.prec = 28
        return decimal_string(Decimal(repr(float(value))))


def _grid(low, high, resolution):
    if resolution > high - low:
        fail('frequency_grid_coarse', 'Resolution exceeds the requested frequency range')
    steps = int(math.floor((high - low) / resolution + 1e-9))
    frequencies = [low + index * resolution for index in range(steps + 1)]
    if frequencies and abs(frequencies[-1] - high) <= resolution * 1e-6:
        frequencies[-1] = high
    else:
        frequencies.append(high)
    if len(frequencies) > MAXIMUM_FREQUENCY_POINTS:
        return None, _diagnostic(
            'frequency_grid_dense',
            'Frequency grid would contain too many points; increase resolution_hz or narrow the range',
            requested_points=len(frequencies),
            maximum_points=MAXIMUM_FREQUENCY_POINTS,
            resolution_hz=resolution,
            range_hz={'minimum': low, 'maximum': high},
        )
    if len(frequencies) < MINIMUM_FREQUENCY_POINTS:
        return None, _diagnostic(
            'frequency_grid_coarse',
            'Resolution leaves too few frequencies in the requested range',
            requested_points=len(frequencies),
            minimum_points=MINIMUM_FREQUENCY_POINTS,
        )
    return frequencies, None


def analyze(db, device_id, metric, start, end, minimum_frequency_hz, maximum_frequency_hz, resolution_hz, *,
            exclude_quality_codes=None, duplicate_time_policy='mean', gap_seconds=None,
            include_maintenance=False, permutations=DEFAULT_PERMUTATIONS, seed=0,
            block_seconds=None, minimum_segment_samples=MINIMUM_SEGMENT_SAMPLES,
            minimum_samples=MINIMUM_SAMPLES, top_peaks=DEFAULT_TOP_PEAKS):
    """Search for a real period in Hangar Ingest observations of one device.

    Times come from observation timestamps (never an interpolated regular grid).
    Frequencies are in Hz and periods in seconds.  ``gap_seconds`` is the
    analysis-specific maximum gap; when omitted the device quality rule's
    ``max_gap_seconds`` is used.  A gap is a break only when it is strictly
    larger than the configured value.  See the returned ``policies`` for the
    exact duplicate, exclusion, segmentation and weighting rules.
    """
    start = _timestamp(start, 'start')
    end = _timestamp(end, 'end')
    if start >= end:
        fail('analysis_window', 'Analysis window must be nonempty', start=start, end=end)
    lower_us = micros(start)
    upper_us = micros(end)

    config = devices.device(db, device_id)
    if config['metric'] != metric:
        fail('analysis_metric', 'Metric does not match the device metric', expected=config['metric'], actual=metric)

    codes = _quality_codes(exclude_quality_codes)
    if duplicate_time_policy not in {'mean', 'first'}:
        fail('duplicate_policy', 'Duplicate time policy must be mean or first')
    if gap_seconds is None:
        gap_seconds = rules.get(db, device_id)['max_gap_seconds']
    integer(gap_seconds, 'gap_seconds', 1, 86400 * 31)
    integer(permutations, 'permutations', 1, MAXIMUM_PERMUTATIONS)
    integer(seed, 'seed')
    integer(minimum_segment_samples, 'minimum_segment_samples', 2, 1000000)
    integer(minimum_samples, 'minimum_samples', 1, 1000000)
    integer(top_peaks, 'top_peaks', 1, 50)
    if type(include_maintenance) is not bool:
        fail('include_maintenance', 'include_maintenance must be boolean')
    if block_seconds is not None:
        block_seconds = _number(block_seconds, 'block_seconds')
        if block_seconds <= 0.0:
            fail('block_seconds', 'Block length must be positive')

    low, high = _frequency_range(minimum_frequency_hz, maximum_frequency_hz)
    resolution = _resolution(resolution_hz)

    retained, excluded, points, duplicate_groups = selection.load(
        db, device_id, start, end,
        exclude_quality_codes=codes,
        duplicate_time_policy=duplicate_time_policy,
    )
    maintenance = selection.maintenance_intervals(db, device_id, start, end)
    excluded_maintenance = []
    if not include_maintenance:
        points, excluded_maintenance = selection.exclude_maintenance(points, maintenance)

    result = {
        'version': VERSION,
        'device_id': device_id,
        'aircraft': config['aircraft'],
        'component': config['component'],
        'metric': metric,
        'unit': config['canonical_unit'],
        'window': {
            'start': start,
            'end': end,
            'duration_seconds': _decimal((upper_us - lower_us) / 1000000.0),
        },
        'requested_frequency_range_hz': {
            'minimum': low,
            'maximum': high,
            'resolution': resolution,
        },
        'policies': {
            'space': 'observation_timestamps_without_interpolation',
            'mean': 'per_segment_floating_mean_with_one_shared_amplitude_and_phase',
            'weights': selection.WEIGHT_POLICY,
            'duplicate_time_policy': duplicate_time_policy,
            'duplicate_time_rule': (
                'observations at identical event_time are averaged'
                if duplicate_time_policy == 'mean'
                else 'observations at identical event_time keep the observation with the lowest observation_id'
            ),
            'excluded_quality_codes': sorted(codes),
            'quality_rule': 'an observation is excluded when any of its quality warning codes is listed in excluded_quality_codes',
            'gap_seconds': gap_seconds,
            'maintenance': 'excluded' if not include_maintenance else 'included',
            'segmentation_rule': (
                'split when consecutive retained samples are farther apart than gap_seconds, '
                'or when a maintenance window lies strictly between them; a single sinusoid is never '
                'fitted across such a break'
            ),
            'weight_rule': 'weights measure local time support, not report count; each is capped at a quarter '
                           'of the segment duration and normalized so a dense burst owns only the time it '
                           'actually covers',
            'significance': 'block permutation of observations within each contiguous segment; the maximum '
                            'spectrum peak of every permutation is compared with the observed maximum',
            'phase_origin': 'first retained observation timestamp in the analysis window',
        },
        'maintenance_windows': [
            {key: value for key, value in interval.items() if not key.endswith('_us')}
            for interval in maintenance
        ],
        'observation_ledger': {
            'retained': retained,
            'excluded_quality': excluded,
            'excluded_maintenance': excluded_maintenance,
            'duplicate_time_groups': duplicate_groups,
            'observation_ids_used': sorted(
                observation_id for point in points for observation_id in point['observation_ids']
            ),
        },
    }

    if not points:
        return _unassessed(result, _diagnostic(
            'no_samples',
            'No observations remain after the current exclusions',
            excluded_quality=len(excluded),
            excluded_maintenance=len(excluded_maintenance),
        ))

    samples_used = sum(len(point['observation_ids']) for point in points)
    if samples_used < minimum_samples:
        return _unassessed(result, _diagnostic(
            'insufficient_samples',
            'Too few usable observations for a period search',
            samples=samples_used,
            required=minimum_samples,
        ))

    span = (points[-1]['time_us'] - points[0]['time_us']) / 1000000.0
    if span < MINIMUM_SPAN_SECONDS:
        return _unassessed(result, _diagnostic(
            'coverage_too_short',
            'Observation span is too short for a frequency analysis',
            span_seconds=_decimal(span),
            required_seconds=_decimal(MINIMUM_SPAN_SECONDS),
        ))

    if len({point['value'] for point in points}) <= 1:
        return _unassessed(result, _diagnostic(
            'constant_series',
            'All usable observations are identical; periodicity cannot be distinguished from a constant level',
            samples=samples_used,
            value=decimal_string(points[0]['value']),
        ))
    if any(not math.isfinite(float(point['value'])) for point in points):
        return _unassessed(result, _diagnostic('nonfinite_value', 'Observation values must be finite'))
    origin_us = points[0]['time_us']
    raw_segments = selection.split_segments(
        points,
        gap_seconds * 1000000,
        maintenance if not include_maintenance else [],
    )
    segments, discarded = _segments(raw_segments, origin_us, minimum_segment_samples)
    result['segments'] = _segment_summary(segments)
    result['discarded_segments'] = discarded

    if not segments:
        return _unassessed(result, _diagnostic(
            'no_contiguous_segment',
            'Every contiguous segment is shorter than the minimum segment size; the record cannot be '
            'analysed without connecting observations across maintenance or gaps',
            minimum_segment_samples=minimum_segment_samples,
        ))

    usable = sum(segment['samples'] for segment in segments)
    if usable < minimum_samples:
        return _unassessed(result, _diagnostic(
            'insufficient_samples',
            'Too few observations remain inside contiguous segments',
            samples=usable,
            required=minimum_samples,
        ))
    if sum(segment['tss'] for segment in segments) <= 0.0:
        return _unassessed(result, _diagnostic(
            'constant_within_segments',
            'Within every contiguous segment all observations are identical; a shared sinusoid cannot be '
            'distinguished from per-segment levels',
            segments=len(segments),
        ))

    shortest = min(segment['duration_seconds'] for segment in segments)
    longest = max(segment['duration_seconds'] for segment in segments)
    diagnostics = []
    frequency_floor = 1.0 / span
    if low < frequency_floor:
        diagnostics.append(_diagnostic(
            'frequency_below_span',
            'The minimum frequency corresponds to less than one cycle over the usable span',
            minimum_supported_frequency_hz=frequency_floor,
            requested_minimum_frequency_hz=low,
        ))
    median_interval = _median_interval(points)
    sampling_limit = 0.5 / median_interval if median_interval and median_interval > 0.0 else None
    if shortest <= 0.0:
        return _unassessed(result, _diagnostic('coverage_too_short', 'A usable segment has zero duration'))
    if sampling_limit is not None and high > sampling_limit:
        diagnostics.append(_diagnostic(
            'frequency_above_sampling_support',
            'Frequencies above roughly half the median sampling rate are dominated by aliasing; '
            'inspect the sampling spectral window before interpreting peaks there',
            sampling_support_hz=sampling_limit,
            requested_maximum_frequency_hz=high,
        ))
        if low >= sampling_limit:
            return _unassessed(result, _diagnostic(
                'frequency_above_sampling_support',
                'The entire requested range lies above half the median sampling rate; peaks there are '
                'alias candidates of the observation pattern rather than independently resolved periods',
                sampling_support_hz=sampling_limit,
                requested_minimum_frequency_hz=low,
            ))

    frequencies, grid_problem = _grid(low, high, resolution)
    if grid_problem is not None:
        return _unassessed(result, grid_problem)
    if low * longest < 1.0:
        supported_floor = 1.0 / longest
        if high < supported_floor:
            return _unassessed(result, _diagnostic(
                'frequency_range_exceeds_segments',
                'No requested frequency completes one cycle inside the longest contiguous segment; '
                'a period longer than a usable segment cannot be tested without bridging a break',
                longest_segment_seconds=_decimal(longest),
                minimum_supported_frequency_hz=_decimal(supported_floor),
                requested_maximum_frequency_hz=high,
            ))
        kept = [frequency for frequency in frequencies if frequency >= supported_floor]
        if len(kept) < MINIMUM_FREQUENCY_POINTS:
            return _unassessed(result, _diagnostic(
                'frequency_grid_coarse',
                'Too few requested frequencies complete a cycle inside the longest contiguous segment; '
                'lower the requested minimum frequency or increase resolution_hz',
                retained_points=len(kept),
                minimum_points=MINIMUM_FREQUENCY_POINTS,
                longest_segment_seconds=_decimal(longest),
            ))
        diagnostics.append(_diagnostic(
            'frequency_range_adjusted',
            'Lowest frequencies were dropped: they do not complete one cycle inside the longest '
            'contiguous segment',
            requested_minimum_frequency_hz=low,
            effective_minimum_frequency_hz=kept[0],
            longest_segment_seconds=_decimal(longest),
        ))
        frequencies = kept
        low = frequencies[0]
    if shortest < longest:
        diagnostics.append(_diagnostic(
            'unequal_segment_lengths',
            'Segments have different durations. Each contributes its own floating level and the whole fit '
            'uses actual timestamps, but a short segment constrains fast frequencies weakly; read the '
            'segment list and the sampling window before trusting a high-frequency peak',
            shortest_segment_seconds=_decimal(shortest),
            longest_segment_seconds=_decimal(longest),
            maximum_frequency_hz=high,
        ))
        if shortest * high < 1.0 <= shortest * low:
            diagnostics.append(_diagnostic(
                'frequency_partially_supported',
                'Part of the requested range completes less than one cycle inside the shortest contiguous '
                'segment; those frequencies rely on the longer segments',
                shortest_segment_seconds=_decimal(shortest),
                one_cycle_limit_hz=_decimal(1.0 / shortest),
                requested_maximum_frequency_hz=high,
            ))
    result['frequency_range_hz'] = {
        'minimum': low,
        'maximum': high,
        'resolution': resolution,
        'points': len(frequencies),
    }
    result['frequency_grid'] = _grid_summary(frequencies)

    permutation_order = lomb.permutation_ranges(segments, block_seconds, DEFAULT_BLOCK_LEVELS, permutations, seed)
    spectrum = lomb.spectrum(segments, frequencies, permutations=permutation_order)
    skipped = spectrum['skipped']
    evaluated = len(frequencies) - len(skipped)
    if skipped:
        ratio = len(skipped) / len(frequencies)
        diagnostics.append(_diagnostic(
            'ill_conditioned_fit',
            'Some frequencies were skipped because the floating-mean design matrix is ill-conditioned '
            '(for example degenerate sampling without enough phase coverage)',
            skipped=len(skipped),
            evaluated=evaluated,
            skipped_fraction=_decimal(ratio),
            first_frequency_hz=frequencies[skipped[0]],
        ))
        if ratio > 0.5:
            result['assessed'] = False
            result['frequency_grid'] = _grid_summary(frequencies)
            result['frequency_range_hz'] = {
                'minimum': low,
                'maximum': high,
                'resolution': resolution,
                'points': len(frequencies),
            }
            result['diagnostics'] = diagnostics
            result['result_digest'] = digest(result)
            return result

    if evaluated == 0:
        return _unassessed(result, _diagnostic('ill_conditioned_fit', 'No frequency could be fitted stably'))

    valid_indices = [index for index in range(len(frequencies)) if index not in set(skipped)]
    powers = spectrum['powers']
    peak_indices = [index for index in lomb.local_maxima(powers) if powers[index] > 0.0]
    peak_indices.sort(key=lambda index: (-powers[index], frequencies[index]))
    selected = peak_indices[:top_peaks]
    peaks = [
        _peak(segments, frequencies[index], powers[index], origin_us) for index in selected
    ]
    strongest = max(valid_indices, key=lambda index: (powers[index], -frequencies[index]))
    strongest_power = powers[strongest]
    if strongest_power >= 1.0 and strongest_power - 1.0 < 1e-12:
        diagnostics.append(_diagnostic(
            'saturated_power',
            'The strongest peak explains all weighted variance; with a floating mean per segment and a '
            'cosine per segment this happens in degenerate configurations and the power should be read as '
            'an upper bound rather than a precise ratio',
            frequency_hz=frequencies[strongest],
            samples=usable,
            segments=len(segments),
        ))

    window_peaks = _window_peaks(frequencies, spectrum['window_powers'], top_peaks)
    main_lobe = _main_lobe(frequencies, spectrum['window_powers'], (upper_us - origin_us) / 1000000.0)
    aliasing = _aliasing(peaks, window_peaks, frequencies, main_lobe)

    significance = _significance(spectrum, frequencies, strongest, seed=seed, block_seconds=block_seconds)
    sampling = _sampling_summary(points, upper_us, window_peaks, frequencies, spectrum, (upper_us - origin_us) / 1000000.0)

    result.update({
        'assessed': True,
        'frequency_grid': _grid_summary(frequencies),
        'spectrum': {
            'normalization': 'fraction_of_weighted_variance_explained',
            'skipped_frequencies': len(skipped),
            'points': [
                {'frequency_hz': frequencies[index], 'power': powers[index]}
                for index in valid_indices
            ],
            'sampling_window_points': [
                {'frequency_hz': frequencies[index], 'power': spectrum['window_powers'][index]}
                for index in valid_indices
            ],
            'maximum_power': strongest_power,
            'maximum_frequency_hz': frequencies[strongest],
            'maximum_period_seconds': 1.0 / frequencies[strongest],
        },
        'peaks': peaks,
        'sampling_window': {
            'definition': 'normalized squared magnitude of the weighted phasor sum of observation times at '
                          'each frequency; peaks here are produced by the observation pattern, not by the signal',
            'main_lobe': main_lobe,
            'highest_side_peaks': window_peaks,
        },
        'aliasing': aliasing,
        'sampling': sampling,
        'significance': significance,
        'diagnostics': diagnostics,
    })
    result['calibration_lineage'] = _calibration_lineage(db, result['observation_ledger']['observation_ids_used'])
    result['result_digest'] = digest(result)
    return result


def _timestamp(value, field):
    return instant(value, field)


def _quality_codes(value):
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > 64:
        fail('quality_codes', 'Excluded quality codes must be a bounded list')
    result = [identity(code, 'quality_code') for code in value]
    if len(result) != len(set(result)):
        fail('quality_codes', 'Excluded quality codes must be unique')
    return result


def _unassessed(result, diagnostic):
    result['assessed'] = False
    result['diagnostics'] = [diagnostic]
    result['result_digest'] = digest(result)
    return result


def _segments(raw_segments, origin_us, minimum_segment_samples):
    segments = []
    discarded = []
    for index, raw in enumerate(raw_segments):
        def drop(reason):
            discarded.append({
                'segment': index,
                'samples': len(raw),
                'first': raw[0]['event_time'],
                'last': raw[-1]['event_time'],
                'reason': reason,
            })
        if len(raw) < minimum_segment_samples:
            drop('fewer_than_minimum_segment_samples')
            continue
        times = [(point['time_us'] - origin_us) / 1000000.0 for point in raw]
        duration = times[-1] - times[0]
        if duration <= 0.0:
            drop('zero_duration')
            continue
        weights = selection.segment_weights(times, duration)
        if weights is None:
            drop('degenerate_sampling')
            continue
        segment = {
            'index': len(segments),
            't': times,
            'y': [float(point['value']) for point in raw],
            'w': weights,
            'W': sum(weights),
            'samples': len(raw),
            'start': raw[0]['event_time'],
            'end': raw[-1]['event_time'],
            'duration_seconds': duration,
            'observation_ids': [item for point in raw for item in point['observation_ids']],
            'calibration_revisions': [revision for point in raw for revision in point['calibration_revisions']],
        }
        segment['sy'] = lomb.dot(segment['w'], segment['y'])
        segment['tss'] = lomb.segment_tss(segment['y'], segment['w'], segment['W'], segment['sy'])
        segments.append(segment)
    return segments, discarded


def _segment_summary(segments):
    return [
        {
            'segment': segment['index'],
            'start': segment['start'],
            'end': segment['end'],
            'samples': segment['samples'],
            'duration_seconds': _decimal(segment['duration_seconds']),
            'weight_sum': _decimal(segment['W']),
            'weighted_variance': _decimal(segment['tss']),
            'calibration_revisions': segment['calibration_revisions'],
            'observation_ids': segment['observation_ids'],
        }
        for segment in segments
    ]


def _grid_summary(frequencies):
    return {
        'minimum_hz': frequencies[0],
        'maximum_hz': frequencies[-1],
        'points': len(frequencies),
        'step_hz': frequencies[1] - frequencies[0] if len(frequencies) > 1 else None,
        'character': 'regular_in_frequency_not_in_time',
    }


def _peak(segments, frequency, power, origin_us):
    fitted = lomb.coefficients(segments, frequency)
    base = {
        'frequency_hz': frequency,
        'period_seconds': 1.0 / frequency,
        'power': power,
        'phase_reference_time': from_micros(origin_us),
    }
    if fitted is None:
        return {**base, 'amplitude': None, 'phase_radians': None, 'unresolved': 'ill_conditioned_fit'}
    cosine = fitted['cosine']
    sine = fitted['sine']
    return {
        **base,
        'amplitude': math.hypot(cosine, sine),
        'amplitude_policy': 'amplitude of the fitted cosine component (half peak-to-peak)',
        'phase_radians': math.atan2(sine, cosine),
        'phase_formula': 'model(t) = level + amplitude * cos(2*pi*frequency_hz*(t - phase_reference_time) - phase_radians)',
        'segment_levels': fitted['levels'],
        'mean_level': sum(fitted['levels']) / len(fitted['levels']),
    }


def _window_peaks(frequencies, window_powers, limit):
    rows = []
    for index in lomb.local_maxima(window_powers):
        if index == 0 or window_powers[index] <= 0.0:
            continue
        rows.append({
            'frequency_hz': frequencies[index],
            'period_seconds': 1.0 / frequencies[index],
            'power': window_powers[index],
        })
    rows.sort(key=lambda row: (-row['power'], row['frequency_hz']))
    return rows[:limit]


def _main_lobe(frequencies, window_powers, span_seconds):
    """Half-power radius of the central (zero-frequency) window lobe.

    The normalized sampling window equals 1 at zero frequency (all phasors
    aligned).  The first frequency where it falls to half power estimates the
    narrowest separable feature, close to 1/span for well-behaved sampling.
    """
    if not window_powers:
        return None
    half_power = None
    for index in range(len(window_powers)):
        if window_powers[index] <= 0.5:
            half_power = frequencies[index]
            break
    return {
        'zero_frequency_power': 1.0,
        'lowest_evaluated_frequency_hz': frequencies[0],
        'lowest_evaluated_power': window_powers[0],
        'half_power_radius_hz': half_power,
        'half_power_period_seconds': (1.0 / half_power) if half_power else None,
        'nominal_resolution_hz': (1.0 / span_seconds) if span_seconds > 0.0 else None,
        'note': 'features closer than this radius cannot be separated; side peaks further out are aliases',
    }


def _aliasing(peaks, window_peaks, frequencies, main_lobe):
    """Look for replicas of the strongest peak offset by sampling-window peaks."""
    if not peaks or not window_peaks:
        return {
            'checked_peaks': len(peaks),
            'coincidences': [],
            'note': 'no sampling-window side peaks were available to test against',
        }
    step = frequencies[1] - frequencies[0] if len(frequencies) > 1 else 0.0
    tolerance = max((main_lobe or {}).get('half_power_radius_hz') or 0.0, 2.0 * step)
    strongest = max(peaks, key=lambda row: row['power'])
    coincidences = []
    for window in window_peaks:
        if window['power'] < 0.25:
            continue
        for sign in (-1, 1):
            candidate = strongest['frequency_hz'] + sign * window['frequency_hz']
            if candidate <= frequencies[0] or candidate >= frequencies[-1]:
                continue
            match = [
                peak for peak in peaks
                if abs(peak['frequency_hz'] - candidate) <= tolerance
                and peak['frequency_hz'] != strongest['frequency_hz']
            ]
            if not match:
                continue
            coincidences.append({
                'strongest_peak_frequency_hz': strongest['frequency_hz'],
                'strongest_peak_period_seconds': strongest['period_seconds'],
                'window_frequency_hz': window['frequency_hz'],
                'window_period_seconds': window['period_seconds'],
                'window_power': window['power'],
                'alias_frequency_hz': match[0]['frequency_hz'],
                'alias_period_seconds': match[0]['period_seconds'],
                'alias_power': match[0]['power'],
                'relation': 'strongest_peak_{}_window'.format('plus' if sign > 0 else 'minus'),
                'interpretation': 'a detected peak sits exactly one sampling-window step from the strongest peak; '
                                  'it may be an alias of the observation pattern rather than an independent period',
            })
    return {
        'checked_peaks': len(peaks),
        'checked_window_peaks': len(window_peaks),
        'tolerance_hz': tolerance,
        'coincidences': coincidences,
        'note': 'no offset replica is not proof of a real period; read the sampling window alongside the spectrum',
    }


def _median_interval(points):
    gaps = [
        (points[index + 1]['time_us'] - points[index]['time_us']) / 1000000.0
        for index in range(len(points) - 1)
        if points[index + 1]['time_us'] > points[index]['time_us']
    ]
    if not gaps:
        return None
    gaps.sort()
    return gaps[len(gaps) // 2]


def _sampling_summary(points, upper_us, window_peaks, frequencies, spectrum, span_seconds):
    intervals = [
        (points[index + 1]['time_us'] - points[index]['time_us']) / 1000000.0
        for index in range(len(points) - 1)
        if points[index + 1]['time_us'] > points[index]['time_us']
    ]
    median = _median_interval(points)
    if intervals:
        mean = sum(intervals) / len(intervals)
        jitter = sum(abs(value - median) / median for value in intervals) / len(intervals) if median else None
    else:
        mean = None
        jitter = None
    main_lobe = _main_lobe(frequencies, spectrum['window_powers'], span_seconds)
    result = {
        'samples': sum(len(point['observation_ids']) for point in points),
        'distinct_times': len(points),
        'span_seconds': _decimal(span_seconds),
        'median_interval_seconds': _decimal(median) if median is not None else None,
        'mean_interval_seconds': _decimal(mean) if mean is not None else None,
        'relative_jitter': _decimal(jitter) if jitter is not None else None,
        'minimum_interval_seconds': _decimal(min(intervals)) if intervals else None,
        'maximum_interval_seconds': _decimal(max(intervals)) if intervals else None,
        'mean_sample_rate_hz': _decimal(len(points) / span_seconds) if span_seconds > 0.0 else None,
        'sample_rate_policy': 'mean sample rate is descriptive only; the fit uses actual timestamps',
        'sampling_window_main_lobe_hz': main_lobe,
        'side_peaks': window_peaks,
    }
    if median:
        result['classical_nyquist_hz'] = 0.5 / median
    return result


def _significance(spectrum, frequencies, strongest, *, seed, block_seconds):
    maxima = spectrum['permutation_maxima']
    observed = spectrum['powers'][strongest]
    if not maxima:
        return {
            'method': 'block_permutation_of_observations_within_contiguous_segments',
            'permutations': 0,
            'seed': seed,
            'false_alarm_probability': None,
            'note': 'no permutation was requested',
        }
    exceed = sum(1 for value in maxima if value >= observed - 1e-15)
    p_value = (exceed + 1.0) / (len(maxima) + 1.0)
    return {
        'method': 'block_permutation_of_observations_within_contiguous_segments',
        'assumption': 'consecutive time blocks keep local correlation structure; blocks never cross segment boundaries',
        'scope': 'search_corrected_maximum_peak_of_the_entire_frequency_grid',
        'note': 'not a single-frequency false-alarm probability; the whole search is repeated for every permutation',
        'permutations': len(maxima),
        'seed': seed,
        'block_seconds': block_seconds,
        'block_policy': 'configured_block_seconds_else_twenty_equal_time_levels_per_segment',
        'observed_maximum_power': observed,
        'maximum_power_over_the_grid_frequency_hz': frequencies[strongest],
        'permutation_maximum_mean': sum(maxima) / len(maxima),
        'permutation_maximum_median': sorted(maxima)[len(maxima) // 2],
        'permutation_maximum_95_percent': _quantile(maxima, 0.95),
        'permutation_maximum': max(maxima),
        'exceedances': exceed,
        'false_alarm_probability': p_value,
        'estimator': 'add_one',
        'reproducible_with_fixed_seed': True,
    }


def _quantile(values, q):
    ordered = sorted(values)
    position = q * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] + fraction * (ordered[upper] - ordered[lower])


def _calibration_lineage(db, observation_ids):
    entries = []
    for observation_id in observation_ids:
        row = db.one(
            'SELECT calibration_id,calibration_revision FROM observations WHERE observation_id=?',
            (observation_id,),
        )
        if row is None or row['calibration_id'] is None:
            entries.append({'calibration_id': None, 'revision_used': None})
            continue
        calibration = db.one('SELECT revision FROM calibrations WHERE calibration_id=?', (row['calibration_id'],))
        entries.append({
            'calibration_id': row['calibration_id'],
            'revision_used': row['calibration_revision'],
            'current_revision': calibration['revision'] if calibration else None,
        })
    unique = []
    for entry in entries:
        if entry not in unique:
            unique.append(entry)
    details = []
    for entry in unique:
        if entry['calibration_id'] is None:
            continue
        row = db.one('SELECT * FROM calibrations WHERE calibration_id=?', (entry['calibration_id'],))
        details.append({**entry, **{key: row[key] for key in ('device_id', 'valid_from', 'valid_until', 'scale', 'offset', 'clock_offset_us')}} if row else entry)
    return {
        'observations': len(entries),
        'calibrations': details,
        'stale': any(entry['revision_used'] != entry['current_revision'] for entry in unique if entry['calibration_id']),
    }
