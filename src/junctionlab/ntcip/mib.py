"""NTCIP 1202 (Actuated Signal Controller) object subset + MIB-II system group.

Only the objects a central system needs for monitoring and pattern control
are implemented. OIDs follow the NTCIP 1202 tree under
``iso.org.dod.internet.private.enterprises.nema(1206).transportation(4)
.devices(2).asc(1)``. Anything not covered by the standard (red+amber
interval, alarms, fault injection) lives under a private enterprise branch.

NOTE: this is a learning/demo subset, not a conformance-tested
implementation; check object definitions against the official MIB before
relying on them.
"""

from __future__ import annotations

from ..snmp import pdu
from ..snmp.agent import MibTree, SetError
from ..snmp.ber import Counter32, Oid, TimeTicks

# ------------------------------------------------------------------ OIDs
MIB2_SYSTEM = Oid("1.3.6.1.2.1.1")
SYS_DESCR = MIB2_SYSTEM + (1, 0)
SYS_OBJECT_ID = MIB2_SYSTEM + (2, 0)
SYS_UPTIME = MIB2_SYSTEM + (3, 0)
SYS_CONTACT = MIB2_SYSTEM + (4, 0)
SYS_NAME = MIB2_SYSTEM + (5, 0)
SYS_LOCATION = MIB2_SYSTEM + (6, 0)

ASC = Oid("1.3.6.1.4.1.1206.4.2.1")
PHASE = ASC + (1,)
MAX_PHASES = PHASE + (1, 0)
PHASE_TABLE = PHASE + (2, 1)            # phaseEntry.<column>.<phaseNumber>
MAX_PHASE_GROUPS = PHASE + (3, 0)
PHASE_STATUS_GROUP = PHASE + (4, 1)     # phaseStatusGroupEntry.<column>.<group>
PHASE_CONTROL_GROUP = PHASE + (5, 1)    # phaseControlGroupEntry.<column>.<group>
COORD = ASC + (4,)
COORD_PATTERN_STATUS = COORD + (10, 0)
SYSTEM_PATTERN_CONTROL = COORD + (14, 0)

# phaseEntry columns
PH_NUMBER, PH_WALK, PH_PED_CLEAR, PH_MIN_GREEN, PH_PASSAGE, PH_MAX1 = 1, 2, 3, 4, 5, 6
PH_YELLOW, PH_RED_CLEAR = 8, 9
# phaseStatusGroupEntry columns
PSG_NUMBER, PSG_REDS, PSG_YELLOWS, PSG_GREENS, PSG_DONT_WALKS, PSG_PED_CLEARS, PSG_WALKS = 1, 2, 3, 4, 5, 6, 7
PSG_VEH_CALLS, PSG_PED_CALLS, PSG_PHASE_ONS, PSG_PHASE_NEXTS = 8, 9, 10, 11
# phaseControlGroupEntry columns
PCG_NUMBER, PCG_VEH_CALL, PCG_PED_CALL = 1, 6, 7

# private enterprise branch (placeholder PEN - not registered with IANA)
JL = Oid("1.3.6.1.4.1.99999.1")
JL_CONTROL_MODE = JL + (1, 0)
JL_CURRENT_STAGE = JL + (2, 0)
JL_INTERVAL = JL + (3, 0)
JL_RED_YELLOWS = JL + (4, 0)
JL_ALARMS = JL + (5, 0)
JL_CYCLE_COUNT = JL + (6, 0)
JL_PREEMPT = JL + (7, 0)
JL_FAULT_INJECT = JL + (8, 0)
JL_FAULT_CLEAR = JL + (9, 0)
JL_FLASHING = JL + (11, 0)
JL_DETECTOR_VOLUME = JL + (10, 1)       # .<phase>
# live traffic model (only meaningful with --sim-traffic, 0 otherwise)
JL_QUEUE = JL + (13, 1)                 # .<phase> vehicles waiting at the stop line
JL_SERVED = JL + (14, 1)                # .<phase> Counter32 vehicles discharged
JL_PED_WAITING = JL + (15, 1)           # .<phase> pedestrians at the kerb
JL_PED_CROSSED = JL + (16, 1)           # .<phase> Counter32 pedestrians started crossing
JL_TRAPS = Oid("1.3.6.1.4.1.99999.0")
TRAP_ALARM = JL_TRAPS + (1,)
TRAP_ALARM_CLEARED = JL_TRAPS + (2,)
TRAP_MODE = JL_TRAPS + (3,)


