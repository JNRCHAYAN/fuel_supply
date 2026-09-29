"""Tests for :mod:`app.intelligence.forecast` (contract §7.1).

The forecaster is owned by workstream A4 and is a pure function of its inputs,
so every test here is a table of inputs and expected properties — no fixtures,
no I/O, no sleeps, no monkeypatching of the clock.

``DemandPoint`` is owned by A2 in ``app/sim/models.py``. It is redeclared here
with exactly the fields contract §5.2 pins, for two reasons: it keeps this test
file runnable before A2's file lands, and it proves the forecaster depends only
on the documented *shape* of ``DemandPoint`` rather than on that module.
"""

from __future__ import annotations

import math
import random
import sys
from dataclasses import dataclass
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import pytest  # noqa: E402

from app.intelligence.forecast import (  # noqa: E402
    DemandForecaster,
    ForecastPoint,
    ForecastResult,
    StockoutRisk,
)

# Contract §7.1: the only method names the field may carry.
VALID_METHODS = {"seasonal_naive", "holt_winters", "ewma", "fallback"}


@dataclass(frozen=True)
class DemandPoint:
    """Mirror of the contract §5.2 type owned by A2."""

    station_id: str
    fuel_type: str
    tick: int
    liters: float


def series(
    values,
    *,
    station: str = "ST-1",
    fuel: str = "DIESEL",
    start: int = 100,
) -> list[DemandPoint]:
    return [
        DemandPoint(station, fuel, start + i, float(v)) for i, v in enumerate(values)
    ]


STEADY = [100.0] * 24
SPIKY = [50.0 if i % 2 else 200.0 for i in range(24)]


@pytest.fixture
def forecaster() -> DemandForecaster:
    return DemandForecaster(min_points=12)


def assert_all_finite(result: ForecastResult) -> None:
    assert math.isfinite(result.confidence)
    for point in result.points:
        assert isinstance(point, ForecastPoint)
        assert math.isfinite(point.tick) or isinstance(point.tick, int)
        assert math.isfinite(point.liters)
        assert math.isfinite(point.lower)
        assert math.isfinite(point.upper)
        assert point.liters >= 0.0
        assert point.lower >= 0.0
        assert point.upper >= point.lower


def assert_valid_risk(risk: StockoutRisk) -> None:
    assert isinstance(risk, StockoutRisk)
    assert math.isfinite(risk.probability), "probability must never be NaN/inf"
    assert 0.0 <= risk.probability <= 1.0, risk.probability
    assert math.isfinite(risk.confidence)
    assert 0.0 <= risk.confidence <= 1.0
    if risk.ticks_to_stockout is not None:
        assert math.isfinite(risk.ticks_to_stockout)
        assert risk.ticks_to_stockout >= 0.0
    assert isinstance(risk.basis, str) and risk.basis.strip()


# --------------------------------------------------------------------------- #
# 1. Degenerate history — the requirement is "never raise, never NaN"
# --------------------------------------------------------------------------- #


def test_empty_history_returns_fallback_without_error(forecaster):
    result = forecaster.forecast([], horizon_ticks=8)

    assert_all_finite(result)
    assert result.fallback_used is True
    assert result.method == "fallback"
    assert result.method in VALID_METHODS
    assert len(result.points) == 8
    # No data means no usable confidence, and it must be visibly low.
    assert result.confidence < 0.2


def test_empty_history_still_returns_one_point_per_horizon_tick(forecaster):
    result = forecaster.forecast([], horizon_ticks=3)

    assert [p.tick for p in result.points] == [1, 2, 3]
    assert all(p.liters == 0.0 for p in result.points)


def test_single_point_uses_fallback_and_level(forecaster):
    result = forecaster.forecast(series([420.0]), horizon_ticks=4)

    assert_all_finite(result)
    assert result.fallback_used is True
    assert result.method == "fallback"
    assert len(result.points) == 4
    # A level-based estimate should sit on the single observation.
    for point in result.points:
        assert 380.0 <= point.liters <= 460.0
        # A one-point band must be non-degenerate but not absurd.
        assert point.upper > point.lower


