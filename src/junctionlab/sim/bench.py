"""Benchmark: fixed-time vs actuated control on the same random traffic.

    jl-bench --preset four_leg --minutes 60 --rates 1:700,2:250,3:650,4:200
"""

from __future__ import annotations

import argparse

from ..controller.config import PATTERN_FREE, PRESETS
from ..controller.fsm import Controller
from .traffic import TrafficSim


def run(preset: str, pattern: int, rates: dict[int, float], minutes: float, seed: int,
        profile: str = "flat", dt: float = 0.1) -> dict:
    ctrl = Controller(PRESETS[preset](), pattern=pattern)
    sim = TrafficSim(ctrl, rates, seed=seed, profile=profile)
    for _ in range(int(minutes * 60 / dt)):
        ctrl.step(dt)
        sim.step(dt)
    out = sim.summary()
    out["cycles"] = ctrl.cycle_count
    return out


def parse_rates(text: str) -> dict[int, float]:
    return {int(k): float(v) for k, v in (item.split(":") for item in text.split(","))}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--preset", default="four_leg", choices=sorted(PRESETS))
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--rates", default="1:700,2:250,3:650,4:200", help="phase:veh_per_hour,...")
    ap.add_argument("--profile", default="peak", choices=["flat", "peak"])
    ap.add_argument("--seeds", type=int, default=5)
    args = ap.parse_args()
    rates = parse_rates(args.rates)
    cfg = PRESETS[args.preset]()

    rows = []
    for pattern in [*sorted(cfg.patterns), PATTERN_FREE]:
        results = [run(args.preset, pattern, rates, args.minutes, seed, args.profile) for seed in range(args.seeds)]
        delay = sum(r["avg_delay_s"] for r in results) / len(results)
        mq = max(r["max_queue"] for r in results)
        served = sum(r["served"] for r in results) / len(results)
        rows.append((cfg.pattern_names.get(pattern, str(pattern)), pattern, delay, mq, served))

    print(f"\n{args.preset}: {args.minutes:.0f} min, demand {rates} veh/h, profile={args.profile}, "
          f"{args.seeds} seeds\n")
    print(f"{'Plan':<18}{'#':>5}{'Avg delay (s)':>16}{'Max queue':>12}{'Served':>10}")
    for name, num, delay, mq, served in rows:
        print(f"{name:<18}{num:>5}{delay:>16.1f}{mq:>12}{served:>10.0f}")
    best_fixed = min(r[2] for r in rows if r[1] != PATTERN_FREE)
    act = next(r[2] for r in rows if r[1] == PATTERN_FREE)
    print(f"\nActuated vs best fixed plan: {100 * (best_fixed - act) / best_fixed:+.1f}% delay reduction")


if __name__ == "__main__":
    main()
