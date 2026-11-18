import { useEffect, useRef, useState } from "react";
import { Phase, State } from "../api";

// Live animation of the controller's traffic model. Nothing is invented here: the number of cars
// standing at each stop line is the backend queue, every car that drives through is a discharged
// vehicle (served counter delta), and pedestrians come from the kerb / crossed counters.
// The browser only interpolates motion between 1 s SNMP polls.
type Dir = "N" | "S" | "E" | "W";
type Car = { id: number; phase: number; a: Dir; lat: number; d: number; v: number; going: boolean; hue: number };
type Ped = { id: number; phase: number; a: Dir; t: number; dir: 1 | -1; off: number; go: boolean };

const STOP = 82;              // distance from entry to stop line (same on every arm)
const LEN = 11, GAP = 4, VMAX = 55, ACC = 45;
const MAX_SHOWN = 5;          // cars that fit on the drawn arm; the rest is shown as "+N"
const MAX_PEDS = 6;
const PED_SPEED = 0.22;       // crossing fraction per second

function carXY(a: Dir, d: number, lat: number) {
  switch (a) {
    case "N": return { x: 143 - lat, y: d, r: 90 };
    case "S": return { x: 143 + lat, y: 286 - d, r: -90 };
    case "W": return { x: d, y: 143 + lat, r: 0 };
    case "E": return { x: 286 - d, y: 143 - lat, r: 180 };
  }
}
function pedXY(a: Dir, t: number, off: number) {
  const along = 100 + t * 86;
  switch (a) {
    case "N": return { x: along, y: 91 + off };
    case "S": return { x: along, y: 195 + off };
    case "W": return { x: 91 + off, y: along };
    case "E": return { x: 195 + off, y: along };
  }
}
// queue-label position just outside the drawn arm
const LABEL: Record<Dir, { x: number; y: number }> = {
  N: { x: 96, y: 12 }, S: { x: 190, y: 280 }, W: { x: 4, y: 196 }, E: { x: 282, y: 98 },
};

const num = (m: Record<string, number> | undefined, p: number) => m?.[String(p)] ?? 0;

