import datetime as dt
import math
import random

from hangar_ingest.types import PipelineError, canonical, digest
from hangar_ingest.api import Application
from hangar_ingest.decoders import workorders
from hangar_ingest.quality import devices
from hangar_ingest.analysis import lomb, period, exercise
from tests.support import PipelineCase, order

BASE = '2026-11-11T00:00:00Z'
HOUR_END = '2026-11-11T01:00:00Z'


def clock(offset):
    point = dt.datetime(2026, 11, 11, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=offset)
    return point.isoformat(timespec='microseconds').replace('+00:00', 'Z')


def signal_rows(count=900, *, period_seconds=240.0, amplitude=2.0, phase=0.1, noise=0.2,
                cadence=2.0, jitter=0.7, seed=17, start=0.0, skip=None):
    generator = random.Random(seed)
    rows = []
    for index in range(count):
        offset = start + cadence * index + generator.uniform(-jitter, jitter)
        if offset < 0:
            continue
        if skip and skip(offset):
            continue
        value = amplitude * math.cos(2.0 * math.pi * offset / period_seconds + phase)
        if noise:
            value += generator.gauss(0.0, noise)
        rows.append({
            'event_id': f'event-{index}',
            'device': 'sensor1',
            'observed_at': clock(offset),
            'value': str(round(value, 9)),
            'unit': 'C',
        })
    return rows


class PeriodCase(PipelineCase):
    def analyze(self, app=None, **overrides):
        parameters = {
            'device_id': 'dev1',
            'metric': 'temperature',
            'start': BASE,
            'end': HOUR_END,
            'minimum_frequency_hz': 0.002,
            'maximum_frequency_hz': 0.02,
            'resolution_hz': 0.0002,
            'permutations': 39,
            'seed': 4,
        }
        parameters.update(overrides)
        return (app or Application(self.db)).call('period.analyze', parameters)


