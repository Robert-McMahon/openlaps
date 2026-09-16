"""The P7.6 monitor kinds: drift, ratio, counter and the whole-car model.

Every kind has the envelope's interface -- ``evaluate(at, readings, valid)``
returning the one-second row, ``snapshot()`` / ``restore()`` for the
checkpoint in ``watch_baselines``, ``freeze()`` when its baseline is
learned -- and every finding summary carries the same three fields,
``expected``, ``observed`` and ``baseline``, so the findings table needs no
per-kind rendering. A kind that reasons per lap also has ``on_lap``, which
the engine calls for every ``lap_completed`` event.

Scores are in ``[0, 1]`` in the database. The envelope's is a fraction of
recent samples out of band; the drift's is its CUSUM as a fraction of the
decision threshold; the counter's is its rate as a fraction of the limit;
the whole-car's is its smoothed distance as a fraction of the threshold in
sigma. A score of 1.0 therefore means "at or beyond the threshold" for
every kind, and ``residual`` carries the kind's own unit of evidence.
"""

from __future__ import annotations

import math
from collections import deque
from statistics import median, pstdev
from typing import Any
from uuid import uuid4

import numpy as np

from pit.watch.config import (
    CounterConfig,
    DriftConfig,
    RatioConfig,
    When,
    WholeCarConfig,
)

Reading = Any  # pit.watch.engine.Reading; imported lazily to avoid a cycle

_MAX_LAP_SERIES = 200
_MAX_COUNTER_HISTORY = 3600


def _row(at: float, status: str = "gated") -> dict:
    return dict(
        time=at,
        score=None,
        residual=None,
        expected=None,
        observed=None,
        baseline_status=status,
        finding=None,
    )


def _holds(when: list[When], readings: dict[str, Reading]) -> bool:
    return all(w.holds(readings[w.channel].value) for w in when)


class _Findings:
    """Open, update and close one finding at a time; shared by every kind."""

    def __init__(self) -> None:
        self.finding: dict | None = None

    def _transition(self, at: float, open_now: bool, score: float, summary: dict, severity: str):
        """Return the row's ``finding`` value for this tick, if any."""
        if open_now:
            if self.finding is None:
                self.finding = dict(
                    finding_id=str(uuid4()),
                    opened_at=at,
                    closed_at=None,
                    severity=severity,
                    peak_score=score,
                    summary=summary,
                )
            if score >= self.finding["peak_score"]:
                self.finding.update(peak_score=score, summary=summary)
            return dict(self.finding)
        if self.finding is not None:
            closed = dict(self.finding, closed_at=at)
            self.finding = None
            return closed
        return None


# --- drift ------------------------------------------------------------------------------


