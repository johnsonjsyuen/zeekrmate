from datetime import datetime, timezone, timedelta

import pytest

from fordlogger.state_machine import StateMachine, VehicleState, classify
from fordlogger.models import Position

T0 = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)


def _pos(speed=0.0, charge_power=None, ts=None, is_driving=None, is_charging=None,
         observed_at=None, **kwargs):
    return Position(
        ts=ts or T0,
        vin="TEST",
        speed_kmh=speed,
        charge_power_kw=charge_power,
        is_driving=is_driving,
        is_charging=is_charging,
        observed_at=observed_at,
        **kwargs,
    )


def _sm(**kwargs):
    kwargs.setdefault("exit_debounce", 1)
    return StateMachine("TEST", **kwargs)


class TestClassify:
    def test_explicit_flags_win_over_fallback(self):
        assert classify(_pos(speed=60.0, is_driving=False)) == (False, None)
        assert classify(_pos(speed=0.0, charge_power=0.0, is_charging=True)) == (False, True)

    def test_fallback(self):
        assert classify(_pos(speed=60.0, charge_power=7.0)) == (True, True)
        assert classify(_pos(speed=0.5, charge_power=0.0)) == (False, False)

    def test_unknown(self):
        assert classify(_pos(speed=None)) == (None, None)

    def test_string_numerics_tolerated(self):
        assert classify(_pos(speed="12.5", charge_power="bogus")) == (True, None)

    def test_ford_strings_are_ignored(self):
        p = _pos(charge_status="ChargingAC", plug_status="CONNECTED", ignition_status="Run")
        assert classify(p) == (False, None)


class TestStateMachine:
    def test_initial_state_parked(self):
        sm = _sm()
        assert sm.state == VehicleState.PARKED
        assert sm.last_change_ts is None

    def test_driving_from_speed(self):
        state, changed = _sm().transition(_pos(speed=60.0))
        assert state == VehicleState.DRIVING
        assert changed is True

    def test_driving_from_explicit_flag(self):
        state, changed = _sm().transition(_pos(speed=None, is_driving=True))
        assert state == VehicleState.DRIVING
        assert changed is True

    def test_explicit_not_driving_overrides_speed(self):
        state, _ = _sm().transition(_pos(speed=60.0, is_driving=False))
        assert state == VehicleState.PARKED

    def test_charging_from_explicit_flag(self):
        state, changed = _sm().transition(_pos(is_charging=True))
        assert state == VehicleState.CHARGING
        assert changed is True

    def test_charging_from_power(self):
        state, changed = _sm().transition(_pos(charge_power=7.0))
        assert state == VehicleState.CHARGING
        assert changed is True

    def test_parked_no_change(self):
        state, changed = _sm().transition(_pos())
        assert state == VehicleState.PARKED
        assert changed is False

    def test_driving_to_parked(self):
        sm = _sm()
        sm.transition(_pos(speed=60.0))
        state, changed = sm.transition(_pos(speed=0.0, ts=T0 + timedelta(minutes=1)))
        assert state == VehicleState.PARKED
        assert changed is True
        assert sm.last_change_ts == T0 + timedelta(minutes=1)

    def test_charging_to_parked(self):
        sm = _sm()
        sm.transition(_pos(is_charging=True))
        state, changed = sm.transition(_pos(is_charging=False, ts=T0 + timedelta(minutes=1)))
        assert state == VehicleState.PARKED
        assert changed is True

    def test_sleep_after_timeout(self):
        sm = _sm(sleep_after_minutes=1)
        sm.transition(_pos(ts=T0))
        state, changed = sm.transition(_pos(ts=T0 + timedelta(minutes=2)))
        assert state == VehicleState.SLEEPING
        assert changed is True

    def test_sleep_stays_sleeping(self):
        sm = _sm(sleep_after_minutes=1)
        sm.transition(_pos(ts=T0))
        sm.transition(_pos(ts=T0 + timedelta(minutes=2)))
        state, changed = sm.transition(_pos(ts=T0 + timedelta(minutes=3)))
        assert state == VehicleState.SLEEPING
        assert changed is False

    def test_wake_from_sleep_driving(self):
        sm = _sm(sleep_after_minutes=1)
        sm.transition(_pos(ts=T0))
        sm.transition(_pos(ts=T0 + timedelta(minutes=2)))
        state, changed = sm.transition(_pos(speed=50.0, ts=T0 + timedelta(minutes=3)))
        assert state == VehicleState.DRIVING
        assert changed is True

    def test_wake_from_sleep_charging(self):
        sm = _sm(sleep_after_minutes=1)
        sm.transition(_pos(ts=T0))
        sm.transition(_pos(ts=T0 + timedelta(minutes=2)))
        state, changed = sm.transition(_pos(charge_power=7.0, ts=T0 + timedelta(minutes=3)))
        assert state == VehicleState.CHARGING
        assert changed is True

    def test_gps_noise_speed_stays_parked(self):
        state, changed = _sm().transition(_pos(speed=0.09))
        assert state == VehicleState.PARKED
        assert changed is False

    def test_driving_priority_over_charging(self):
        state, _ = _sm().transition(_pos(speed=30.0, charge_power=7.0))
        assert state == VehicleState.DRIVING

    def test_significant_change_delays_sleep(self):
        sm = _sm(sleep_after_minutes=1)
        sm.transition(_pos(ts=T0))
        sm.transition(_pos(charge_power=7.0, ts=T0 + timedelta(seconds=30)))
        assert sm.state == VehicleState.CHARGING
        sm.transition(_pos(charge_power=0.0, ts=T0 + timedelta(seconds=45)))
        assert sm.state == VehicleState.PARKED
        state, _ = sm.transition(_pos(ts=T0 + timedelta(seconds=90)))
        assert state == VehicleState.PARKED  # not sleeping yet


