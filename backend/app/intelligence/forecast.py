"""Demand forecasting and stockout-risk estimation (contract §7.1, brief §7).

Method choice
-------------
:class:`DemandForecaster` uses **Holt's linear (damped-trend) exponential
smoothing**, reported as ``method="holt_winters"``. Holt's linear method is the
trend-bearing member of the Holt-Winters family; the seasonal member is
deliberately *not* estimated.

Why damped Holt rather than a seasonal model
--------------------------------------------
Demand arrives as a stream of ``DemandPoint(station_id, fuel_type, tick,
liters)``. This forecaster is a pure function of that sequence, so it has no
calendar, no ``tick_minutes`` and no way to know how many ticks make a day. A
seasonal period would therefore have to be guessed, and a guessed period fitted
to a 12-40 point series produces confident-looking nonsense. Holt's linear
method needs only a level and a slope, both estimable from a dozen points, and
the damping factor ``phi = 0.9`` stops a short-lived ramp from being
extrapolated into an unbounded spike. The prediction interval widens with the
square root of the damped horizon, so an 8-tick-ahead number is visibly softer
than a 1-tick-ahead number.

Why a grid search rather than fixed constants
---------------------------------------------
``alpha`` and ``beta`` are selected by an exhaustive, deterministic grid search
over the in-sample sum of squared one-step errors (20 combinations). This is a
complete enumeration, not a stochastic optimiser: identical input always yields
identical output. There is no random initialisation, no clock read, no I/O and
no state carried between calls.

Why this and not an ML model
----------------------------
Brief §7 asks for decision usefulness, not maximum accuracy. A model whose
parameters cannot be explained to an operator is worse than a transparent
exponential smoother when its output feeds a human-reviewed recommendation. The
``basis`` string on :class:`StockoutRisk` exists for the same reason.

Degradation
-----------
With fewer than ``min_points`` observations the forecaster does not guess a
trend. It falls back to a recency-weighted **level** estimate (EWMA, alpha 0.5),
sets ``method="fallback"`` and ``fallback_used=True``, and reports a materially
lower ``confidence``. With *no* observations it returns a flat zero forecast at
the minimum confidence, and :meth:`DemandForecaster.stockout_risk` then reports
an explicit *uninformed* 50% prior rather than a reassuring 0%: "no data" must
never read as "no risk".

Dependencies
------------
Standard library only. The single normal-quantile constant and the normal CDF
are computed with :mod:`math`, so the module imports nothing outside the pinned
stack and cannot fail on an import error.

Purity
------
Nothing here reads the clock, the network, the database or any global.
``forecast`` and ``stockout_risk`` are pure functions of their arguments: the
same arguments always produce an equal result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:  # pragma: no cover - typing only, no runtime coupling
    from app.sim.models import DemandPoint

__all__ = [
    "ForecastPoint",
    "ForecastResult",
    "StockoutRisk",
    "DemandForecaster",
]


# --------------------------------------------------------------------------- #
# Tuning constants. Every one is a named constant so the behaviour is
# inspectable rather than buried in the arithmetic.
# --------------------------------------------------------------------------- #

#: Half-width of an 80% two-sided normal interval, in standard deviations.
#: Equal to ``scipy.stats.norm.ppf(0.9)``; hard-coded to keep this module
#: dependency-free for a single constant.
_Z80 = 1.2815515655446004

#: Consistency constant making the MAD an unbiased scale estimate for a normal
#: sample: ``sigma ~= 1.4826 * median(|x - median(x)|)``.
_MAD_TO_SIGMA = 1.4826

#: The forecast interval is never narrower than this fraction of the level. A
#: perfectly flat history still has to produce a non-degenerate band, otherwise
#: stockout probability collapses to a step function.
_MIN_RELATIVE_SIGMA = 0.05

#: Coefficient of variation assumed when exactly one observation exists and the
#: spread therefore cannot be measured at all.
_SINGLE_POINT_CV = 0.25

#: A fallback (short-history) band is never narrower than this fraction of level.
_FALLBACK_MIN_CV = 0.15

#: Damping factor on the trend. 0.9 damps the slope to a finite total
#: extrapolation of phi / (1 - phi) = 9 x the current slope.
_PHI = 0.9

#: Deterministic grid searched for the smoothing constants, cheapest first.
_ALPHAS = (0.1, 0.3, 0.5, 0.7, 0.9)
_BETAS = (0.05, 0.1, 0.2, 0.35)

#: A per-tick drift steeper than this fraction of the current level is treated
#: as a transient artefact of a short series rather than extrapolated.
_MAX_TREND_FRACTION = 0.25

#: Confidence model. ``confidence = base * 1 / (1 + decay * horizon)`` so the
#: value strictly decreases as the horizon grows and increases with the amount
#: of history available.
_CONFIDENCE_CAP = 0.95
_QUALITY_FLOOR = 0.30
_HORIZON_DECAY = 0.035
_RISK_HORIZON_DECAY = 0.02
_FALLBACK_PENALTY = 0.5
_FALLBACK_CAP = 0.35
_NO_DATA_CONFIDENCE = 0.10
_MIN_CONFIDENCE = 0.02
_MAX_CONFIDENCE = 0.97

#: Where a risk window extends past the forecast horizon, confidence is cut.
_TRUNCATED_WINDOW_PENALTY = 0.75

#: Reported when there is no demand information at all. A flat 50% is the
#: maximum-entropy answer; it is deliberately *not* 0, so that "we have no data"
#: can never be misread by an operator as "this station is safe".
_UNINFORMED_PRIOR = 0.5

_EPS = 1e-9

METHOD_FALLBACK = "fallback"
METHOD_HOLT_WINTERS = "holt_winters"


# --------------------------------------------------------------------------- #
# Contract types (contract §7.1) — names and fields exactly as specified.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ForecastPoint:
    """One future tick's demand estimate and its 80% prediction interval."""

    tick: int
    liters: float
    lower: float
    upper: float


