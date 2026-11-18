from junctionlab.controller.config import PATTERN_FLASH, PATTERN_FREE, four_leg, four_leg_left_turn
from junctionlab.controller.fsm import Alarm, Controller, Interval, Mode

DT = 0.1


def run(ctrl, seconds, on_tick=None):
    for _ in range(int(round(seconds / DT))):
        ctrl.step(DT)
        if on_tick:
            on_tick(ctrl)


def test_startup_all_red_then_red_yellow_then_green():
    c = Controller(four_leg(), pattern=1)
    assert c.mode == Mode.STARTUP
    run(c, 3.9)
    assert c.indications().greens == set() and c.indications().reds == {1, 2, 3, 4}
    run(c, 0.2)
    assert c.interval == Interval.RED_YELLOW and c.indications().red_yellows == {1, 3}
    run(c, 2.0)
    assert c.interval == Interval.GREEN and c.indications().greens == {1, 3}


def test_conflicting_phases_are_never_green_together():
    for cfg in (four_leg(), four_leg_left_turn()):
        conflicts = cfg.conflicts()
        for pattern in (1, PATTERN_FREE):
            c = Controller(cfg, pattern=pattern)
            seen = []

            def check(ctrl):
                g = ctrl.indications().greens
                seen.append(frozenset(g))
                assert not any(pair <= g for pair in conflicts), g
            # give every approach some traffic so actuated mode cycles too
            for t in range(600):
                if t % 3 == 0:
                    for p in cfg.phases:
                        c.vehicle_arrival(p)
                run(c, 1.0, check)
            assert c.alarms == Alarm.NONE
            assert len(set(seen)) > 2


def test_fixed_time_cycle_length():
    cfg = four_leg()
    c = Controller(cfg, pattern=1)
    run(c, 10)  # pass startup
    starts = []
    prev = c.stage

    def watch(ctrl):
        nonlocal prev
        if ctrl.interval == Interval.GREEN and ctrl.stage == 0 and prev != 0:
            starts.append(ctrl.now)
        prev = ctrl.stage if ctrl.interval == Interval.GREEN else prev
    run(c, 400, watch)
    tm = cfg.timing
    expected = sum(cfg.patterns[1]) + 2 * (tm.yellow + tm.all_red + tm.red_yellow)
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert gaps and all(abs(g - expected) < 0.5 for g in gaps), (gaps, expected)


def test_yellow_always_follows_green():
    c = Controller(four_leg(), pattern=1)
    prev_greens: set[int] = set()

    def check(ctrl):
        nonlocal prev_greens
        ind = ctrl.indications()
        for p in prev_greens - ind.greens:
            assert p in ind.yellows, f"phase {p} went green -> {ctrl.interval.name} without yellow"
        prev_greens = ind.greens
    run(c, 200, check)


def test_actuated_rests_in_main_road_without_side_demand():
    c = Controller(four_leg(), pattern=PATTERN_FREE)
    run(c, 120)
    assert c.stage == 0 and c.interval == Interval.GREEN


def test_actuated_serves_side_road_call_after_min_green():
    cfg = four_leg()
    c = Controller(cfg, pattern=PATTERN_FREE)
    run(c, 60)
    c.vehicle_arrival(2)
    run(c, cfg.stage_passage(0) + cfg.timing.yellow + cfg.timing.all_red + cfg.timing.red_yellow + 0.5)
    assert c.stage == 1 and c.interval == Interval.GREEN


def test_actuated_max_out_under_continuous_demand():
    cfg = four_leg()
    c = Controller(cfg, pattern=PATTERN_FREE)
    run(c, 10)
    c.vehicle_arrival(2)  # side road waiting
    longest = 0.0

    def feed(ctrl):
        nonlocal longest
        ctrl.vehicle_arrival(1)  # main road never gaps out
        ctrl.vehicle_arrival(2)  # and the side road keeps calling
        if ctrl.stage == 0:
            longest = max(longest, ctrl.green_time)
    run(c, 120, feed)
    assert c.cycle_count >= 1
    assert abs(longest - cfg.stage_max(0)) < 0.25


def test_flash_command_goes_through_yellow_then_recovers_via_startup():
    c = Controller(four_leg(), pattern=1)
    run(c, 15)
    assert c.interval == Interval.GREEN
    c.set_pattern(PATTERN_FLASH)
    run(c, 0.1)
    assert c.interval == Interval.YELLOW
    run(c, 3.1)
    assert c.mode == Mode.FLASH and c.indications().flashing
    c.set_pattern(1)
    assert c.mode == Mode.STARTUP


def test_conflict_monitor_trips_to_flash():
    c = Controller(four_leg(), pattern=1)
    events = []
    c.listeners.append(lambda k, d: events.append((k, d)))
    run(c, 15)  # stage A (1,3) green
    c.inject_fault(Alarm.CONFLICT, phase=2)
    run(c, 0.1)
    assert c.mode == Mode.FLASH
    assert Alarm.CONFLICT in c.alarms
    alarm = next(d for k, d in events if k == "alarm")
    assert alarm["critical"] and 2 in alarm["phases"]
    c.clear_faults()
    assert c.mode == Mode.STARTUP and c.alarms == Alarm.NONE


def test_red_lamp_failure_is_critical():
    c = Controller(four_leg(), pattern=1)
    run(c, 15)
    c.inject_fault(Alarm.RED_LAMP_OUT, phase=2)
    assert c.mode == Mode.FLASH


def test_detector_fault_is_not_critical_but_forces_max_out():
    cfg = four_leg()
    c = Controller(cfg, pattern=PATTERN_FREE)
    run(c, 10)
    c.inject_fault(Alarm.DETECTOR_FAULT, phase=1)
    c.vehicle_arrival(2)
    longest = 0.0

    def watch(ctrl):
        nonlocal longest
        if ctrl.stage == 0:
            longest = max(longest, ctrl.green_time)
    run(c, 90, watch)
    assert c.mode == Mode.ACTUATED
    assert abs(longest - cfg.stage_max(0)) < 0.25


def test_preempt_jumps_to_requested_stage_and_holds():
    cfg = four_leg_left_turn()
    c = Controller(cfg, pattern=1)
    run(c, 10)
    assert c.stage == 0
    c.request_preempt(2)
    run(c, cfg.timing.preempt_min_green + 8)
    assert c.stage == 2 and c.interval == Interval.GREEN  # skipped stage 1
    run(c, cfg.timing.preempt_hold - 2)
    assert c.stage == 2
    run(c, 40)
    assert c.preempt_target is None


def test_pedestrian_walk_then_clearance():
    cfg = four_leg()
    c = Controller(cfg, pattern=PATTERN_FREE)
    run(c, 30)
    c.ped_request(2)
    for _ in range(300):
        c.step(DT)
        if c.stage == 1 and c.interval == Interval.GREEN:
            break
    assert 2 in c.indications().walks
    run(c, cfg.phases[2].walk + 0.1)
    assert 2 in c.indications().ped_clears