def test_two_points_uses_fallback(forecaster):
    result = forecaster.forecast(series([100.0, 120.0]), horizon_ticks=3)

    assert_all_finite(result)
    assert result.fallback_used is True
    assert result.method == "fallback"


def test_min_points_boundary_switches_method(forecaster):
    below = forecaster.forecast(series([100.0] * 11), horizon_ticks=4)
    at = forecaster.forecast(series([100.0] * 12), horizon_ticks=4)

    assert below.method == "fallback" and below.fallback_used is True
    assert at.method == "holt_winters" and at.fallback_used is False
    assert at.confidence > below.confidence


def test_station_and_fuel_are_taken_from_history(forecaster):
    result = forecaster.forecast(
        series([100.0] * 5, station="ST-9", fuel="OCTANE"), horizon_ticks=2
    )

    assert result.station_id == "ST-9"
    assert result.fuel_type == "OCTANE"


def test_empty_history_reports_no_station(forecaster):
    result = forecaster.forecast([], horizon_ticks=2)

    assert result.station_id == ""
    assert result.fuel_type == ""


# --------------------------------------------------------------------------- #
# 2. Shape of the forecast
# --------------------------------------------------------------------------- #


def test_steady_demand_forecast_tracks_the_level(forecaster):
    result = forecaster.forecast(series(STEADY), horizon_ticks=6)

    assert result.method == "holt_winters"
    assert result.fallback_used is False
    assert_all_finite(result)
    assert len(result.points) == 6
    for point in result.points:
        assert 90.0 <= point.liters <= 110.0
        assert point.lower < point.liters < point.upper


def test_rising_demand_is_projected_upwards(forecaster):
    history = series([100.0 + 5.0 * i for i in range(24)])
    result = forecaster.forecast(history, horizon_ticks=5)

    assert_all_finite(result)
    assert result.points[-1].liters > result.points[0].liters
    assert result.points[0].liters > 100.0


def test_spiky_demand_gets_a_wider_band_than_steady(forecaster):
    steady = forecaster.forecast(series(STEADY), horizon_ticks=6)
    spiky = forecaster.forecast(series(SPIKY), horizon_ticks=6)

    def relative_width(result: ForecastResult) -> float:
        point = result.points[-1]
        return (point.upper - point.lower) / max(point.liters, 1.0)

    assert_all_finite(spiky)
    # Both series average 125 L/tick; the volatile one must look less certain.
    assert relative_width(spiky) > relative_width(steady)
    assert spiky.confidence < steady.confidence


def test_horizon_ticks_are_consecutive_after_history(forecaster):
    result = forecaster.forecast(series([100.0] * 12, start=500), horizon_ticks=5)

    assert [p.tick for p in result.points] == [512, 513, 514, 515, 516]


def test_zero_and_negative_horizon_return_no_points(forecaster):
    for horizon in (0, -3):
        result = forecaster.forecast(series(STEADY), horizon_ticks=horizon)
        assert result.points == ()
        assert math.isfinite(result.confidence)
        assert 0.0 <= result.confidence <= 1.0


def test_non_integer_horizon_does_not_raise(forecaster):
    for horizon in ("nonsense", None, 4.7):
        result = forecaster.forecast(series(STEADY), horizon_ticks=horizon)  # type: ignore[arg-type]
        assert_all_finite(result)


def test_malformed_history_is_sanitised(forecaster):
    history = [
        DemandPoint("ST-1", "DIESEL", 1, float("nan")),
        DemandPoint("ST-1", "DIESEL", 2, float("inf")),
        DemandPoint("ST-1", "DIESEL", 3, -50.0),
        DemandPoint("ST-1", "DIESEL", 4, 100.0),
    ]
    result = forecaster.forecast(history, horizon_ticks=4)

    assert_all_finite(result)
    assert result.method == "fallback"