class Drift(_Findings):
    """Per-lap mean under a condition, CUSUM-ed against the baseline laps."""

    def __init__(self, config: DriftConfig) -> None:
        super().__init__()
        self.config = config
        self.frozen = False
        self.learned = 0  # baseline laps banked
        self.units: dict[str, str] = {}
        self.source_session: str | None = None
        self.baseline_laps: list[dict] = []
        self.mean: float | None = None
        self.scale: float | None = None
        self.laps: list[dict] = []
        self.cusum_up = 0.0
        self.cusum_down = 0.0
        self.score = 0.0
        self.calm = 0  # consecutive laps within a sigma, for the close
        self._sum = 0.0
        self._n = 0
        self._dirty = False
        self._pending: dict | None = None

    @property
    def channels(self) -> list[str]:
        return self.config.channels

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "kind": "drift",
            "config": self.config.model_dump(mode="json"),
            "learned": self.learned,
            "frozen": self.frozen,
            "units": self.units,
            "source_session": self.source_session,
            "baseline_laps": self.baseline_laps,
            "mean": self.mean,
            "scale": self.scale,
            "laps": self.laps[-_MAX_LAP_SERIES:],
            "cusum_up": self.cusum_up,
            "cusum_down": self.cusum_down,
            "score": self.score,
            "calm": self.calm,
            "finding": self.finding,
            "lap_sum": self._sum,
            "lap_n": self._n,
            "lap_dirty": self._dirty,
        }

    def restore(self, model: dict) -> None:
        if model.get("version") != 1 or model["config"] != self.config.model_dump(mode="json"):
            raise ValueError("baseline configuration changed; fit a matching baseline")
        self.source_session = model.get("source_session")
        for key in (
            "learned",
            "frozen",
            "units",
            "baseline_laps",
            "mean",
            "scale",
            "laps",
            "cusum_up",
            "cusum_down",
            "score",
            "calm",
            "finding",
        ):
            setattr(self, key, model[key])
        self._sum, self._n, self._dirty = model["lap_sum"], model["lap_n"], model["lap_dirty"]

    def freeze(self) -> None:
        means = [lap["mean"] for lap in self.baseline_laps]
        self.mean = sum(means) / len(means)
        self.scale = max(self.config.min_scale, pstdev(means))
        self.frozen = True

    def on_pit(self) -> None:
        """The car is in the pits: the current lap is an in-lap or out-lap."""
        self._dirty = True

    def on_lap(self, event: dict, at: float) -> None:
        """A ``lap_completed`` event: bank the lap's mean, or judge it."""
        n, total, dirty = self._n, self._sum, self._dirty
        self._sum, self._n, self._dirty = 0.0, 0, False
        clean = bool(event.get("valid", True)) and not dirty and n >= self.config.min_lap_samples
        lap_number = event.get("lap_number")
        if not clean:
            return
        lap_mean = total / n
        if not self.frozen:
            self.baseline_laps.append(dict(lap_number=lap_number, at=at, mean=lap_mean, n=n))
            self.learned = len(self.baseline_laps)
            if self.learned >= self.config.baseline_laps:
                self.freeze()
            return
        assert self.mean is not None and self.scale is not None
        residual = (lap_mean - self.mean) / self.scale
        k, h = self.config.cusum_k, self.config.cusum_h
        if self.config.direction in ("both", "up"):
            self.cusum_up = max(0.0, self.cusum_up + residual - k)
        if self.config.direction in ("both", "down"):
            self.cusum_down = max(0.0, self.cusum_down - residual - k)
        statistic = max(self.cusum_up, self.cusum_down)
        self.calm = self.calm + 1 if abs(residual) <= 1.0 else 0
        if self.finding is not None and self.calm >= 3:
            # Three laps back inside a sigma: the drift has stopped or been
            # fixed. Start the accumulation again rather than waiting for
            # the sum to bleed off at k per lap.
            self.cusum_up = self.cusum_down = statistic = 0.0
        self.score = min(1.0, statistic / h)
        self.laps.append(
            dict(
                lap_number=lap_number,
                at=at,
                mean=lap_mean,
                n=n,
                residual=residual,
                cusum=statistic,
            )
        )
        del self.laps[:-_MAX_LAP_SERIES]
        direction = "up" if self.cusum_up >= self.cusum_down else "down"
        summary = {
            "kind": "drift",
            "target": self.config.target,
            "expected": self.mean,
            "observed": lap_mean,
            "unit": self.units.get(self.config.target, ""),
            "baseline": self.config.baseline,
            "baseline_session": self.source_session,
            "baseline_laps": len(self.baseline_laps),
            "baseline_scale": self.scale,
            "residual": residual,
            "cusum": statistic,
            "cusum_h": h,
            "direction": direction,
            "lap_number": lap_number,
            "when": [w.model_dump(exclude_none=True) for w in self.config.when],
            "series": self.laps[-30:],
            "message": (
                f"{self.config.target} per lap {direction} from "
                f"{self.mean:.3g} to {lap_mean:.3g} {self.units.get(self.config.target, '')} "
                f"over {len(self.laps)} lap(s); CUSUM {statistic:.1f} of {h}"
            ),
        }
        self._pending = self._transition(
            at, statistic >= h, self.score, summary, self.config.severity
        )

    def evaluate(self, at: float, readings: dict[str, Reading], valid: bool) -> dict:
        result = _row(at)
        # A finding that opened or closed at the lap boundary is reported on
        # the next row whether or not this second is gated.
        if self._pending is not None:
            result["finding"], self._pending = self._pending, None
        if not valid:
            return result
        units = {c: readings[c].unit for c in self.channels}
        if self.units and units != self.units:
            result["baseline_status"] = "unit_mismatch"
            return result
        self.units = units
        observed = readings[self.config.target].value
        if _holds(self.config.when, readings):
            self._sum += observed
            self._n += 1
        result["observed"] = observed
        if not self.frozen:
            result["baseline_status"] = "learning"
            return result
        last = self.laps[-1] if self.laps else None
        result.update(
            score=self.score,
            residual=None if last is None else last["residual"],
            expected=self.mean,
            baseline_status="ready",
        )
        return result


