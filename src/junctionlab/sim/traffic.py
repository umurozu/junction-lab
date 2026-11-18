"""Point-queue traffic model attached to a controller.

Vehicles arrive per approach (Poisson), trigger the upstream detector, wait in
a queue during red and discharge at saturation flow during green. Used for
the live demo (so the dashboard has something to show without real loops) and
by the benchmark / experiment suite that compares control strategies.

Detector placement (``detector``):
  * ``stopline`` - presence loop at the stop line: calls while a queue waits on
    red, one pulse per departing vehicle on green (default).
  * ``upstream`` - a single pulse loop ``setback_m`` metres before the stop
    line: pulse when the vehicle passes it, vehicle joins the queue after the
    travel time; the loop is held occupied when the queue spills back over it.
  * ``both``     - stop-line presence + upstream pulse loop.
  * ``none``     - no detection at all (only recall stages get served).
``miss_rate`` drops that share of detector pulses (dirty / broken loop).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from ..controller.fsm import Controller


@dataclass
class ApproachStats:
    queue: int = 0
    arrived: int = 0
    served: int = 0
    delay: float = 0.0       # vehicle-seconds spent queued
    max_queue: int = 0
    credit: float = 0.0      # fractional discharge capacity


@dataclass
class TrafficSim:
    ctrl: Controller
    rates: dict[int, float]                      # veh/h per phase
    sat_flow: float = 1800.0                     # veh/h of green per phase
    start_loss: float = 2.0                      # s lost at start of green
    detectors: bool = True                       # feed arrivals to the controller
    seed: int | None = None
    profile: str = "flat"                        # flat | peak (sinusoidal demand)
    peds: bool = False                           # simulate pedestrians pushing the button
    ped_rate: float = 90.0                       # ped/h per crosswalk
    detector: str = "stopline"                   # stopline | upstream | both | none
    setback_m: float = 40.0                      # upstream loop distance from stop line
    speed_ms: float = 13.9                       # approach speed (50 km/h)
    veh_len_m: float = 7.0                       # queue spacing per vehicle
    miss_rate: float = 0.0                       # share of missed detector pulses
    stats: dict[int, ApproachStats] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        self.stats = {p: ApproachStats() for p in self.ctrl.cfg.phases}
        self.rates = {p: self.rates.get(p, 0.0) for p in self.ctrl.cfg.phases}
        self.ped_rng = random.Random(None if self.seed is None else self.seed + 1000)
        ped_phases = [p for p, ph in self.ctrl.cfg.phases.items() if ph.has_ped] if self.peds else []
        self.ped_waiting: dict[int, int] = {p: 0 for p in ped_phases}   # at the kerb
        self.ped_crossed: dict[int, int] = {p: 0 for p in ped_phases}   # started crossing (counter)
        self.det_rng = random.Random(None if self.seed is None else self.seed + 2000)
        self.in_transit: dict[int, list[float]] = {p: [] for p in self.ctrl.cfg.phases}  # arrival times at queue
        if self.detector not in ("stopline", "upstream", "both", "none"):
            raise ValueError(f"unknown detector mode {self.detector}")

    @property
    def _stopline(self) -> bool:
        return self.detector in ("stopline", "both")

    @property
    def _upstream(self) -> bool:
        return self.detector in ("upstream", "both")

    def _pulse(self, p: int) -> None:
        if self.det_rng.random() >= self.miss_rate:
            self.ctrl.vehicle_arrival(p)

    def multiplier(self) -> float:
        if self.profile == "peak":  # 10-minute wave between 0.5x and 1.5x
            return 1.0 + 0.5 * math.sin(2 * math.pi * self.ctrl.now / 600)
        return 1.0

    def step(self, dt: float) -> None:
        ind = self.ctrl.indications()
        mult = self.multiplier()
        for p, st in self.stats.items():
            lam = self.rates[p] * mult / 3600.0 * dt
            n = _poisson(self.rng, lam)
            if n:
                st.arrived += n
                if self.detectors and self._upstream:
                    travel = self.setback_m / self.speed_ms
                    for _ in range(n):
                        self._pulse(p)  # passes the upstream loop now
                        self.in_transit[p].append(self.ctrl.now + travel)
                else:
                    st.queue += n
            if self.in_transit[p]:
                due = [t for t in self.in_transit[p] if t <= self.ctrl.now]
                if due:
                    st.queue += len(due)
                    self.in_transit[p] = [t for t in self.in_transit[p] if t > self.ctrl.now]
            if self.detectors and self._upstream and st.queue * self.veh_len_m >= self.setback_m:
                # queue spilled back over the upstream loop: occupied = continuous presence
                self.ctrl.last_actuation[p] = self.ctrl.now
                self.ctrl.place_call(p)
            if st.queue and self.detectors and self._stopline:
                self.ctrl.place_call(p)  # waiting vehicle sits on the stop-line loop
            green = p in ind.greens and self.ctrl.green_time >= self.start_loss
            if green:
                st.credit += self.sat_flow / 3600.0 * dt
                while st.credit >= 1 and st.queue:
                    st.queue -= 1
                    st.served += 1
                    st.credit -= 1
                    if self.detectors and self._stopline:
                        self._pulse(p)  # crossing the loop: count + extend green
                if not st.queue:
                    st.credit = min(st.credit, 1.0)
            else:
                st.credit = 0.0
            st.delay += st.queue * dt
            st.max_queue = max(st.max_queue, st.queue)
        for p in self.ped_waiting:
            self.ped_waiting[p] += _poisson(self.ped_rng, self.ped_rate * mult / 3600.0 * dt)
            if not self.ped_waiting[p]:
                continue
            if p in ind.walks:
                self.ped_crossed[p] += self.ped_waiting[p]
                self.ped_waiting[p] = 0
            else:
                self.ctrl.ped_request(p)

    def summary(self) -> dict:
        served = sum(s.served for s in self.stats.values())
        delay = sum(s.delay for s in self.stats.values())
        return {
            "served": served,
            "avg_delay_s": round(delay / served, 1) if served else 0.0,
            "max_queue": max((s.max_queue for s in self.stats.values()), default=0),
            "queues": {p: s.queue for p, s in self.stats.items()},
            "per_phase_delay": {p: round(s.delay / s.served, 1) if s.served else 0.0
                                for p, s in self.stats.items()},
        }


def _poisson(rng: random.Random, lam: float) -> int:
    if lam <= 0:
        return 0
    limit, k, p = math.exp(-lam), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1
