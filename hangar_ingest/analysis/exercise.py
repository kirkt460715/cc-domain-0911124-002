"""Local numeric exercise for the period search.

The exercise ingests synthetic observations through the normal Hangar Ingest
path into an isolated in-memory database and prints numeric evidence only:

* ``true_period`` -- an irregularly sampled sinusoid should be recovered;
* ``pure_noise`` -- independent noise recorded at exactly the same timestamps
  should not produce a significant searched maximum;
* ``window_alias`` -- a deliberately periodic reporting pattern creates a
  sampling-window side lobe, and the detector should flag the replica.

No graphical output is produced.
"""

import datetime as dt
import math
import random

from hangar_ingest.analysis import period
from hangar_ingest.types import fail
from hangar_ingest.decoders import sensors
from hangar_ingest.runs import batches, processor
from hangar_ingest.runs.database import Database
from hangar_ingest.quality import devices, rules

BASE = dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc)
TRUE_PERIOD_SECONDS = 300.0
TRUE_AMPLITUDE = 3.0
TRUE_PHASE = 0.4
NOISE_SIGMA = 0.35
SPAN_SECONDS = 7200.0
CADENCE_SECONDS = 2.1


def _timestamp(offset):
    return (BASE + dt.timedelta(seconds=offset)).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def _jittered_times(count, cadence, jitter, seed):
    generator = random.Random(seed)
    times = []
    for index in range(count):
        offset = cadence * index + generator.uniform(-jitter, jitter)
        if offset >= 0.0:
            times.append(offset)
    return times


def _burst_times():
    """Observation times whose pattern creates a strong 600 s alias."""
    times = []
    for block in range(12):
        start = block * 600.0
        for index in range(40):
            times.append(start + index * 2.0)
    return times


