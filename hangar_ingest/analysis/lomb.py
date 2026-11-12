"""Generalized Lomb-Scargle primitives for irregularly sampled observations.

The implementation uses a per-segment floating mean: every contiguous time
segment owns its own constant term, while one common amplitude and phase is
shared by all segments.  Frequencies are ordinary (temporal) frequencies in
Hz, so the angular frequency used internally is ``2*pi*f``.
"""

import math
import operator
import random
from bisect import bisect_right

TOLERANCE = 1e-12


def dot(left, right):
    return sum(map(operator.mul, left, right))


def invert2(h00, h01, h11):
    """Return the inverse Gram entries, or ``None`` when ill-conditioned."""
    determinant = h00 * h11 - h01 * h01
    scale = h00 * h11
    if scale <= 0.0 or determinant <= TOLERANCE * scale:
        return None
    return h11 / determinant, -h01 / determinant, h00 / determinant


def projection(times, weights, omega, weight_sum):
    """Project cos/sin onto the weighted per-segment floating mean.

    Returns weighted cos/sin arrays for a later dot product, the raw weighted
    cos/sin sums (for the sampling spectral window), the reduced 2x2 Gram
    matrix and its inverse.
    """
    count = len(times)
    weighted_cosines = [0.0] * count
    weighted_sines = [0.0] * count
    cc = ss = cs = pc = ps = 0.0
    for index in range(count):
        point = times[index]
        cosine = math.cos(omega * point)
        sine = math.sin(omega * point)
        weight = weights[index]
        weighted_cosines[index] = weight * cosine
        weighted_sines[index] = weight * sine
        cc += weight * cosine * cosine
        ss += weight * sine * sine
        cs += weight * cosine * sine
        pc += weight * cosine
        ps += weight * sine
    if weight_sum <= 0.0:
        return None
    h00 = cc - pc * pc / weight_sum
    h01 = cs - pc * ps / weight_sum
    h11 = ss - ps * ps / weight_sum
    inverse = invert2(h00, h01, h11)
    if inverse is None:
        return None
    return {
        'h': (h00, h01, h11),
        'inverse': inverse,
        'weighted_cosines': weighted_cosines,
        'weighted_sines': weighted_sines,
        'cosine_sum': pc,
        'sine_sum': ps,
    }


def fit(segments, frequency):
    """Fit one common cos/sin pair with per-segment offsets at ``frequency``.

    Returns ``(inverse, parts, r0, r1)`` after eliminating every segment level,
    or ``None`` if no segment can be projected.  ``r0``/``r1`` are the reduced
    right-hand sides (weighted cos/sin sums with the floating mean removed).
    """
    omega = 2.0 * math.pi * frequency
    gram = None
    parts = []
    for segment in segments:
        part = projection(segment['t'], segment['w'], omega, segment['W'])
        if part is None:
            return None
        parts.append(part)
        h00, h01, h11 = part['h']
        if gram is None:
            gram = [h00, h01, h11]
        else:
            gram[0] += h00
            gram[1] += h01
            gram[2] += h11
    inverse = invert2(*gram)
    if inverse is None:
        return None
    r0 = r1 = 0.0
    for part, segment in zip(parts, segments):
        p0, p1 = reduced_rhs(part, segment['y'], segment['sy'], segment['W'])
        r0 += p0
        r1 += p1
    return inverse, parts, r0, r1


def explained(inverse, r0, r1):
    i00, i01, i11 = inverse
    return i00 * r0 * r0 + 2.0 * i01 * r0 * r1 + i11 * r1 * r1


def reduced_rhs(part, values, sy, weight_sum):
    """Reduced right-hand sides with the segment's floating mean removed."""
    return (
        dot(part['weighted_cosines'], values) - part['cosine_sum'] * sy / weight_sum,
        dot(part['weighted_sines'], values) - part['sine_sum'] * sy / weight_sum,
    )


def segment_tss(values, weights, weight_sum, sy=None):
    sy = dot(weights, values) if sy is None else sy
    syy = dot(weights, [value * value for value in values])
    return syy - sy * sy / weight_sum


def assemble(values, ranges):
    ordered = []
    for start, stop in ranges:
        ordered.extend(values[start:stop])
    return ordered