export default function Traffic({ phases, state }: { phases: Phase[]; state: Partial<State> }) {
  const stRef = useRef(state); stRef.current = state;
  const phRef = useRef(phases); phRef.current = phases;
  const cars = useRef<Car[]>([]);
  const peds = useRef<Ped[]>([]);
  const prev = useRef<{ served: Record<string, number>; crossed: Record<string, number> } | null>(null);
  const [, force] = useState(0);

  // lane per phase: one phase per arm -> centre of the half-road; two -> straight outside, turn inside
  const lane = (p: Phase) => {
    const same = phases.filter((q) => q.approach === p.approach).sort((x, y) => x.number - y.number);
    return same.length === 1 ? 20 : same.indexOf(p) === 0 ? 29 : 11;
  };

  // reconcile with each new SNMP sample
  useEffect(() => {
    const s = state;
    if (!s.served || !s.queues) return;
    const before = prev.current;
    prev.current = { served: { ...s.served }, crossed: { ...(s.peds_crossed ?? {}) } };
    let id = Date.now();
    for (const p of phases) {
      const a = p.approach as Dir;
      const mine = cars.current.filter((c) => c.phase === p.number && !c.going).sort((x, y) => y.d - x.d);
      // vehicles that crossed the stop line since the last sample
      let out = before ? Math.min(num(s.served, p.number) - num(before.served, p.number), 8) : 0;
      if (out < 0) out = 0;  // controller restarted
      for (; out > 0; out--) {
        const c = mine.shift();
        if (c) c.going = true;
        else cars.current.push({ id: id++, phase: p.number, a, lat: lane(p), d: STOP - 2, v: VMAX * 0.6,
          going: true, hue: Math.floor(Math.random() * 360) });
      }
      // queue on the stop line
      const want = Math.min(num(s.queues, p.number), MAX_SHOWN);
      while (mine.length > want) mine.pop()!.going = true;
      for (let k = mine.length; k < want; k++)
        cars.current.push({ id: id++, phase: p.number, a, lat: lane(p), d: -LEN * (k - mine.length), v: VMAX,
          going: false, hue: Math.floor(Math.random() * 360) });

      // pedestrians
      if (p.has_ped && s.peds_waiting) {
        let crossed = before ? num(s.peds_crossed, p.number) - num(before.crossed, p.number) : 0;
        crossed = Math.max(0, Math.min(crossed, MAX_PEDS));
        const kerb = peds.current.filter((x) => x.phase === p.number && !x.go);
        for (; crossed > 0; crossed--) {
          const w = kerb.shift();
          if (w) w.go = true;
          else {
            const dir = Math.random() < 0.5 ? 1 : -1;
            peds.current.push({ id: id++, phase: p.number, a, t: dir === 1 ? 0 : 1, dir, off: Math.random() * 6 - 3,
              go: true });
          }
        }
        const wantP = Math.min(num(s.peds_waiting, p.number), MAX_PEDS);
        while (kerb.length > wantP) {
          const r = kerb.pop()!;
          peds.current = peds.current.filter((x) => x !== r);
        }
        for (let k = kerb.length; k < wantP; k++) {
          const dir = Math.random() < 0.5 ? 1 : -1;
          peds.current.push({ id: id++, phase: p.number, a, t: dir === 1 ? 0 : 1, dir, off: Math.random() * 6 - 3,
            go: false });
        }
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state]);

  // animation loop
  useEffect(() => {
    let raf = 0, last = performance.now();
    const loop = (now: number) => {
      const dt = Math.min(0.1, (now - last) / 1000); last = now;
      const byLane = new Map<string, Car[]>();
      for (const c of cars.current) {
        const k = `${c.a}${c.lat}`;
        (byLane.get(k) ?? byLane.set(k, []).get(k)!).push(c);
      }
      for (const laneCars of byLane.values()) {
        laneCars.sort((x, y) => y.d - x.d);
        let slot = 0;
        laneCars.forEach((c, i) => {
          let limit = i > 0 ? laneCars[i - 1].d - LEN - GAP : Infinity;
          if (!c.going) limit = Math.min(limit, STOP - slot++ * (LEN + GAP));
          const target = limit === Infinity ? VMAX : Math.min(VMAX, Math.max(0, (limit - c.d) * 3));
          c.v = c.v < target ? Math.min(target, c.v + ACC * dt) : target;
          c.d = Math.min(c.d + c.v * dt, limit === Infinity ? Infinity : Math.max(c.d, limit));
        });
      }
      cars.current = cars.current.filter((c) => c.d < 300);
      for (const p of peds.current) if (p.go) p.t += p.dir * PED_SPEED * dt;
      peds.current = peds.current.filter((p) => p.t >= -0.02 && p.t <= 1.02);
      force((n) => (n + 1) & 0xffff);
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    return () => cancelAnimationFrame(raf);
  }, []);

  const arms = [...new Set(phases.map((p) => p.approach))] as Dir[];
  return (
    <g className="traffic">
      {cars.current.map((c) => {
        const { x, y, r } = carXY(c.a, c.d, c.lat);
        return (
          <g key={c.id} transform={`translate(${x},${y}) rotate(${r})`}>
            <rect x={-LEN / 2} y={-4} width={LEN} height={8} rx={2} fill={`hsl(${c.hue} 65% 55%)`} />
            <rect x={1} y={-3} width={3} height={6} rx={1} fill="#0b1220" opacity={0.6} />
          </g>
        );
      })}
      {peds.current.map((p) => {
        const { x, y } = pedXY(p.a, p.t, p.off);
        return <circle key={p.id} cx={x} cy={y} r={2.6} className={`ped ${p.go ? "moving" : ""}`} />;
      })}
      {arms.map((a) => {
        const q = phases.filter((p) => p.approach === a).reduce((n, p) => n + num(state.queues, p.number), 0);
        const pw = phases.filter((p) => p.approach === a).reduce((n, p) => n + num(state.peds_waiting, p.number), 0);
        const l = LABEL[a];
        return (
          <text key={a} x={l.x} y={l.y} className="queue-label" textAnchor={a === "E" || a === "N" ? "end" : "start"}>
            🚗{q}{pw ? ` 🚶${pw}` : ""}
          </text>
        );
      })}
    </g>
  );
}