# --- ratio --------------------------------------------------------------------------------


class Ratio(_Findings):
    """A physical relationship that should be a constant, per group."""

    def __init__(self, config: RatioConfig) -> None:
        super().__init__()
        self.config = config
        self.learned = 0
        self.frozen = False
        self.groups: dict[str, list[float]] = {}
        self.table: dict[str, dict] = {}
        self.units: dict[str, str] = {}
        self.score = 0.0
        self.source_session: str | None = None

    @property
    def channels(self) -> list[str]:
        return self.config.channels

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "kind": "ratio",
            "config": self.config.model_dump(mode="json"),
            "learned": self.learned,
            "frozen": self.frozen,
            "groups": self.groups,
            "table": self.table,
            "units": self.units,
            "score": self.score,
            "finding": self.finding,
            "source_session": self.source_session,
        }

    def restore(self, model: dict) -> None:
        if model.get("version") != 1 or model["config"] != self.config.model_dump(mode="json"):
            raise ValueError("baseline configuration changed; fit a matching baseline")
        self.source_session = model.get("source_session")
        for key in ("learned", "frozen", "groups", "table", "units", "score", "finding"):
            setattr(self, key, model[key])

    def freeze(self) -> None:
        for key, values in self.groups.items():
            center = median(values)
            self.table[key] = {
                "median": center,
                "mad": median(abs(v - center) for v in values),
                "count": len(values),
            }
        self.groups = {}
        self.frozen = True

    def _ratio(self, readings: dict[str, Reading]) -> float | None:
        denominators = [readings[c].value for c in self.config.denominators]
        denominator = sum(denominators) / len(denominators)
        if denominator <= 0:
            return None
        return readings[self.config.numerator].value / denominator

    def evaluate(self, at: float, readings: dict[str, Reading], valid: bool) -> dict:
        cfg = self.config
        result = _row(at)
        if not valid or not _holds(cfg.when, readings):
            return result
        units = {c: readings[c].unit for c in self.channels}
        if self.units and units != self.units:
            result["baseline_status"] = "unit_mismatch"
            return result
        self.units = units
        observed = self._ratio(readings)
        if observed is None:
            return result
        key = str(int(readings[cfg.per].value)) if cfg.per else "all"
        result["observed"] = observed
        if not self.frozen:
            self.groups.setdefault(key, []).append(observed)
            self.learned += 1
            if self.learned >= cfg.baseline_seconds:
                self.freeze()
            result["baseline_status"] = "learning"
            return result
        stats = self.table.get(key)
        if stats is None or stats["count"] < cfg.min_group_samples:
            result["baseline_status"] = "insufficient_group"
            return result
        expected = stats["median"]
        scale = max(cfg.min_scale, 1.4826 * stats["mad"])
        residual = (observed - expected) / scale
        outside = float(abs(residual) > cfg.residual_sigma)
        self.score += -math.expm1(-1 / cfg.score_window) * (outside - self.score)
        direction = "low" if observed < expected else "high"
        meaning = cfg.low_means if direction == "low" else cfg.high_means
        group = {cfg.per: int(readings[cfg.per].value)} if cfg.per else {}
        summary = {
            "kind": "ratio",
            "target": cfg.numerator,
            "numerator": cfg.numerator,
            "denominator": cfg.denominators,
            "expected": expected,
            "observed": observed,
            "unit": "",
            "baseline": cfg.baseline,
            "baseline_session": self.source_session,
            "baseline_samples": stats["count"],
            "median": expected,
            "mad": stats["mad"],
            "residual": residual,
            "direction": direction,
            "meaning": meaning,
            "group": group,
            "numerator_value": readings[cfg.numerator].value,
            "denominator_values": {c: readings[c].value for c in cfg.denominators},
            "value_unit": units[cfg.numerator],
            "message": (
                f"{cfg.numerator} / {' + '.join(cfg.denominators)}"
                f"{' in ' + ', '.join(f'{k} {v}' for k, v in group.items()) if group else ''}"
                f" is {observed:.4g}, expected {expected:.4g} ({direction}"
                f"{': ' + meaning if meaning else ''})"
            ),
        }
        result["finding"] = self._transition(
            at, self.score > cfg.open_finding_above, self.score, summary, cfg.severity
        )
        result.update(
            score=self.score, residual=residual, expected=expected, baseline_status="ready"
        )
        return result


