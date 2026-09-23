"""Per-config-entry runtime: listens to the source energy/power sensors and
maintains cost + bonus state. Shared by every entity of one plan so there is
a single listener per source sensor, not one per entity.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_change,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

try:
    from homeassistant.components.recorder.models import (
        StatisticData,
        StatisticMeanType,
        StatisticMetaData,
    )
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.statistics import (
        async_add_external_statistics,
        get_last_statistics,
    )

    _HAS_RECORDER_STATISTICS = True
except ImportError:  # recorder not available (e.g. running tests standalone)
    _HAS_RECORDER_STATISTICS = False

from . import tariff_engine as engine
from .const import (
    BONUS_CALC_LIVE_POWER,
    CONF_BILLING_CYCLE_DAYS,
    CONF_BILLING_CYCLE_START,
    CONF_BILLING_CYCLE_TYPE,
    CONF_BONUS_END_TIME,
    CONF_BONUS_START_TIME,
    CONF_BONUS_THRESHOLD_W,
    CONF_DAILY_CHARGE,
    CONF_EXPORT_ENERGY_SENSOR,
    CONF_EXPORT_PERIODS,
    CONF_IMPORT_ENERGY_SENSOR,
    CONF_IMPORT_POWER_SENSOR,
    CONF_INTERVAL_ATTRIBUTE,
    CONF_INTERVAL_SOURCE_ENTITY,
    CONF_PERIOD_BONUS,
    CONF_PERIOD_DAYS,
    CONF_PERIOD_END_TIME,
    CONF_PERIOD_NAME,
    CONF_PERIOD_START_TIME,
    CONF_PERIOD_TIERS,
    CONF_PERIODS,
    CONF_SMOOTH_DASHBOARD_HISTORY,
    CONF_TIER_RESET_CADENCE,
    BILLING_CYCLE_CALENDAR_MONTH,
    DAYS_ALL,
    DEFAULT_INTERVAL_ATTRIBUTE,
    DOMAIN,
    TIER_RESET_BILLING_PERIOD,
    TIER_RESET_DAILY,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9_]+")
_REPEATED_UNDERSCORES = re.compile(r"_+")


def _slugify(value: str) -> str:
    """Lowercase, safe-charset slug for a recorder statistic_id (domain:slug).

    The recorder's statistic_id validator rejects doubled, leading or
    trailing underscores, so those are collapsed/stripped after the
    charset substitution.
    """
    slug = _SLUG_INVALID_CHARS.sub("_", value.lower())
    slug = _REPEATED_UNDERSCORES.sub("_", slug).strip("_")
    return slug or "plan"


def _ledger_key_before(key: str, cutoff: datetime) -> bool:
    """Whether an interval_ledger ISO-timestamp key is older than `cutoff`."""
    parsed = dt_util.parse_datetime(key)
    if parsed is None:
        return True  # unparsable entries are safe to drop
    return dt_util.as_local(parsed) < cutoff


@dataclass
class BonusWindowSample:
    """Accumulator for live-power-sensor bonus averaging within one window."""

    watt_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    last_time: datetime | None = None
    last_watts: float = 0.0

    def sample(self, now: datetime, watts: float) -> None:
        if self.last_time is not None:
            dt = (now - self.last_time).total_seconds()
            self.watt_seconds += self.last_watts * dt
            self.elapsed_seconds += dt
        self.last_time = now
        self.last_watts = watts

    def average(self) -> float:
        if self.elapsed_seconds <= 0:
            return self.last_watts
        return self.watt_seconds / self.elapsed_seconds

    def reset(self) -> None:
        self.watt_seconds = 0.0
        self.elapsed_seconds = 0.0
        self.last_time = None
        self.last_watts = 0.0


@dataclass
class PlanRuntime:
    """Live state for one configured tariff plan."""

    hass: HomeAssistant
    entry_id: str
    plan_name: str
    options: dict[str, Any]

    # The source sensors' actual unit_of_measurement, read once at setup so
    # entities/labels display the right unit even when a plan is pointed at
    # a non-electricity sensor (e.g. a gas sensor in MJ). Not persisted -
    # cheap to recompute from hass.states on every setup, and doing so keeps
    # it in sync with the source sensor's currently-configured unit rather
    # than whatever it happened to be the first time the entry was set up.
    import_energy_unit: str = "kWh"
    export_energy_unit: str = "kWh"

    last_energy_kwh: float | None = None
    # When last_energy_kwh was last updated - used only by
    # CONF_SMOOTH_DASHBOARD_HISTORY to know how many days a delta spans.
    last_energy_reading_at: datetime | None = None
    tier_usage_today: dict[str, float] = field(default_factory=dict)
    energy_by_period_today: dict[str, float] = field(default_factory=dict)
    bonus_earned_today: dict[str, bool | None] = field(default_factory=dict)
    bonus_samples: dict[str, BonusWindowSample] = field(default_factory=dict)
    # ISO date string per period name, recording the last day that period's
    # bonus was finalized. Two jobs: stop the finalizer crediting the same
    # day twice (a DST fall-back repeats the hour its callback is scheduled
    # in), and let setup settle a bonus whose end time passed while Home
    # Assistant was down.
    bonus_finalized_for: dict[str, str] = field(default_factory=dict)

    last_export_kwh: float | None = None
    export_tier_usage_today: dict[str, float] = field(default_factory=dict)

    # Independent running total of kWh used in each period today. Separate
    # from energy_by_period_today (which the avg-watts calc owns) so this
    # can't interfere with that calc; resets at midnight, not at the
    # period's own end time, so it holds its value for the rest of the day
    # once the window closes.
    period_energy_kwh_today: dict[str, float] = field(default_factory=dict)
    # Running total kWh used in each period across the current billing
    # period. Resets only when the billing period itself rolls over (see
    # _recompute_billing_bounds), unlike period_energy_kwh_today which
    # resets nightly.
    period_energy_kwh_billing_period: dict[str, float] = field(default_factory=dict)
    export_period_energy_kwh_billing_period: dict[str, float] = field(default_factory=dict)
    # All-time running total kWh per period. Never reset by any rollover or
    # manual reset - a lifetime counter.
    period_energy_kwh_total: dict[str, float] = field(default_factory=dict)
    export_period_energy_kwh_total: dict[str, float] = field(default_factory=dict)

    # GloBird interval-array backfill (see _handle_interval_event). Keyed by
    # ISO timestamp string of a half-hour slot's start -> the last raw slot
    # value seen for it, so a re-delivered slot with an unchanged value is a
    # no-op and a revised value applies only the difference, never the full
    # value again. Pruned to entries from the current billing period onward.
    interval_ledger: dict[str, float] = field(default_factory=dict)
    # Full-day tier cost already applied for a backfilled/corrected PAST day,
    # keyed by "{day_iso}:{period_name}" -> {hour_iso: cost applied in that
    # hour}, last time this day was replayed. A re-replay of the same day
    # (e.g. GloBird revises one slot) only needs to apply the per-hour
    # difference between the new and cached breakdown, not double-count the
    # whole day - this also gives _push_external_cost_statistics an hourly
    # cost delta to push, the same way interval_ledger does for energy.
    # Only meaningful for daily-cadence periods - see _apply_interval_slots.
    daily_cost_replay_cache: dict[str, dict[str, float]] = field(default_factory=dict)
    # Running cumulative kWh/$ already pushed to the recorder's external
    # statistics for this plan (see _push_external_statistics /
    # _push_external_cost_statistics). "sum" in HA's statistics API is a
    # cumulative running total, not a per-row delta, so these track that
    # running total independently of any of the live-scoped
    # period_energy_kwh_*/cost_* counters above.
    external_stat_cumulative_kwh: float = 0.0
    external_stat_cumulative_cost: float = 0.0

    cost_today: float = 0.0
    cost_month: float = 0.0
    cost_billing_period: float = 0.0
    bonus_savings_billing_period: float = 0.0
    # Number of days in the current billing period whose bonus was earned.
    # Incremented by the finalizer, so it counts settled days directly
    # rather than inferring them from binary sensor state changes - a
    # history_stats count over the bonus binary sensor double-counts any
    # day where the integration is reloaded after the bonus is settled,
    # because re-adding the entity records a second entry into "on".
    bonus_days_earned_billing_period: int = 0

    export_credit_today: float = 0.0
    export_credit_month: float = 0.0
    export_credit_billing_period: float = 0.0

    today: date = field(default_factory=lambda: dt_util.now().date())
    billing_period_start: date | None = None
    billing_period_end: date | None = None

    listeners: list[Callable[[], None]] = field(default_factory=list)
    update_callbacks: list[Callable[[], None]] = field(default_factory=list)
    _store: Store | None = field(default=None, repr=False)
    _unsub_source: Callable[[], None] | None = field(default=None, repr=False)
    _unsub_power: Callable[[], None] | None = field(default=None, repr=False)
    _unsub_export: Callable[[], None] | None = field(default=None, repr=False)
    _unsub_intervals: Callable[[], None] | None = field(default=None, repr=False)

    @property
    def periods(self) -> list[dict[str, Any]]:
        return self.options.get(CONF_PERIODS, [])

    @property
    def export_periods(self) -> list[dict[str, Any]]:
        return self.options.get(CONF_EXPORT_PERIODS, [])

    @property
    def daily_charge(self) -> float:
        return self.options.get(CONF_DAILY_CHARGE, 0.0)

    def current_period(self) -> dict[str, Any] | None:
        return engine.find_active_period(self.periods, dt_util.now())

    def current_rate(self) -> float | None:
        period = self.current_period()
        if period is None:
            return None
        used_so_far, tiers = self._tier_usage_input(period)
        return engine.tier_rate_for_usage(tiers, used_so_far)

    def _tier_usage_input(
        self, period: dict[str, Any]
    ) -> tuple[float, list[dict[str, Any]]]:
        """Return (usage_so_far, tiers) to feed tier_rate_for_usage/cost_of_delta,
        honoring the period's tier_reset_cadence.

        A "daily" period (the default) compares today's usage against the
        tiers as configured. A "billing_period" period instead compares
        TOTAL usage across the whole billing period against the tiers
        scaled up by the number of days in that period - see
        engine.scale_tiers_for_billing_period. tier_usage_today keeps being
        bumped unconditionally in _apply_delta regardless of cadence; it is
        simply not read here for billing_period-cadence periods.
        """
        name = period[CONF_PERIOD_NAME]
        tiers = period.get(CONF_PERIOD_TIERS, [])
        cadence = period.get(CONF_TIER_RESET_CADENCE, TIER_RESET_DAILY)
        if cadence == TIER_RESET_BILLING_PERIOD:
            days = 1
            if self.billing_period_start and self.billing_period_end:
                days = max((self.billing_period_end - self.billing_period_start).days, 1)
            return (
                self.period_energy_kwh_billing_period.get(name, 0.0),
                engine.scale_tiers_for_billing_period(tiers, days),
            )
        return self.tier_usage_today.get(name, 0.0), tiers

    def current_period_avg_watts(self, period_name: str) -> float | None:
        """Average import power today for one period.

        Today's energy for the period over the hours its windows have been
        open today. Both sides cover the whole of today, so the figure is
        stable once a window closes and needs no frozen snapshot - and a
        restart cannot distort it, because neither side depends on having
        watched the window open.
        """
        period = next(
            (p for p in self.periods if p[CONF_PERIOD_NAME] == period_name), None
        )
        if period is None:
            return None
        elapsed_hours = engine.period_elapsed_hours_today(period, dt_util.now())
        if elapsed_hours <= 0:
            return None
        return engine.avg_watts_from_energy(
            self.period_energy_kwh_today.get(period_name, 0.0), elapsed_hours
        )

    def current_export_period(self) -> dict[str, Any] | None:
        return engine.find_active_period(self.export_periods, dt_util.now())

    def current_export_rate(self) -> float | None:
        period = self.current_export_period()
        if period is None:
            return None
        used_today = self.export_tier_usage_today.get(period[CONF_PERIOD_NAME], 0.0)
        return engine.tier_rate_for_usage(period[CONF_PERIOD_TIERS], used_today)

    def days_remaining(self) -> int | None:
        if self.billing_period_end is None:
            return None
        return (self.billing_period_end - dt_util.now().date()).days

    @property
    def has_bonus(self) -> bool:
        return any(p.get(CONF_PERIOD_BONUS) for p in self.periods)

    def bonus_days_elapsed(self) -> int | None:
        """Whole days of the billing period that have finished.

        Today is excluded: its bonus windows may not have closed yet, so
        counting it would drag the success rate down for most of the day.
        """
        if self.billing_period_start is None:
            return None
        return max((dt_util.now().date() - self.billing_period_start).days, 0)

    def bonus_day_percentage(self) -> float | None:
        elapsed = self.bonus_days_elapsed()
        if not elapsed:
            return None
        return round(self.bonus_days_earned_billing_period / elapsed * 100, 1)

    def register_update_callback(self, cb: Callable[[], None]) -> Callable[[], None]:
        self.update_callbacks.append(cb)

        def _remove() -> None:
            self.update_callbacks.remove(cb)

        return _remove

    def _notify(self) -> None:
        for cb in self.update_callbacks:
            cb()

    @property
    def _external_stats_enabled(self) -> bool:
        """Whether this plan pushes to the Energy Dashboard's external
        statistics at all - either the real GloBird interval array, or the
        opt-in day-smoothing toggle for sparse sources like gas."""
        return bool(
            self.options.get(CONF_INTERVAL_SOURCE_ENTITY)
            or self.options.get(CONF_SMOOTH_DASHBOARD_HISTORY)
        )

    async def async_setup(self) -> None:
        self._store = Store(
            self.hass, STORAGE_VERSION, f"{DOMAIN}_{self.entry_id}"
        )
        saved = await self._store.async_load()
        if saved:
            self._restore(saved)

        now = dt_util.now()
        today = now.date()
        # Whether this setup is resuming a day already in progress. A first
        # ever run has no history to settle, and a run on a later day has
        # had its inputs cleared by _recompute_daily_bounds below - in
        # either case a bonus catch-up would credit against zero usage and
        # hand out a bonus that was never earned.
        resuming_same_day = bool(saved) and self.today == today

        self._recompute_billing_bounds(today)
        self._recompute_month_bounds(today)
        self._recompute_daily_bounds(today)
        if resuming_same_day and self._catch_up_bonuses(now):
            await self._async_save()

        energy_sensor = self.options[CONF_IMPORT_ENERGY_SENSOR]
        energy_state = self.hass.states.get(energy_sensor)
        if energy_state and energy_state.attributes.get("unit_of_measurement"):
            self.import_energy_unit = energy_state.attributes["unit_of_measurement"]
        self._unsub_source = async_track_state_change_event(
            self.hass, [energy_sensor], self._handle_energy_event
        )

        power_sensor = self.options.get(CONF_IMPORT_POWER_SENSOR)
        if power_sensor:
            self._unsub_power = async_track_state_change_event(
                self.hass, [power_sensor], self._handle_power_event
            )

        export_sensor = self.options.get(CONF_EXPORT_ENERGY_SENSOR)
        if export_sensor:
            export_state = self.hass.states.get(export_sensor)
            if export_state and export_state.attributes.get("unit_of_measurement"):
                self.export_energy_unit = export_state.attributes["unit_of_measurement"]
            self._unsub_export = async_track_state_change_event(
                self.hass, [export_sensor], self._handle_export_energy_event
            )

        interval_entity = self.options.get(CONF_INTERVAL_SOURCE_ENTITY)
        if interval_entity:
            self._unsub_intervals = async_track_state_change_event(
                self.hass, [interval_entity], self._handle_interval_event
            )
        if self._external_stats_enabled:
            await self._resync_external_stat_baselines()

        # Midnight rollover: reset daily/period accumulators, apply daily charge.
        self.listeners.append(
            async_track_time_change(
                self.hass, self._handle_midnight, hour=0, minute=0, second=0
            )
        )

        # Finalize bonus windows at the bonus's own end time if it defines a
        # sub-window narrower than the enclosing period, else the period's end time.
        for period in self.periods:
            bonus = period.get(CONF_PERIOD_BONUS)
            if not bonus:
                continue
            end_time = engine.parse_time(
                bonus.get(CONF_BONUS_END_TIME) or period[CONF_PERIOD_END_TIME]
            )
            self.listeners.append(
                async_track_time_change(
                    self.hass,
                    self._make_bonus_finalizer(period[CONF_PERIOD_NAME]),
                    hour=end_time.hour,
                    minute=end_time.minute,
                    second=end_time.second,
                )
            )

        # Mark window-open time and finalize avg import power for every
        # period. A period can be made of several disjoint windows, so this
        # registers one marker/finalizer pair per window, not per period.
        for period in self.periods:
            name = period[CONF_PERIOD_NAME]
            for _start_time, end_time in engine.period_windows(period):
                self.listeners.append(
                    async_track_time_change(
                        self.hass,
                        self._make_period_avg_finalizer(name),
                        hour=end_time.hour,
                        minute=end_time.minute,
                        second=end_time.second,
                    )
                )

    def async_unload(self) -> None:
        if self._unsub_source:
            self._unsub_source()
        if self._unsub_power:
            self._unsub_power()
        if self._unsub_export:
            self._unsub_export()
        if self._unsub_intervals:
            self._unsub_intervals()
        for unsub in self.listeners:
            unsub()

    # ---- persistence -----------------------------------------------------

    def _restore(self, saved: dict[str, Any]) -> None:
        self.last_energy_kwh = saved.get("last_energy_kwh")
        if saved.get("last_energy_reading_at"):
            self.last_energy_reading_at = dt_util.parse_datetime(
                saved["last_energy_reading_at"]
            )
        self.tier_usage_today = saved.get("tier_usage_today", {})
        self.energy_by_period_today = saved.get("energy_by_period_today", {})
        self.bonus_earned_today = saved.get("bonus_earned_today", {})
        self.bonus_finalized_for = saved.get("bonus_finalized_for", {})
        self.cost_today = saved.get("cost_today", 0.0)
        self.cost_month = saved.get("cost_month", 0.0)
        self.cost_billing_period = saved.get("cost_billing_period", 0.0)
        self.bonus_savings_billing_period = saved.get("bonus_savings_billing_period", 0.0)
        self.bonus_days_earned_billing_period = saved.get(
            "bonus_days_earned_billing_period", 0
        )
        self.last_export_kwh = saved.get("last_export_kwh")
        self.export_tier_usage_today = saved.get("export_tier_usage_today", {})
        self.export_credit_today = saved.get("export_credit_today", 0.0)
        self.export_credit_month = saved.get("export_credit_month", 0.0)
        self.export_credit_billing_period = saved.get("export_credit_billing_period", 0.0)
        self.period_energy_kwh_today = saved.get("period_energy_kwh_today", {})
        self.period_energy_kwh_billing_period = saved.get(
            "period_energy_kwh_billing_period", {}
        )
        self.export_period_energy_kwh_billing_period = saved.get(
            "export_period_energy_kwh_billing_period", {}
        )
        self.period_energy_kwh_total = saved.get("period_energy_kwh_total", {})
        self.export_period_energy_kwh_total = saved.get(
            "export_period_energy_kwh_total", {}
        )
        self.interval_ledger = saved.get("interval_ledger", {})
        # Upgrade path: daily_cost_replay_cache changed shape from a flat
        # {cache_key: total_day_cost} to {cache_key: {hour_iso: cost}} when
        # the per-hour cost statistic was added - drop any float-shaped
        # legacy entries rather than let them crash the dict-based replay
        # logic. Only ever held same-day-or-later-billing-period entries,
        # so simply losing a couple of cached diffs just costs one harmless
        # extra replay next time that day's data changes.
        self.daily_cost_replay_cache = {
            k: v
            for k, v in saved.get("daily_cost_replay_cache", {}).items()
            if isinstance(v, dict)
        }
        self.external_stat_cumulative_kwh = saved.get("external_stat_cumulative_kwh", 0.0)
        self.external_stat_cumulative_cost = saved.get("external_stat_cumulative_cost", 0.0)
        if saved.get("today"):
            self.today = date.fromisoformat(saved["today"])
        if saved.get("billing_period_start"):
            self.billing_period_start = date.fromisoformat(saved["billing_period_start"])
        if saved.get("billing_period_end"):
            self.billing_period_end = date.fromisoformat(saved["billing_period_end"])

        # Upgrade path: installs from before bonus_finalized_for existed have
        # no record of which bonuses already settled today, and the setup
        # catch-up would credit them a second time. The old code set
        # bonus_earned_today[name] at the moment it finalized, so treat any
        # entry there as already settled for the stored day.
        if "bonus_finalized_for" not in saved and self.bonus_earned_today:
            today_iso = self.today.isoformat()
            self.bonus_finalized_for = {
                name: today_iso for name in self.bonus_earned_today
            }

    async def _async_save(self) -> None:
        if not self._store:
            return
        await self._store.async_save(
            {
                "last_energy_kwh": self.last_energy_kwh,
                "last_energy_reading_at": (
                    self.last_energy_reading_at.isoformat()
                    if self.last_energy_reading_at
                    else None
                ),
                "tier_usage_today": self.tier_usage_today,
                "energy_by_period_today": self.energy_by_period_today,
                "bonus_earned_today": self.bonus_earned_today,
                "bonus_finalized_for": self.bonus_finalized_for,
                "cost_today": self.cost_today,
                "cost_month": self.cost_month,
                "cost_billing_period": self.cost_billing_period,
                "bonus_savings_billing_period": self.bonus_savings_billing_period,
                "bonus_days_earned_billing_period": self.bonus_days_earned_billing_period,
                "last_export_kwh": self.last_export_kwh,
                "export_tier_usage_today": self.export_tier_usage_today,
                "export_credit_today": self.export_credit_today,
                "export_credit_month": self.export_credit_month,
                "export_credit_billing_period": self.export_credit_billing_period,
                "period_energy_kwh_today": self.period_energy_kwh_today,
                "period_energy_kwh_billing_period": self.period_energy_kwh_billing_period,
                "export_period_energy_kwh_billing_period": self.export_period_energy_kwh_billing_period,
                "period_energy_kwh_total": self.period_energy_kwh_total,
                "export_period_energy_kwh_total": self.export_period_energy_kwh_total,
                "interval_ledger": self.interval_ledger,
                "daily_cost_replay_cache": self.daily_cost_replay_cache,
                "external_stat_cumulative_kwh": self.external_stat_cumulative_kwh,
                "external_stat_cumulative_cost": self.external_stat_cumulative_cost,
                "today": self.today.isoformat(),
                "billing_period_start": (
                    self.billing_period_start.isoformat()
                    if self.billing_period_start
                    else None
                ),
                "billing_period_end": (
                    self.billing_period_end.isoformat()
                    if self.billing_period_end
                    else None
                ),
            }
        )

    # ---- manual reset ------------------------------------------------

    async def async_reset_costs(
        self,
        *,
        reset_today: bool = True,
        reset_month: bool = True,
        reset_billing_period: bool = True,
        reset_power_tracking: bool = True,
        reset_tier_usage: bool = False,
    ) -> None:
        """Zero accumulated cost/credit/power counters. Never touches the
        configured billing period dates or time-of-use period definitions.
        """
        if reset_today:
            self.cost_today = 0.0
            self.export_credit_today = 0.0
        if reset_month:
            self.cost_month = 0.0
            self.export_credit_month = 0.0
        if reset_billing_period:
            self.cost_billing_period = 0.0
            self.bonus_savings_billing_period = 0.0
            self.bonus_days_earned_billing_period = 0
            self.export_credit_billing_period = 0.0
            self.period_energy_kwh_billing_period = {}
            self.export_period_energy_kwh_billing_period = {}
            # Stale otherwise: a subsequent interval-array delivery would
            # diff against a cached full-day cost computed before this
            # reset, applying only the (near-zero) difference instead of
            # the day's real cost into the now-zeroed cost_billing_period.
            # interval_ledger itself holds raw kWh values, not cost, so it
            # stays valid and is not cleared here.
            self.daily_cost_replay_cache = {}
        if reset_power_tracking:
            self.energy_by_period_today = {}
            self.period_energy_kwh_today = {}
            for sample in self.bonus_samples.values():
                sample.reset()
        if reset_tier_usage:
            self.tier_usage_today = {}
            self.export_tier_usage_today = {}

        await self._async_save()
        self._notify()

    async def async_reset_interval_backfill(self) -> None:
        """Testing/debug aid: forget every GloBird interval slot seen so
        far, so the next interval-array update reprocesses everything it
        currently sees as brand new - lets you watch the backfill mechanism
        run again without waiting for GloBird to publish genuinely new or
        revised data.

        A raw interval_ledger wipe on its own would double-count: every
        currently-seen slot would be re-applied on top of totals that
        already include it once. This bundles the wipe with the same full
        reset async_reset_costs already performs, so the plan lands back in
        a clean, internally-consistent state - equivalent to how it looked
        the moment the interval source was first configured, not "twice as
        much energy/cost as before". async_reset_costs deliberately never
        touches period_energy_kwh_total/export_period_energy_kwh_total (by
        design, for its normal callers - they're genuine lifetime counters)
        so those are zeroed directly here instead.

        external_stat_cumulative_kwh/cost are deliberately left untouched:
        they back the Energy Dashboard's external statistic, whose "sum" is
        contractually a lifetime-growing total (HA has no reset-detection
        for externally-pushed statistics, unlike a total_increasing sensor).
        Zeroing them here would make the next push describe a huge, fake
        negative delta to the Dashboard for whatever hour the button was
        pressed in - a real incident that happened during development and
        left a permanent (manually corrected) dip in the recorded history.

        Today's own daily charge is also re-applied immediately afterward.
        async_reset_costs zeroes cost_today/month/billing_period, but
        today's charge was already added once by the midnight tick that
        started today - without re-adding it here, today would look
        charge-free until a future midnight happens to pass, rather than
        reflecting that the charge already genuinely applies to today.

        Also doubles as the reset for CONF_SMOOTH_DASHBOARD_HISTORY plans
        (no interval_ledger of their own): last_energy_reading_at is
        cleared so the next cumulative-sensor update is treated as a fresh
        first reading - no smoothing until the one after that, mirroring
        how the interval ledger starts empty again above.
        """
        self.interval_ledger = {}
        self.daily_cost_replay_cache = {}
        self.last_energy_reading_at = None
        self.period_energy_kwh_total = {}
        self.export_period_energy_kwh_total = {}
        await self.async_reset_costs(
            reset_today=True,
            reset_month=True,
            reset_billing_period=True,
            reset_power_tracking=True,
            reset_tier_usage=True,
        )
        self.cost_today = self.daily_charge
        self.cost_month += self.daily_charge
        self.cost_billing_period += self.daily_charge
        self._push_daily_charge_cost_statistic(self.today)
        await self._async_save()
        self._notify()

    # ---- billing period bookkeeping --------------------------------------

    def _recompute_billing_bounds(self, today: date) -> None:
        cycle_type = self.options.get(
            CONF_BILLING_CYCLE_TYPE, BILLING_CYCLE_CALENDAR_MONTH
        )
        cycle_days = self.options.get(CONF_BILLING_CYCLE_DAYS)
        cycle_start_raw = self.options.get(CONF_BILLING_CYCLE_START)
        cycle_start = (
            date.fromisoformat(cycle_start_raw) if cycle_start_raw else None
        )
        start, end = engine.billing_period_bounds(
            cycle_type, cycle_days, cycle_start, today
        )

        # Only wipe the running totals when the billing period has genuinely
        # elapsed - i.e. today has reached the end of the period we were
        # last tracking. Testing "did the computed start change?" instead
        # conflates a real rollover with an edit to the cycle config, and
        # editing the cycle start by even one day in the options flow
        # reloads the entry, recomputes a different start, and silently
        # zeroes a whole period's accumulated cost and bonuses.
        #
        # Anything else (a config edit mid-period, a restart, a reload)
        # re-anchors the dates and keeps the money. A first run has no
        # stored end, and its counters are zero already.
        rolled_over = self.billing_period_end is not None and today >= self.billing_period_end

        if self.billing_period_start != start or self.billing_period_end != end:
            _LOGGER.debug(
                "%s: billing period bounds %s..%s -> %s..%s (%s)",
                self.plan_name,
                self.billing_period_start,
                self.billing_period_end,
                start,
                end,
                "rolled over, counters reset" if rolled_over else "re-anchored, counters kept",
            )

        self.billing_period_start = start
        self.billing_period_end = end

        if rolled_over:
            self.cost_billing_period = 0.0
            self.bonus_savings_billing_period = 0.0
            self.bonus_days_earned_billing_period = 0
            self.export_credit_billing_period = 0.0
            self.period_energy_kwh_billing_period = {}
            self.export_period_energy_kwh_billing_period = {}

    def _recompute_month_bounds(self, today: date) -> None:
        """Self-correct cost_month/export_credit_month on setup if the last
        known day is in a different calendar month than now.

        _handle_midnight is the only other place these reset, and it only
        fires from a callback scheduled for exactly 00:00:00 - if Home
        Assistant is restarting/reloading right at that moment, that
        specific tick never runs, and last month's cost would otherwise
        stay stuck inside this month's total until the following midnight
        happens to land cleanly. billing_period_start already gets this
        same self-correction in _recompute_billing_bounds above; this
        mirrors it for the calendar-month counters.
        """
        if (self.today.year, self.today.month) != (today.year, today.month):
            self.cost_month = 0.0
            self.export_credit_month = 0.0

    def _recompute_daily_bounds(self, today: date) -> None:
        """Self-correct the day-scoped counters on setup if the last known
        day is earlier than today.

        _handle_midnight is the only other place these reset, and it only
        fires from a callback scheduled for exactly 00:00:00 - if Home
        Assistant is restarting/reloading right at that moment, that
        specific tick never runs, and yesterday's values (including its
        peak `_energy_today` total) stay stuck in today's counters until
        the following midnight happens to land cleanly. cost_this_month
        and the billing period bounds already get this same self-
        correction on setup; this mirrors it for the daily counters.
        """
        if self.today == today:
            return
        now = dt_util.now()
        self.tier_usage_today = {}
        self.period_energy_kwh_today = {}
        self.energy_by_period_today = {
            name: total
            for name, total in self.energy_by_period_today.items()
            if any(
                p[CONF_PERIOD_NAME] == name and engine.period_contains_time(p, now)
                for p in self.periods
            )
        }
        self.bonus_earned_today = {}
        self.export_tier_usage_today = {}
        self.export_credit_today = 0.0
        self.cost_today = self.daily_charge
        # _handle_midnight also adds the new day's supply charge to the
        # running totals. This path only runs when that tick was missed, so
        # it has to do the same or the day's charge is missing from the
        # month and billing-period figures while showing correctly in
        # "cost today".
        self.cost_month += self.daily_charge
        self.cost_billing_period += self.daily_charge
        self._push_daily_charge_cost_statistic(today)
        self.today = today

    # ---- energy sensor handling -------------------------------------------

    @callback
    def _handle_energy_event(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            new_kwh = float(new_state.state)
        except ValueError:
            return

        now = dt_util.now()
        if self.last_energy_kwh is None:
            self.last_energy_kwh = new_kwh
            self.last_energy_reading_at = now
            self._notify()
            return

        delta = new_kwh - self.last_energy_kwh
        if delta < 0:
            # Meter reset (e.g. firmware restart) rather than real usage.
            delta = new_kwh
        self.last_energy_kwh = new_kwh
        reading_since = self.last_energy_reading_at
        self.last_energy_reading_at = now

        # When a GloBird interval-array source is configured, it becomes the
        # authoritative source of cost/tier accounting (see
        # _handle_interval_event) - it costs the same underlying usage
        # correctly, split by half-hour and at the right historical tariff
        # period, whereas this plain cumulative-delta path can only lump
        # the whole gap into one delta priced at "now". If both ran, usage
        # covered by the interval array would be double-costed (the two
        # sensors typically come from the same GloBird account). The raw
        # reading is still tracked above for continuity even when the
        # interval array is authoritative.
        if delta > 0 and not self.options.get(CONF_INTERVAL_SOURCE_ENTITY):
            cost = self._apply_delta(at=now, delta_kwh=delta)
            if self.options.get(CONF_SMOOTH_DASHBOARD_HISTORY) and reading_since:
                self._push_smoothed_history(reading_since, now, delta, cost)

        self.hass.async_create_task(self._async_save())
        self._notify()

    def _apply_delta(self, at: datetime, delta_kwh: float) -> float:
        """Cost and account for `delta_kwh` of usage that happened `at`.
        Returns the $ cost applied, so a caller bucketing costs by hour (see
        _push_external_cost_statistics) doesn't have to recompute it.

        `at` need not be "now" - the GloBird interval-array backfill
        (_handle_interval_event) calls this with a slot's own historical
        timestamp so usage is costed at whatever tariff period was actually
        active then, not whatever period happens to be active when the
        backfilled data arrives. tier_usage_today/period_energy_kwh_today
        are still "today"-scoped fields with no date-check here, though -
        callers backfilling a day other than self.today must not call this
        directly for the tier_usage_today/period_energy_kwh_today portion;
        see _apply_interval_slots.
        """
        period = engine.find_active_period(self.periods, at)
        if period is None:
            return 0.0  # configuration gap in period coverage
        name = period[CONF_PERIOD_NAME]
        used_so_far, tiers = self._tier_usage_input(period)
        cost = engine.cost_of_delta({**period, CONF_PERIOD_TIERS: tiers}, used_so_far, delta_kwh)

        self.tier_usage_today[name] = self.tier_usage_today.get(name, 0.0) + delta_kwh
        self.energy_by_period_today[name] = (
            self.energy_by_period_today.get(name, 0.0) + delta_kwh
        )
        self.period_energy_kwh_today[name] = (
            self.period_energy_kwh_today.get(name, 0.0) + delta_kwh
        )
        self.period_energy_kwh_billing_period[name] = (
            self.period_energy_kwh_billing_period.get(name, 0.0) + delta_kwh
        )
        self.period_energy_kwh_total[name] = (
            self.period_energy_kwh_total.get(name, 0.0) + delta_kwh
        )
        self.cost_today += cost
        self.cost_month += cost
        self.cost_billing_period += cost
        return cost

    # ---- GloBird interval-array backfill --------------------------------
    #
    # GloBird's own sensor exposes a half-hourly interval array for the most
    # recent completed day as a state attribute. Unlike the plain cumulative
    # energy sensor (_handle_energy_event), this lets a whole day's usage be
    # costed at the tariff rate actually active during each half-hour, not
    # lumped into one delta priced at whenever the daily update lands. It
    # also handles GloBird revising an earlier day's data after the fact
    # (seen in practice with gas bill-smoothing corrections) idempotently,
    # via interval_ledger.

    @callback
    def _handle_interval_event(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None:
            return
        attr_name = self.options.get(CONF_INTERVAL_ATTRIBUTE, DEFAULT_INTERVAL_ATTRIBUTE)
        intervals = new_state.attributes.get(attr_name)
        # "latest_day" is the array's sibling date attribute - a GloBird API
        # convention, hardcoded rather than a third config field (see const.py).
        latest_day = new_state.attributes.get("latest_day")
        if not intervals or not latest_day:
            return

        old_state: State | None = event.data.get("old_state")
        if old_state is not None and old_state.attributes.get(attr_name) == intervals:
            # Cheap early-exit for an unrelated attribute update on the same
            # entity (e.g. registers/daily changing while the interval array
            # itself didn't) - correctness doesn't depend on this, the
            # per-slot ledger diff below is a no-op either way.
            return

        try:
            year, month, day_num = (int(p) for p in latest_day.split("/"))
            day = date(year, month, day_num)
        except (ValueError, TypeError, AttributeError):
            _LOGGER.warning(
                "%s: could not parse latest_day '%s' from %s",
                self.plan_name,
                latest_day,
                new_state.entity_id,
            )
            return

        self._apply_interval_slots(day, intervals)
        self.hass.async_create_task(self._async_save())
        self._notify()

    def _interval_slot_time(self, day: date, index: int) -> datetime:
        return dt_util.start_of_local_day(day) + timedelta(minutes=30 * index)

    def _apply_interval_slots(self, day: date, intervals: list) -> None:
        """Diff `day`'s half-hourly slots against interval_ledger and apply
        whatever changed - a new slot in full, an unchanged slot as a no-op,
        a revised slot as the difference between old and new value.
        """
        if self.billing_period_start and day < self.billing_period_start:
            return  # nothing correct to attribute a slot before this period to

        is_today = day == self.today
        # Net kWh/$ delta actually applied this call, bucketed to the
        # top-of-hour it falls in - the recorder's external-statistics API
        # requires hourly rows, but GloBird's slots are half-hourly, so two
        # consecutive slots' deltas are summed into one hour's row below.
        hour_bucket_deltas: dict[datetime, float] = {}
        hour_bucket_cost_deltas: dict[datetime, float] = {}

        for index, raw_value in enumerate(intervals):
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue

            slot_time = self._interval_slot_time(day, index)
            if self.billing_period_start:
                period_start_dt = dt_util.start_of_local_day(self.billing_period_start)
                if slot_time < period_start_dt:
                    continue

            key = slot_time.isoformat()
            previous = self.interval_ledger.get(key)
            if previous is not None and previous == value:
                continue  # unchanged, already applied

            energy_delta = value if previous is None else value - previous
            if previous is not None and energy_delta < 0:
                _LOGGER.warning(
                    "%s: GloBird revised %s slot %s downward (%.4f -> %.4f kWh, "
                    "approx refund %.4f)",
                    self.plan_name,
                    day.isoformat(),
                    slot_time.isoformat(),
                    previous,
                    value,
                    -energy_delta,
                )
            self.interval_ledger[key] = value

            if energy_delta == 0:
                continue

            if is_today:
                cost = self._apply_delta(at=slot_time, delta_kwh=energy_delta)
            else:
                cost = self._apply_backfill_slot(slot_time, energy_delta)

            hour_start = slot_time.replace(minute=0, second=0, microsecond=0)
            hour_bucket_deltas[hour_start] = (
                hour_bucket_deltas.get(hour_start, 0.0) + energy_delta
            )
            if cost:
                hour_bucket_cost_deltas[hour_start] = (
                    hour_bucket_cost_deltas.get(hour_start, 0.0) + cost
                )

        if not is_today:
            # Fills in hour_bucket_cost_deltas for daily-cadence periods,
            # whose cost isn't computed per-slot above (see
            # _apply_backfill_slot) - the day-level replay is the only
            # place that cost is known, so it contributes its own hourly
            # breakdown here rather than through the per-slot loop.
            self._replay_day_cost_for_daily_cadence_periods(day, hour_bucket_cost_deltas)

        self._prune_stale_ledger_entries()

        if hour_bucket_deltas:
            self._push_external_statistics(hour_bucket_deltas)
        if hour_bucket_cost_deltas:
            self._push_external_cost_statistics(hour_bucket_cost_deltas)

    def _apply_backfill_slot(self, at: datetime, delta_kwh: float) -> float:
        """Apply one interval slot's delta for a day other than self.today.
        Returns the $ cost applied (0.0 for a daily-cadence period, whose
        cost is instead handled by _replay_day_cost_for_daily_cadence_periods).

        tier_usage_today/period_energy_kwh_today are "today"-scoped fields
        with no date-check - they must not be touched for a backfilled past
        day. The billing-period/lifetime energy totals ARE safe to bump
        directly (order-independent running totals). Cost for a
        billing_period-cadence period is likewise safe to apply immediately
        via _tier_usage_input (see Fix 2/3 design notes); a daily-cadence
        period's cost for a past day is instead handled by
        _replay_day_cost_for_daily_cadence_periods, since tier_usage_today
        doesn't retain a past day's usage-so-far once the day has rolled
        over - this method only bumps its energy totals here, not cost_*.
        """
        period = engine.find_active_period(self.periods, at)
        if period is None:
            return 0.0
        name = period[CONF_PERIOD_NAME]
        cadence = period.get(CONF_TIER_RESET_CADENCE, TIER_RESET_DAILY)
        cost = 0.0

        if cadence == TIER_RESET_BILLING_PERIOD:
            used_so_far, tiers = self._tier_usage_input(period)
            cost = engine.cost_of_delta(
                {**period, CONF_PERIOD_TIERS: tiers}, used_so_far, delta_kwh
            )
            self.cost_billing_period += cost
            if at.year == self.today.year and at.month == self.today.month:
                self.cost_month += cost

        self.period_energy_kwh_billing_period[name] = (
            self.period_energy_kwh_billing_period.get(name, 0.0) + delta_kwh
        )
        self.period_energy_kwh_total[name] = (
            self.period_energy_kwh_total.get(name, 0.0) + delta_kwh
        )
        return cost

    def _day_ledger_slots(self, day: date) -> list[tuple[datetime, float]]:
        """This day's (slot_time, value) pairs currently in the ledger,
        chronologically ordered. Reflects the ledger's current (possibly
        revised) values, not a fixed 48-slot assumption."""
        slots = []
        for key, value in self.interval_ledger.items():
            slot_time = dt_util.parse_datetime(key)
            if slot_time is None:
                continue
            slot_time = dt_util.as_local(slot_time)
            if slot_time.date() == day:
                slots.append((slot_time, value))
        slots.sort(key=lambda pair: pair[0])
        return slots

    def _replay_day_cost_for_daily_cadence_periods(
        self, day: date, hour_bucket_cost_deltas: dict[datetime, float]
    ) -> None:
        """Recompute a backfilled/corrected past day's full tier cost for
        every daily-cadence period, from the ledger's current values, and
        apply only the per-hour delta versus the last time this day was
        replayed (daily_cost_replay_cache) - idempotent by construction, so
        a re-replay of an unchanged day contributes nothing. Adds this
        call's hourly cost deltas into `hour_bucket_cost_deltas` (shared
        with the per-slot loop in _apply_interval_slots) so the caller can
        push them to the cost external statistic the same way it does for
        energy.

        billing_period-cadence periods don't need this: their cost is
        applied directly per-slot in _apply_backfill_slot, since that
        cadence's running total is order-independent (see design notes).
        """
        day_iso = day.isoformat()
        slots = self._day_ledger_slots(day)
        if not slots:
            return

        for period in self.periods:
            cadence = period.get(CONF_TIER_RESET_CADENCE, TIER_RESET_DAILY)
            if cadence != TIER_RESET_DAILY:
                continue
            name = period[CONF_PERIOD_NAME]

            usage_so_far = 0.0
            new_hour_costs: dict[str, float] = {}
            for slot_time, value in slots:
                if value <= 0:
                    continue
                if not engine.period_contains_time(period, slot_time):
                    continue
                slot_cost = engine.cost_of_delta(period, usage_so_far, value)
                usage_so_far += value
                hour_key = slot_time.replace(
                    minute=0, second=0, microsecond=0
                ).isoformat()
                new_hour_costs[hour_key] = new_hour_costs.get(hour_key, 0.0) + slot_cost

            cache_key = f"{day_iso}:{name}"
            previous_hour_costs = self.daily_cost_replay_cache.get(cache_key, {})
            total_delta = 0.0
            for hour_key in set(new_hour_costs) | set(previous_hour_costs):
                hour_delta = new_hour_costs.get(hour_key, 0.0) - previous_hour_costs.get(
                    hour_key, 0.0
                )
                if hour_delta == 0:
                    continue
                total_delta += hour_delta
                hour_start = dt_util.parse_datetime(hour_key)
                if hour_start is None:
                    continue
                hour_bucket_cost_deltas[hour_start] = (
                    hour_bucket_cost_deltas.get(hour_start, 0.0) + hour_delta
                )

            if total_delta != 0:
                self.cost_billing_period += total_delta
                if day.year == self.today.year and day.month == self.today.month:
                    self.cost_month += total_delta
            self.daily_cost_replay_cache[cache_key] = new_hour_costs

    def _prune_stale_ledger_entries(self) -> None:
        """Drop ledger/replay-cache entries from before the current billing
        period - they're never consulted again for it. Cross-billing-period
        corrections from GloBird are out of scope for this fork."""
        if not self.billing_period_start:
            return
        cutoff = dt_util.start_of_local_day(self.billing_period_start)

        for key in [k for k in self.interval_ledger if _ledger_key_before(k, cutoff)]:
            del self.interval_ledger[key]

        cutoff_iso = self.billing_period_start.isoformat()
        for key in [
            k for k in self.daily_cost_replay_cache if k.split(":", 1)[0] < cutoff_iso
        ]:
            del self.daily_cost_replay_cache[key]

    async def _resync_external_stat_baselines(self) -> None:
        """Trust the recorder's own last-pushed sum over the persisted
        external_stat_cumulative_kwh/cost fields, if the recorder's is higher.

        These fields exist only so each push can compute the next
        cumulative row; the recorder's last row is what the Energy
        Dashboard actually shows right now. If the persisted Store value
        is ever behind that (a bug, a restore from an older backup, a
        manual edit) the next push would describe a fake negative dip to
        the Dashboard for that hour - which is exactly what happened
        during development when a debug button briefly zeroed these
        fields. Resyncing up to the recorder's own value on every setup
        makes that whole failure class self-healing.
        """
        if not _HAS_RECORDER_STATISTICS:
            return

        instance = get_instance(self.hass)
        for statistic_id, attr in (
            (f"{DOMAIN}:{_slugify(self.entry_id)}_energy", "external_stat_cumulative_kwh"),
            (f"{DOMAIN}:{_slugify(self.entry_id)}_cost", "external_stat_cumulative_cost"),
        ):
            last = await instance.async_add_executor_job(
                get_last_statistics, self.hass, 1, statistic_id, False, {"sum"}
            )
            rows = last.get(statistic_id)
            if not rows:
                continue
            last_sum = rows[0].get("sum")
            if last_sum is not None and last_sum > getattr(self, attr):
                setattr(self, attr, last_sum)

    def _push_smoothed_history(
        self, since: datetime, until: datetime, delta_kwh: float, cost: float
    ) -> None:
        """Spread `delta_kwh`/`cost` evenly across the days between two
        cumulative-sensor readings, for the Energy Dashboard's external
        statistics only (CONF_SMOOTH_DASHBOARD_HISTORY).

        Only meaningful for sparse, non-interval sources like GloBird's
        basic gas meter reads, where a single delta can span weeks with no
        way to know the real day-by-day shape - this plan's own
        cost_today/cost_billing_period etc. (set by the _apply_delta call
        this follows) still lump the whole delta at `until`, unchanged.
        This purely keeps the Dashboard graph plausible instead of showing
        one giant spike; it's linear interpolation between two known
        checkpoints, not a claim about real usage on any single day.
        """
        since_day = dt_util.as_local(since).date()
        until_day = dt_util.as_local(until).date()
        days = (until_day - since_day).days
        if days < 1:
            hour_bucket_kwh = {dt_util.start_of_local_day(until_day): delta_kwh}
            hour_bucket_cost = {dt_util.start_of_local_day(until_day): cost}
        else:
            per_day_kwh = delta_kwh / days
            per_day_cost = cost / days
            hour_bucket_kwh = {}
            hour_bucket_cost = {}
            for i in range(1, days + 1):
                day_start = dt_util.start_of_local_day(since_day + timedelta(days=i))
                hour_bucket_kwh[day_start] = per_day_kwh
                hour_bucket_cost[day_start] = per_day_cost
        self._push_external_statistics(hour_bucket_kwh)
        self._push_external_cost_statistics(hour_bucket_cost)

    def _push_external_statistics(self, hour_bucket_deltas: dict[datetime, float]) -> None:
        """Backfill the Energy Dashboard's own history for this plan.

        A normal sensor state write is always timestamped "now" by HA's
        recorder, so even though _apply_interval_slots costs each slot at
        its correct historical tariff period, the plan's _energy_total
        sensor's own state history would still show one lump jump per day.
        This pushes real hourly rows into the recorder's statistics tables
        via the external-statistics API so the Dashboard's graphs are
        accurate too - it's additive to, not a replacement for,
        period_energy_kwh_total/cost_* above, which remain this
        integration's own source of truth. `hour_bucket_deltas` is this
        call's net kWh delta per top-of-hour bucket (two half-hour slots
        summed into one hourly row, since GloBird's slots are half-hourly
        but the recorder's external-statistics API requires hourly rows).
        """
        if not _HAS_RECORDER_STATISTICS or not hour_bucket_deltas:
            return

        statistic_id = f"{DOMAIN}:{_slugify(self.entry_id)}_energy"
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"{self.plan_name} energy",
            source=DOMAIN,
            statistic_id=statistic_id,
            unit_class="energy",
            unit_of_measurement=self.import_energy_unit,
        )

        rows = []
        for hour_start in sorted(hour_bucket_deltas):
            self.external_stat_cumulative_kwh += hour_bucket_deltas[hour_start]
            rows.append(
                StatisticData(
                    start=dt_util.as_utc(hour_start),
                    sum=self.external_stat_cumulative_kwh,
                )
            )

        try:
            async_add_external_statistics(self.hass, metadata, rows)
        except HomeAssistantError:
            _LOGGER.exception(
                "%s: failed to push external statistics for %s", self.plan_name, statistic_id
            )

    def _push_external_cost_statistics(self, hour_bucket_deltas: dict[datetime, float]) -> None:
        """Mirror of _push_external_statistics, for $ instead of kWh.

        Lets the Energy Dashboard's single-day drill-down cost graph (and
        anything else reading this statistic, e.g. correlating a specific
        device's usage window against the marginal rate at that time - a
        car charge on a time-of-use plan, say) show cost distributed across
        the real hours it was incurred, not lumped at whenever the
        interval array happened to be processed. `hour_bucket_deltas` is
        this call's net $ delta per top-of-hour bucket, combining both the
        billing_period-cadence per-slot cost (see _apply_backfill_slot)
        and the daily-cadence full-day replay's per-hour breakdown (see
        _replay_day_cost_for_daily_cadence_periods).
        """
        if not _HAS_RECORDER_STATISTICS or not hour_bucket_deltas:
            return

        statistic_id = f"{DOMAIN}:{_slugify(self.entry_id)}_cost"
        metadata = StatisticMetaData(
            has_sum=True,
            mean_type=StatisticMeanType.NONE,
            name=f"{self.plan_name} cost",
            source=DOMAIN,
            statistic_id=statistic_id,
            # Currency isn't a physical unit HA's recorder auto-converts
            # between (unlike kWh/MJ for the energy statistic above), so
            # there's no matching unit_class converter to name here.
            unit_class=None,
            unit_of_measurement=self.hass.config.currency,
        )

        rows = []
        for hour_start in sorted(hour_bucket_deltas):
            self.external_stat_cumulative_cost += hour_bucket_deltas[hour_start]
            rows.append(
                StatisticData(
                    start=dt_util.as_utc(hour_start),
                    sum=self.external_stat_cumulative_cost,
                )
            )

        try:
            async_add_external_statistics(self.hass, metadata, rows)
        except HomeAssistantError:
            _LOGGER.exception(
                "%s: failed to push external cost statistics for %s",
                self.plan_name,
                statistic_id,
            )

    def _push_daily_charge_cost_statistic(self, day: date) -> None:
        """Spread the flat daily supply charge evenly across `day`'s 24
        hourly rows in the cost external statistic (1/24th each).

        The daily charge isn't tied to any hour of usage - it's a once-a-
        day fee added directly to cost_today/month/billing_period by
        _handle_midnight/_recompute_daily_bounds, entirely separate from
        the per-slot interval-array costing that otherwise feeds
        _push_external_cost_statistics. Without this, the Dashboard's
        hourly cost breakdown would permanently under-report every day by
        exactly the daily charge, even once the usage-cost side is
        accurate. An even hourly spread (rather than one lump at
        midnight) is the fairer allocation for per-hour cost figures -
        every hour of the day equally "hosts" a share of a fee that's
        incurred regardless of when in the day usage actually happens.
        """
        if not self._external_stats_enabled or not self.daily_charge:
            return
        per_hour = self.daily_charge / 24
        day_start = dt_util.start_of_local_day(day)
        hour_bucket_deltas = {day_start + timedelta(hours=i): per_hour for i in range(24)}
        self._push_external_cost_statistics(hour_bucket_deltas)

    # ---- export sensor handling --------------------------------------

    @callback
    def _handle_export_energy_event(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            new_kwh = float(new_state.state)
        except ValueError:
            return

        now = dt_util.now()
        if self.last_export_kwh is None:
            self.last_export_kwh = new_kwh
            self._notify()
            return

        delta = new_kwh - self.last_export_kwh
        if delta < 0:
            # Meter reset (e.g. firmware restart) rather than real usage.
            delta = new_kwh
        self.last_export_kwh = new_kwh

        if delta > 0:
            self._apply_export_delta(now, delta)

        self.hass.async_create_task(self._async_save())
        self._notify()

    def _apply_export_delta(self, now: datetime, delta_kwh: float) -> None:
        period = engine.find_active_period(self.export_periods, now)
        if period is None:
            return  # no export period configured for this moment
        name = period[CONF_PERIOD_NAME]
        used_today = self.export_tier_usage_today.get(name, 0.0)
        credit = engine.cost_of_delta(period, used_today, delta_kwh)

        self.export_tier_usage_today[name] = used_today + delta_kwh
        self.export_period_energy_kwh_billing_period[name] = (
            self.export_period_energy_kwh_billing_period.get(name, 0.0) + delta_kwh
        )
        self.export_period_energy_kwh_total[name] = (
            self.export_period_energy_kwh_total.get(name, 0.0) + delta_kwh
        )

        # Credit nets straight out of the running cost totals.
        self.cost_today -= credit
        self.cost_month -= credit
        self.cost_billing_period -= credit
        self.export_credit_today += credit
        self.export_credit_month += credit
        self.export_credit_billing_period += credit

    # ---- live power sensor handling (bonus calc_mode=live_power_sensor) --

    @callback
    def _handle_power_event(self, event: Event) -> None:
        new_state: State | None = event.data.get("new_state")
        if new_state is None or new_state.state in ("unknown", "unavailable"):
            return
        try:
            watts = float(new_state.state)
        except ValueError:
            return

        now = dt_util.now()
        active = engine.find_active_period(self.periods, now)
        if active is None:
            return
        bonus = active.get(CONF_PERIOD_BONUS)
        if not bonus or bonus.get("calc_mode") != BONUS_CALC_LIVE_POWER:
            return
        bonus_start = bonus.get(CONF_BONUS_START_TIME)
        bonus_end = bonus.get(CONF_BONUS_END_TIME)
        if bonus_start and bonus_end:
            window = {
                CONF_PERIOD_START_TIME: bonus_start,
                CONF_PERIOD_END_TIME: bonus_end,
                CONF_PERIOD_DAYS: active.get(CONF_PERIOD_DAYS, DAYS_ALL),
            }
            if not engine.period_contains_time(window, now):
                return
        name = active[CONF_PERIOD_NAME]
        sample = self.bonus_samples.setdefault(name, BonusWindowSample())
        sample.sample(now, watts)
        self._notify()

    # ---- bonus finalization -------------------------------------------

    def _finalize_bonus(self, period_name: str, now: datetime) -> bool:
        """Settle one period's bonus for the day `now` falls on.

        Returns True if it settled the bonus, False if there was nothing to
        do (no such period, no bonus configured, or already settled today).
        Callers own saving and notifying.
        """
        period = next(
            (p for p in self.periods if p[CONF_PERIOD_NAME] == period_name), None
        )
        if period is None:
            return False
        bonus = period.get(CONF_PERIOD_BONUS)
        if not bonus:
            return False

        # One settlement per period per day. Without this the bonus can be
        # credited twice: the finalizer is scheduled by wall-clock time, and
        # a DST fall-back repeats the hour it sits in.
        today_iso = now.date().isoformat()
        if self.bonus_finalized_for.get(period_name) == today_iso:
            return False

        threshold = bonus[CONF_BONUS_THRESHOLD_W]
        if bonus.get("calc_mode") == BONUS_CALC_LIVE_POWER and period_name in self.bonus_samples:
            avg_w = self.bonus_samples[period_name].average()
        else:
            start_t = engine.parse_time(
                bonus.get(CONF_BONUS_START_TIME) or period[CONF_PERIOD_START_TIME]
            )
            end_t = engine.parse_time(
                bonus.get(CONF_BONUS_END_TIME) or period[CONF_PERIOD_END_TIME]
            )
            # engine.window_hours, not a plain subtraction: a bonus window
            # that wraps midnight (23:00-06:00) subtracts to a negative
            # span, which would make average power negative and hand out
            # the bonus unconditionally.
            energy = self.energy_by_period_today.get(period_name, 0.0)
            avg_w = engine.avg_watts_from_energy(
                energy, engine.window_hours(start_t, end_t)
            )

        earned = avg_w < threshold
        self.bonus_earned_today[period_name] = earned
        self.bonus_finalized_for[period_name] = today_iso
        if earned:
            # The bonus is a credit against the day's bill, so it belongs in
            # every cost accumulator - the same three that usage and export
            # credit already adjust. Previously only the billing-period
            # total carried it, leaving "cost today" and "cost this month"
            # overstated by the bonus amount for every day it was earned.
            amount = bonus["amount"]
            self.cost_today -= amount
            self.cost_month -= amount
            self.cost_billing_period -= amount
            self.bonus_savings_billing_period += amount
            self.bonus_days_earned_billing_period += 1

        if period_name in self.bonus_samples:
            self.bonus_samples[period_name].reset()
        self.energy_by_period_today.pop(period_name, None)
        return True

    def _bonus_window_end(self, period: dict[str, Any]):
        """The time of day a period's bonus is settled at."""
        bonus = period.get(CONF_PERIOD_BONUS)
        if not bonus:
            return None
        return engine.parse_time(
            bonus.get(CONF_BONUS_END_TIME) or period[CONF_PERIOD_END_TIME]
        )

    def _catch_up_bonuses(self, now: datetime) -> bool:
        """Settle any bonus whose window already closed today but was never
        finalized.

        _finalize_bonus only ever runs from a callback scheduled for one
        exact wall-clock second. If Home Assistant is down, restarting or
        reloading at that second, the tick never fires and the day's bonus
        is silently never credited - the cost accumulators end the period
        overstated with no way to tell after the fact, because the next
        midnight clears the underlying energy figures.

        Only today's bonuses can be recovered: once the day rolls over the
        inputs are gone. Mirrors the same self-correction the daily,
        monthly and billing-period counters already get on setup.
        """
        settled = False
        for period in self.periods:
            end_t = self._bonus_window_end(period)
            if end_t is None:
                continue
            if now.time() < end_t:
                continue
            name = period[CONF_PERIOD_NAME]
            if self._finalize_bonus(name, now):
                settled = True
                _LOGGER.debug(
                    "%s: settled missed bonus for period %s (window closed %s)",
                    self.plan_name,
                    name,
                    end_t,
                )
        return settled

    def _make_bonus_finalizer(self, period_name: str) -> Callable[[datetime], None]:
        @callback
        def _finalize(now: datetime) -> None:
            if not self._finalize_bonus(period_name, now):
                return
            self.hass.async_create_task(self._async_save())
            self._notify()

        return _finalize

    # ---- per-period average import power -------------------------------

    def _make_period_avg_finalizer(self, period_name: str) -> Callable[[datetime], None]:
        @callback
        def _finalize(now: datetime) -> None:
            period = next(
                (p for p in self.periods if p[CONF_PERIOD_NAME] == period_name), None
            )
            if period is None:
                return

            # Average power itself is computed on demand from the configured
            # windows and the clock (see current_period_avg_watts), so there
            # is nothing to snapshot here - the figure is already stable now
            # that the window has closed. This callback survives only to
            # clear the bonus numerator below and to push a state update.

            # period_energy_kwh_today is NOT touched here - it resets at
            # midnight (see _handle_midnight), not at the period's own end
            # time, so it reads as a genuine "used today" total that holds
            # its value for the rest of the day once the window closes.

            # Reset the avg-watts numerator itself now that it's been
            # consumed above. A period that spans midnight (e.g. Controlled
            # Load) is exempt from the midnight-rollover clear (so its energy
            # survives being split by the day boundary while its window is
            # still open) - but with no bonus finalizer either to pop it,
            # nothing was ever clearing it, so it accumulated night after
            # night indefinitely and inflated this same average further each
            # day. Resetting it here, at the point the value has just been
            # used, closes that gap for every period regardless of whether
            # it has a bonus. Harmless if a bonus finalizer already cleared
            # it - both just want it empty by day's end.
            self.energy_by_period_today.pop(period_name, None)

            self.hass.async_create_task(self._async_save())
            self._notify()

        return _finalize

    # ---- daily/monthly/billing-period rollover -----------------------

    @callback
    def _handle_midnight(self, now: datetime) -> None:
        today = now.date()
        self.tier_usage_today = {}
        self.period_energy_kwh_today = {}
        # A period whose window is still open right at midnight (e.g. an
        # overnight 22:00-06:00 window) needs its running energy total kept
        # until its own finalizer closes it out later this morning - only
        # clear totals for periods that are NOT mid-window right now.
        self.energy_by_period_today = {
            name: total
            for name, total in self.energy_by_period_today.items()
            if any(
                p[CONF_PERIOD_NAME] == name and engine.period_contains_time(p, now)
                for p in self.periods
            )
        }
        self.bonus_earned_today = {}
        self.export_tier_usage_today = {}
        self.export_credit_today = 0.0

        # Roll the period bounds *before* applying the new day's charges.
        # A rollover zeroes the billing-period totals, so charging first
        # meant the first day of every billing period had its supply charge
        # added and then immediately wiped - that day was silently free.
        self._recompute_billing_bounds(today)
        if today.month != self.today.month:
            self.cost_month = 0.0
            self.export_credit_month = 0.0

        self.cost_today = self.daily_charge
        self.cost_billing_period += self.daily_charge
        self.cost_month += self.daily_charge
        self._push_daily_charge_cost_statistic(today)

        self.today = today

        self.hass.async_create_task(self._async_save())
        self._notify()
