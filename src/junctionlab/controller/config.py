"""Junction configuration: phases (signal groups), stages and timing patterns."""

from __future__ import annotations

from dataclasses import dataclass, field

PATTERN_FREE = 254   # NTCIP systemPatternControl: free (actuated, no coordination)
PATTERN_FLASH = 255  # NTCIP systemPatternControl: flash


@dataclass
class Phase:
    number: int
    name: str
    approach: str            # N / E / S / W - used by the UI and by the camera zones
    min_green: int = 7       # s   (NTCIP phaseMinimumGreen)
    max_green: int = 45      # s   (NTCIP phaseMaximum1)
    passage: float = 2.5     # s   (NTCIP phasePassage, vehicle extension / gap)
    walk: int = 6            # s   (NTCIP phaseWalk)
    ped_clear: int = 5       # s   (NTCIP phasePedestrianClear)
    has_ped: bool = True


@dataclass
class Stage:
    name: str
    phases: tuple[int, ...]
    recall: bool = False     # always served even without demand (main road)


@dataclass
class Timing:
    yellow: float = 3.0      # s  amber
    all_red: float = 2.0     # s  red clearance
    red_yellow: float = 2.0  # s  red+amber before green (TR/EU practice, not in NTCIP)
    startup_all_red: float = 4.0
    preempt_min_green: float = 5.0
    preempt_hold: float = 15.0


@dataclass
class JunctionConfig:
    name: str
    phases: dict[int, Phase]
    stages: list[Stage]
    # fixed-time patterns: pattern number -> green time (s) per stage
    patterns: dict[int, list[int]]
    pattern_names: dict[int, str] = field(default_factory=dict)
    timing: Timing = field(default_factory=Timing)

    def stage_min(self, i: int) -> float:
        return max(self.phases[p].min_green for p in self.stages[i].phases)

    def stage_max(self, i: int) -> float:
        return max(self.phases[p].max_green for p in self.stages[i].phases)

    def stage_passage(self, i: int) -> float:
        return max(self.phases[p].passage for p in self.stages[i].phases)

    def conflicts(self) -> set[frozenset[int]]:
        """Phase pairs that are never green together -> must never be green together."""
        together = {frozenset((a, b)) for s in self.stages for a in s.phases for b in s.phases if a != b}
        nums = sorted(self.phases)
        return {frozenset((a, b)) for i, a in enumerate(nums) for b in nums[i + 1:]} - together

    def validate(self) -> None:
        for s in self.stages:
            for p in s.phases:
                if p not in self.phases:
                    raise ValueError(f"stage {s.name} references unknown phase {p}")
        for num, greens in self.patterns.items():
            if len(greens) != len(self.stages):
                raise ValueError(f"pattern {num} has {len(greens)} splits for {len(self.stages)} stages")
            if num in (PATTERN_FREE, PATTERN_FLASH) or not 1 <= num <= 253:
                raise ValueError(f"pattern number {num} is reserved / invalid")


def four_leg(name: str = "4-arm junction") -> JunctionConfig:
    """Classic 4-arm junction, 2 stages: north-south then east-west."""
    phases = {
        1: Phase(1, "North", "N", min_green=10, max_green=50),
        2: Phase(2, "East", "E", min_green=7, max_green=35),
        3: Phase(3, "South", "S", min_green=10, max_green=50),
        4: Phase(4, "West", "W", min_green=7, max_green=35),
    }
    stages = [Stage("A: North-South", (1, 3), recall=True), Stage("B: East-West", (2, 4))]
    patterns = {1: [35, 20], 2: [25, 30], 3: [15, 12]}
    names = {1: "AM peak", 2: "PM peak", 3: "Night", PATTERN_FREE: "Actuated",
             PATTERN_FLASH: "Amber flash"}
    cfg = JunctionConfig(name, phases, stages, patterns, names)
    cfg.validate()
    return cfg


def four_leg_left_turn(name: str = "Junction with left-turn stage") -> JunctionConfig:
    """4-arm junction with a protected left-turn stage for the main road (3 stages)."""
    phases = {
        1: Phase(1, "North", "N", min_green=10, max_green=45),
        2: Phase(2, "East", "E", min_green=7, max_green=30),
        3: Phase(3, "South", "S", min_green=10, max_green=45),
        4: Phase(4, "West", "W", min_green=7, max_green=30),
        5: Phase(5, "North left turn", "N", min_green=5, max_green=15, has_ped=False),
        6: Phase(6, "South left turn", "S", min_green=5, max_green=15, has_ped=False),
    }
    stages = [Stage("A: North-South through", (1, 3), recall=True),
              Stage("B: Left turns", (5, 6)),
              Stage("C: East-West", (2, 4))]
    patterns = {1: [30, 10, 20], 2: [25, 12, 25], 3: [15, 6, 12]}
    names = {1: "AM peak", 2: "PM peak", 3: "Night", PATTERN_FREE: "Actuated",
             PATTERN_FLASH: "Amber flash"}
    cfg = JunctionConfig(name, phases, stages, patterns, names)
    cfg.validate()
    return cfg


def t_junction(name: str = "T-junction") -> JunctionConfig:
    phases = {
        1: Phase(1, "West", "W", min_green=10, max_green=45),
        2: Phase(2, "East", "E", min_green=10, max_green=45),
        3: Phase(3, "South", "S", min_green=6, max_green=25),
    }
    stages = [Stage("A: Main road", (1, 2), recall=True), Stage("B: Side road", (3,))]
    patterns = {1: [35, 15], 2: [30, 20], 3: [20, 10]}
    names = {1: "AM peak", 2: "PM peak", 3: "Night", PATTERN_FREE: "Actuated",
             PATTERN_FLASH: "Amber flash"}
    cfg = JunctionConfig(name, phases, stages, patterns, names)
    cfg.validate()
    return cfg


PRESETS = {"four_leg": four_leg, "left_turn": four_leg_left_turn, "t_junction": t_junction}
