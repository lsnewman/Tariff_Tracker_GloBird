from datetime import date, datetime

import tariff_engine as engine

OFF_PEAK = {
    "name": "Off-peak",
    "start_time": "21:00",
    "end_time": "07:00",
    "days": "all",
    "tiers": [
        {"limit_kwh": 50, "rate": 0.0},
        {"limit_kwh": None, "rate": 0.275},
    ],
}

PEAK_WEEKDAYS = {
    "name": "Peak",
    "start_time": "18:00",
    "end_time": "21:00",
    "days": "weekdays",
    "tiers": [{"limit_kwh": None, "rate": 0.44}],
}


def test_overnight_window_wraps_midnight():
    assert engine.period_contains_time(OFF_PEAK, datetime(2026, 7, 6, 23, 0))
    assert engine.period_contains_time(OFF_PEAK, datetime(2026, 7, 6, 3, 0))
    assert not engine.period_contains_time(OFF_PEAK, datetime(2026, 7, 6, 12, 0))


def test_weekday_only_period_excludes_weekend():
    # 2026-07-06 is a Monday.
    assert engine.period_contains_time(PEAK_WEEKDAYS, datetime(2026, 7, 6, 19, 0))
    # 2026-07-11 is a Saturday.
    assert not engine.period_contains_time(PEAK_WEEKDAYS, datetime(2026, 7, 11, 19, 0))


def test_find_active_period_first_match_wins():
    periods = [OFF_PEAK, PEAK_WEEKDAYS]
    assert engine.find_active_period(periods, datetime(2026, 7, 6, 19, 0))["name"] == "Peak"
    assert engine.find_active_period(periods, datetime(2026, 7, 6, 23, 0))["name"] == "Off-peak"
    assert engine.find_active_period(periods, datetime(2026, 7, 6, 12, 0)) is None


def test_tier_rate_for_usage_picks_correct_bracket():
    tiers = OFF_PEAK["tiers"]
    assert engine.tier_rate_for_usage(tiers, 0) == 0.0
    assert engine.tier_rate_for_usage(tiers, 49.9) == 0.0
    assert engine.tier_rate_for_usage(tiers, 50) == 0.275
    assert engine.tier_rate_for_usage(tiers, 100) == 0.275


def test_cost_of_delta_splits_across_tier_boundary():
    tiers = OFF_PEAK["tiers"]
    # Already used 48 kWh today at the free tier; import 4 more kWh, so
    # 2 kWh finish the free tier and 2 kWh land in the 0.275 balance tier.
    cost = engine.cost_of_delta(OFF_PEAK, kwh_already_used_in_period_today=48, delta_kwh=4)
    assert round(cost, 4) == round(2 * 0.275, 4)


def test_cost_of_delta_flat_rate_period():
    cost = engine.cost_of_delta(PEAK_WEEKDAYS, kwh_already_used_in_period_today=0, delta_kwh=2)
    assert round(cost, 4) == round(2 * 0.44, 4)


def test_billing_period_bounds_calendar_month():
    start, end = engine.billing_period_bounds("calendar_month", None, None, date(2026, 7, 15))
    assert start == date(2026, 7, 1)
    assert end == date(2026, 8, 1)


def test_billing_period_bounds_every_n_days():
    anchor = date(2026, 1, 1)
    start, end = engine.billing_period_bounds("every_n_days", 28, anchor, date(2026, 7, 6))
    # 186 days after anchor -> 6 full 28-day cycles have elapsed (168 days).
    assert start == date(2026, 6, 18)
    assert end == date(2026, 7, 16)


def test_avg_watts_from_energy():
    # 0.18 kWh imported over a 3 hour window == 60W average.
    assert round(engine.avg_watts_from_energy(0.18, 3), 1) == 60.0
    assert engine.avg_watts_from_energy(0, 3) == 0.0
    assert engine.avg_watts_from_energy(1, 0) == 0.0


# Export/feed-in periods use the exact same period/tier machinery as import
# periods - the direction (charge vs credit) is applied by the caller
# (runtime.py subtracts instead of adds), not by tariff_engine itself.
SOLAR_FEED_IN = {
    "name": "Feed-in",
    "start_time": "00:00",
    "end_time": "23:59:59",
    "days": "all",
    "tiers": [
        {"limit_kwh": 5, "rate": 0.10},
        {"limit_kwh": None, "rate": 0.05},
    ],
}


def test_export_tier_rate_drops_after_first_tier():
    tiers = SOLAR_FEED_IN["tiers"]
    assert engine.tier_rate_for_usage(tiers, 0) == 0.10
    assert engine.tier_rate_for_usage(tiers, 4.99) == 0.10
    assert engine.tier_rate_for_usage(tiers, 5) == 0.05


def test_export_cost_of_delta_splits_across_tier_boundary():
    # 3 kWh already exported today; export 4 more, so 2 kWh finish the
    # premium first tier and 2 kWh land in the lower balance tier.
    credit = engine.cost_of_delta(SOLAR_FEED_IN, kwh_already_used_in_period_today=3, delta_kwh=4)
    assert round(credit, 4) == round(2 * 0.10 + 2 * 0.05, 4)


# billing_period tier reset cadence: tier limits are a *daily* allowance,
# but a billing_period-cadence period compares TOTAL usage across the whole
# billing period against that allowance scaled up to the period's length.
DAILY_TIERS = [
    {"limit_kwh": 15, "rate": 0.19333},
    {"limit_kwh": None, "rate": 0.22468},
]


def test_scale_tiers_for_billing_period_scales_bounded_tiers_only():
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 28)
    assert scaled[0]["limit_kwh"] == 15 * 28
    assert scaled[0]["rate"] == 0.19333
    assert scaled[1]["limit_kwh"] is None
    assert scaled[1]["rate"] == 0.22468
    # Original tiers untouched.
    assert DAILY_TIERS[0]["limit_kwh"] == 15


def test_scale_tiers_for_billing_period_single_day():
    # days_in_period=1 is a no-op - the billing_period-cadence math should
    # degrade to the same thresholds as daily cadence.
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 1)
    assert scaled[0]["limit_kwh"] == 15


def test_scale_tiers_for_billing_period_long_cycle():
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 90)
    assert scaled[0]["limit_kwh"] == 1350


def test_tier_rate_for_usage_with_billing_period_scaled_limits_below_threshold():
    # 28-day billing period, scaled first-tier limit = 420 kWh. 400 kWh used
    # so far this period is still inside the scaled first tier.
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 28)
    assert engine.tier_rate_for_usage(scaled, 400) == 0.19333


def test_tier_rate_for_usage_with_billing_period_scaled_limits_above_threshold():
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 28)
    assert engine.tier_rate_for_usage(scaled, 450) == 0.22468


def test_cost_of_delta_with_billing_period_scaled_limits_straddling_boundary():
    # Scaled first-tier limit = 420 kWh. 418 kWh already used this billing
    # period; import 4 more, so 2 kWh finish the first tier and 2 kWh land
    # in the balance tier.
    scaled = engine.scale_tiers_for_billing_period(DAILY_TIERS, 28)
    period = {**OFF_PEAK, "tiers": scaled}
    cost = engine.cost_of_delta(period, kwh_already_used_in_period_today=418, delta_kwh=4)
    assert round(cost, 4) == round(2 * 0.19333 + 2 * 0.22468, 4)