# --- counter -----------------------------------------------------------------------------


class Counter(_Findings):
    """A monotonic count whose rate should be zero; the finding is the rate."""

    def __init__(self, config: CounterConfig) -> None:
        super().__init__()
        self.config = config
        self.frozen = True  # nothing to learn: the expectation is "unchanged"
        self.learned = 0
        self.units: dict[str, str] = {}
        self.source_session: str | None = None
        self.history: deque[tuple[float, float]] = deque(maxlen=_MAX_COUNTER_HISTORY)
        self.score = 0.0

    @property
    def channels(self) -> list[str]:
        return self.config.channels

    def snapshot(self) -> dict:
        return {
            "version": 1,
            "kind": "counter",
            "config": self.config.model_dump(mode="json"),
            "learned": self.learned,
            "frozen": True,
            "units": self.units,
            "history": list(self.history),
            "score": self.score,
            "finding": self.finding,
            "source_session": self.source_session,
        }

    def restore(self, model: dict) -> None:
        if model.get("version") != 1 or model["config"] != self.config.model_dump(mode="json"):
            raise ValueError("baseline configuration changed; fit a matching baseline")
        self.source_session = model.get("source_session")
        self.units = model["units"]
        self.history = deque(
            (tuple(entry) for entry in model["history"]), maxlen=_MAX_COUNTER_HISTORY
        )
        self.score, self.finding = model["score"], model["finding"]

    def freeze(self) -> None:
        self.frozen = True

    def evaluate(self, at: float, readings: dict[str, Reading], valid: bool) -> dict:
        cfg = self.config
        result = _row(at)
        if not valid or not _holds(cfg.when, readings):
            return result
        units = {c: readings[c].unit for c in self.channels}
        self.units = units
        observed = readings[cfg.channel].value
        # Drop everything older than the window, then measure across it.
        self.history.append((at, observed))
        while self.history and at - self.history[0][0] > cfg.score_window:
            self.history.popleft()
        first_at, first_value = self.history[0]
        span = at - first_at
        if span < min(cfg.score_window, 10.0):
            result.update(observed=observed, expected=first_value, baseline_status="warming")
            return result
        rise = max(0.0, observed - first_value)
        rate_per_min = rise * 60.0 / span
        self.score = min(1.0, rate_per_min / cfg.open_finding_above)
        summary = {
            "kind": "counter",
            "target": cfg.channel,
            "expected": first_value,
            "observed": observed,
            "unit": units[cfg.channel],
            "baseline": "unchanged",
            "baseline_session": self.source_session,
            "residual": rise,
            "rate_per_min": rate_per_min,
            "limit_per_min": cfg.open_finding_above,
            "window_s": span,
            "message": (
                f"{cfg.channel} rose by {rise:g} in {span:.0f} s "
                f"({rate_per_min:.2f}/min; limit {cfg.open_finding_above:g}/min)"
            ),
        }
        result["finding"] = self._transition(
            at, rate_per_min >= cfg.open_finding_above, self.score, summary, cfg.severity
        )
        result.update(
            score=self.score,
            residual=rise,
            expected=first_value,
            observed=observed,
            baseline_status="ready",
        )
        return result


