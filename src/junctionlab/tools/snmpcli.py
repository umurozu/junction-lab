"""jl-snmp: tiny snmpget / snmpwalk / snmpset for field debugging.

    jl-snmp 127.0.0.1:16101 get  greens reds sysName
    jl-snmp 127.0.0.1:16101 walk 1.3.6.1.4.1.1206.4.2.1.1.2
    jl-snmp 127.0.0.1:16101 set  pattern 255
    jl-snmp 127.0.0.1:16101 watch            # live phase status, 1 Hz

Symbolic names map to the NTCIP objects in junctionlab.ntcip.mib.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from ..netutil import run as netrun
from ..ntcip import mib as ntcip
from ..snmp.ber import Oid
from ..snmp.client import SnmpClient, SnmpError, SnmpTimeout

NAMES = {
    "sysDescr": ntcip.SYS_DESCR, "sysName": ntcip.SYS_NAME, "sysUpTime": ntcip.SYS_UPTIME,
    "maxPhases": ntcip.MAX_PHASES,
    "reds": ntcip.status_oid(ntcip.PSG_REDS), "yellows": ntcip.status_oid(ntcip.PSG_YELLOWS),
    "greens": ntcip.status_oid(ntcip.PSG_GREENS), "walks": ntcip.status_oid(ntcip.PSG_WALKS),
    "vehCalls": ntcip.status_oid(ntcip.PSG_VEH_CALLS), "phaseOns": ntcip.status_oid(ntcip.PSG_PHASE_ONS),
    "pattern": ntcip.SYSTEM_PATTERN_CONTROL, "patternStatus": ntcip.COORD_PATTERN_STATUS,
    "mode": ntcip.JL_CONTROL_MODE, "stage": ntcip.JL_CURRENT_STAGE, "alarms": ntcip.JL_ALARMS,
    "phaseTable": ntcip.PHASE_TABLE, "asc": ntcip.ASC,
}
REVERSE = {str(v): k for k, v in NAMES.items()}


def resolve(name: str) -> Oid:
    return NAMES.get(name) or Oid(name)


def fmt(oid: Oid, val) -> str:
    name = REVERSE.get(str(oid), str(oid))
    if isinstance(val, bytes):
        try:
            val = val.decode()
        except UnicodeDecodeError:
            val = val.hex()
    extra = ""
    if name in ("reds", "yellows", "greens", "walks", "vehCalls", "phaseOns") and isinstance(val, int):
        extra = f"   phases {ntcip.bits(val)}"
    return f"{name} = {type(val).__name__}: {val}{extra}"


async def amain(args: argparse.Namespace) -> int:
    host, _, port = args.target.rpartition(":")
    c = SnmpClient(host or "127.0.0.1", int(port), args.community, args.write_community, timeout=args.timeout)
    try:
        if args.op == "get":
            for oid, val in (await c.get(*(resolve(n) for n in args.args))).items():
                print(fmt(oid, val))
        elif args.op == "walk":
            n = 0
            async for oid, val in c.walk(resolve(args.args[0] if args.args else "asc")):
                print(fmt(oid, val))
                n += 1
            print(f"-- {n} objects", file=sys.stderr)
        elif args.op == "set":
            pairs = [(resolve(args.args[i]), int(args.args[i + 1])) for i in range(0, len(args.args), 2)]
            for oid, val in (await c.set(*pairs)).items():
                print(fmt(oid, val))
        elif args.op == "watch":
            oids = [NAMES[n] for n in ("greens", "yellows", "reds", "stage", "mode", "patternStatus")]
            while True:
                t0 = time.perf_counter()
                v = await c.get(*oids)
                g, y, r, st, mode, pat = (v[o] for o in oids)
                print(f"\r{time.strftime('%H:%M:%S')}  stage {st}  mode {mode}  pattern {pat:3}  "
                      f"G{ntcip.bits(g)} Y{ntcip.bits(y)} R{ntcip.bits(r)}  "
                      f"{(time.perf_counter() - t0) * 1000:.1f} ms     ", end="", flush=True)
                await asyncio.sleep(1)
        return 0
    except SnmpTimeout as exc:
        print(f"timeout: {exc}", file=sys.stderr)
        return 2
    except SnmpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        c.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", help="host:port")
    ap.add_argument("op", choices=["get", "walk", "set", "watch"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("-c", "--community", default="public")
    ap.add_argument("-w", "--write-community", default="private")
    ap.add_argument("-t", "--timeout", type=float, default=1.0)
    try:
        sys.exit(netrun(amain(ap.parse_args())))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
