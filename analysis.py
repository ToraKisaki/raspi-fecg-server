#!/usr/bin/env python3
"""
Real-time fetal/maternal heart-rate + signal-quality + alarm analyzer.

The backend already receives every (t, raw, fecg) sample from the device, so it
is the natural single source of truth for derived clinical metrics. One
``PatientAnalyzer`` per streaming patient keeps a short rolling window and, on
every incoming batch, estimates:

    * FHR  — fetal heart rate, from the AI-extracted ``fecg`` channel
    * MHR  — maternal heart rate, from the dominant QRS in the ``raw`` channel
    * SQ   — a 0-100 signal-quality index (beat-to-beat regularity + coverage)
    * alarm— "ok" | "low" (brady) | "high" (tachy) | "signal" (loss)

Detection is a dependency-light Pan-Tompkins-style pipeline (numpy only):
baseline removal -> derivative -> squaring -> moving-window integration ->
adaptive thresholding with a physiologic refractory period. Cheap enough to run
on every 20-sample batch at 250 Hz on a Raspberry Pi.

Clinical thresholds (singleton, term) follow the widely used NICHD/ACOG and
FIGO intrapartum definitions:
    * normal fetal baseline ....... 110-160 bpm
    * fetal bradycardia ........... < 110 bpm
    * fetal tachycardia ........... > 160 bpm
This is research/educational software, NOT a certified medical device.
"""

from collections import deque

import numpy as np

# ---- clinical thresholds (fetal, bpm) ----
FHR_LOW = 110        # below -> bradycardia
FHR_HIGH = 160       # above -> tachycardia
FHR_MIN_PLAUS = 90   # detector search range (allow some margin past alarm band)
FHR_MAX_PLAUS = 220
MHR_MIN_PLAUS = 45
MHR_MAX_PLAUS = 140
SQ_USABLE = 30       # signal quality below this -> "signal loss"

WINDOW_SEC = 6.0     # rolling analysis window
MIN_SEC = 2.5        # need at least this much signal before estimating


def _moving_avg(x, w):
    """Centered moving average, same length as x (w>=1)."""
    if w <= 1:
        return x
    k = np.ones(w, dtype=np.float64) / w
    return np.convolve(x, k, mode="same")


def _pick_peaks(x, thr, refractory):
    """Local maxima above ``thr``, enforcing a min spacing of ``refractory``.

    Within a refractory gap only the larger peak is kept. O(n).
    """
    peaks = []
    n = len(x)
    for i in range(1, n - 1):
        xi = x[i]
        if xi >= thr and xi >= x[i - 1] and xi >= x[i + 1]:
            if peaks and (i - peaks[-1]) < refractory:
                if xi > x[peaks[-1]]:
                    peaks[-1] = i
            else:
                peaks.append(i)
    return np.asarray(peaks, dtype=np.int64)


def estimate_bpm(sig, fs, min_bpm, max_bpm, refractory_s):
    """Estimate heart rate (bpm) from a 1-D ECG-like window.

    Returns (bpm, cv, n_beats) or None when no reliable rhythm is found.
    ``cv`` is the coefficient of variation of the accepted R-R intervals
    (lower = more regular = higher quality).
    """
    x = np.asarray(sig, dtype=np.float64)
    n = x.size
    if n < int(fs * MIN_SEC):
        return None
    if not np.all(np.isfinite(x)):
        x = np.nan_to_num(x)

    # 1) baseline removal (high-pass): subtract a ~0.2 s moving average
    x = x - _moving_avg(x, max(1, int(0.2 * fs)))
    # 2) QRS emphasis: derivative, squared
    d = np.diff(x, prepend=x[:1])
    e = d * d
    # 3) moving-window integration (~40 ms)
    integ = _moving_avg(e, max(1, int(0.04 * fs)))
    peak_ref = float(np.percentile(integ, 99))
    if peak_ref <= 0:
        return None
    # 4) adaptive threshold + refractory peak picking
    thr = 0.35 * peak_ref
    refr = max(1, int(refractory_s * fs))
    peaks = _pick_peaks(integ, thr, refr)
    if peaks.size < 3:
        return None

    rr = np.diff(peaks) / fs                      # seconds
    lo, hi = 60.0 / max_bpm, 60.0 / min_bpm       # plausible R-R window
    rr = rr[(rr >= lo) & (rr <= hi)]
    if rr.size < 2:
        return None

    bpm = 60.0 / float(np.median(rr))
    mean_rr = float(np.mean(rr))
    cv = float(np.std(rr) / mean_rr) if mean_rr > 0 else 1.0
    return bpm, cv, int(rr.size)


def _ema(prev, new, alpha=0.45):
    return new if prev is None else (alpha * new + (1 - alpha) * prev)