def test_history_with_unparseable_ticks_is_ignored(forecaster):
    history = series(STEADY[:12]) + [DemandPoint("ST-1", "DIESEL", "x", 100.0)]  # type: ignore[arg-type]
    result = forecaster.forecast(history, horizon_ticks=4)

    assert_all_finite(result)
    assert result.method == "holt_winters"


def test_duplicate_ticks_are_averaged(forecaster):
    history = [
        DemandPoint("ST-1", "DIESEL", 1, 100.0),
        DemandPoint("ST-1", "DIESEL", 1, 200.0),
        DemandPoint("ST-1", "DIESEL", 2, 150.0),
    ]
    result = forecaster.forecast(history, horizon_ticks=2)

    assert_all_finite(result)
    assert 100.0 <= result.points[0].liters <= 200.0


def test_dominant_series_is_selected_deterministically(forecaster):
    long_series = series([100.0] * 20, station="ST-A", fuel="DIESEL")
    short_series = series([7.0] * 4, station="ST-B", fuel="PETROL")

    result = forecaster.forecast(short_series + long_series, horizon_ticks=3)
    assert result.station_id == "ST-A"
    assert result.fuel_type == "DIESEL"

    # Order of the input must not change which series is chosen.
    reversed_result = forecaster.forecast(long_series + short_series, horizon_ticks=3)
    assert reversed_result == result


# --------------------------------------------------------------------------- #
# 3. Confidence must move with horizon and history
# --------------------------------------------------------------------------- #


def test_confidence_falls_as_horizon_grows(forecaster):
    history = series(STEADY)
    confidences = [
        forecaster.forecast(history, horizon_ticks=h).confidence for h in range(1, 13)
    ]

    for earlier, later in zip(confidences, confidences[1:]):
        assert later < earlier, confidences
    assert confidences[0] <= 1.0 and confidences[-1] > 0.0


def test_confidence_falls_as_horizon_grows_on_fallback_too(forecaster):
    history = series([100.0] * 5)  # below min_points -> fallback path
    confidences = [
        forecaster.forecast(history, horizon_ticks=h).confidence for h in range(1, 13)
    ]

    assert confidences == sorted(confidences, reverse=True)
    for earlier, later in zip(confidences, confidences[1:]):
        assert later < earlier, confidences


def test_confidence_falls_as_horizon_grows_with_no_history(forecaster):
    confidences = [
        forecaster.forecast([], horizon_ticks=h).confidence for h in range(1, 13)
    ]

    assert confidences == sorted(confidences, reverse=True)
    assert confidences[0] < 0.2


def test_confidence_is_not_constant_across_horizons(forecaster):
    history = series(STEADY)
    confidences = {
        forecaster.forecast(history, horizon_ticks=h).confidence for h in (1, 4, 8, 16)
    }

    assert len(confidences) == 4, "a constant confidence is a bug (contract §7.1)"


def test_confidence_rises_as_history_grows(forecaster):
    horizon = 4
    confidences = [
        forecaster.forecast(series([100.0] * n), horizon_ticks=horizon).confidence
        for n in (1, 6, 11, 12, 48)
    ]

    for earlier, later in zip(confidences, confidences[1:]):
        assert later > earlier, confidences


def test_confidence_always_within_unit_interval(forecaster):
    for n in (0, 1, 2, 5, 11, 12, 50, 200):
        for horizon in (0, 1, 8, 64):
            result = forecaster.forecast(series([100.0] * n), horizon_ticks=horizon)
            assert 0.0 <= result.confidence <= 1.0


# --------------------------------------------------------------------------- #
# 4. Determinism / purity
# --------------------------------------------------------------------------- #


def test_identical_input_gives_identical_output(forecaster):
    history = series(SPIKY)
    first = forecaster.forecast(history, horizon_ticks=8)
    second = forecaster.forecast(history, horizon_ticks=8)

    assert first == second
    assert (
        forecaster.stockout_risk(inventory_liters=900.0, forecast=first)
        == forecaster.stockout_risk(inventory_liters=900.0, forecast=first)
    )


