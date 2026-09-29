"""Tests for :mod:`app.intelligence.stockout` (design doc 2026-09-29).

The analyser is owned by the stockout workstream and is a pure function of its
arguments — no I/O, no clock, no state — so every test here is a table of inputs
and expected properties: no fixtures, no sleeps, no monkeypatching.

Forecasts are built directly from :class:`app.intelligence.forecast.ForecastResult`
rather than through ``DemandForecaster``. That keeps this file asserting the
*stockout* arithmetic and nothing about how demand was estimated: a change to the
forecaster must not silently move what this file claims about timing.
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path
from types import SimpleNamespace

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

import pytest  # noqa: E402

from app.intelligence.forecast import ForecastPoint, ForecastResult  # noqa: E402
from app.intelligence.stockout import (  # noqa: E402
    LEVEL_ORDER,
    IncomingSupply,
    RiskLevel,
    RiskThresholds,
    StockoutAnalyzer,
    StockoutAssessment,
)

HORIZON = 8
FLAT = [100.0] * HORIZON


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def forecast_of(
    liters,
    *,
    station: str = "ST-1",
    fuel: str = "DIESEL",
    method: str = "holt_winters",
    confidence: float = 0.75,
) -> ForecastResult:
    """A ``ForecastResult`` whose points carry the given per-tick demand."""
    points = tuple(
        ForecastPoint(
            tick=i + 1,
            liters=float(v),
            lower=float(v) * 0.8,
            upper=float(v) * 1.2,
        )
        for i, v in enumerate(liters)
    )
    return ForecastResult(
        station_id=station,
        fuel_type=fuel,
        points=points,
        confidence=confidence,
        method=method,
        fallback_used=False,
    )


def flat(count: int = HORIZON, liters: float = 100.0) -> list[float]:
    return [liters] * count


def level(result: StockoutAssessment) -> int:
    """Severity rank: comparable, unlike the enum itself."""
    return LEVEL_ORDER.index(result.risk)


def assert_well_formed(result: StockoutAssessment) -> None:
    """Every invariant the purity contract promises, for any input at all.

    ``+inf`` is permitted only where the engine documents unbounded stock:
    ``current_inventory`` / ``projected_inventory`` (either sign finite) and the
    non-negative surplus. Everything else must be a finite, non-negative number,
    and nothing anywhere may be NaN.
    """
    assert isinstance(result, StockoutAssessment)
    assert isinstance(result.risk, RiskLevel)
    assert isinstance(result.basis, str) and result.basis.strip()
    assert isinstance(result.horizon_ticks, int) and result.horizon_ticks >= 0

    for name in ("current_inventory", "projected_inventory"):
        value = getattr(result, name)
        assert not math.isnan(value), name
        assert math.isfinite(value) or value == math.inf, (name, value)

    for name in ("expected_demand", "incoming_supply", "shortage_amount", "surplus_amount"):
        value = getattr(result, name)
        assert not math.isnan(value), name
        assert math.isfinite(value) or value == math.inf, (name, value)
        assert value >= 0.0, (name, value)

    assert math.isfinite(result.demand_confidence)
    assert 0.0 <= result.demand_confidence <= 1.0
    assert isinstance(result.demand_method, str)

    if result.ticks_until_stockout is not None:
        assert math.isfinite(result.ticks_until_stockout)
        assert result.ticks_until_stockout >= 0.0
        assert isinstance(result.stockout_tick, int)
    else:
        assert result.stockout_tick is None

    for source in result.incoming_sources:
        assert isinstance(source, IncomingSupply)


@pytest.fixture
def analyzer() -> StockoutAnalyzer:
    return StockoutAnalyzer()


# --------------------------------------------------------------------------- #
# Required case 1 — sufficient inventory
# --------------------------------------------------------------------------- #


def test_sufficient_inventory_is_low_with_surplus_and_no_crossing(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=5_000.0,
        forecast=forecast_of(FLAT),
    )

    assert result.risk == RiskLevel.LOW
    assert result.surplus_amount > 0.0
    assert result.shortage_amount == 0.0
    assert result.stockout_tick is None
    assert result.ticks_until_stockout is None
    assert result.projected_inventory > 0.0
    assert result.expected_demand == pytest.approx(800.0)
    assert result.surplus_amount == pytest.approx(5_000.0 - 800.0)


def test_exactly_enough_inventory_lands_on_zero_at_the_horizon_edge(analyzer):
    # 800 L against 800 L of demand: the walk touches zero on the final tick,
    # which is a crossing (the level is not strictly positive), but there is no
    # shortage -- nothing is left unserved.
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=800.0,
        forecast=forecast_of(FLAT),
    )

    assert result.projected_inventory == pytest.approx(0.0)
    assert result.shortage_amount == 0.0
    assert result.surplus_amount == 0.0
    assert result.ticks_until_stockout == pytest.approx(8.0)
    assert result.stockout_tick == 8
    assert result.risk == RiskLevel.MEDIUM


# --------------------------------------------------------------------------- #
# Required case 2 — low inventory (below safety stock, no crossing)
# --------------------------------------------------------------------------- #


def test_low_inventory_is_medium_below_safety_stock_with_no_crossing(analyzer):
    # Expected demand 800 L, so safety stock is 0.25 * 800 = 200 L. At 900 L the
    # station ends the horizon at 100 L: positive, but under the floor.
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=900.0,
        forecast=forecast_of(FLAT),
    )

    assert result.risk == RiskLevel.MEDIUM
    assert result.ticks_until_stockout is None
    assert result.stockout_tick is None
    assert result.projected_inventory > 0.0
    assert result.projected_inventory == pytest.approx(100.0)
    assert result.shortage_amount == 0.0


# --------------------------------------------------------------------------- #
# Required case 3 — shortage (demand exceeds stock)
# --------------------------------------------------------------------------- #


def test_shortage_is_negative_projection_with_equal_shortage_amount(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast_of(FLAT),
    )

    assert result.projected_inventory < 0.0
    assert result.shortage_amount == pytest.approx(-result.projected_inventory)
    assert result.shortage_amount == pytest.approx(400.0)
    assert result.surplus_amount == 0.0
    assert level(result) >= LEVEL_ORDER.index(RiskLevel.MEDIUM)


# --------------------------------------------------------------------------- #
# Required case 4 — immediate stockout (nothing on hand)
# --------------------------------------------------------------------------- #


def test_empty_inventory_is_critical_and_dry_right_now(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=0.0,
        forecast=forecast_of(FLAT),
        current_tick=1_000,
    )

    assert result.risk == RiskLevel.CRITICAL
    assert result.ticks_until_stockout == 0.0
    assert result.stockout_tick == 1_000
    # Nothing on hand means the whole horizon's demand is unserved.
    assert result.shortage_amount == pytest.approx(result.expected_demand)
    assert result.shortage_amount == pytest.approx(800.0)
    assert result.surplus_amount == 0.0


def test_negative_inventory_is_also_critical(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=-250.0,
        forecast=forecast_of(FLAT),
        current_tick=42,
    )

    assert result.risk == RiskLevel.CRITICAL
    assert result.ticks_until_stockout == 0.0
    assert result.stockout_tick == 42


# --------------------------------------------------------------------------- #
# Required case 5 — incoming supply
# --------------------------------------------------------------------------- #


def test_projected_inventory_identity_holds_exactly_with_incoming(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast_of(FLAT),
        incoming=(IncomingSupply(quantity=100.0, arrival_tick=2),),
    )

    assert result.incoming_supply == pytest.approx(100.0)
    assert result.projected_inventory == pytest.approx(
        result.current_inventory + result.incoming_supply - result.expected_demand
    )
    assert result.projected_inventory == pytest.approx(-300.0)


def test_projected_inventory_identity_holds_with_several_arrivals(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=150.0,
        forecast=forecast_of(flat()),
        incoming=(
            IncomingSupply(quantity=500.0, arrival_tick=1),
            IncomingSupply(quantity=250.0, arrival_tick=5),
        ),
    )

    assert result.incoming_supply == pytest.approx(750.0)
    assert result.projected_inventory == pytest.approx(
        result.current_inventory + result.incoming_supply - result.expected_demand
    )
    assert len(result.incoming_sources) == 2


def test_incoming_supply_turns_a_stockout_into_no_stockout(analyzer):
    forecast = forecast_of(FLAT)
    without = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast,
    )
    with_supply = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast,
        incoming=(IncomingSupply(quantity=1_000.0, arrival_tick=1),),
    )

    # The baseline genuinely stockouts...
    assert without.ticks_until_stockout is not None
    assert without.stockout_tick is not None
    # ...and the shipment inside the horizon removes the crossing entirely.
    assert with_supply.ticks_until_stockout is None
    assert with_supply.stockout_tick is None
    assert with_supply.projected_inventory > 0.0
    assert with_supply.incoming_supply == pytest.approx(1_000.0)


def test_arrival_after_the_horizon_does_not_rescue_the_station(analyzer):
    horizon = 8
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast_of(FLAT),
        incoming=(IncomingSupply(quantity=10_000.0, arrival_tick=horizon + 1),),
    )

    assert result.incoming_supply == 0.0
    assert result.incoming_sources == ()
    # The station is still projected dry well before the shipment lands.
    assert result.ticks_until_stockout is not None
    assert result.stockout_tick is not None
    assert result.projected_inventory < 0.0


def test_arrival_on_the_last_horizon_tick_is_kept(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast_of(FLAT),
        incoming=(IncomingSupply(quantity=10_000.0, arrival_tick=8),),
    )

    assert result.incoming_supply == pytest.approx(10_000.0)
    assert len(result.incoming_sources) == 1


# --------------------------------------------------------------------------- #
# Required case 6 — multiple future ticks and interpolation
# --------------------------------------------------------------------------- #


def test_crossing_lands_on_the_expected_tick(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=400.0,
        forecast=forecast_of(FLAT),
    )

    # 400 / 100 = 4 ticks, so the level reaches zero at the fourth step.
    assert result.ticks_until_stockout == pytest.approx(4.0)
    assert result.stockout_tick == 4


def test_fractional_interpolation_inside_the_crossing_tick(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast_of(FLAT),
    )

    # 250 L against 100 L/tick: 2 full ticks plus half of the third.
    assert result.ticks_until_stockout == pytest.approx(2.5)
    assert result.ticks_until_stockout != 3.0
    assert result.stockout_tick == 3


def test_fractional_interpolation_is_relative_to_current_tick(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast_of(FLAT),
        current_tick=100,
    )

    assert result.ticks_until_stockout == pytest.approx(2.5)
    # ceil(2.5) == 3, offset from the supplied clock.
    assert result.stockout_tick == 103


def test_crossing_on_non_flat_demand_tracks_that_tick(analyzer):
    # 50 L/tick: 175 L survives three ticks and dies half-way through the fourth.
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=175.0,
        forecast=forecast_of(flat(liters=50.0)),
    )

    assert result.ticks_until_stockout == pytest.approx(3.5)
    assert result.stockout_tick == 4
    assert result.expected_demand == pytest.approx(400.0)


def test_mid_horizon_arrival_delays_the_crossing_without_removing_it(analyzer):
    forecast = forecast_of(FLAT)
    base = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast,
    )
    delayed = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast,
        incoming=(IncomingSupply(quantity=100.0, arrival_tick=2),),
    )

    assert base.ticks_until_stockout == pytest.approx(2.5)
    assert base.stockout_tick == 3
    # The crossing still happens, just one tick later.
    assert delayed.ticks_until_stockout is not None
    assert delayed.stockout_tick is not None
    assert delayed.ticks_until_stockout > base.ticks_until_stockout
    assert delayed.ticks_until_stockout == pytest.approx(3.5)
    assert delayed.stockout_tick == 4


def test_forecast_shorter_than_horizon_carries_zero_demand_past_the_end(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast_of(flat(3, 100.0)),
    )

    # Only three ticks of demand exist, so none of the later ticks consume.
    assert result.expected_demand == pytest.approx(300.0)
    assert result.ticks_until_stockout == pytest.approx(2.5)
    assert result.stockout_tick == 3


# --------------------------------------------------------------------------- #
# Thresholds are configurable
# --------------------------------------------------------------------------- #


def test_widened_critical_band_promotes_high_to_critical():
    forecast = forecast_of(FLAT)
    default = StockoutAnalyzer()
    widened = StockoutAnalyzer(thresholds=RiskThresholds(critical_ticks=5.0))

    default_result = default.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast,
    )
    widened_result = widened.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast,
    )

    # 2.5 ticks from dry: inside the default HIGH band, inside the widened one's CRITICAL.
    assert default_result.ticks_until_stockout == pytest.approx(2.5)
    assert default_result.risk == RiskLevel.HIGH
    assert widened_result.ticks_until_stockout == default_result.ticks_until_stockout
    assert widened_result.risk == RiskLevel.CRITICAL


def test_thresholds_property_reports_what_was_passed():
    thresholds = RiskThresholds(
        critical_ticks=2.0,
        high_ticks=6.0,
        safety_stock_fraction=0.5,
        horizon_ticks=12,
    )
    analyzer = StockoutAnalyzer(thresholds=thresholds)

    assert analyzer.thresholds is thresholds
    assert analyzer.thresholds == thresholds
    assert StockoutAnalyzer().thresholds == RiskThresholds()


def test_changing_safety_stock_fraction_changes_the_band():
    forecast = forecast_of(FLAT)
    strict = StockoutAnalyzer()  # 0.25 * 800 = 200 L floor
    lax = StockoutAnalyzer(thresholds=RiskThresholds(safety_stock_fraction=0.0))

    strict_result = strict.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=900.0,
        forecast=forecast,
    )
    lax_result = lax.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=900.0,
        forecast=forecast,
    )

    assert strict_result.risk == RiskLevel.MEDIUM
    assert lax_result.risk == RiskLevel.LOW


def test_changing_the_horizon_changes_the_band():
    long_horizon = StockoutAnalyzer()
    short_horizon = StockoutAnalyzer(thresholds=RiskThresholds(horizon_ticks=1))

    long_result = long_horizon.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=900.0,
        forecast=forecast_of(FLAT),
    )
    short_result = short_horizon.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=900.0,
        forecast=forecast_of(FLAT),
    )

    assert long_result.horizon_ticks == 8
    assert short_result.horizon_ticks == 1
    assert long_result.expected_demand == pytest.approx(800.0)
    assert short_result.expected_demand == pytest.approx(100.0)
    assert long_result.risk == RiskLevel.MEDIUM
    assert short_result.risk == RiskLevel.LOW


def test_risk_thresholds_clamps_negative_values_to_zero():
    thresholds = RiskThresholds(
        critical_ticks=-2.0,
        high_ticks=-1.0,
        safety_stock_fraction=-0.5,
        horizon_ticks=-5,
    )

    assert thresholds.critical_ticks == 0.0
    assert thresholds.high_ticks == 0.0
    assert thresholds.safety_stock_fraction == 0.0
    assert thresholds.horizon_ticks == 0


@pytest.mark.parametrize(
    "horizon",
    [float("inf"), float("-inf"), float("nan"), -5, -1, 0],
    ids=["inf", "-inf", "nan", "-5", "-1", "0"],
)
def test_risk_thresholds_non_finite_or_negative_horizon_clamps_to_zero(horizon):
    # ``int(inf)`` / ``int(nan)`` raise from the constructor, which would break
    # the never-raises contract before ``assess`` is ever reached.
    assert RiskThresholds(horizon_ticks=horizon).horizon_ticks == 0


@pytest.mark.parametrize(
    "horizon, expected",
    [(float("inf"), 0), (float("nan"), 0), (3.9, 3), ("4", 4), (5.0, 5)],
)
def test_risk_thresholds_normalises_fractional_and_non_finite_horizons(horizon, expected):
    thresholds = RiskThresholds(horizon_ticks=horizon)

    assert thresholds.horizon_ticks == expected
    assert isinstance(thresholds.horizon_ticks, int)


def test_risk_thresholds_preserves_a_large_integer_horizon_exactly():
    big = 10**1000
    thresholds = RiskThresholds(horizon_ticks=big)

    assert thresholds.horizon_ticks == big
    assert isinstance(thresholds.horizon_ticks, int)
    # A float round-trip would have corrupted the value, so prove it is exact.
    assert thresholds.horizon_ticks - big == 0


def test_non_finite_horizon_thresholds_still_assess_without_raising(analyzer):
    # Clamped to 0, so the analyser projects nothing rather than looping or raising.
    for horizon in (float("inf"), float("-inf"), float("nan")):
        local = StockoutAnalyzer(thresholds=RiskThresholds(horizon_ticks=horizon))
        result = local.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=250.0,
            forecast=forecast_of(FLAT),
        )

        assert_well_formed(result)
        assert result.horizon_ticks == 0
        assert result.expected_demand == 0.0
        assert result.projected_inventory == pytest.approx(250.0)
        assert result.ticks_until_stockout is None


def test_risk_thresholds_coerces_fractional_horizon_to_int():
    assert RiskThresholds(horizon_ticks=3.9).horizon_ticks == 3
    assert RiskThresholds(horizon_ticks="4").horizon_ticks == 4


def test_zero_horizon_projects_nothing(analyzer):
    zero = StockoutAnalyzer(thresholds=RiskThresholds(horizon_ticks=0))
    result = zero.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=10.0,
        forecast=forecast_of(FLAT),
    )

    assert result.horizon_ticks == 0
    assert result.expected_demand == 0.0
    assert result.incoming_supply == 0.0
    assert result.projected_inventory == pytest.approx(10.0)
    assert result.ticks_until_stockout is None
    # Nothing to consume and nothing projected dry, so no risk is reported.
    assert result.risk == RiskLevel.LOW


# --------------------------------------------------------------------------- #
# Determinism / purity
# --------------------------------------------------------------------------- #


def test_assess_twice_with_equal_arguments_is_equal(analyzer):
    kwargs = dict(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=275.0,
        forecast=forecast_of(FLAT),
        incoming=(IncomingSupply(quantity=120.0, arrival_tick=3),),
        current_tick=17,
    )

    first = analyzer.assess(**kwargs)
    second = analyzer.assess(**kwargs)

    assert first == second
    assert first.ticks_until_stockout == second.ticks_until_stockout
    assert first.basis == second.basis


def test_a_fresh_analyzer_produces_the_same_result(analyzer):
    kwargs = dict(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=275.0,
        forecast=forecast_of(FLAT),
    )

    assert analyzer.assess(**kwargs) == StockoutAnalyzer().assess(**kwargs)


def test_assess_many_is_stable_regardless_of_row_order(analyzer):
    forecast = forecast_of(FLAT)
    rows = [
        {"station_id": "ST-B", "fuel_type": "DIESEL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-A", "fuel_type": "PETROL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-A", "fuel_type": "DIESEL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-C", "fuel_type": "DIESEL", "current_inventory": 900.0, "forecast": forecast},
        {"station_id": "ST-D", "fuel_type": "DIESEL", "current_inventory": 5_000.0, "forecast": forecast},
        {"station_id": "ST-E", "fuel_type": "DIESEL", "current_inventory": 300.0, "forecast": forecast},
    ]

    forward = analyzer.assess_many(rows=rows, current_tick=0)
    backward = analyzer.assess_many(rows=list(reversed(rows)), current_tick=0)

    assert forward == backward
    assert [(a.station_id, a.fuel_type, a.risk) for a in forward] == [
        (a.station_id, a.fuel_type, a.risk) for a in backward
    ]


def test_assess_many_does_not_mutate_its_rows(analyzer):
    forecast = forecast_of(FLAT)
    rows = [
        {"station_id": "ST-A", "fuel_type": "DIESEL", "current_inventory": 100.0, "forecast": forecast},
        {"station_id": "ST-B", "fuel_type": "DIESEL", "current_inventory": 5_000.0, "forecast": forecast},
    ]
    snapshot = [dict(row) for row in rows]

    analyzer.assess_many(rows=rows, current_tick=0)

    assert rows == snapshot


# --------------------------------------------------------------------------- #
# Never raises, never NaN
# --------------------------------------------------------------------------- #


def test_garbage_inputs_never_raise_and_stay_well_formed(analyzer):
    class NoPoints:
        """Forecast-like object with no ``points`` attribute at all."""

    junk_points = SimpleNamespace(
        points=(object(), SimpleNamespace(liters=None), SimpleNamespace(liters="abc"),
                SimpleNamespace(liters=float("nan")), SimpleNamespace(liters=float("inf"))),
        method=None,
        confidence=None,
    )

    forecasts = [
        None,
        object(),
        NoPoints(),
        SimpleNamespace(points=()),
        SimpleNamespace(points=[]),
        SimpleNamespace(points=None),
        junk_points,
        forecast_of([-50.0, -1.0, 0.0, 10.0, 20.0]),  # negative demand points
    ]
    inventories = [None, "abc", float("nan"), float("inf"), -100.0, 0.0, 10.0, 1_000.0]

    for forecast in forecasts:
        for inventory in inventories:
            result = analyzer.assess(
                station_id="ST-1",
                fuel_type="DIESEL",
                current_inventory=inventory,
                forecast=forecast,
            )
            assert_well_formed(result)


def test_junk_incoming_never_raises_and_is_all_dropped(analyzer):
    junk = (
        object(),
        None,
        IncomingSupply(quantity=float("nan"), arrival_tick=2),
        IncomingSupply(quantity=float("inf"), arrival_tick=2),
        IncomingSupply(quantity=-500.0, arrival_tick=2),
        IncomingSupply(quantity=100.0, arrival_tick=float("nan")),
        IncomingSupply(quantity=100.0, arrival_tick=float("inf")),
        IncomingSupply(quantity=100.0, arrival_tick=float("-inf")),
        IncomingSupply(quantity=100.0, arrival_tick=0),
        IncomingSupply(quantity=100.0, arrival_tick=-3),
        IncomingSupply(quantity=100.0, arrival_tick=99),
        SimpleNamespace(quantity="abc", arrival_tick=1),
    )

    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=1_000.0,
        forecast=forecast_of(FLAT),
        incoming=junk,
    )

    assert_well_formed(result)
    assert result.incoming_supply == 0.0
    assert result.incoming_sources == ()


def test_non_finite_arrival_tick_contributes_nothing(analyzer):
    """``int(inf)`` used to raise ``OverflowError``; a non-finite arrival is
    now read as tick 0, which is outside the horizon and therefore dropped."""
    valid = IncomingSupply(quantity=500.0, arrival_tick=2)
    for arrival in (float("inf"), float("-inf"), float("nan")):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=100.0,
            forecast=forecast_of(FLAT),
            incoming=(
                IncomingSupply(quantity=250.0, arrival_tick=arrival),
                valid,
            ),
        )

        assert_well_formed(result)
        # Only the valid consignment counts; the non-finite one is dropped.
        assert result.incoming_supply == pytest.approx(500.0)
        assert result.incoming_sources == (valid,)


def test_unparseable_current_tick_is_treated_as_zero(analyzer):
    for tick in (None, "abc", 0):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=0.0,
            forecast=forecast_of(FLAT),
            current_tick=tick,
        )
        assert_well_formed(result)
        assert result.stockout_tick == 0


def test_non_finite_current_tick_is_treated_as_zero(analyzer):
    for tick in (float("inf"), float("-inf"), float("nan")):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=250.0,
            forecast=forecast_of(FLAT),
            current_tick=tick,
        )

        assert_well_formed(result)
        assert result.ticks_until_stockout == pytest.approx(2.5)
        # The crossing is offset from tick 0, not from a garbage clock.
        assert result.stockout_tick == 3

        dry = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=0.0,
            forecast=forecast_of(FLAT),
            current_tick=tick,
        )
        assert_well_formed(dry)
        assert dry.ticks_until_stockout == 0.0
        assert dry.stockout_tick == 0


def test_large_integer_current_tick_passes_through_exactly(analyzer):
    """An ``int`` tick must not be round-tripped through a lossy float."""
    big = 10**1000
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=0.0,
        forecast=forecast_of(FLAT),
        current_tick=big,
    )

    assert_well_formed(result)
    assert result.stockout_tick == big
    assert isinstance(result.stockout_tick, int)

    crossing = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=250.0,
        forecast=forecast_of(FLAT),
        current_tick=big,
    )
    assert crossing.stockout_tick == big + 3



def test_missing_station_and_fuel_do_not_raise(analyzer):
    for station, fuel in ((None, None), ("", ""), ("ST-1", None), (None, "DIESEL")):
        result = analyzer.assess(
            station_id=station,
            fuel_type=fuel,
            current_inventory=1_000.0,
            forecast=forecast_of(FLAT),
        )
        assert_well_formed(result)
        assert isinstance(result.station_id, str)
        assert isinstance(result.fuel_type, str)


def test_no_forecast_is_treated_as_zero_demand_not_as_a_stockout(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=500.0,
        forecast=None,
    )

    assert result.expected_demand == 0.0
    assert result.ticks_until_stockout is None
    assert result.stockout_tick is None
    assert result.projected_inventory == pytest.approx(500.0)
    assert result.demand_method == "unknown"


# --------------------------------------------------------------------------- #
# ``+inf`` inventory is unbounded stock, never empty
# --------------------------------------------------------------------------- #


def test_infinite_inventory_is_preserved_as_unbounded_and_low_risk(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=float("inf"),
        forecast=forecast_of(FLAT),
    )

    assert result.current_inventory == math.inf
    assert result.projected_inventory == math.inf
    assert result.risk == RiskLevel.LOW
    assert result.ticks_until_stockout is None
    assert result.stockout_tick is None
    assert result.shortage_amount == 0.0
    assert result.surplus_amount == math.inf


def test_infinite_inventory_with_incoming_stays_unbounded(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=float("inf"),
        forecast=forecast_of(FLAT),
        incoming=(IncomingSupply(quantity=50.0, arrival_tick=1),),
    )

    assert_well_formed(result)
    assert result.risk == RiskLevel.LOW
    assert result.ticks_until_stockout is None


def test_non_finite_inventory_other_than_positive_infinity_degrades_to_empty(analyzer):
    for value in (float("nan"), float("-inf"), "abc", None):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=value,
            forecast=forecast_of(FLAT),
        )
        assert_well_formed(result)
        assert result.current_inventory == 0.0
        assert result.risk == RiskLevel.CRITICAL


# --------------------------------------------------------------------------- #
# Monotonicity and the "a stockout is never LOW" guarantee
# --------------------------------------------------------------------------- #

MONOTONE_INVENTORIES = [5_000.0, 2_000.0, 1_500.0, 1_200.0, 1_000.0, 900.0,
                        800.0, 700.0, 500.0, 400.0, 300.0, 200.0, 100.0, 50.0, 0.0]


def test_decreasing_inventory_never_lowers_the_risk_level(analyzer):
    forecast = forecast_of(FLAT)
    ranks = [
        level(analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=inventory,
            forecast=forecast,
        ))
        for inventory in MONOTONE_INVENTORIES
    ]

    assert ranks == sorted(ranks), ranks
    assert ranks[0] == LEVEL_ORDER.index(RiskLevel.LOW)
    assert ranks[-1] == LEVEL_ORDER.index(RiskLevel.CRITICAL)


def test_monotonicity_holds_across_a_fine_sweep(analyzer):
    forecast = forecast_of(FLAT)
    ranks = []
    for tenth in range(0, 161):  # 0.0 .. 16.0 in 0.1 steps, falling
        inventory = 1_600.0 - tenth * 10.0
        rank = level(analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=inventory,
            forecast=forecast,
        ))
        ranks.append(rank)

    assert ranks == sorted(ranks), ranks


def test_a_projected_stockout_is_never_low(analyzer):
    forecast = forecast_of(FLAT)
    checked = 0
    for tenth in range(0, 121):
        inventory = 1_200.0 - tenth * 10.0
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=inventory,
            forecast=forecast,
        )
        if result.ticks_until_stockout is not None:
            assert result.risk != RiskLevel.LOW, inventory
            assert level(result) >= LEVEL_ORDER.index(RiskLevel.MEDIUM), inventory
            checked += 1
        if result.shortage_amount > 0.0:
            assert result.risk != RiskLevel.LOW, inventory
            checked += 1

    assert checked > 0, "the sweep never produced a stockout, so it proved nothing"


def test_a_projected_stockout_with_incoming_supply_is_never_low(analyzer):
    forecast = forecast_of(FLAT)
    checked = 0
    for tenth in range(0, 121):
        inventory = 1_200.0 - tenth * 10.0
        for arrival in (1, 4, 8):
            result = analyzer.assess(
                station_id="ST-1",
                fuel_type="DIESEL",
                current_inventory=inventory,
                forecast=forecast,
                incoming=(IncomingSupply(quantity=250.0, arrival_tick=arrival),),
            )
            if result.ticks_until_stockout is not None:
                assert result.risk != RiskLevel.LOW, (inventory, arrival)
                checked += 1

    assert checked > 0


# --------------------------------------------------------------------------- #
# assess_many ordering
# --------------------------------------------------------------------------- #


def test_assess_many_sorts_most_severe_then_station_then_fuel(analyzer):
    forecast = forecast_of(FLAT)
    rows = [
        {"station_id": "ST-D", "fuel_type": "DIESEL", "current_inventory": 5_000.0, "forecast": forecast},
        {"station_id": "ST-C", "fuel_type": "DIESEL", "current_inventory": 900.0, "forecast": forecast},
        {"station_id": "ST-B", "fuel_type": "DIESEL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-A", "fuel_type": "PETROL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-A", "fuel_type": "DIESEL", "current_inventory": 0.0, "forecast": forecast},
        {"station_id": "ST-E", "fuel_type": "DIESEL", "current_inventory": 300.0, "forecast": forecast},
    ]

    result = analyzer.assess_many(rows=rows, current_tick=0)

    assert [(a.station_id, a.fuel_type, a.risk) for a in result] == [
        ("ST-A", "DIESEL", RiskLevel.CRITICAL),
        ("ST-A", "PETROL", RiskLevel.CRITICAL),
        ("ST-B", "DIESEL", RiskLevel.CRITICAL),
        ("ST-E", "DIESEL", RiskLevel.HIGH),
        ("ST-C", "DIESEL", RiskLevel.MEDIUM),
        ("ST-D", "DIESEL", RiskLevel.LOW),
    ]
    ranks = [level(a) for a in result]
    assert ranks == sorted(ranks, reverse=True), ranks


def test_assess_many_handles_empty_and_missing_rows(analyzer):
    assert analyzer.assess_many(rows=[]) == []
    assert analyzer.assess_many(rows=None) == []
    assert analyzer.assess_many(rows=iter([])) == []


def test_assess_many_accepts_rows_without_incoming(analyzer):
    rows = [{"station_id": "ST-A", "fuel_type": "DIESEL", "current_inventory": 100.0,
             "forecast": forecast_of(FLAT)}]
    result = analyzer.assess_many(rows=rows)

    assert len(result) == 1
    assert result[0].incoming_sources == ()


def test_assess_many_honours_current_tick(analyzer):
    rows = [{"station_id": "ST-A", "fuel_type": "DIESEL", "current_inventory": 0.0,
             "forecast": forecast_of(FLAT)}]
    result = analyzer.assess_many(rows=rows, current_tick=909)

    assert result[0].stockout_tick == 909


# --------------------------------------------------------------------------- #
# ``basis`` explains itself
# --------------------------------------------------------------------------- #


def test_basis_is_populated_and_cites_its_numbers(analyzer):
    result = analyzer.assess(
        station_id="ST-7",
        fuel_type="PETROL",
        current_inventory=5_000.0,
        forecast=forecast_of(FLAT, station="ST-7", fuel="PETROL"),
    )

    assert isinstance(result.basis, str)
    assert result.basis.strip()
    assert f"{result.current_inventory:,.0f}" in result.basis
    assert "5,000 L on hand" in result.basis
    assert f"risk {result.risk.value}" in result.basis
    assert "ST-7" in result.basis
    assert "PETROL" in result.basis
    assert "holt_winters" in result.basis


def test_basis_mentions_the_risk_level_for_every_band(analyzer):
    forecast = forecast_of(FLAT)
    for inventory in (5_000.0, 900.0, 300.0, 0.0):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=inventory,
            forecast=forecast,
        )
        assert f"risk {result.risk.value}" in result.basis


def test_basis_is_unbounded_labelled_for_infinite_inventory(analyzer):
    result = analyzer.assess(
        station_id="ST-1",
        fuel_type="DIESEL",
        current_inventory=float("inf"),
        forecast=forecast_of(FLAT),
    )

    assert "unbounded" in result.basis
    assert f"risk {result.risk.value}" in result.basis


# --------------------------------------------------------------------------- #
# Randomised (seeded) sweep — the invariants must hold for any input
# --------------------------------------------------------------------------- #


def test_randomised_inputs_keep_every_invariant(analyzer):
    rng = random.Random(20240929)
    checked = 0

    for _ in range(300):
        horizon = rng.choice([0, 1, 3, 8, 16])
        points = [
            rng.choice([0.0, 5.0, 100.0, 900.0, rng.uniform(0.0, 400.0)])
            for _ in range(rng.choice([0, 1, horizon]))
        ]
        forecast = forecast_of(points)

        incoming = tuple(
            IncomingSupply(
                quantity=rng.choice([0.0, 50.0, 500.0, 5_000.0]),
                arrival_tick=rng.choice(
                    [0, 1, 3, 8, 20, float("inf"), float("-inf"), float("nan")]
                ),
            )
            for _ in range(rng.randint(0, 3))
        )

        inventory = rng.choice([-5.0, 0.0, 10.0, 250.0, 750.0, 5_000.0, 1e9])
        local = StockoutAnalyzer(thresholds=RiskThresholds(horizon_ticks=horizon))
        result = local.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=inventory,
            forecast=forecast,
            incoming=incoming,
            current_tick=rng.choice(
                [0, 5, 1_000, float("inf"), float("-inf"), float("nan")]
            ),
        )

        assert_well_formed(result)
        if result.ticks_until_stockout is not None:
            assert result.risk != RiskLevel.LOW
        # The telescoping identity must survive any of the above.
        assert result.projected_inventory == pytest.approx(
            result.current_inventory + result.incoming_supply - result.expected_demand
        )
        checked += 1

    assert checked == 300


def test_huge_and_tiny_demand_stays_finite(analyzer):
    for magnitude in (1e-9, 1e-3, 1.0, 1e9, 1e12):
        result = analyzer.assess(
            station_id="ST-1",
            fuel_type="DIESEL",
            current_inventory=magnitude * 4.0,
            forecast=forecast_of([magnitude] * HORIZON),
        )
        assert_well_formed(result)
        assert math.isfinite(result.expected_demand)