def _install(db, device_id, source, external_id, times, values, *, maximum_gap_seconds):
    devices.register(db, device_id, 'plane1', 'engine1', 'temperature', 'C')
    devices.add_alias(db, source, external_id, device_id, '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
    devices.add_calibration(db, f'cal-{device_id}', device_id, '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
    rules.configure(db, device_id, minimum='-1000', maximum='1000', max_gap_seconds=maximum_gap_seconds)
    rows = [
        {
            'event_id': f'{external_id}-{index}',
            'device': external_id,
            'observed_at': _timestamp(offset),
            'value': str(round(value, 9)),
            'unit': 'C',
        }
        for index, (offset, value) in enumerate(zip(times, values))
    ]
    batch_id = f'batch-{device_id}'
    batches.stage(db, batch_id, source, sensors.encode(rows))
    processor.drain(db, batch_id, chunk=500)
    return len(rows)


def _values(times, *, signal, seed, phase=TRUE_PHASE):
    generator = random.Random(seed)
    values = []
    for offset in times:
        value = generator.gauss(0.0, NOISE_SIGMA)
        if signal:
            value += TRUE_AMPLITUDE * math.cos(2.0 * math.pi * offset / TRUE_PERIOD_SECONDS + phase)
        values.append(value)
    return values


def _analyze(db, device_id, start, end, *, minimum_hz, maximum_hz, resolution_hz, seed, permutations,
             gap_seconds=None):
    return period.analyze(
        db, device_id, 'temperature', _timestamp(start), _timestamp(end),
        minimum_hz, maximum_hz, resolution_hz,
        permutations=permutations, seed=seed, gap_seconds=gap_seconds,
    )


def _summary(result):
    top = result['peaks'][0] if result.get('peaks') else None
    return {
        'assessed': result['assessed'],
        'top_peak': None if top is None else {
            'period_seconds': top['period_seconds'],
            'frequency_hz': top['frequency_hz'],
            'power': top['power'],
            'amplitude': top['amplitude'],
            'phase_radians': top['phase_radians'],
            'phase_reference_time': top['phase_reference_time'],
        },
        'false_alarm_probability': result['significance']['false_alarm_probability'],
        'permutations': result['significance']['permutations'],
        'observed_maximum_power': result['significance']['observed_maximum_power'],
        'permutation_maximum_mean': result['significance']['permutation_maximum_mean'],
        'permutation_maximum_95_percent': result['significance']['permutation_maximum_95_percent'],
        'sampling_window_side_peaks': result['sampling_window']['highest_side_peaks'],
        'aliasing_coincidences': result['aliasing']['coincidences'],
        'segments': len(result.get('segments', [])),
        'diagnostics': [row['code'] for row in result.get('diagnostics', [])],
        'result_digest': result['result_digest'],
    }


def _period_ok(result):
    if not result['assessed'] or not result.get('peaks'):
        return False
    top = result['peaks'][0]
    return abs(top['frequency_hz'] - 1.0 / TRUE_PERIOD_SECONDS) <= result['frequency_range_hz']['resolution']


def run(*, seed=13, permutations=99, noise_seed=29, alias_noise_seed=41):
    """Run the three-case exercise and return numeric evidence."""
    for value, field in ((seed, 'seed'), (permutations, 'permutations'), (noise_seed, 'noise_seed'), (alias_noise_seed, 'alias_noise_seed')):
        if type(value) is not int or value < 0:
            fail('exercise_parameter', 'Exercise parameters must be non-negative integers', field=field)
    if permutations < 19:
        fail('exercise_parameter', 'At least 19 permutations are needed for the add-one estimate to reach 0.05',
             permutations=permutations)
    db = Database(':memory:')
    try:
        sample_times = _jittered_times(int(SPAN_SECONDS / CADENCE_SECONDS), CADENCE_SECONDS, 0.9, seed)
        end = max(sample_times) + 60.0
        window = {
            'start': 0.0,
            'end': end,
            'minimum_frequency_hz': 0.001,
            'maximum_frequency_hz': 0.02,
            'resolution_hz': 0.00005,
        }

        _install(
            db, 'dev-signal', 'feed-signal', 'sensor-signal', sample_times,
            _values(sample_times, signal=True, seed=seed),
            maximum_gap_seconds=int(CADENCE_SECONDS * 3),
        )
        signal = _analyze(
            db, 'dev-signal', 0.0, end,
            minimum_hz=window['minimum_frequency_hz'], maximum_hz=window['maximum_frequency_hz'],
            resolution_hz=window['resolution_hz'], seed=seed, permutations=permutations,
        )

        _install(
            db, 'dev-noise', 'feed-noise', 'sensor-noise', sample_times,
            _values(sample_times, signal=False, seed=noise_seed),
            maximum_gap_seconds=int(CADENCE_SECONDS * 3),
        )
        noise = _analyze(
            db, 'dev-noise', 0.0, end,
            minimum_hz=window['minimum_frequency_hz'], maximum_hz=window['maximum_frequency_hz'],
            resolution_hz=window['resolution_hz'], seed=seed, permutations=permutations,
        )

        burst_times = _burst_times()
        alias_end = max(burst_times) + 60.0
        alias_signal_period = 300.0
        alias_values = []
        generator = random.Random(alias_noise_seed)
        for offset in burst_times:
            alias_values.append(
                3.0 * math.cos(2.0 * math.pi * offset / alias_signal_period + 0.25)
                + generator.gauss(0.0, 0.25)
            )
        _install(
            db, 'dev-alias', 'feed-alias', 'sensor-alias', burst_times, alias_values,
            maximum_gap_seconds=700,
        )
        alias = _analyze(
            db, 'dev-alias', 0.0, alias_end,
            minimum_hz=0.001, maximum_hz=0.02, resolution_hz=0.00005,
            seed=seed, permutations=permutations, gap_seconds=700,
        )

        evidence = {
            'true_period': {
                'sampling': {'samples': len(sample_times), 'span_seconds': SPAN_SECONDS,
                             'cadence_seconds': CADENCE_SECONDS, 'expected_period_seconds': TRUE_PERIOD_SECONDS,
                             'expected_amplitude': TRUE_AMPLITUDE, 'expected_phase_radians': TRUE_PHASE},
                'result': _summary(signal),
            },
            'pure_noise_same_timestamps': {
                'sampling': {'samples': len(sample_times), 'same_timestamps_as': 'true_period'},
                'result': _summary(noise),
            },
            'sampling_window_alias': {
                'sampling': {'samples': len(burst_times), 'reporting_period_seconds': 600.0,
                             'block_seconds': 80.0, 'expected_signal_period_seconds': alias_signal_period},
                'result': _summary(alias),
            },
        }
        checks = {
            'true_period_recovered': _period_ok(signal),
            'true_period_significant': signal['significance']['false_alarm_probability'] <= 0.05,
            'noise_not_significant': (
                not noise['assessed']
                or noise['significance']['false_alarm_probability'] > 0.05
            ),
            'alias_flagged': bool(alias.get('aliasing', {}).get('coincidences')),
        }
        evidence['checks'] = checks
        evidence['parameters'] = {
            'seed': seed,
            'permutations': permutations,
            'noise_seed': noise_seed,
            'alias_noise_seed': alias_noise_seed,
            'frequency_window_hz': {
                'minimum': window['minimum_frequency_hz'],
                'maximum': window['maximum_frequency_hz'],
                'resolution': window['resolution_hz'],
            },
        }
        return {'ok': all(checks.values()), 'evidence': evidence}
    finally:
        db.close()