class TestClocks:
    """C06: sleep detection uses only the provider clock."""

    @pytest.mark.parametrize("offset", [timedelta(hours=10), -timedelta(hours=10)])
    def test_first_sample_never_sleeps_regardless_of_wall_clock(self, offset):
        sm = _sm(sleep_after_minutes=30)
        now = datetime.now(timezone.utc) + offset
        assert sm.transition(_pos(ts=now)) == (VehicleState.PARKED, False)

    def test_lagging_provider_clock_still_sleeps(self):
        sm = _sm(sleep_after_minutes=30)
        old = datetime(2020, 1, 1, tzinfo=timezone.utc)
        sm.transition(_pos(ts=old))
        state, _ = sm.transition(_pos(ts=old + timedelta(minutes=31)))
        assert state == VehicleState.SLEEPING


class TestDebounce:
    """C09: one partial/odd reading must not split a trip or charge."""

    def test_single_parked_reading_does_not_end_drive(self):
        sm = StateMachine("TEST", exit_debounce=2)
        sm.transition(_pos(speed=60.0, ts=T0))
        state, changed = sm.transition(_pos(speed=0.0, ts=T0 + timedelta(minutes=1)))
        assert (state, changed) == (VehicleState.DRIVING, False)
        state, changed = sm.transition(_pos(speed=50.0, ts=T0 + timedelta(minutes=2)))
        assert (state, changed) == (VehicleState.DRIVING, False)

    def test_two_parked_readings_end_drive_at_first(self):
        sm = StateMachine("TEST", exit_debounce=2)
        sm.transition(_pos(speed=60.0, ts=T0))
        sm.transition(_pos(speed=0.0, ts=T0 + timedelta(minutes=1)))
        state, changed = sm.transition(_pos(speed=0.0, ts=T0 + timedelta(minutes=2)))
        assert (state, changed) == (VehicleState.PARKED, True)
        assert sm.last_change_ts == T0 + timedelta(minutes=1)

    def test_unknown_power_keeps_charging(self):
        sm = StateMachine("TEST", exit_debounce=1)
        sm.transition(_pos(charge_power=7.0, ts=T0))
        state, _ = sm.transition(_pos(charge_power=None, ts=T0 + timedelta(minutes=1)))
        assert state == VehicleState.CHARGING

    def test_unknown_speed_keeps_driving(self):
        sm = StateMachine("TEST", exit_debounce=1)
        sm.transition(_pos(speed=60.0, ts=T0))
        state, _ = sm.transition(_pos(speed=None, ts=T0 + timedelta(minutes=1)))
        assert state == VehicleState.DRIVING

    def test_drive_into_charge_is_immediate(self):
        sm = StateMachine("TEST", exit_debounce=3)
        sm.transition(_pos(speed=60.0, ts=T0))
        state, changed = sm.transition(_pos(speed=0.0, charge_power=50.0,
                                            ts=T0 + timedelta(minutes=1)))
        assert (state, changed) == (VehicleState.CHARGING, True)


class TestTick:
    """C05: a frozen snapshot must not keep a vehicle DRIVING all night."""

    def test_stale_drive_ends_at_last_fresh_reading(self):
        sm = _sm()
        obs = datetime(2026, 10, 1, 8, 0, 5, tzinfo=timezone.utc)
        sm.transition(_pos(speed=60.0, ts=T0, observed_at=obs))
        assert sm.tick(obs + timedelta(minutes=5), stale_after_s=900) == (VehicleState.DRIVING, False)
        state, changed = sm.tick(obs + timedelta(minutes=16), stale_after_s=900)
        assert (state, changed) == (VehicleState.PARKED, True)
        assert sm.last_change_ts == T0

    def test_frozen_parked_snapshot_goes_to_sleep(self):
        sm = _sm(sleep_after_minutes=30)
        obs = T0 + timedelta(hours=10)  # provider clock offset must not matter
        sm.transition(_pos(ts=T0, observed_at=obs))
        assert sm.tick(obs + timedelta(minutes=29), stale_after_s=900)[1] is False
        state, changed = sm.tick(obs + timedelta(minutes=31), stale_after_s=900)
        assert (state, changed) == (VehicleState.SLEEPING, True)
        assert sm.last_change_ts == T0 + timedelta(minutes=31)

    def test_tick_without_any_reading_is_noop(self):
        sm = _sm()
        assert sm.tick(T0, stale_after_s=1) == (VehicleState.PARKED, False)
