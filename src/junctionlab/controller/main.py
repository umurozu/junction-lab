"""Virtual junction controller process.

Runs the signal state machine in real time and exposes it through:
  * SNMPv2c agent with NTCIP 1202 objects       (udp/1161 by default)
  * SNMPv2c traps for alarms / mode changes     (-> center udp/1162)
  * JLP binary protocol: status/counts/alarms   (-> center udp/15050)
                         detector + commands     (<- udp/6000+id)

    jl-controller --device-id 1 --preset four_leg --sim-traffic
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time

from .. import sdnotify
from ..netutil import run as netrun
from ..netutil import udp_endpoint
from ..ntcip import mib as ntcip
from ..proto import frame as jlp
from ..proto.frame import Command, Frame, MsgType
from ..sim.bench import parse_rates
from ..sim.traffic import TrafficSim
from ..snmp.agent import SnmpAgent
from ..snmp.ber import Oid
from .config import PATTERN_FREE, PRESETS
from .fsm import FAULT_CODES, Controller, mask

log = logging.getLogger("controller")


class JlpEndpoint(asyncio.DatagramProtocol):
    def __init__(self, app: ControllerApp) -> None:
        self.app = app
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:  # type: ignore[override]
        try:
            f = Frame.decode(data)
        except jlp.FrameError as exc:
            self.app.bad_frames += 1
            log.warning("bad JLP frame from %s: %s", addr, exc)
            return
        if f.type == MsgType.DETECTOR:
            phase, count, presence = jlp.unpack_detector(f.payload)
            if count:
                self.app.ctrl.vehicle_arrival(phase, count)  # vehicle crossed the (virtual) loop
            elif presence:
                self.app.ctrl.place_call(phase)              # vehicle waiting in the zone
        elif f.type == MsgType.COMMAND:
            ok, reason = self.app.execute(*jlp.unpack_command(f.payload))
            ack = Frame(MsgType.ACK, self.app.device_id, jlp.pack_ack(f.seq, ok, reason), f.seq)
            if self.transport:
                self.transport.sendto(ack.encode(), addr)


class ControllerApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.device_id: int = args.device_id
        cfg = PRESETS[args.preset](args.name or PRESETS[args.preset]().name)
        self.ctrl = Controller(cfg, pattern=args.pattern)
        self.sim = TrafficSim(self.ctrl, parse_rates(args.rates), seed=args.device_id,
                              profile="peak", peds=True) if args.sim_traffic else None
        self.agent = SnmpAgent(ntcip.MibTree(), args.community.encode(), args.write_community.encode())
        self.agent.mib = ntcip.build_mib(self.ctrl, self.agent.uptime, args.name or cfg.name, args.location,
                                        sim=self.sim)
        self.center = _hostport(args.center)
        self.agent.trap_targets = [_hostport(t) for t in args.trap_target] if args.trap_target else []
        self.jlp: JlpEndpoint | None = None
        self.seq = 0
        self.bad_frames = 0
        self.started = time.monotonic()
        self.ctrl.listeners.append(self.on_event)

    # ------------------------------------------------------------ events
    def on_event(self, kind: str, data: dict) -> None:
        log.info("event %s %s", kind, data)
        if kind == "alarm":
            self.agent.send_trap(ntcip.TRAP_ALARM, [(ntcip.JL_ALARMS, int(self.ctrl.alarms)),
                                                    (Oid(ntcip.JL + (12, 0)), data.get("phase", 0))])
            self.send(MsgType.ALARM, jlp.pack_alarm(data["alarm"], data.get("phase", 0), True))
        elif kind == "alarm_cleared":
            self.agent.send_trap(ntcip.TRAP_ALARM_CLEARED, [(ntcip.JL_ALARMS, 0)])
            self.send(MsgType.ALARM, jlp.pack_alarm(data["alarms"], 0, False))
        elif kind == "mode":
            self.agent.send_trap(ntcip.TRAP_MODE, [(ntcip.JL_CONTROL_MODE, data["new"])])

    def execute(self, cmd: Command, arg: int, arg2: int) -> tuple[bool, str]:
        try:
            if cmd == Command.SET_PATTERN:
                self.ctrl.set_pattern(arg)
            elif cmd == Command.PREEMPT:
                self.ctrl.request_preempt(arg)
            elif cmd == Command.INJECT_FAULT:
                self.ctrl.inject_fault(FAULT_CODES[arg], arg2 or 1)
            elif cmd == Command.CLEAR_FAULTS:
                self.ctrl.clear_faults()
            elif cmd == Command.PED_REQUEST:
                self.ctrl.ped_request(arg)
            log.info("command %s(%d, %d) ok", cmd.name, arg, arg2)
            return True, ""
        except (ValueError, KeyError) as exc:
            log.warning("command %s(%d, %d) rejected: %s", cmd.name, arg, arg2, exc)
            return False, str(exc)

    # ------------------------------------------------------------ io
    def send(self, mtype: MsgType, payload: bytes) -> None:
        if not self.jlp or not self.jlp.transport or not self.center:
            return
        self.seq = (self.seq + 1) & 0xFFFF
        self.jlp.transport.sendto(Frame(mtype, self.device_id, payload, self.seq).encode(), self.center)

    def status(self) -> jlp.Status:
        c, ind = self.ctrl, self.ctrl.indications()
        return jlp.Status(int(time.monotonic() - self.started), int(c.mode), c.pattern, c.stage + 1,
                          int(c.interval), mask(ind.greens), mask(ind.yellows), mask(ind.reds),
                          mask(ind.red_yellows), mask(ind.walks), int(c.alarms), c.cycle_count)

    async def run(self) -> None:
        a = self.args
        await self.agent.start(a.bind, a.snmp_port)
        _, self.jlp = await udp_endpoint(lambda: JlpEndpoint(self), a.bind, a.jlp_port)
        log.info("JLP listening on udp/%s:%d, reporting to %s", a.bind, a.jlp_port, self.center)
        sdnotify.notify("READY=1")
        wd = sdnotify.watchdog_interval()

        tick = a.tick
        sim_dt = tick * a.speed
        next_t = time.monotonic()
        last_status = last_counts = last_wd = 0.0
        while True:
            self.ctrl.step(sim_dt)
            if self.sim:
                self.sim.step(sim_dt)
            now = time.monotonic()
            if now - last_status >= 1.0:
                self.send(MsgType.STATUS, self.status().pack())
                last_status = now
            if now - last_counts >= 10.0:
                self.send(MsgType.COUNTS, jlp.pack_counts(self.ctrl.volume))
                last_counts = now
            if wd and now - last_wd >= wd:
                sdnotify.notify("WATCHDOG=1")
                last_wd = now
            next_t += tick
            delay = next_t - time.monotonic()
            if delay < -1.0:  # fell far behind (debugger, suspend) - resync, don't burst
                log.warning("tick loop behind by %.1fs, resyncing", -delay)
                next_t = time.monotonic()
            await asyncio.sleep(max(0.0, delay))


def _raise_interrupt(*_) -> None:
    raise KeyboardInterrupt


def _hostport(text: str | None) -> tuple[str, int] | None:
    if not text:
        return None
    host, _, port = text.rpartition(":")
    return host or "127.0.0.1", int(port)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device-id", type=int, default=1)
    ap.add_argument("--preset", default="four_leg", choices=sorted(PRESETS))
    ap.add_argument("--name", default="")
    ap.add_argument("--location", default="")
    ap.add_argument("--pattern", type=int, default=PATTERN_FREE)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--snmp-port", type=int, default=1161)
    ap.add_argument("--community", default="public")
    ap.add_argument("--write-community", default="private")
    ap.add_argument("--jlp-port", type=int, default=0, help="default 6000 + device id")
    ap.add_argument("--center", default="127.0.0.1:15050", help="center JLP endpoint host:port")
    ap.add_argument("--trap-target", action="append", default=None, help="host:port (repeatable)")
    ap.add_argument("--sim-traffic", action="store_true", help="simulate vehicles + detectors")
    ap.add_argument("--rates", default="1:700,2:250,3:650,4:200,5:120,6:100", help="phase:veh/h,...")
    ap.add_argument("--tick", type=float, default=0.1)
    ap.add_argument("--speed", type=float, default=1.0, help="simulation speed factor")
    ap.add_argument("--log-level", default="INFO")
    return ap


def main() -> None:
    args = build_parser().parse_args()
    if not args.jlp_port:
        args.jlp_port = 6000 + args.device_id
    if args.trap_target is None:
        args.trap_target = ["127.0.0.1:1162"]
    logging.basicConfig(level=args.log_level.upper(),
                        format=f"%(asctime)s %(levelname)s [dev{args.device_id}] %(name)s: %(message)s")
    app = ControllerApp(args)
    try:
        signal.signal(signal.SIGTERM, _raise_interrupt)  # systemctl stop -> clean shutdown
        netrun(app.run())
    except KeyboardInterrupt:
        sdnotify.notify("STOPPING=1")
        log.info("stopped")


if __name__ == "__main__":
    main()
