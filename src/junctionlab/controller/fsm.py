"""Stage-based traffic signal controller state machine.

Pure and deterministic: time only advances through ``step(dt)``, so the same
code runs in real time on the device and many times faster in tests and in
the benchmark simulator.

Interval sequence between two stages (phases staying green across the
transition keep their green):

    GREEN(A) -> YELLOW(A-B) -> ALL_RED -> RED_YELLOW(B-A) -> GREEN(B)

Safety features modelled on real controllers:
  * conflict monitor: two conflicting phases green at once -> immediate flash
  * red lamp monitoring: a failed red lamp -> immediate flash
  * startup all-red before the first green
"""

from __future__ import annotations

import enum
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .config import PATTERN_FLASH, PATTERN_FREE, JunctionConfig

log = logging.getLogger("controller")


class Mode(enum.IntEnum):
    FIXED = 1      # fixed time
    ACTUATED = 2   # actuated
    FLASH = 3      # amber flash
    STARTUP = 4


class Interval(enum.IntEnum):
    GREEN = 1
    YELLOW = 2
    ALL_RED = 3
    RED_YELLOW = 4
    FLASH = 5
    STARTUP = 6


class Alarm(enum.IntFlag):
    NONE = 0
    CONFLICT = 1 << 0        # conflict monitor tripped (critical)
    RED_LAMP_OUT = 1 << 1    # red lamp failure (critical)
    DETECTOR_FAULT = 1 << 2  # detector stuck on (non-critical)
    POWER_FAIL = 1 << 3      # running on UPS battery (non-critical)
    COMM_LOSS = 1 << 4       # lost center link (non-critical)


CRITICAL = Alarm.CONFLICT | Alarm.RED_LAMP_OUT

FAULT_CODES = {1: Alarm.CONFLICT, 2: Alarm.RED_LAMP_OUT, 3: Alarm.DETECTOR_FAULT, 4: Alarm.POWER_FAIL}


@dataclass
class Indications:
    greens: set[int] = field(default_factory=set)
    yellows: set[int] = field(default_factory=set)
    reds: set[int] = field(default_factory=set)
    red_yellows: set[int] = field(default_factory=set)
    walks: set[int] = field(default_factory=set)
    ped_clears: set[int] = field(default_factory=set)
    dont_walks: set[int] = field(default_factory=set)
    flashing: bool = False


def mask(phases: set[int] | list[int] | tuple[int, ...]) -> int:
    m = 0
    for p in phases:
        m |= 1 << (p - 1)
    return m


Listener = Callable[[str, dict], None]