def status_oid(column: int, group: int = 1) -> Oid:
    return PHASE_STATUS_GROUP + (column, group)


def phase_oid(column: int, phase: int) -> Oid:
    return PHASE_TABLE + (column, phase)


def build_mib(ctrl, agent_uptime, name: str = "junction", location: str = "", sim=None) -> MibTree:
    """Expose a fsm.Controller through the MIB tree."""
    from ..controller.fsm import FAULT_CODES, mask  # local import to avoid a cycle

    cfg = ctrl.cfg
    mib = MibTree()

    mib.scalar(SYS_DESCR, f"junction-lab virtual ASC ({cfg.name})")
    mib.scalar(SYS_OBJECT_ID, JL)
    mib.register(SYS_UPTIME, agent_uptime, value_type=None)
    mib.scalar(SYS_CONTACT, "R&D")
    mib.scalar(SYS_NAME, name)
    mib.scalar(SYS_LOCATION, location)

    # ---- phase configuration
    mib.scalar(MAX_PHASES, max(cfg.phases))
    mib.scalar(MAX_PHASE_GROUPS, 1)
    tm = cfg.timing
    for num, ph in sorted(cfg.phases.items()):
        mib.scalar(phase_oid(PH_NUMBER, num), num)
        mib.register(phase_oid(PH_WALK, num), lambda ph=ph: ph.walk)
        mib.register(phase_oid(PH_PED_CLEAR, num), lambda ph=ph: ph.ped_clear)
        mib.register(phase_oid(PH_MIN_GREEN, num), lambda ph=ph: ph.min_green,
                     _setter_range(lambda v, ph=ph: setattr(ph, "min_green", v), 1, 255))
        mib.register(phase_oid(PH_PASSAGE, num), lambda ph=ph: int(ph.passage * 10),  # tenths
                     _setter_range(lambda v, ph=ph: setattr(ph, "passage", v / 10), 0, 255))
        mib.register(phase_oid(PH_MAX1, num), lambda ph=ph: ph.max_green,
                     _setter_range(lambda v, ph=ph: setattr(ph, "max_green", v), 1, 255))
        mib.scalar(phase_oid(PH_YELLOW, num), int(tm.yellow * 10))
        mib.scalar(phase_oid(PH_RED_CLEAR, num), int(tm.all_red * 10))

    # ---- phase status group 1 (phases 1..8)
    def ind_mask(attr: str):
        return lambda: mask(getattr(ctrl.indications(), attr)) & 0xFF

    def phase_ons() -> int:
        return mask(ctrl._cur()) & 0xFF

    def phase_nexts() -> int:
        return mask(ctrl._nxt()) & 0xFF

    mib.scalar(status_oid(PSG_NUMBER), 1)
    mib.register(status_oid(PSG_REDS), ind_mask("reds"))
    mib.register(status_oid(PSG_YELLOWS), ind_mask("yellows"))
    mib.register(status_oid(PSG_GREENS), ind_mask("greens"))
    mib.register(status_oid(PSG_DONT_WALKS), ind_mask("dont_walks"))
    mib.register(status_oid(PSG_PED_CLEARS), ind_mask("ped_clears"))
    mib.register(status_oid(PSG_WALKS), ind_mask("walks"))
    mib.register(status_oid(PSG_VEH_CALLS), lambda: mask(ctrl.veh_calls) & 0xFF)
    mib.register(status_oid(PSG_PED_CALLS), lambda: mask(ctrl.ped_calls) & 0xFF)
    mib.register(status_oid(PSG_PHASE_ONS), phase_ons)
    mib.register(status_oid(PSG_PHASE_NEXTS), phase_nexts)

    # ---- phase control group 1: writing a bitmask places calls
    def place(fn):
        def setter(v: int) -> None:
            if not 0 <= v <= 0xFF:
                raise SetError(pdu.WRONG_VALUE)
            for p in range(1, 9):
                if v & (1 << (p - 1)):
                    fn(p)
        return setter

    mib.scalar(PHASE_CONTROL_GROUP + (PCG_NUMBER, 1), 1)
    mib.register(PHASE_CONTROL_GROUP + (PCG_VEH_CALL, 1), lambda: 0, place(ctrl.vehicle_arrival))
    mib.register(PHASE_CONTROL_GROUP + (PCG_PED_CALL, 1), lambda: 0, place(ctrl.ped_request))

    # ---- coordination / pattern
    def set_pattern(v: int) -> None:
        try:
            ctrl.set_pattern(v)
        except ValueError as exc:
            raise SetError(pdu.WRONG_VALUE, str(exc)) from exc

    mib.register(COORD_PATTERN_STATUS, lambda: ctrl.pattern)
    mib.register(SYSTEM_PATTERN_CONTROL, lambda: ctrl.pattern, set_pattern)

    # ---- private branch
    mib.register(JL_CONTROL_MODE, lambda: int(ctrl.mode))
    mib.register(JL_CURRENT_STAGE, lambda: ctrl.stage + 1)  # 1-based, 0 = none
    mib.register(JL_INTERVAL, lambda: int(ctrl.interval))
    mib.register(JL_RED_YELLOWS, ind_mask("red_yellows"))
    mib.register(JL_ALARMS, lambda: int(ctrl.alarms))
    mib.register(JL_CYCLE_COUNT, lambda: Counter32(ctrl.cycle_count & 0xFFFFFFFF), value_type=None)
    mib.register(JL_FLASHING, lambda: int(ctrl.indications().flashing))

    def set_preempt(v: int) -> None:
        if v == 0:
            ctrl.preempt_target = None
            return
        try:
            ctrl.request_preempt(v - 1)
        except ValueError as exc:
            raise SetError(pdu.WRONG_VALUE, str(exc)) from exc

    mib.register(JL_PREEMPT, lambda: (ctrl.preempt_target + 1) if ctrl.preempt_target is not None else 0,
                 set_preempt)

    def inject(v: int) -> None:  # value = code*100 + phase, e.g. 201 = red lamp out on phase 1
        code, phase = divmod(v, 100)
        if code not in FAULT_CODES or phase not in cfg.phases:
            raise SetError(pdu.WRONG_VALUE)
        ctrl.inject_fault(FAULT_CODES[code], phase)

    mib.register(JL_FAULT_INJECT, lambda: 0, inject)
    mib.register(JL_FAULT_CLEAR, lambda: 0, lambda v: ctrl.clear_faults())
    for num in sorted(cfg.phases):
        mib.register(JL_DETECTOR_VOLUME + (num,), lambda n=num: Counter32(ctrl.volume[n] & 0xFFFFFFFF),
                     value_type=None)

    def stat(n: int, attr: str) -> int:
        st = sim.stats.get(n) if sim else None
        return getattr(st, attr) if st else 0

    def ped(n: int, attr: str) -> int:
        return getattr(sim, attr).get(n, 0) if sim else 0

    for num in sorted(cfg.phases):
        mib.register(JL_QUEUE + (num,), lambda n=num: stat(n, "queue"))
        mib.register(JL_SERVED + (num,), lambda n=num: Counter32(stat(n, "served") & 0xFFFFFFFF), value_type=None)
        mib.register(JL_PED_WAITING + (num,), lambda n=num: ped(n, "ped_waiting"))
        mib.register(JL_PED_CROSSED + (num,), lambda n=num: Counter32(ped(n, "ped_crossed") & 0xFFFFFFFF),
                     value_type=None)
    return mib


def _setter_range(fn, lo: int, hi: int):
    def setter(v: int) -> None:
        if not lo <= v <= hi:
            raise SetError(pdu.WRONG_VALUE, f"{v} not in {lo}..{hi}")
        fn(v)
    return setter


def bits(value: int) -> list[int]:
    """Bitmask -> 1-based phase list."""
    return [i + 1 for i in range(16) if value & (1 << i)]


__all__ = ["TimeTicks", "bits", "build_mib"]
