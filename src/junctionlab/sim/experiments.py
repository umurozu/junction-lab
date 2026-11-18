"""Experiment suite: runs the controller FSM against the traffic simulator under
many scenarios and parameter settings, and records performance + safety.

    jl-experiments                       # all experiments, writes reports/results.json
    jl-experiments --only E2,E3 --seeds 3 --minutes 30

Every run is deterministic for a given seed. Next to traffic metrics each run
carries an independent *safety observer* that never trusts the FSM: it checks
the lamp outputs every step for conflicting greens and for illegal lamp
sequences (green -> red without amber, red -> green without red+amber).
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from ..controller.config import PATTERN_FREE, PRESETS, JunctionConfig
from ..controller.fsm import Alarm, Controller, Interval
from .traffic import TrafficSim

WEBSTER = 200          # pattern number we install for the Webster-optimised plan
SAT = 1800.0
START_LOSS = 2.0

BASE_RATES = {
    "four_leg": {1: 700, 2: 250, 3: 650, 4: 200},
    "left_turn": {1: 600, 2: 250, 3: 550, 4: 220, 5: 150, 6: 120},
    "t_junction": {1: 650, 2: 600, 3: 220},
}


# --------------------------------------------------------------- helpers
def webster_plan(cfg: JunctionConfig, rates: dict[int, float]) -> tuple[list[int], float]:
    """Webster (1958) optimum cycle C0 = (1.5 L + 5) / (1 - Y), greens ~ y_i."""
    tm = cfg.timing
    lost_per_stage = tm.yellow + tm.all_red + tm.red_yellow + START_LOSS
    L = lost_per_stage * len(cfg.stages)
    ys = [max(rates.get(p, 0.0) for p in s.phases) / SAT for s in cfg.stages]
    Y = sum(ys)
    C = 150.0 if Y >= 0.9 else min(150.0, max(30.0, (1.5 * L + 5) / (1 - Y)))
    eff = max(C - L, 1.0)
    greens = []
    for i, y in enumerate(ys):
        g = eff * (y / Y if Y else 1 / len(ys)) + START_LOSS
        greens.append(int(round(max(g, cfg.stage_min(i)))))
    return greens, C


def build_cfg(spec: dict) -> JunctionConfig:
    cfg = PRESETS[spec["preset"]]()
    for ph in cfg.phases.values():
        if "passage" in spec:
            ph.passage = spec["passage"]
        if "max_green" in spec:
            ph.max_green = max(spec["max_green"], ph.min_green)
        if "min_green" in spec:
            ph.min_green = spec["min_green"]
    if "yellow" in spec:
        cfg.timing.yellow = spec["yellow"]
    if "all_red" in spec:
        cfg.timing.all_red = spec["all_red"]
    return cfg


class SafetyObserver:
    """Independent check of lamp outputs (does not use the FSM's own monitor)."""

    def __init__(self, ctrl: Controller) -> None:
        self.ctrl = ctrl
        self.conflicts = ctrl.cfg.conflicts()
        self.prev: dict[int, str] = {}
        self.conflict_steps = 0
        self.conflict_time = 0.0
        self.first_conflict_at: float | None = None
        self.bad_sequences = 0
        self.flash_at: float | None = None

    def check(self, dt: float) -> None:
        ind = self.ctrl.indications()
        if ind.flashing:
            if self.flash_at is None:
                self.flash_at = self.ctrl.now
            self.prev = {}
            return
        if any(pair <= ind.greens for pair in self.conflicts):
            self.conflict_steps += 1
            self.conflict_time += dt
            if self.first_conflict_at is None:
                self.first_conflict_at = self.ctrl.now
        cur = {}
        for p in self.ctrl.cfg.phases:
            if p in ind.greens:
                cur[p] = "G"
            elif p in ind.yellows:
                cur[p] = "Y"
            elif p in ind.red_yellows:
                cur[p] = "RY"
            else:
                cur[p] = "R"
            old = self.prev.get(p)
            if old == "G" and cur[p] == "R" and not self.ctrl.fault_phase:
                self.bad_sequences += 1
            if old == "R" and cur[p] == "G" and not self.ctrl.fault_phase:
                self.bad_sequences += 1
        self.prev = cur


def run_one(spec: dict) -> dict:
    """Run a single simulation described by ``spec``; returns metrics."""
    cfg = build_cfg(spec)
    rates = spec["rates"]
    pattern = spec["pattern"]
    info: dict = {}
    if pattern == WEBSTER:
        greens, C = webster_plan(cfg, rates)
        cfg.patterns[WEBSTER] = greens
        info = {"webster_greens": greens, "webster_cycle": round(C, 1)}
    elif pattern == "best_fixed":
        pattern = spec["best_fixed"]
    ctrl = Controller(cfg, pattern=pattern)
    sim = TrafficSim(ctrl, dict(rates), seed=spec["seed"], profile=spec.get("profile", "flat"),
                     detector=spec.get("detector", "stopline"), setback_m=spec.get("setback_m", 40.0),
                     miss_rate=spec.get("miss_rate", 0.0), peds=spec.get("peds", False),
                     ped_rate=spec.get("ped_rate", 90.0))
    obs = SafetyObserver(ctrl)
    dt = spec.get("dt", 0.1)
    steps = int(spec["minutes"] * 60 / dt)
    gap_out = max_out = fixed_end = 0
    greens_len: list[float] = []
    prev_int = ctrl.interval
    preempt_every = spec.get("preempt_every_s")
    fault = spec.get("fault")
    fault_at = spec.get("fault_at_s", 600.0)
    for i in range(steps):
        if preempt_every and i and (i * dt) % preempt_every < dt / 2:
            ctrl.request_preempt(len(cfg.stages) - 1)
        if fault and abs(i * dt - fault_at) < dt / 2:
            ctrl.inject_fault(Alarm[fault["alarm"]], fault.get("phase", 1))
        g_before, stage_before = ctrl.green_time, ctrl.stage
        ctrl.step(dt)
        sim.step(dt)
        obs.check(dt)
        if prev_int == Interval.GREEN and ctrl.interval == Interval.YELLOW:
            greens_len.append(g_before)
            if ctrl.pattern != PATTERN_FREE:
                fixed_end += 1
            elif g_before + 1e-6 >= cfg.stage_max(stage_before) - dt:
                max_out += 1
            else:
                gap_out += 1
        prev_int = ctrl.interval
    out = sim.summary()
    arrived = sum(s.arrived for s in sim.stats.values())
    in_net = sum(len(v) for v in sim.in_transit.values())
    out.update(info)
    out.update({
        "arrived": arrived,
        "throughput_ratio": round(out["served"] / max(arrived - in_net, 1), 3),
        "residual_queue": sum(s.queue for s in sim.stats.values()),
        "cycles": ctrl.cycle_count,
        "avg_cycle_s": round(spec["minutes"] * 60 / ctrl.cycle_count, 1) if ctrl.cycle_count else None,
        "gap_outs": gap_out, "max_outs": max_out, "fixed_ends": fixed_end,
        "avg_green_s": round(sum(greens_len) / len(greens_len), 1) if greens_len else 0.0,
        "conflict_steps": obs.conflict_steps,
        "conflict_time_s": round(obs.conflict_time, 2),
        "bad_sequences": obs.bad_sequences,
        "flash_at_s": None if obs.flash_at is None else round(obs.flash_at, 2),
        "first_conflict_at_s": None if obs.first_conflict_at is None else round(obs.first_conflict_at, 2),
        "alarms": [a.name for a in Alarm if a and a in ctrl.alarms],
        "peds_crossed": sum(sim.ped_crossed.values()),
    })
    return out


def aggregate(runs: list[dict]) -> dict:
    n = len(runs)

    def mean(k: str) -> float:
        vals = [r[k] for r in runs if r.get(k) is not None]
        return round(sum(vals) / len(vals), 2) if vals else None

    agg = {k: mean(k) for k in ("avg_delay_s", "served", "arrived", "throughput_ratio", "residual_queue",
                                "cycles", "avg_cycle_s", "gap_outs", "max_outs", "avg_green_s",
                                "conflict_time_s", "flash_at_s", "peds_crossed")}
    agg["max_queue"] = max(r["max_queue"] for r in runs)
    agg["delay_min"] = min(r["avg_delay_s"] for r in runs)
    agg["delay_max"] = max(r["avg_delay_s"] for r in runs)
    agg["conflict_steps"] = sum(r["conflict_steps"] for r in runs)
    agg["bad_sequences"] = sum(r["bad_sequences"] for r in runs)
    agg["alarms"] = sorted({a for r in runs for a in r["alarms"]})
    agg["per_phase_delay"] = {p: round(sum(r["per_phase_delay"][p] for r in runs) / n, 1)
                              for p in runs[0]["per_phase_delay"]}
    for k in ("webster_greens", "webster_cycle"):
        if k in runs[0]:
            agg[k] = runs[0][k]
    agg["seeds"] = n
    return agg


# --------------------------------------------------------------- experiments
def scaled(rates: dict[int, float], f: float) -> dict[int, float]:
    return {p: round(v * f, 1) for p, v in rates.items()}


def experiments(minutes: float) -> dict[str, dict]:
    fl = BASE_RATES["four_leg"]
    base = {"preset": "four_leg", "rates": fl, "minutes": minutes, "pattern": PATTERN_FREE}
    E: dict[str, dict] = {}

    E["E1"] = {"title": "Demand level sweep (4-arm, fixed vs Webster vs actuated)",
               "param": "demand_factor", "cases": []}
    for f in (0.4, 0.6, 0.8, 1.0, 1.15, 1.3):
        for name, pat in (("Fixed P1 (35/20)", 1), ("Webster", WEBSTER), ("Actuated", PATTERN_FREE)):
            E["E1"]["cases"].append({"label": name, "x": f, **base, "rates": scaled(fl, f), "pattern": pat})

    E["E2"] = {"title": "Detector placement (actuated, 4-arm, demand 1.0x)", "param": "detector", "cases": []}
    E["E2"]["cases"].append({"label": "Stop line (presence)", "x": "stopline", **base})
    for d in (10, 25, 40, 70, 120):
        E["E2"]["cases"].append({"label": f"Upstream only {d} m", "x": f"upstream_{d}", **base,
                                 "detector": "upstream", "setback_m": d})
    E["E2"]["cases"].append({"label": "Stop line + 40 m", "x": "both_40", **base,
                             "detector": "both", "setback_m": 40})
    E["E2"]["cases"].append({"label": "No detector", "x": "none", **base, "detector": "none"})

    E["E3"] = {"title": "Passage (gap) time sweep", "param": "passage", "cases": []}
    for f in (0.8, 1.15):
        for pas in (0.5, 1.0, 1.5, 2.5, 3.5, 5.0, 7.0):
            E["E3"]["cases"].append({"label": f"demand {f}x", "x": pas, **base, "rates": scaled(fl, f),
                                     "passage": pas})

    E["E4"] = {"title": "Max green sweep (demand 1.15x)", "param": "max_green", "cases": []}
    for mg in (15, 20, 30, 45, 60, 90):
        E["E4"]["cases"].append({"label": "Actuated", "x": mg, **base, "rates": scaled(fl, 1.15),
                                 "max_green": mg})

    E["E5"] = {"title": "Detector miss rate (faulty / dirty loop)", "param": "miss_rate", "cases": []}
    for m in (0.0, 0.05, 0.1, 0.2, 0.35, 0.5):
        E["E5"]["cases"].append({"label": "Stop line", "x": m, **base, "miss_rate": m})
        E["E5"]["cases"].append({"label": "Upstream 40 m", "x": m, **base, "miss_rate": m,
                                 "detector": "upstream", "setback_m": 40})

    E["E6"] = {"title": "Junction layouts", "param": "preset", "cases": []}
    for preset in ("four_leg", "left_turn", "t_junction"):
        b = {**base, "preset": preset, "rates": BASE_RATES[preset]}
        for name, pat in (("Fixed P1", 1), ("Fixed P2", 2), ("Webster", WEBSTER), ("Actuated", PATTERN_FREE)):
            E["E6"]["cases"].append({"label": name, "x": preset, **b, "pattern": pat})

    E["E7"] = {"title": "Pedestrian demand (push button)", "param": "ped_rate", "cases": []}
    for pr in (0, 30, 90, 200, 400):
        E["E7"]["cases"].append({"label": "Actuated", "x": pr, **base, "peds": pr > 0, "ped_rate": pr})
        E["E7"]["cases"].append({"label": "Fixed P1", "x": pr, **base, "pattern": 1, "peds": pr > 0, "ped_rate": pr})

    E["E8"] = {"title": "Emergency vehicle preemption frequency", "param": "preempt_every_s", "cases": []}
    for pe in (0, 600, 300, 120, 60):
        E["E8"]["cases"].append({"label": "Actuated", "x": pe, **base,
                                 **({"preempt_every_s": pe} if pe else {})})

    E["E9"] = {"title": "Fault injection (t=600 s)", "param": "fault", "cases": []}
    E["E9"]["cases"].append({"label": "No fault", "x": "none", **base})
    E["E9"]["cases"].append({"label": "Stuck detector (phase 2, always occupied)", "x": "DETECTOR_FAULT", **base,
                             "fault": {"alarm": "DETECTOR_FAULT", "phase": 2}})
    E["E9"]["cases"].append({"label": "Welded relay: phase 2 green (conflict)", "x": "CONFLICT", **base,
                             "fault": {"alarm": "CONFLICT", "phase": 2}})
    E["E9"]["cases"].append({"label": "Red lamp failure (phase 3)", "x": "RED_LAMP_OUT", **base,
                             "fault": {"alarm": "RED_LAMP_OUT", "phase": 3}})

    E["E10"] = {"title": "Demand profile: flat vs peak wave", "param": "profile", "cases": []}
    for prof in ("flat", "peak"):
        for name, pat in (("Fixed P1", 1), ("Webster", WEBSTER), ("Actuated", PATTERN_FREE)):
            E["E10"]["cases"].append({"label": name, "x": prof, **base, "profile": prof, "pattern": pat})

    E["E11"] = {"title": "Intergreen (amber + all-red) effect", "param": "intergreen", "cases": []}
    for y, ar in ((3, 1), (3, 2), (4, 2), (4, 3)):
        E["E11"]["cases"].append({"label": "Actuated", "x": f"{y}+{ar}", **base, "yellow": y, "all_red": ar})
    return E


def _run_case(args: tuple[dict, int]) -> dict:
    case, seeds = args
    runs = [run_one({**case, "seed": s}) for s in range(seeds)]
    return aggregate(runs)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--only", default="", help="comma separated experiment ids")
    ap.add_argument("--out", default="reports/results.json")
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 2)
    args = ap.parse_args()

    E = experiments(args.minutes)
    if args.only:
        keep = set(args.only.split(","))
        E = {k: v for k, v in E.items() if k in keep}
    jobs = [(eid, i, c) for eid, e in E.items() for i, c in enumerate(e["cases"])]
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=args.jobs) as ex:
        results = list(ex.map(_run_case, [(c, args.seeds) for _, _, c in jobs]))
    for (eid, i, _), res in zip(jobs, results):
        E[eid]["cases"][i]["result"] = res
    out = {"meta": {"minutes": args.minutes, "seeds": args.seeds, "runs": len(jobs) * args.seeds,
                    "wall_s": round(time.time() - t0, 1)}, "experiments": E}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(copy.deepcopy(out), ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    for eid, e in E.items():
        print(f"\n== {eid}: {e['title']}")
        for c in e["cases"]:
            r = c["result"]
            print(f"  {c['label']:<38} x={c['x']!s:<14} delay={r['avg_delay_s']:>7}s  "
                  f"maxQ={r['max_queue']:>4}  thr={r['throughput_ratio']}  gap/max={r['gap_outs']}/{r['max_outs']}"
                  f"  cyc={r['avg_cycle_s']}  conflict_steps={r['conflict_steps']}  badseq={r['bad_sequences']}"
                  f"  flash={r['flash_at_s']}")
    print(f"\n{out['meta']}")


if __name__ == "__main__":
    main()