class Controller:
    def __init__(self, cfg: JunctionConfig, pattern: int = PATTERN_FREE) -> None:
        cfg.validate()
        self.cfg = cfg
        self.now = 0.0
        self.pattern = pattern
        self.stage = -1                 # -1 before the first green
        self.next_stage: int | None = None
        self.interval = Interval.STARTUP
        self.t = 0.0                    # time in current interval
        self.green_time = 0.0           # time since current stage became green
        self.cycle_count = 0
        self.alarms = Alarm.NONE
        self.fault_phase: dict[Alarm, int] = {}
        self.veh_calls: set[int] = set()
        self.ped_calls: set[int] = set()
        self.ped_served: set[int] = set()
        self.last_actuation: dict[int, float] = {p: -1e9 for p in cfg.phases}
        self.volume: dict[int, int] = {p: 0 for p in cfg.phases}
        self.preempt_target: int | None = None
        self.preempt_served_at: float | None = None
        self.listeners: list[Listener] = []
        self._conflicts = cfg.conflicts()
        self._last_mode = self.mode

    # ------------------------------------------------------------------ api
    @property
    def mode(self) -> Mode:
        if self.interval == Interval.STARTUP:
            return Mode.STARTUP
        if self.interval == Interval.FLASH:
            return Mode.FLASH
        return Mode.ACTUATED if self.pattern == PATTERN_FREE else Mode.FIXED

    def set_pattern(self, pattern: int) -> None:
        if pattern not in self.cfg.patterns and pattern not in (PATTERN_FREE, PATTERN_FLASH):
            raise ValueError(f"unknown pattern {pattern}")
        if pattern == self.pattern:
            return
        old, self.pattern = self.pattern, pattern
        self._emit("pattern", {"old": old, "new": pattern})
        if pattern != PATTERN_FLASH and self.interval == Interval.FLASH and not self.alarms & CRITICAL:
            self._enter(Interval.STARTUP)

    def vehicle_arrival(self, phase: int, count: int = 1) -> None:
        """Detector actuation (loop, magnetic sensor or camera)."""
        if phase not in self.cfg.phases:
            return
        self.volume[phase] += count
        self.last_actuation[phase] = self.now
        if phase not in self._indications_core().greens:
            self.veh_calls.add(phase)

    def place_call(self, phase: int) -> None:
        """Presence on a red approach (queue detector): demand only, no count."""
        if phase in self.cfg.phases and phase not in self._indications_core().greens:
            self.veh_calls.add(phase)

    def ped_request(self, phase: int) -> None:
        if phase in self.cfg.phases and self.cfg.phases[phase].has_ped:
            self.ped_calls.add(phase)
            self.veh_calls.add(phase)  # a pedestrian also needs the stage to be served

    def request_preempt(self, stage: int) -> None:
        """Emergency vehicle priority, stage is 0-based."""
        if not 0 <= stage < len(self.cfg.stages):
            raise ValueError(f"unknown stage {stage}")
        self.preempt_target = stage
        self.preempt_served_at = None
        self._emit("preempt", {"stage": stage, "state": "requested"})

    def inject_fault(self, alarm: Alarm, phase: int = 1) -> None:
        if alarm == Alarm.CONFLICT:
            # simulates a stuck output relay: phase driven green regardless of the FSM
            self.fault_phase[alarm] = phase
        else:
            self.fault_phase[alarm] = phase
            self._raise(alarm, phase)

    def clear_faults(self) -> None:
        self.fault_phase.clear()
        if self.alarms:
            cleared = self.alarms
            self.alarms = Alarm.NONE
            self._emit("alarm_cleared", {"alarms": int(cleared), "names": _names(cleared)})
        if self.interval == Interval.FLASH and self.pattern != PATTERN_FLASH:
            self._enter(Interval.STARTUP)

    # ------------------------------------------------------------------ time
    def step(self, dt: float) -> None:
        self.now += dt
        self.t += dt
        if self.interval == Interval.GREEN:
            self.green_time += dt
        if Alarm.DETECTOR_FAULT in self.fault_phase:
            p = self.fault_phase[Alarm.DETECTOR_FAULT]
            self.last_actuation[p] = self.now  # stuck-on detector: permanent presence

        self._advance()
        self._monitor()
        if self.mode != self._last_mode:
            self._emit("mode", {"old": int(self._last_mode), "new": int(self.mode)})
            self._last_mode = self.mode

    def _advance(self) -> None:
        tm = self.cfg.timing
        it = self.interval

        if it == Interval.FLASH:
            return
        if it == Interval.STARTUP:
            if self.pattern == PATTERN_FLASH:
                self._enter(Interval.FLASH)
            elif self.t >= tm.startup_all_red:
                self.stage, self.next_stage = -1, 0
                self._enter(Interval.RED_YELLOW)
            return

        if it == Interval.GREEN:
            nxt = self._green_should_end()
            if nxt is not False:
                self.next_stage = nxt  # None means "going to flash"
                self._enter(Interval.YELLOW)
            return

        leaving, entering = self._leaving(), self._entering()
        if it == Interval.YELLOW and (self.t >= tm.yellow or not leaving):
            if self.next_stage is None:
                self._enter(Interval.FLASH)
            else:
                self._enter(Interval.ALL_RED)
        elif it == Interval.ALL_RED and (self.t >= tm.all_red or not leaving):
            self._enter(Interval.RED_YELLOW)
        elif it == Interval.RED_YELLOW and (self.t >= tm.red_yellow or not entering):
            self._start_green(self.next_stage)  # type: ignore[arg-type]

    def _green_should_end(self) -> int | None | bool:
        """False = keep green, None = go to flash, int = next stage."""
        cfg, tm = self.cfg, self.cfg.timing
        if self.pattern == PATTERN_FLASH:
            return None
        g = self.green_time

        if self.preempt_target is not None:
            if self.preempt_target == self.stage:
                if self.preempt_served_at is None:
                    self.preempt_served_at = self.now
                    self._emit("preempt", {"stage": self.stage, "state": "served"})
                if self.now - self.preempt_served_at < tm.preempt_hold:
                    return False
                self.preempt_target = None
                self._emit("preempt", {"stage": self.stage, "state": "cleared"})
            elif g >= min(tm.preempt_min_green, cfg.stage_min(self.stage)):
                return self.preempt_target

        n = len(cfg.stages)
        if self.pattern in cfg.patterns:  # fixed time
            return (self.stage + 1) % n if g >= cfg.patterns[self.pattern][self.stage] else False

        # actuated
        min_g = cfg.stage_min(self.stage)
        if self._ped_active():
            min_g = max(min_g, max(cfg.phases[p].walk + cfg.phases[p].ped_clear for p in self.ped_served))
        if g < min_g:
            return False
        nxt = self._next_with_demand()
        if nxt is None:
            return False  # rest in green, nobody else is waiting
        gap = self.now - max(self.last_actuation[p] for p in cfg.stages[self.stage].phases)
        if gap >= cfg.stage_passage(self.stage) or g >= cfg.stage_max(self.stage):
            return nxt
        return False

    def _next_with_demand(self) -> int | None:
        stages = self.cfg.stages
        for i in range(1, len(stages)):
            c = (self.stage + i) % len(stages)
            if stages[c].recall or any(p in self.veh_calls for p in stages[c].phases):
                return c
        return None

    def _ped_active(self) -> bool:
        return bool(self.ped_served)

    def _start_green(self, stage: int) -> None:
        if self.stage != -1 and stage <= self.stage:  # wrapped around -> one more cycle
            self.cycle_count += 1
        self.stage, self.next_stage = stage, None
        phases = self.cfg.stages[stage].phases
        self.veh_calls -= set(phases)
        self.ped_served = {p for p in phases if p in self.ped_calls}
        self.ped_calls -= self.ped_served
        for p in phases:
            self.last_actuation[p] = max(self.last_actuation[p], self.now)
        self.green_time = 0.0
        self._enter(Interval.GREEN)
        self._emit("stage", {"stage": stage, "name": self.cfg.stages[stage].name})

    def _enter(self, interval: Interval) -> None:
        if interval == Interval.STARTUP:
            self.stage, self.next_stage = -1, None
        if interval == Interval.FLASH:
            self.next_stage = None
        self.interval, self.t = interval, 0.0

    # ------------------------------------------------------------ outputs
    def _cur(self) -> set[int]:
        return set(self.cfg.stages[self.stage].phases) if self.stage >= 0 else set()

    def _nxt(self) -> set[int]:
        return set(self.cfg.stages[self.next_stage].phases) if self.next_stage is not None else set()

    def _leaving(self) -> set[int]:
        return self._cur() - self._nxt()

    def _entering(self) -> set[int]:
        return self._nxt() - self._cur()

    def _indications_core(self) -> Indications:
        ind = Indications()
        it, cur, nxt = self.interval, self._cur(), self._nxt()
        if it == Interval.FLASH:
            ind.flashing = True
            return ind
        if it == Interval.GREEN:
            ind.greens = cur
        elif it == Interval.YELLOW:
            ind.greens, ind.yellows = cur & nxt, cur - nxt
        elif it == Interval.ALL_RED:
            ind.greens = cur & nxt
        elif it == Interval.RED_YELLOW:
            ind.greens, ind.red_yellows = cur & nxt, nxt - cur
        return ind

    def indications(self) -> Indications:
        ind = self._indications_core()
        if Alarm.CONFLICT in self.fault_phase and not ind.flashing:
            ind.greens = ind.greens | {self.fault_phase[Alarm.CONFLICT]}  # stuck relay
        allp = set(self.cfg.phases)
        if not ind.flashing:
            ind.reds = allp - ind.greens - ind.yellows
            if Alarm.RED_LAMP_OUT in self.fault_phase:
                ind.reds.discard(self.fault_phase[Alarm.RED_LAMP_OUT])
        # pedestrians
        for p in allp:
            ph = self.cfg.phases[p]
            if not ph.has_ped or ind.flashing:
                continue
            if self.interval == Interval.GREEN and p in ind.greens and p in self.ped_served:
                if self.green_time < ph.walk:
                    ind.walks.add(p)
                elif self.green_time < ph.walk + ph.ped_clear:
                    ind.ped_clears.add(p)
                else:
                    ind.dont_walks.add(p)
            else:
                ind.dont_walks.add(p)
        return ind

    # ------------------------------------------------------------ safety
    def _monitor(self) -> None:
        if self.interval == Interval.FLASH:
            return
        greens = self.indications().greens
        for pair in self._conflicts:
            if pair <= greens:
                a, b = sorted(pair)
                self._raise(Alarm.CONFLICT, a, extra={"phases": [a, b]})
                return

    def _raise(self, alarm: Alarm, phase: int, extra: dict | None = None) -> None:
        new = alarm not in self.alarms
        self.alarms |= alarm
        if new:
            self._emit("alarm", {"alarm": int(alarm), "name": _names(alarm)[0], "phase": phase,
                                 "critical": bool(alarm & CRITICAL), **(extra or {})})
        if alarm & CRITICAL and self.interval != Interval.FLASH:
            log.error("critical alarm %s on phase %d -> FLASH", alarm.name, phase)
            self._enter(Interval.FLASH)

    # ------------------------------------------------------------ misc
    def _emit(self, kind: str, data: dict) -> None:
        for fn in self.listeners:
            try:
                fn(kind, data)
            except Exception:
                log.exception("listener failed")

    def snapshot(self) -> dict:
        ind = self.indications()
        stage = self.cfg.stages[self.stage].name if self.stage >= 0 else None
        return {
            "mode": self.mode.name, "pattern": self.pattern, "stage": self.stage, "stage_name": stage,
            "interval": self.interval.name, "interval_time": round(self.t, 1),
            "green_time": round(self.green_time, 1), "cycle": self.cycle_count,
            "greens": sorted(ind.greens), "yellows": sorted(ind.yellows), "reds": sorted(ind.reds),
            "red_yellows": sorted(ind.red_yellows), "walks": sorted(ind.walks),
            "ped_clears": sorted(ind.ped_clears), "flashing": ind.flashing,
            "veh_calls": sorted(self.veh_calls), "ped_calls": sorted(self.ped_calls),
            "alarms": _names(self.alarms), "volume": dict(self.volume),
            "preempt": self.preempt_target,
        }


def _names(flags: Alarm) -> list[str]:
    return [a.name for a in Alarm if a and a in flags]  # type: ignore[misc]