def test_a_fresh_instance_gives_the_same_output(forecaster):
    history = series(SPIKY)
    assert forecaster.forecast(history, horizon_ticks=8) == DemandForecaster(
        min_points=12
    ).forecast(history, horizon_ticks=8)


def test_input_ordering_does_not_change_the_result(forecaster):
    history = series(SPIKY)
    shuffled = list(history)
    random.Random(20240929).shuffle(shuffled)

    assert forecaster.forecast(shuffled, horizon_ticks=8) == forecaster.forecast(
        history, horizon_ticks=8
    )


def test_forecast_does_not_mutate_its_input(forecaster):
    history = series(SPIKY)
    snapshot = list(history)
    forecaster.forecast(history, horizon_ticks=8)

    assert history == snapshot


def test_repeated_forecasts_from_one_instance_are_stable(forecaster):
    history = series(SPIKY)
    results = [forecaster.forecast(history, horizon_ticks=5) for _ in range(5)]

    assert all(result == results[0] for result in results)


# --------------------------------------------------------------------------- #
# 5. Stockout risk
# --------------------------------------------------------------------------- #


def test_ticks_to_stockout_is_none_when_inventory_covers_the_forecast(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    risk = forecaster.stockout_risk(inventory_liters=1_000_000.0, forecast=forecast)

    assert_valid_risk(risk)
    assert risk.ticks_to_stockout is None
    assert risk.probability < 0.01
    assert "covers" in risk.basis


def test_ticks_to_stockout_is_reported_when_inventory_is_short(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    risk = forecaster.stockout_risk(inventory_liters=350.0, forecast=forecast)

    assert_valid_risk(risk)
    assert risk.ticks_to_stockout is not None
    # 350 L at 100 L/tick runs dry half way through the fourth tick.
    assert 3.0 <= risk.ticks_to_stockout <= 4.0
    assert risk.probability > 0.5


def test_zero_inventory_is_certain_stockout(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    risk = forecaster.stockout_risk(inventory_liters=0.0, forecast=forecast)

    assert_valid_risk(risk)
    assert risk.ticks_to_stockout == 0.0
    assert risk.probability == 1.0


def test_probability_rises_as_inventory_falls(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    # Descending stock, so the resulting probabilities must ascend.
    inventories = [5000.0, 1200.0, 800.0, 400.0, 200.0, 0.0]
    probabilities = [
        forecaster.stockout_risk(inventory_liters=inv, forecast=forecast).probability
        for inv in inventories
    ]

    assert probabilities == sorted(probabilities), probabilities
    assert probabilities[0] < 0.05
    assert probabilities[-1] == 1.0


def test_transit_ticks_beyond_horizon_is_flagged_and_lowers_confidence(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)

    within = forecaster.stockout_risk(
        inventory_liters=300.0, forecast=forecast, transit_ticks=4
    )
    beyond = forecaster.stockout_risk(
        inventory_liters=300.0, forecast=forecast, transit_ticks=40
    )

    assert_valid_risk(within)
    assert_valid_risk(beyond)
    assert "resupply lead time" in within.basis
    assert "beyond" in beyond.basis
    assert beyond.confidence < within.confidence


def test_transit_ticks_reduce_risk_compared_with_no_resupply(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)

    without = forecaster.stockout_risk(inventory_liters=500.0, forecast=forecast)
    with_short_lead = forecaster.stockout_risk(
        inventory_liters=500.0, forecast=forecast, transit_ticks=2
    )

    # Only two ticks of exposure instead of eight: strictly less demand at risk.
    assert with_short_lead.probability < without.probability


def test_no_history_is_not_reported_as_safe(forecaster):
    forecast = forecaster.forecast([], horizon_ticks=8)
    risk = forecaster.stockout_risk(inventory_liters=1000.0, forecast=forecast)

    assert_valid_risk(risk)
    assert risk.ticks_to_stockout is None
    # Absence of evidence must not be rendered as 0% risk.
    assert risk.probability == 0.5
    assert risk.confidence < 0.2
    assert "uninformed" in risk.basis


def test_zero_demand_history_is_not_reported_as_safe(forecaster):
    forecast = forecaster.forecast(series([0.0] * 3), horizon_ticks=6)
    risk = forecaster.stockout_risk(inventory_liters=1000.0, forecast=forecast)

    assert_valid_risk(risk)
    assert risk.probability == 0.5


def test_risk_with_empty_forecast_horizon_does_not_raise(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=0)
    risk = forecaster.stockout_risk(inventory_liters=100.0, forecast=forecast)

    assert_valid_risk(risk)


def test_basis_is_human_readable_and_cites_its_numbers(forecaster):
    history = series(STEADY, station="ST-7", fuel="PETROL")
    forecast = forecaster.forecast(history, horizon_ticks=8)
    risk = forecaster.stockout_risk(inventory_liters=450.0, forecast=forecast)

    assert_valid_risk(risk)
    basis = risk.basis
    assert "PETROL" in basis and "ST-7" in basis
    assert "holt_winters" in basis
    assert "450 L on hand" in basis
    assert f"{risk.probability:.0%}" in basis
    assert 20 <= len(basis) <= 600


def test_extreme_inventory_values_stay_in_bounds(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    for inventory in (-1000.0, 0.0, 1e12, float("inf"), float("nan"), None):
        risk = forecaster.stockout_risk(
            inventory_liters=inventory, forecast=forecast  # type: ignore[arg-type]
        )
        assert_valid_risk(risk)


def test_infinite_inventory_is_treated_as_covered_not_as_empty(forecaster):
    forecast = forecaster.forecast(series(STEADY), horizon_ticks=8)
    risk = forecaster.stockout_risk(
        inventory_liters=float("inf"), forecast=forecast
    )

    assert_valid_risk(risk)
    assert risk.ticks_to_stockout is None
    assert risk.probability == 0.0


# --------------------------------------------------------------------------- #
# 6. Randomised (seeded) sweep — the invariants must hold for any input
# --------------------------------------------------------------------------- #


def test_randomised_inputs_keep_every_invariant(forecaster):
    rng = random.Random(1234567)
    checked = 0

    for _ in range(300):
        n = rng.choice([0, 1, 2, 5, 11, 12, 13, 40, 120])
        shape = rng.choice(["flat", "ramp", "spiky", "noisy", "zero"])
        values = []
        for i in range(n):
            if shape == "flat":
                values.append(100.0)
            elif shape == "ramp":
                values.append(50.0 + 10.0 * i)
            elif shape == "spiky":
                values.append(rng.choice([0.0, 5.0, 900.0]))
            elif shape == "zero":
                values.append(0.0)
            else:
                values.append(max(0.0, rng.uniform(0.0, 500.0)))
        history = series(values)

        horizon = rng.choice([0, 1, 3, 8, 24])
        result = forecaster.forecast(history, horizon_ticks=horizon)

        assert_all_finite(result)
        assert result.method in VALID_METHODS
        assert 0.0 <= result.confidence <= 1.0
        assert len(result.points) == max(0, min(horizon, 4096))

        inventory = rng.choice([-5.0, 0.0, 10.0, 250.0, 750.0, 5000.0, 1e9])
        transit = rng.choice([None, 0, 1, 4, 30])
        risk = forecaster.stockout_risk(
            inventory_liters=inventory, forecast=result, transit_ticks=transit
        )

        assert_valid_risk(risk)
        checked += 1

    assert checked == 300


def test_huge_and_tiny_demand_is_finite(forecaster):
    for magnitude in (1e-9, 1e-3, 1.0, 1e9, 1e12):
        history = series([magnitude] * 20)
        result = forecaster.forecast(history, horizon_ticks=8)
        assert_all_finite(result)

        risk = forecaster.stockout_risk(
            inventory_liters=magnitude * 4.0, forecast=result
        )
        assert_valid_risk(risk)