# --- whole car -------------------------------------------------------------------------


class WholeCar(_Findings):
    """Every channel predicted from all the others by ridge regression.

    Learning keeps only sufficient statistics -- the count, the sums and the
    second-moment matrix -- so the per-tick checkpoint is a p x p table,
    not the baseline window. At the freeze, each channel's coefficients
    come from the correlation matrix, the residual covariance follows
    analytically, and its inverse gives the Mahalanobis distance. The
    linear form of an autoencoder, with the explanation built in.
    """

    _SD_FLOOR_FRACTION = 0.01
    _RESIDUAL_VARIANCE_FLOOR = 0.01
    _RIDGE = 0.1

    def __init__(self, config: WholeCarConfig) -> None:
        super().__init__()
        self.config = config
        self.frozen = False
        self.learned = 0
        self.units: dict[str, str] = {}
        self.source_session: str | None = None
        self.source_stint: int | None = None
        p = len(config.channels)
        self._sum = np.zeros(p)
        self._sum_sq = np.zeros((p, p))
        self.mean: np.ndarray | None = None
        self.sd: np.ndarray | None = None
        self.coef: np.ndarray | None = None
        self.residual_sd: np.ndarray | None = None
        self.cov_inv: np.ndarray | None = None
        self.score = 0.0  # smoothed distance in sigma
        self.baseline_seconds = int(config.baseline_minutes * 60)

    @property
    def channels(self) -> list[str]:
        return list(self.config.channels)

    def snapshot(self) -> dict:
        def rows(array: np.ndarray | None):
            return None if array is None else array.tolist()

        return {
            "version": 1,
            "kind": "whole_car",
            "config": self.config.model_dump(mode="json"),
            "learned": self.learned,
            "frozen": self.frozen,
            "units": self.units,
            "source_session": self.source_session,
            "source_stint": self.source_stint,
            "sum": self._sum.tolist(),
            "sum_sq": self._sum_sq.tolist(),
            "mean": rows(self.mean),
            "sd": rows(self.sd),
            "coef": rows(self.coef),
            "residual_sd": rows(self.residual_sd),
            "cov_inv": rows(self.cov_inv),
            "score": self.score,
            "finding": self.finding,
        }

    def restore(self, model: dict) -> None:
        if model.get("version") != 1 or model["config"] != self.config.model_dump(mode="json"):
            raise ValueError("baseline configuration changed; fit a matching baseline")

        def array(value):
            return None if value is None else np.asarray(value, dtype=float)

        self.source_session = model.get("source_session")
        self.source_stint = model.get("source_stint")
        self.learned, self.frozen, self.units = model["learned"], model["frozen"], model["units"]
        self._sum, self._sum_sq = array(model["sum"]), array(model["sum_sq"])
        self.mean, self.sd = array(model["mean"]), array(model["sd"])
        self.coef, self.residual_sd = array(model["coef"]), array(model["residual_sd"])
        self.cov_inv = array(model["cov_inv"])
        self.score, self.finding = model["score"], model["finding"]

    def freeze(self) -> None:
        n = self.learned
        p = len(self.config.channels)
        mean = self._sum / n
        cov = self._sum_sq / n - np.outer(mean, mean)
        sd = np.sqrt(np.maximum(np.diag(cov), 0.0))
        sd = np.maximum(sd, np.maximum(self._SD_FLOOR_FRACTION * np.abs(mean), 1e-6))
        corr = cov / np.outer(sd, sd)
        corr = np.clip(np.nan_to_num(corr), -1.0, 1.0)
        np.fill_diagonal(corr, 1.0)
        coef = np.zeros((p, p))
        for j in range(p):
            others = [i for i in range(p) if i != j]
            gram = corr[np.ix_(others, others)] + self._RIDGE * np.eye(p - 1)
            coef[j, others] = np.linalg.solve(gram, corr[others, j])
        residual_map = np.eye(p) - coef
        residual_cov = residual_map @ corr @ residual_map.T
        residual_cov = residual_cov + self._RESIDUAL_VARIANCE_FLOOR * np.eye(p)
        self.mean, self.sd, self.coef = mean, sd, coef
        self.residual_sd = np.sqrt(np.maximum(np.diag(residual_cov), 0.0))
        self.cov_inv = np.linalg.pinv(residual_cov)
        self.frozen = True

    def evaluate(self, at: float, readings: dict[str, Reading], valid: bool) -> dict:
        cfg = self.config
        result = _row(at)
        if not valid:
            return result
        units = {c: readings[c].unit for c in self.channels}
        if self.units and units != self.units:
            result["baseline_status"] = "unit_mismatch"
            return result
        self.units = units
        x = np.array([readings[c].value for c in self.channels], dtype=float)
        if not self.frozen:
            self._sum += x
            self._sum_sq += np.outer(x, x)
            self.learned += 1
            if self.learned >= self.baseline_seconds:
                self.freeze()
            result["baseline_status"] = "learning"
            return result
        assert self.mean is not None and self.sd is not None and self.coef is not None
        assert self.residual_sd is not None and self.cov_inv is not None
        z = (x - self.mean) / self.sd
        predicted = self.coef @ z
        residual = z - predicted
        p = len(z)
        distance = math.sqrt(max(0.0, float(residual @ self.cov_inv @ residual)) / p)
        self.score += -math.expm1(-1 / cfg.score_window) * (distance - self.score)
        per_channel = residual / self.residual_sd
        order = np.argsort(-np.abs(per_channel))
        expected_units = self.mean + self.sd * predicted
        ranked = []
        for index in order:
            channel = self.channels[index]
            expected, observed = float(expected_units[index]), float(x[index])
            predictors = [
                self.channels[k]
                for k in np.argsort(-np.abs(self.coef[index]))[:3]
                if abs(self.coef[index, k]) > 0.05
            ]
            percent = None if expected == 0 else 100.0 * (observed - expected) / abs(expected)
            ranked.append(
                dict(
                    channel=channel,
                    expected=expected,
                    observed=observed,
                    unit=units[channel],
                    residual_sigma=float(per_channel[index]),
                    direction="above" if observed > expected else "below",
                    percent=percent,
                    predicted_from=predictors,
                )
            )
        top = ranked[0]
        summary = {
            "kind": "whole_car",
            "target": top["channel"],
            "expected": top["expected"],
            "observed": top["observed"],
            "unit": top["unit"],
            "baseline": cfg.baseline,
            "baseline_session": self.source_session,
            "baseline_stint": self.source_stint,
            "baseline_samples": self.learned,
            "score_sigma": self.score,
            "threshold_sigma": cfg.open_finding_above,
            "distance_sigma": distance,
            "channels": ranked,
            "message": "; ".join(_describe(entry) for entry in ranked[:3]),
        }
        result["finding"] = self._transition(
            at,
            self.score > cfg.open_finding_above,
            min(1.0, self.score / cfg.open_finding_above),
            summary,
            cfg.severity,
        )
        result.update(
            score=min(1.0, self.score / cfg.open_finding_above),
            residual=distance,
            expected=top["expected"],
            observed=top["observed"],
            baseline_status="ready",
        )
        return result


def _describe(entry: dict) -> str:
    amount = (
        f"{abs(entry['percent']):.0f} %"
        if entry["percent"] is not None
        else f"{abs(entry['observed'] - entry['expected']):.3g} {entry['unit']}"
    )
    source = ", ".join(entry["predicted_from"]) or "the others"
    return f"{entry['channel']} {amount} {entry['direction']} expected from {source}"