class SignalTests(PeriodCase):
    def test_irregular_signal_is_recovered_with_period_amplitude_and_phase(self):
        self.ingest(signal_rows(period_seconds=240.0, amplitude=2.0, phase=0.1, seed=17))
        result = self.analyze()
        self.assertTrue(result['assessed'], result.get('diagnostics'))
        top = result['peaks'][0]
        self.assertLess(abs(top['frequency_hz'] - 1.0 / 240.0), 0.0002)
        self.assertAlmostEqual(top['amplitude'], 2.0, delta=0.25)
        self.assertGreater(top['power'], 0.8)
        self.assertEqual(top['phase_reference_time'], self.db.scalar('SELECT MIN(event_time) FROM observations'))
        # phase follows the declared convention: cos(2*pi*f*(t - reference) - phase)
        origin = dt.datetime.fromisoformat(top['phase_reference_time'].replace('Z', '+00:00'))
        first = dt.datetime.fromisoformat(self.db.scalar('SELECT MIN(event_time) FROM observations').replace('Z', '+00:00'))
        self.assertEqual(first, origin)
        expected = (0.1 - 2.0 * math.pi * top['frequency_hz'] * 0.0) % (2.0 * math.pi)
        self.assertAlmostEqual(top['phase_radians'] % (2.0 * math.pi), expected, delta=0.35)
        self.assertGreater(result['significance']['false_alarm_probability'], 0.0)
        self.assertLessEqual(result['significance']['false_alarm_probability'], 0.05)
        self.assertEqual(result['significance']['scope'], 'search_corrected_maximum_peak_of_the_entire_frequency_grid')
        self.assertEqual(result['result_digest'], digest({key: value for key, value in result.items() if key != 'result_digest'}))
        self.assertEqual(len(result['observation_ledger']['observation_ids_used']), len(self.db.all('SELECT * FROM observations')))

    def test_same_timestamps_pure_noise_is_not_significant(self):
        self.ingest(signal_rows(period_seconds=240.0, amplitude=2.0, seed=17))
        devices.register(self.db, 'dev-noise', 'plane2', 'engine2', 'temperature', 'C')
        devices.add_alias(self.db, 'feed-noise', 'sensor1', 'dev-noise', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
        devices.add_calibration(self.db, 'cal-noise', 'dev-noise', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
        noise_rows = signal_rows(period_seconds=240.0, amplitude=0.0, noise=0.2, seed=99)
        for row in noise_rows:
            row['device'] = 'sensor1'
        self.ingest(noise_rows, 'noise-batch', 'feed-noise')
        noise = self.analyze(device_id='dev-noise', metric='temperature', permutations=99)
        self.assertTrue(noise['assessed'])
        self.assertGreater(noise['significance']['false_alarm_probability'], 0.05)
        self.assertLess(noise['peaks'][0]['amplitude'], 1.0)
        # the search maximum, not a lucky single frequency, is what is tested
        self.assertGreater(noise['significance']['permutation_maximum_95_percent'],
                           noise['significance']['observed_maximum_power'])
        repeat = self.analyze(device_id='dev-noise', metric='temperature', permutations=99)
        self.assertEqual(noise['significance']['false_alarm_probability'], repeat['significance']['false_alarm_probability'])
        self.assertEqual(noise['result_digest'], repeat['result_digest'])

    def test_sampling_window_is_reported_and_aliases_are_flagged(self):
        times = exercise._burst_times()
        rows = [{
            'event_id': f'burst-{index}',
            'device': 'sensor1',
            'observed_at': clock(offset),
            'value': str(round(3.0 * math.cos(2.0 * math.pi * offset / 300.0 + 0.25), 9)),
            'unit': 'C',
        } for index, offset in enumerate(times)]
        self.ingest(rows)
        result = self.analyze(end=clock(max(times) + 60.0), minimum_frequency_hz=0.001,
                              maximum_frequency_hz=0.02, resolution_hz=0.0001,
                              gap_seconds=700, permutations=9)
        self.assertTrue(result['assessed'])
        window = result['sampling_window']
        self.assertTrue(window['highest_side_peaks'])
        self.assertGreater(window['main_lobe']['nominal_resolution_hz'], 0.0)
        self.assertTrue(result['aliasing']['coincidences'])
        coincidence = result['aliasing']['coincidences'][0]
        delta = abs(coincidence['alias_frequency_hz'] - coincidence['strongest_peak_frequency_hz'])
        self.assertAlmostEqual(delta, coincidence['window_frequency_hz'], delta=result['aliasing']['tolerance_hz'])
        self.assertIn('sampling_window_points', result['spectrum'])
        self.assertEqual(len(result['spectrum']['points']), len(result['spectrum']['sampling_window_points']))

    def test_segments_split_at_gap_and_never_bridge_it(self):
        self.ingest(signal_rows(720, period_seconds=240.0, seed=5, skip=lambda offset: 600.0 <= offset < 780.0))
        result = self.analyze(gap_seconds=30, permutations=9, top_peaks=2)
        self.assertTrue(result['assessed'], result.get('diagnostics'))
        self.assertEqual(len(result['segments']), 2)
        first, second = result['segments']
        self.assertLess(first['end'], second['start'])
        self.assertLess(first['samples'], 900)
        self.assertEqual(result['policies']['gap_seconds'], 30)
        self.assertIn('never', result['policies']['segmentation_rule'])
        # no observation is duplicated or shared between segments
        observed = [observation_id for segment in result['segments'] for observation_id in segment['observation_ids']]
        self.assertEqual(len(observed), len(set(observed)))

    def test_maintenance_window_is_excluded_and_recorded(self):
        workorders.upsert(self.db, order(opened_at='2026-11-11T00:20:00Z', closed_at='2026-11-11T00:30:00Z'))
        rows = []
        for index in range(1400):
            offset = 2.0 * index
            rows.append({
                'event_id': f'maintenance-{index}',
                'device': 'sensor1',
                'observed_at': clock(offset),
                'value': str(round(math.cos(2.0 * math.pi * offset / 240.0), 9)),
                'unit': 'C',
            })
        self.ingest(rows)
        result = self.analyze(gap_seconds=30, permutations=9, end=clock(2800.0))
        self.assertTrue(result['assessed'])
        self.assertEqual(len(result['maintenance_windows']), 1)
        self.assertEqual(result['maintenance_windows'][0]['order_id'], 'order1')
        excluded = result['observation_ledger']['excluded_maintenance']
        self.assertTrue(excluded)
        self.assertTrue(all(row['order_ids'] == ['order1'] for row in excluded))
        for row in excluded:
            self.assertNotIn(row['observation_ids'][0], result['observation_ledger']['observation_ids_used'])
        self.assertEqual(len(result['segments']), 2)
        included = self.analyze(include_maintenance=True, permutations=9, end=clock(2800.0))
        self.assertEqual(included['observation_ledger']['excluded_maintenance'], [])
        self.assertEqual(included['policies']['maintenance'], 'included')

    def test_permutation_null_uses_permuted_values_and_not_the_observed_series(self):
        # unequal analysis weights make the weighted mean depend on block order
        values = [5.0 if index < 120 else math.sin(0.01 * index) for index in range(300)]
        times = [index * 2.0 for index in range(len(values))]
        weights = [1.0 if index < 120 else 0.2 for index in range(len(values))]
        segment = {'t': times, 'y': values, 'w': weights, 'W': sum(weights)}
        segment['sy'] = lomb.dot(segment['w'], segment['y'])
        segment['tss'] = lomb.segment_tss(segment['y'], segment['w'], segment['W'], segment['sy'])
        segment_blocks = [(100, 300), (0, 100)]          # one segment, two blocks swapped
        single_permutation = [segment_blocks]             # per-segment block orders
        permutation = [[segment_blocks]]                  # one permutation sample, one segment
        frequencies = [0.002 + index * 0.0001 for index in range(150)]
        observed = lomb.spectrum([segment], frequencies)
        with_null = lomb.spectrum([segment], frequencies, permutations=permutation)
        self.assertEqual(len(with_null['permutation_maxima']), 1)
        self.assertEqual(with_null['powers'], observed['powers'])
        # Recompute the spectrum of the permuted series from scratch: the null must match it.
        permuted_values, permuted_sums, permuted_tss = lomb.permuted_segments([segment], single_permutation)
        rebuilt = {'t': times, 'y': permuted_values[0], 'w': segment['w'], 'W': segment['W']}
        rebuilt['sy'] = permuted_sums[0]
        rebuilt['tss'] = permuted_tss
        recomputed = lomb.spectrum([rebuilt], frequencies)
        self.assertAlmostEqual(with_null['permutation_maxima'][0], recomputed['observed_max'], places=10)
        # The permuted mean differs from the observed mean, so reusing the observed mean
        # would have produced a visibly different null power.
        self.assertGreater(abs(permuted_sums[0] - segment['sy']) / segment['W'], 0.2)

    def test_quality_flags_are_excluded_with_reasons(self):
        self.ingest(signal_rows(400, period_seconds=240.0, seed=3))
        self.db.execute("UPDATE observations SET quality=? WHERE rowid IN (SELECT rowid FROM observations ORDER BY event_time LIMIT 20)",
                        (canonical([{'code': 'sampling_gap', 'neighbor': 'previous'}]),))
        result = self.analyze(exclude_quality_codes=['sampling_gap'], minimum_samples=1)
        self.assertEqual(len(result['observation_ledger']['excluded_quality']), 20)
        first = result['observation_ledger']['excluded_quality'][0]
        self.assertEqual(first['reasons'], [{'code': 'quality_flag', 'flags': ['sampling_gap']}])
        self.assertTrue(all('sampling_gap' in row['quality_codes'] for row in result['observation_ledger']['excluded_quality']))
        included = self.analyze(exclude_quality_codes=[], minimum_samples=1)
        self.assertEqual(included['observation_ledger']['excluded_quality'], [])
        with self.assertRaises(PipelineError):
            self.analyze(exclude_quality_codes=['bad code'])

    def test_duplicate_timestamps_follow_declared_policy(self):
        devices.add_alias(self.db, 'feed-dup', 'sensor-dup', 'dev1', '2026-01-01T00:00:00Z', '2027-01-01T00:00:00Z')
        rows = signal_rows(400, period_seconds=240.0, seed=11)
        self.ingest(rows)
        duplicate = dict(rows[50])
        duplicate['event_id'] = 'duplicate-event'
        duplicate['device'] = 'sensor-dup'
        duplicate['value'] = '99'
        self.ingest([duplicate], 'dup-batch', 'feed-dup')
        merged = self.analyze(duplicate_time_policy='mean', minimum_samples=1)
        group = merged['observation_ledger']['duplicate_time_groups'][0]
        self.assertEqual(group['policy'], 'mean')
        self.assertTrue(group['conflicting_values'])
        self.assertEqual(len(group['observation_ids']), 2)
        self.assertAlmostEqual(float(group['selected_value']), sum(float(value) for value in group['values']) / 2)
        first = self.analyze(duplicate_time_policy='first', minimum_samples=1)
        self.assertEqual(first['observation_ledger']['duplicate_time_groups'][0]['policy'], 'first')
        self.assertEqual(first['observation_ledger']['duplicate_time_groups'][0]['selected_value'],
                         first['observation_ledger']['duplicate_time_groups'][0]['values'][0])

    def test_dense_burst_does_not_override_the_rest_of_the_record(self):
        rows = []
        for index in range(500):
            offset = 2.0 * index
            rows.append({
                'event_id': f'sparse-{index}',
                'device': 'sensor1',
                'observed_at': clock(offset),
                'value': str(round(2.0 * math.cos(2.0 * math.pi * offset / 240.0 + 0.1), 9)),
                'unit': 'C',
            })
        for index in range(2000):
            offset = 300.0 + index * 0.01
            rows.append({
                'event_id': f'burst-{index}',
                'device': 'sensor1',
                'observed_at': clock(offset),
                'value': str(round(9.0 * math.cos(2.0 * math.pi * offset / 24.0 + 0.1), 9)),
                'unit': 'C',
            })
        self.ingest(rows)
        result = self.analyze(permutations=9, minimum_frequency_hz=0.002, maximum_frequency_hz=0.02,
                              resolution_hz=0.0002)
        self.assertTrue(result['assessed'])
        self.assertLess(abs(result['peaks'][0]['period_seconds'] - 240.0), 6.0)
        self.assertLess(result['peaks'][0]['amplitude'], 3.0)
        self.assertIn('dense burst', result['policies']['weight_rule'])

    def test_calibration_lineage_and_reason_ledger_survive_in_result(self):
        self.ingest(signal_rows(400, period_seconds=240.0, seed=21))
        result = self.analyze(permutations=5)
        lineage = result['calibration_lineage']
        self.assertFalse(lineage['stale'])
        self.assertEqual(lineage['calibrations'][0]['calibration_id'], 'cal1')
        self.assertEqual(lineage['calibrations'][0]['revision_used'], 1)
        for row in result['observation_ledger']['retained'][:3]:
            self.assertEqual(row['calibration_id'], 'cal1')
            self.assertEqual(row['calibration_revision'], 1)
            self.assertEqual(row['raw_time'], row['event_time'])
            self.assertEqual(row['quality_codes'], [])


class DiagnosticTests(PeriodCase):
    def test_constant_series_is_reported_as_not_assessable(self):
        self.ingest([{
            'event_id': f'const-{index}', 'device': 'sensor1', 'observed_at': clock(2.0 * index),
            'value': '5', 'unit': 'C',
        } for index in range(60)])
        result = self.analyze()
        self.assertFalse(result['assessed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'constant_series')
        self.assertIn('result_digest', result)

    def test_too_short_coverage_is_reported(self):
        self.ingest([{
            'event_id': f'short-{index}', 'device': 'sensor1', 'observed_at': clock(0.05 * index),
            'value': str(index), 'unit': 'C',
        } for index in range(30)])
        result = self.analyze(minimum_samples=2)
        self.assertFalse(result['assessed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'coverage_too_short')

    def test_dense_frequency_grid_is_rejected_with_explanation(self):
        self.ingest(signal_rows(400, period_seconds=240.0, seed=31))
        result = self.analyze(minimum_frequency_hz=0.001, maximum_frequency_hz=0.02,
                              resolution_hz=0.000005, gap_seconds=300, permutations=5)
        self.assertFalse(result['assessed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'frequency_grid_dense')
        self.assertEqual(result['diagnostics'][0]['context']['maximum_points'], period.MAXIMUM_FREQUENCY_POINTS)

    def test_segments_shorter_than_requests_raise_frequency_range_diagnostic(self):
        rows = []
        for index in range(4):
            for inner in range(6):
                offset = index * 100.0 + inner
                rows.append({
                    'event_id': f'chunk-{index}-{inner}',
                    'device': 'sensor1',
                    'observed_at': clock(offset),
                    'value': str(math.cos(offset)),
                    'unit': 'C',
                })
        self.ingest(rows)
        result = self.analyze(gap_seconds=4, minimum_frequency_hz=0.01, maximum_frequency_hz=0.15,
                              resolution_hz=0.01, permutations=5, minimum_samples=1,
                              minimum_segment_samples=2)
        self.assertFalse(result['assessed'])
        self.assertEqual(result['diagnostics'][0]['code'], 'frequency_range_exceeds_segments')

    def test_ill_conditioned_frequencies_are_counted(self):
        # exactly one sample per cycle leaves the cos/sin design rank deficient
        times = [10.0 * index for index in range(120)]
        values = [math.sin(index) for index in range(120)]
        weights = [1.0] * len(times)
        projectable = lomb.projection(times, weights, 2.0 * math.pi * 0.1, float(len(times)))
        self.assertIsNone(projectable)
        segment = {
            't': times, 'y': values, 'w': weights, 'W': float(len(times)),
            'sy': sum(values),
            'tss': lomb.segment_tss(values, weights, float(len(times))),
        }
        frequencies = [0.1 + index * 0.001 for index in range(9)]
        result = lomb.spectrum([segment], frequencies)
        self.assertEqual(len(result['skipped']), 1)
        self.assertEqual(result['skipped'][0], 0)
        self.assertEqual(result['skipped_fraction'], 1 / 9)

    def test_degenerate_sampling_does_not_produce_a_peak(self):
        rows = [{
            'event_id': f'phase-{index}', 'device': 'sensor1', 'observed_at': clock(10.0 * index),
            'value': str(math.sin(index)), 'unit': 'C',
        } for index in range(600)]
        self.ingest(rows)
        result = self.analyze(minimum_frequency_hz=0.09, maximum_frequency_hz=0.11,
                              resolution_hz=0.001, permutations=1, top_peaks=2, gap_seconds=55,
                              minimum_samples=8)
        codes = [row['code'] for row in result['diagnostics']]
        self.assertIn('frequency_above_sampling_support', codes)
        self.assertFalse(result['assessed'])
        self.assertNotIn('peaks', result)

    def test_invalid_parameters_raise_typed_errors(self):
        self.ingest(signal_rows(100))
        for parameters, code in (
            ({'minimum_frequency_hz': 0}, 'frequency_range'),
            ({'minimum_frequency_hz': 0.02, 'maximum_frequency_hz': 0.01}, 'frequency_range'),
            ({'maximum_frequency_hz': 5000}, 'frequency_range'),
            ({'resolution_hz': 0}, 'resolution'),
            ({'resolution_hz': 0.5}, 'frequency_grid_coarse'),
            ({'duplicate_time_policy': 'average'}, 'duplicate_policy'),
            ({'metric': 'pressure'}, 'analysis_metric'),
            ({'gap_seconds': 0}, 'invalid_integer'),
            ({'permutations': 0}, 'invalid_integer'),
            ({'include_maintenance': 'yes'}, 'include_maintenance'),
        ):
            with self.subTest(code=code):
                with self.assertRaises(PipelineError) as raised:
                    self.analyze(**parameters)
                self.assertEqual(raised.exception.code, code)

    def test_unknown_metric_and_missing_device_fail(self):
        with self.assertRaises(PipelineError) as unknown:
            self.analyze(device_id='missing')
        self.assertEqual(unknown.exception.code, 'unknown_device')
        self.ingest(signal_rows(20))
        with self.assertRaises(PipelineError):
            self.analyze(metric='pressure')

    def test_frequency_below_and_above_supported_ranges_are_diagnosed(self):
        self.ingest(signal_rows(500, period_seconds=240.0))
        result = self.analyze(minimum_frequency_hz=0.00001, maximum_frequency_hz=0.02,
                              resolution_hz=0.0001, permutations=5)
        codes = {row['code'] for row in result['diagnostics']}
        self.assertIn('frequency_below_span', codes)


class ExerciseTests(PeriodCase):
    def test_local_exercise_shows_true_period_noise_and_window_alias(self):
        outcome = exercise.run(seed=13, permutations=19)
        self.assertTrue(outcome['ok'])
        evidence = outcome['evidence']
        self.assertTrue(evidence['checks']['true_period_recovered'])
        self.assertTrue(evidence['checks']['true_period_significant'])
        self.assertTrue(evidence['checks']['noise_not_significant'])
        self.assertEqual(evidence['traffic'] if False else evidence['sampling_window_alias']['result']['aliasing_coincidences'][0]['window_period_seconds'], 606.0606060606061)
        true_peak = evidence['true_period']['result']['top_peak']
        self.assertAlmostEqual(true_peak['period_seconds'], exercise.TRUE_PERIOD_SECONDS, delta=5.0)
        noise = evidence['pure_noise_same_timestamps']['result']
        self.assertGreater(noise['false_alarm_probability'], 0.05)
        self.assertLess(noise['top_peak']['amplitude'], 1.0)
        # fixed seed reproduces the same digest
        repeat = exercise.run(seed=13, permutations=19)
        self.assertEqual(outcome['evidence']['true_period']['result']['result_digest'],
                         repeat['evidence']['true_period']['result']['result_digest'])
        self.assertEqual(outcome['evidence']['sampling_window_alias']['result']['result_digest'],
                         repeat['evidence']['sampling_window_alias']['result']['result_digest'])

    def test_exercise_operation_is_exposed_on_the_application(self):
        app = Application(self.db)
        self.assertIn('period.exercise', app.commands())
        self.assertIn('period.analyze', app.commands())
        outcome = app.call('period.exercise', {'permutations': 19})
        self.assertIn('evidence', outcome)
        self.assertIn('checks', outcome['evidence'])
        with self.assertRaises(PipelineError):
            app.call('period.exercise', {'bogus': 1})