def block_ranges(times, block_seconds, default_blocks, limit=200):
    """Partition one segment into at least two consecutive time blocks."""
    count = len(times)
    if count < 2:
        return []
    duration = times[-1] - times[0]
    if block_seconds is None or block_seconds <= 0:
        desired = default_blocks
    else:
        desired = int(math.ceil(duration / block_seconds))
    blocks = max(2, min(desired, count // 2, limit))
    cuts = [0]
    for index in range(1, blocks):
        boundary = times[0] + duration * index / blocks
        cut = bisect_right(times, boundary)
        if cut > cuts[-1] and cut < count:
            cuts.append(cut)
    cuts.append(count)
    return [(cuts[index], cuts[index + 1]) for index in range(len(cuts) - 1)]


def permutation_ranges(segments, block_seconds, default_blocks, permutations, seed):
    """Block permutations of values within each segment, never across gaps."""
    generator = random.Random(seed)
    result = []
    for _ in range(permutations):
        per_segment = []
        for segment in segments:
            pieces = block_ranges(segment['t'], block_seconds, default_blocks)
            order = list(pieces)
            generator.shuffle(order)
            per_segment.append(order)
        result.append(per_segment)
    return result


def permuted_segments(segments, permutation):
    """Materialize permuted values, their weighted sums and reduced variance."""
    values = []
    weighted_sums = []
    tss = 0.0
    for segment, ranges in zip(segments, permutation):
        ordered = assemble(segment['y'], ranges)
        sy = dot(segment['w'], ordered)
        values.append(ordered)
        weighted_sums.append(sy)
        tss += segment_tss(ordered, segment['w'], segment['W'], sy)
    return values, weighted_sums, tss


def spectrum(segments, frequencies, *, permutations=None):
    """Evaluate the normalized GLS power and sampling window on a grid.

    Powers are the fraction of weighted variance explained, in ``[0, 1]``.
    ``permutations`` is the output of :func:`permutation_ranges`; the maximum
    power over the whole grid is recorded for every permutation so that a
    search-corrected false-alarm probability can be derived later.
    """
    observed_tss = sum(segment['tss'] for segment in segments)
    weight_total = sum(segment['W'] for segment in segments)
    powers = [0.0] * len(frequencies)
    window_powers = [0.0] * len(frequencies)
    skipped = []
    observed_max = 0.0
    observed_max_index = None
    applied = permutations or []
    permutation_maxima = [0.0] * len(applied)
    materialized = [permuted_segments(segments, ranges) for ranges in applied]

    for index, frequency in enumerate(frequencies):
        fitted = fit(segments, frequency)
        if fitted is None or observed_tss <= 0.0:
            skipped.append(index)
            continue
        inverse, parts, r0, r1 = fitted
        power = min(1.0, max(0.0, explained(inverse, r0, r1) / observed_tss))
        powers[index] = power
        if power > observed_max:
            observed_max = power
            observed_max_index = index
        if weight_total > 0.0:
            cosine_sum = sum(part['cosine_sum'] for part in parts)
            sine_sum = sum(part['sine_sum'] for part in parts)
            window = (cosine_sum * cosine_sum + sine_sum * sine_sum) / (weight_total * weight_total)
            window_powers[index] = min(1.0, max(0.0, window))
        for order, (values, weighted_sums, tss) in enumerate(materialized):
            if tss <= 0.0:
                continue
            q0 = q1 = 0.0
            for part, segment, permuted, sy in zip(parts, segments, values, weighted_sums):
                p0, p1 = reduced_rhs(part, permuted, sy, segment['W'])
                q0 += p0
                q1 += p1
            value = min(1.0, max(0.0, explained(inverse, q0, q1) / tss))
            if value > permutation_maxima[order]:
                permutation_maxima[order] = value

    return {
        'powers': powers,
        'window_powers': window_powers,
        'skipped': skipped,
        'observed_max': observed_max,
        'observed_max_index': observed_max_index,
        'permutation_maxima': permutation_maxima,
        'skipped_fraction': len(skipped) / len(frequencies) if frequencies else 0.0,
    }


def local_maxima(values):
    result = []
    for index, value in enumerate(values):
        left = values[index - 1] if index > 0 else -1.0
        right = values[index + 1] if index + 1 < len(values) else -1.0
        if value > left and value >= right:
            result.append(index)
    return result


def coefficients(segments, frequency):
    """Fit the common amplitude/phase pair and per-segment offsets."""
    fitted = fit(segments, frequency)
    if fitted is None:
        return None
    inverse, parts, r0, r1 = fitted
    i00, i01, i11 = inverse
    cosine = i00 * r0 + i01 * r1
    sine = i01 * r0 + i11 * r1
    levels = []
    for part, segment in zip(parts, segments):
        weighted_mean = dot(segment['w'], segment['y']) / segment['W']
        levels.append(
            weighted_mean
            - cosine * part['cosine_sum'] / segment['W']
            - sine * part['sine_sum'] / segment['W']
        )
    return {'cosine': cosine, 'sine': sine, 'levels': levels, 'inverse': inverse}