@dataclass(frozen=True)
class ForecastResult:
    """A forecast for one (station, fuel type) series."""

    station_id: str
    fuel_type: str
    points: tuple[ForecastPoint, ...]
    confidence: float
    method: str
    fallback_used: bool


@dataclass(frozen=True)
class StockoutRisk:
    """Probability and timing of inventory exhaustion, plus how it was derived."""

    probability: float
    ticks_to_stockout: float | None
    confidence: float
    basis: str


# --------------------------------------------------------------------------- #
# Small numeric helpers
# --------------------------------------------------------------------------- #


def _finite(value: Any, default: float = 0.0) -> float:
    """Coerce to a finite float, else ``default``. Never raises, never NaN."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _clip(value: float, low: float, high: float) -> float:
    value = _finite(value, low)
    return max(low, min(high, value))


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    size = len(ordered)
    if size == 0:
        return 0.0
    middle = size // 2
    if size % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _normal_cdf(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _robust_sigma(residuals: Sequence[float], level: float) -> float:
    """Robust scale from residuals, blended so spikes are not fully suppressed.

    The MAD alone is deliberately insensitive to outliers; that is what makes it
    the right centre estimate, but it understates the band on genuinely volatile
    demand. Taking the larger of the MAD estimate and half the RMS gives a band
    that still ignores a single spike's effect on the *level* while not
    pretending a jumpy series is calm.
    """
    if not residuals:
        return max(_MIN_RELATIVE_SIGMA * abs(level), _EPS)
    mad_sigma = _MAD_TO_SIGMA * _median([abs(r) for r in residuals])
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    sigma = max(mad_sigma, 0.5 * rms)
    return max(sigma, _MIN_RELATIVE_SIGMA * abs(level), _EPS)


def _noise_factor(sigma: float, level: float) -> float:
    """1.0 for a perfectly predictable series, smaller as relative noise grows."""
    if abs(level) <= _EPS:
        return 1.0
    cv = min(abs(sigma) / abs(level), 1.0)
    return 1.0 / (1.0 + 2.0 * cv)


def _fmt_liters(value: float) -> str:
    return f"{value:,.0f}"


def _fmt_ticks(value: float) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}"
    return f"{value:.1f}"


# --------------------------------------------------------------------------- #
# The forecaster
# --------------------------------------------------------------------------- #


class DemandForecaster:
    """Deterministic demand forecaster and stockout-risk estimator.

    Pure: the same ``history`` and arguments always produce an equal result.
    No I/O, no clock, no randomness, no mutable per-call state.
    """

    #: Method names reported in :attr:`ForecastResult.method`.
    METHOD_FALLBACK = METHOD_FALLBACK
    METHOD_HOLT_WINTERS = METHOD_HOLT_WINTERS

    #: Method name contract §7.1 does not list but the degradation requirement
    #: mandates for short history.
    FALLBACK_THRESHOLD_NAME = "fallback"

    def __init__(self, *, min_points: int = 12) -> None:
        # A threshold below 2 cannot distinguish a level from a trend, so it is
        # clamped rather than honoured into a degenerate code path.
        self._min_points = max(2, int(min_points))

    @property
    def min_points(self) -> int:
        return self._min_points

    # -- public API -------------------------------------------------------- #

    def forecast(
        self,
        history: Sequence["DemandPoint"],
        *,
        horizon_ticks: int = 8,
    ) -> ForecastResult:
        """Forecast the next ``horizon_ticks`` ticks of demand.

        Never raises and never returns NaN or infinity, including for empty or
        malformed history. ``history`` may contain several (station, fuel type)
        series; the largest is forecast and the others are ignored.
        """
        station_id, fuel_type, series = self._select_series(history)
        horizon = self._coerce_horizon(horizon_ticks)

        if len(series) >= self._min_points:
            level, trend, sigma, residuals = self._fit_holt(series)
            method, fallback = METHOD_HOLT_WINTERS, False
        else:
            level, trend, sigma, residuals = self._fit_level_fallback(series)
            method, fallback = METHOD_FALLBACK, True

        points = self._project(series, level, trend, sigma, horizon)
        confidence = self._confidence(
            n_points=len(series),
            horizon=horizon,
            fallback=fallback,
            sigma=sigma,
            level=level,
        )
        return ForecastResult(
            station_id=station_id,
            fuel_type=fuel_type,
            points=points,
            confidence=confidence,
            method=method,
            fallback_used=fallback,
        )

    def stockout_risk(
        self,
        *,
        inventory_liters: float,
        forecast: ForecastResult,
        transit_ticks: int | None = None,
    ) -> StockoutRisk:
        """Probability that on-hand inventory is exhausted before resupply.

        ``transit_ticks`` is the lead time of the replenishment already on its
        way, if any. When it is supplied, the risk window is the resupply lead
        time rather than the whole forecast horizon: running dry *after* the
        truck arrives is not a stockout.
        """
        points = tuple(getattr(forecast, "points", ()) or ())
        inventory = self._coerce_inventory(inventory_liters)
        horizon = len(points)
        station = str(getattr(forecast, "station_id", "") or "")
        fuel = str(getattr(forecast, "fuel_type", "") or "")
        method = str(getattr(forecast, "method", "") or "unknown")
        forecast_confidence = _clip(getattr(forecast, "confidence", 0.0), 0.0, 1.0)
        # A5/A8 call this with an empty history for a station they know about
        # but the forecaster cannot, since the contract gives ``forecast`` no way
        # to pass the station when there is no point to read it from.
        subject = (
            f"{fuel} at {station}"
            if (fuel and station)
            else (fuel or station or "This station")
        )

        # Inventory already gone is a fact, not a forecast: no model is needed
        # and no amount of missing history makes it less true.
        if inventory <= 0.0:
            basis = (
                f"{subject}: {_fmt_liters(inventory)} L on hand, so the station is "
                f"already out of stock. No depletion has to be projected to reach "
                f"that conclusion. model={method}."
            )
            return StockoutRisk(
                probability=1.0,
                ticks_to_stockout=0.0,
                confidence=forecast_confidence,
                basis=basis,
            )

        # No demand information at all (no usable history, or no horizon to
        # project over): an explicit uninformed prior. Returning 0 here would
        # tell an operator a station we know nothing about is safe.
        no_demand_signal = not any(_finite(p.liters, 0.0) > 0.0 for p in points)
        if no_demand_signal and (
            getattr(forecast, "fallback_used", False) or horizon == 0
        ):
            reason = (
                "no historical demand above zero, so no depletion can be projected"
                if horizon > 0
                else "no forecast horizon was supplied, so nothing can be projected"
            )
            basis = (
                f"{subject}: {reason}. Reporting the uninformed 50% prior rather than "
                f"0% - absence of evidence is not evidence of safety. "
                f"model={method}, confidence={forecast_confidence:.2f}."
            )
            return StockoutRisk(
                probability=_UNINFORMED_PRIOR,
                ticks_to_stockout=None,
                confidence=forecast_confidence,
                basis=basis,
            )

        ticks_to_stockout = self._depletion_tick(points, inventory)

        if transit_ticks is None:
            window = horizon
            lead_time: float | None = None
        else:
            lead_time = max(0.0, _finite(transit_ticks, 0.0))
            window = min(horizon, int(math.ceil(lead_time)))
        window = max(0, window)
        truncated = lead_time is not None and lead_time > horizon

        mean_draw, draw_sd = self._window_demand(points, window)
        if draw_sd <= _EPS:
            probability = 1.0 if mean_draw > inventory else 0.0
        else:
            probability = 1.0 - _normal_cdf((inventory - mean_draw) / draw_sd)
        probability = _clip(probability, 0.0, 1.0)

        risk_confidence = forecast_confidence / (1.0 + _RISK_HORIZON_DECAY * window)
        if truncated:
            risk_confidence *= _TRUNCATED_WINDOW_PENALTY
        risk_confidence = _clip(risk_confidence, _MIN_CONFIDENCE, _MAX_CONFIDENCE)

        basis = self._build_basis(
            subject=subject,
            method=method,
            inventory=inventory,
            forecast_confidence=forecast_confidence,
            mean_draw=mean_draw,
            draw_sd=draw_sd,
            probability=probability,
            window=window,
            horizon=horizon,
            lead_time=lead_time,
            ticks_to_stockout=ticks_to_stockout,
            truncated=truncated,
        )
        return StockoutRisk(
            probability=probability,
            ticks_to_stockout=ticks_to_stockout,
            confidence=risk_confidence,
            basis=basis,
        )

    # -- input normalisation ----------------------------------------------- #

    @staticmethod
    def _coerce_horizon(horizon_ticks: Any) -> int:
        try:
            horizon = int(horizon_ticks)
        except (TypeError, ValueError):
            return 8
        # A negative or absurd horizon is clamped rather than raised on: this
        # function is upstream of an operator-facing endpoint.
        return max(0, min(horizon, 4096))

    def _select_series(
        self, history: Sequence["DemandPoint"] | None
    ) -> tuple[str, str, list[tuple[int, float]]]:
        """Pick the dominant (station, fuel) series and collapse duplicate ticks.

        History from the API may be unordered and may carry several stations.
        Grouping, averaging duplicate ticks and sorting by tick is what makes the
        result invariant to the order the caller supplied.
        """
        groups: dict[tuple[str, str], dict[int, list[float]]] = {}
        for point in history or ():
            station_id = str(getattr(point, "station_id", "") or "")
            fuel_type = str(getattr(point, "fuel_type", "") or "")
            try:
                tick = int(getattr(point, "tick", 0))
            except (TypeError, ValueError):
                continue
            liters = max(_finite(getattr(point, "liters", 0.0), 0.0), 0.0)
            groups.setdefault((station_id, fuel_type), {}).setdefault(tick, []).append(liters)

        if not groups:
            return "", "", []

        # Largest group wins; ties broken lexicographically so the choice is
        # deterministic rather than dict-insertion dependent.
        key = min(groups, key=lambda k: (-len(groups[k]), k))
        by_tick = groups[key]
        series = [(tick, sum(vals) / len(vals)) for tick, vals in sorted(by_tick.items())]
        return key[0], key[1], series

    # -- model fitting ------------------------------------------------------ #

    def _fit_holt(
        self, series: Sequence[tuple[int, float]]
    ) -> tuple[float, float, float, list[float]]:
        """Grid-search alpha/beta, return (level, trend, sigma, residuals)."""
        best_sse: float | None = None
        best_level = _finite(series[-1][1], 0.0) if series else 0.0
        best_trend = 0.0
        best_errors: list[float] = []

        for alpha in _ALPHAS:
            for beta in _BETAS:
                level, trend, errors = self._smooth(series, alpha, beta)
                sse = sum(e * e for e in errors)
                # Strict improvement only, so ties resolve to the first
                # (lowest-alpha, lowest-beta) combination every time.
                if best_sse is None or sse < best_sse - _EPS:
                    best_sse = sse
                    best_level, best_trend, best_errors = level, trend, errors

        if abs(best_level) > _EPS:
            limit = _MAX_TREND_FRACTION * abs(best_level)
            best_trend = max(-limit, min(limit, best_trend))
        sigma = _robust_sigma(best_errors, best_level)
        return best_level, best_trend, sigma, best_errors

    @staticmethod
    def _smooth(
        series: Sequence[tuple[int, float]], alpha: float, beta: float
    ) -> tuple[float, float, list[float]]:
        """One pass of Holt's linear method; returns (level, trend, errors)."""
        level = series[0][1]
        trend = (series[1][1] - series[0][1]) if len(series) > 1 else 0.0
        errors: list[float] = []
        for _, value in series[1:]:
            fitted = level + _PHI * trend
            errors.append(value - fitted)
            new_level = alpha * value + (1.0 - alpha) * fitted
            new_trend = beta * (new_level - level) + (1.0 - beta) * _PHI * trend
            level, trend = new_level, new_trend
        return level, trend, errors

    @staticmethod
    def _fit_level_fallback(
        series: Sequence[tuple[int, float]]
    ) -> tuple[float, float, float, list[float]]:
        """Level-only estimate (EWMA alpha 0.5) for short or empty history.

        No trend is fitted: a slope estimated from fewer than ``min_points``
        points is noise, and extrapolating noise is how a forecaster invents a
        crisis. Recency weighting is kept so a sudden step change in demand is
        reflected rather than averaged away.
        """
        if not series:
            return 0.0, 0.0, 0.0, []

        level = series[0][1]
        for _, value in series[1:]:
            level = 0.5 * value + 0.5 * level

        residuals = [value - level for _, value in series]
        if len(series) >= 2:
            measured = _robust_sigma(residuals, level)
        else:
            measured = 0.0
        sigma = max(measured, _FALLBACK_MIN_CV * abs(level), _EPS)
        return level, 0.0, sigma, residuals

    # -- projection and confidence ------------------------------------------ #

    @staticmethod
    def _project(
        series: Sequence[tuple[int, float]],
        level: float,
        trend: float,
        sigma: float,
        horizon: int,
    ) -> tuple[ForecastPoint, ...]:
        """Build the horizon points, widening the band as the horizon grows."""
        if horizon <= 0:
            return ()

        last_tick = series[-1][0] if series else 0
        points: list[ForecastPoint] = []
        growth = 0.0
        variance_step = 0.0
        for step in range(1, horizon + 1):
            growth += _PHI**step
            if step > 1:
                # Variance of a damped-trend forecast grows by phi^(2j) for each
                # additional step j beyond the first.
                variance_step += _PHI ** (2 * (step - 1))
            mean = max(0.0, level + growth * trend)
            sigma_h = sigma * math.sqrt(1.0 + variance_step)
            half_width = _Z80 * sigma_h
            lower = max(0.0, mean - half_width)
            upper = max(lower, mean + half_width)
            points.append(
                ForecastPoint(
                    tick=last_tick + step,
                    liters=_finite(mean, 0.0),
                    lower=_finite(lower, 0.0),
                    upper=_finite(upper, 0.0),
                )
            )
        return tuple(points)

    def _confidence(
        self,
        *,
        n_points: int,
        horizon: int,
        fallback: bool,
        sigma: float,
        level: float,
    ) -> float:
        """Confidence in 0..1, falling with horizon and rising with history."""
        if horizon <= 0:
            return _MIN_CONFIDENCE

        horizon_factor = 1.0 / (1.0 + _HORIZON_DECAY * horizon)

        if fallback and n_points == 0:
            # Nothing to be confident about. Still decays with the horizon so
            # the "confidence falls as the horizon grows" property holds here
            # too, rather than being a special case that returns a constant.
            return _clip(_NO_DATA_CONFIDENCE * horizon_factor, _MIN_CONFIDENCE, _MAX_CONFIDENCE)

        adequacy = min(1.0, n_points / (2.0 * self._min_points))
        quality = _QUALITY_FLOOR + (1.0 - _QUALITY_FLOOR) * adequacy * _noise_factor(sigma, level)
        base = _CONFIDENCE_CAP * quality
        if fallback:
            base = min(base * _FALLBACK_PENALTY, _FALLBACK_CAP)
        return _clip(base * horizon_factor, _MIN_CONFIDENCE, _MAX_CONFIDENCE)

    # -- risk arithmetic ----------------------------------------------------- #

    @staticmethod
    def _coerce_inventory(inventory_liters: Any) -> float:
        """Normalise inventory, never raising and never inverting its meaning.

        ``+inf`` is preserved as unbounded stock: coercing it to 0 would turn
        "unlimited supply" into a certain stockout, which is the worst possible
        direction to be wrong in. Everything else non-finite or unparseable
        degrades to 0.0 (i.e. exhausted, the cautious reading).
        """
        try:
            number = float(inventory_liters)
        except (TypeError, ValueError):
            return 0.0
        if math.isinf(number) and number > 0.0:
            return math.inf
        if not math.isfinite(number):
            return 0.0
        return number

    @staticmethod
    def _depletion_tick(
        points: Sequence[ForecastPoint], inventory: float
    ) -> float | None:
        """Ticks from now until inventory is exhausted, or None if covered.

        Returns ``0.0`` for inventory that is already exhausted, and interpolates
        within the step that crosses zero so the answer is not quantised to whole
        ticks. ``None`` means the forecast's cumulative demand never exceeds
        inventory — i.e. the forecast does not project a stockout.
        """
        if inventory <= 0.0:
            return 0.0
        cumulative = 0.0
        for index, point in enumerate(points):
            step_demand = max(_finite(point.liters, 0.0), 0.0)
            if step_demand > 0.0 and cumulative + step_demand > inventory:
                fraction = (inventory - cumulative) / step_demand
                fraction = max(0.0, min(1.0, fraction))
                return float(index) + fraction
            cumulative += step_demand
        return None

    @staticmethod
    def _window_demand(
        points: Sequence[ForecastPoint], window: int
    ) -> tuple[float, float]:
        """Mean and standard deviation of cumulative demand over ``window`` ticks.

        The per-tick standard deviation is recovered from the published 80%
        interval, so the probability uses the same uncertainty the operator can
        see, not a second hidden estimate.
        """
        mean = 0.0
        variance = 0.0
        for point in points[:window]:
            mean += max(_finite(point.liters, 0.0), 0.0)
            half = max(
                _finite(point.upper, 0.0) - _finite(point.liters, 0.0),
                _finite(point.liters, 0.0) - _finite(point.lower, 0.0),
                0.0,
            )
            sd = half / _Z80
            variance += sd * sd
        return mean, math.sqrt(variance)

    @staticmethod
    def _build_basis(
        *,
        subject: str,
        method: str,
        inventory: float,
        forecast_confidence: float,
        mean_draw: float,
        draw_sd: float,
        probability: float,
        window: int,
        horizon: int,
        lead_time: float | None,
        ticks_to_stockout: float | None,
        truncated: bool,
    ) -> str:
        """One operator-facing sentence carrying every number behind the score."""
        if lead_time is None:
            window_text = f"the {horizon}-tick forecast horizon"
        else:
            window_text = (
                f"the {_fmt_ticks(lead_time)}-tick resupply lead time "
                f"({window} of {horizon} forecast ticks)"
            )

        if ticks_to_stockout is None:
            timing = (
                "inventory covers the projected draw, so no depletion is projected"
                if lead_time is None
                else "no depletion projected before resupply arrives"
            )
        elif lead_time is None:
            timing = f"projected dry after about {_fmt_ticks(ticks_to_stockout)} ticks"
        else:
            timing = (
                f"projected dry after about {_fmt_ticks(ticks_to_stockout)} ticks, "
                f"before the resupply lands"
            )

        clauses = [
            f"{subject} - {method} forecast, {horizon}-tick horizon, "
            f"model confidence {forecast_confidence:.0%}",
            f"{_fmt_liters(inventory)} L on hand against {_fmt_liters(mean_draw)} L "
            f"projected draw over {window_text}",
            f"estimated stockout probability {probability:.0%}, "
            f"80% demand band implies +/-{_fmt_liters(draw_sd * _Z80)} L over the window",
            timing,
        ]
        if truncated:
            clauses.append(
                f"resupply lands beyond the {horizon}-tick forecast horizon, so risk "
                f"after that point is not projected"
            )
        return "; ".join(clauses) + "."