class PatientAnalyzer:
    """Streaming per-patient FHR/MHR/SQ/alarm estimator."""

    def __init__(self, sample_rate=250):
        self.sr_hint = float(sample_rate or 250)
        self._t = deque()
        self._raw = deque()
        self._fe = deque()
        self.fhr = None
        self.mhr = None
        self.sq = 0
        self.alarm = "signal"
        self.label = "Acquiring…"

    def _fs(self):
        n = len(self._t)
        if n >= 2:
            span = self._t[-1] - self._t[0]
            if span > 0:
                return (n - 1) / span
        return self.sr_hint

    def push(self, batch):
        """Feed a batch of [t, raw, fecg] rows; returns the current snapshot."""
        for row in batch:
            self._t.append(row[0])
            self._raw.append(row[1])
            self._fe.append(row[2])
        if self._t:
            tmax = self._t[-1]
            while self._t and (tmax - self._t[0]) > WINDOW_SEC:
                self._t.popleft()
                self._raw.popleft()
                self._fe.popleft()
        return self._compute()

    def _compute(self):
        fs = self._fs()
        fe = np.fromiter(self._fe, dtype=np.float64)
        raw = np.fromiter(self._raw, dtype=np.float64)

        f = estimate_bpm(fe, fs, FHR_MIN_PLAUS, FHR_MAX_PLAUS, 0.25)
        m = estimate_bpm(raw, fs, MHR_MIN_PLAUS, MHR_MAX_PLAUS, 0.35)

        if f:
            self.fhr = _ema(self.fhr, f[0])
            cv = f[1]
            # quality: regularity (cv) blended with beat coverage
            reg = max(0.0, 1.0 - cv / 0.5)            # cv 0 ->1, 0.5 ->0
            cover = min(1.0, f[2] / 6.0)              # ~>=6 beats -> full
            self.sq = int(round(100 * (0.7 * reg + 0.3 * cover)))
        else:
            # decay quality toward zero when fetal rhythm is lost
            self.sq = int(self.sq * 0.5)

        if m:
            self.mhr = _ema(self.mhr, m[0])

        self._set_alarm()
        return self.snapshot()

    def _set_alarm(self):
        fhr = self.fhr
        if fhr is None or self.sq < SQ_USABLE:
            self.alarm, self.label = "signal", "Signal loss"
        elif fhr < FHR_LOW:
            self.alarm, self.label = "low", "Fetal bradycardia"
        elif fhr > FHR_HIGH:
            self.alarm, self.label = "high", "Fetal tachycardia"
        else:
            self.alarm, self.label = "ok", "Normal"

    def snapshot(self):
        return {
            "fhr": int(round(self.fhr)) if self.fhr is not None else None,
            "mhr": int(round(self.mhr)) if self.mhr is not None else None,
            "sq": int(max(0, min(100, self.sq))),
            "alarm": self.alarm,
            "label": self.label,
        }

    def spark(self, n=64):
        """Down-sampled recent fECG for a dashboard sparkline (<= n points)."""
        m = len(self._fe)
        if m == 0:
            return []
        fe = np.fromiter(self._fe, dtype=np.float64)
        if m > n:
            idx = np.linspace(0, m - 1, n).astype(np.int64)
            fe = fe[idx]
        return [round(float(v), 4) for v in fe]


# --------------------------------------------------------------------------
# self-test: synthesize known maternal (75) + fetal (140) rhythms and check
#   python3 analysis.py
# --------------------------------------------------------------------------
def _synth(fs, secs, mat_hr=75.0, fet_hr=140.0, fet_amp=0.18, seed=7):
    import math
    rng = np.random.default_rng(seed)
    n = int(fs * secs)
    raw = np.zeros(n)
    fecg = np.zeros(n)

    def beat(t, hr, amp, width):
        period = 60.0 / hr
        phase = (t % period) / period
        d = (phase if phase < 0.5 else phase - 1.0) * period
        x = d / width
        return amp * (1 - x * x) * math.exp(-(x * x) * 4) if abs(x) < 1.5 else 0.0

    for i in range(n):
        t = i / fs
        mat = beat(t, mat_hr, 1.0, 0.05)
        fet = beat(t, fet_hr, fet_amp, 0.025)
        raw[i] = mat + fet + 0.05 * math.sin(2 * math.pi * 0.3 * t) + rng.normal(0, 0.02)
        fecg[i] = fet + rng.normal(0, 0.01)
    return raw, fecg


if __name__ == "__main__":
    FS = 250
    raw, fecg = _synth(FS, 6.0)
    f = estimate_bpm(fecg, FS, FHR_MIN_PLAUS, FHR_MAX_PLAUS, 0.25)
    m = estimate_bpm(raw, FS, MHR_MIN_PLAUS, MHR_MAX_PLAUS, 0.35)
    print(f"fetal  -> {f[0]:.1f} bpm (expect ~140)  cv={f[1]:.3f} beats={f[2]}")
    print(f"mother -> {m[0]:.1f} bpm (expect ~75)   cv={m[1]:.3f} beats={m[2]}")

    a = PatientAnalyzer(FS)
    rows = [[i / FS, raw[i], fecg[i]] for i in range(len(raw))]
    for j in range(0, len(rows), 20):
        snap = a.push(rows[j:j + 20])
    print("analyzer snapshot:", snap, "spark_pts:", len(a.spark()))
    assert 130 <= snap["fhr"] <= 150, snap
    assert 68 <= snap["mhr"] <= 82, snap
    assert snap["alarm"] == "ok", snap
    print("OK: analyzer self-test passed")
