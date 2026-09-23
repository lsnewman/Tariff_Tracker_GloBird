"""Constants for Tariff Tracker."""

DOMAIN = "tariff_tracker"

CONF_PLAN_NAME = "plan_name"
CONF_IMPORT_ENERGY_SENSOR = "import_energy_sensor"
CONF_IMPORT_POWER_SENSOR = "import_power_sensor"
CONF_EXPORT_ENERGY_SENSOR = "export_energy_sensor"
CONF_DAILY_CHARGE = "daily_charge"

# Optional GloBird-specific interval-array backfill (see runtime.py
# _handle_interval_event). The entity exposing the interval array, and the
# name of the attribute holding it - the array's sibling date attribute is
# assumed to be named "latest_day" (a GloBird API convention paired with
# this array, not something worth a third config field for a fork-only
# feature).
CONF_INTERVAL_SOURCE_ENTITY = "interval_source_entity"
CONF_INTERVAL_ATTRIBUTE = "interval_attribute"
DEFAULT_INTERVAL_ATTRIBUTE = "latest_intervals"

CONF_BILLING_CYCLE_TYPE = "billing_cycle_type"
CONF_BILLING_CYCLE_DAYS = "billing_cycle_days"
CONF_BILLING_CYCLE_START = "billing_cycle_start"

BILLING_CYCLE_CALENDAR_MONTH = "calendar_month"
BILLING_CYCLE_EVERY_N_DAYS = "every_n_days"

CONF_PERIODS = "periods"
CONF_EXPORT_PERIODS = "export_periods"

# Per-period keys (stored as list of dicts in CONF_PERIODS / CONF_EXPORT_PERIODS)
CONF_PERIOD_NAME = "name"
CONF_PERIOD_START_TIME = "start_time"
CONF_PERIOD_END_TIME = "end_time"
# Optional list of {start_time, end_time} dicts, for a period made of several
# disjoint windows (e.g. a shoulder band split by peak and off-peak blocks).
# When absent the period's own start_time/end_time are its single window, so
# configs written before multi-window support keep working. When present, the
# period's start_time/end_time mirror the first window for display and for
# anything still reading the single-window keys.
CONF_PERIOD_WINDOWS = "windows"
CONF_PERIOD_DAYS = "days"
CONF_PERIOD_TIERS = "tiers"
CONF_PERIOD_BONUS = "bonus"
# How a period's tiers reset. "daily" (default) matches typical retailer
# "first N kWh/day" wording. "billing_period" instead compares TOTAL usage
# for the whole billing period against each tier's daily limit scaled up by
# the number of days in the period - GloBird's actual rule for some plans.
CONF_TIER_RESET_CADENCE = "tier_reset_cadence"
TIER_RESET_DAILY = "daily"
TIER_RESET_BILLING_PERIOD = "billing_period"

# Per-tier keys (list of dicts in CONF_PERIOD_TIERS)
CONF_TIER_LIMIT_KWH = "limit_kwh"
CONF_TIER_RATE = "rate"

# Bonus keys (dict in CONF_PERIOD_BONUS, absent/None if disabled)
CONF_BONUS_AMOUNT = "amount"
CONF_BONUS_THRESHOLD_W = "threshold_w"
CONF_BONUS_CALC_MODE = "calc_mode"
# Optional sub-window within the enclosing period that the bonus is actually
# evaluated over (e.g. a 6pm-9pm bonus window inside a 4pm-11pm peak period).
# Falls back to the enclosing period's own start/end when absent.
CONF_BONUS_START_TIME = "bonus_start_time"
CONF_BONUS_END_TIME = "bonus_end_time"

BONUS_CALC_ENERGY_DELTA = "energy_delta"
BONUS_CALC_LIVE_POWER = "live_power_sensor"

DAYS_ALL = "all"
DAYS_WEEKDAYS = "weekdays"
DAYS_WEEKENDS = "weekends"
DAYS_CUSTOM = "custom"

DEFAULT_DAILY_CHARGE = 0.0
