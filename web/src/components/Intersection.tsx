import { Phase, State } from "../api";
import Traffic from "./Traffic";

type Lamp = { r: boolean; y: boolean; g: boolean };

function lamp(p: number, s: Partial<State>): Lamp {
  const ry = s.red_yellows?.includes(p) ?? false;
  return {
    r: (s.reds?.includes(p) ?? false) || ry,
    y: (s.yellows?.includes(p) ?? false) || ry || !!s.flashing,
    g: s.greens?.includes(p) ?? false,
  };
}

// stop-line anchor (where the signal head stands) and direction of the stacked heads per approach
const ANCHOR: Record<string, { x: number; y: number; dx: number; dy: number }> = {
  // heads stand on the kerb corner next to each approach's stop line
  N: { x: 196, y: 40, dx: 18, dy: 0 },
  S: { x: 90, y: 192, dx: -18, dy: 0 },
  E: { x: 200, y: 192, dx: 18, dy: 0 },
  W: { x: 86, y: 40, dx: -18, dy: 0 },
};

function Head({ x, y, l, label, flashing }: { x: number; y: number; l: Lamp; label: string; flashing: boolean }) {
  return (
    <g transform={`translate(${x},${y})`}>
      <rect x={-8} y={0} width={16} height={42} rx={4} className="head" />
      <circle cx={0} cy={8} r={5} className={`lamp red ${l.r ? "on" : ""}`} />
      <circle cx={0} cy={21} r={5} className={`lamp amber ${l.y ? "on" : ""} ${flashing ? "blink" : ""}`} />
      <circle cx={0} cy={34} r={5} className={`lamp green ${l.g ? "on" : ""}`} />
      <text x={0} y={54} className="head-label">{label}</text>
    </g>
  );
}

export default function Intersection({ phases, state }: { phases: Phase[]; state: Partial<State> }) {
  const byApproach: Record<string, Phase[]> = {};
  phases.forEach((p) => (byApproach[p.approach] ??= []).push(p));
  const arms = new Set(phases.map((p) => p.approach));
  const walk = (a: string) => (byApproach[a] ?? []).some((p) => state.walks?.includes(p.number));
  const pedClear = (a: string) => (byApproach[a] ?? []).some((p) => state.ped_clears?.includes(p.number));

  return (
    <svg viewBox="0 0 286 286" className="intersection" role="img" aria-label="Junction signal state">
      {/* roads */}
      {arms.has("N") && <rect x={103} y={0} width={80} height={110} className="road" />}
      {arms.has("S") && <rect x={103} y={176} width={80} height={110} className="road" />}
      {arms.has("W") && <rect x={0} y={103} width={110} height={80} className="road" />}
      {arms.has("E") && <rect x={176} y={103} width={110} height={80} className="road" />}
      <rect x={103} y={103} width={80} height={80} className="road" />
      {/* centre lines */}
      {arms.has("N") && <line x1={143} y1={0} x2={143} y2={96} className="lane" />}
      {arms.has("S") && <line x1={143} y1={190} x2={143} y2={286} className="lane" />}
      {arms.has("W") && <line x1={0} y1={143} x2={96} y2={143} className="lane" />}
      {arms.has("E") && <line x1={190} y1={143} x2={286} y2={143} className="lane" />}
      {/* pedestrian crossings */}
      {(["N", "S", "E", "W"] as const).filter((a) => arms.has(a)).map((a) => {
        const cls = `zebra ${walk(a) ? "walk" : pedClear(a) ? "clear" : ""}`;
        const v = a === "N" || a === "S";
        const base = { N: 86, S: 190, W: 86, E: 190 }[a];
        return (
          <g key={a} className={cls}>
            {Array.from({ length: 7 }, (_, i) => v
              ? <rect key={i} x={106 + i * 11} y={base} width={6} height={10} />
              : <rect key={i} x={base} y={106 + i * 11} width={10} height={6} />)}
          </g>
        );
      })}
      <Traffic phases={phases} state={state} />
      {/* signal heads */}
      {Object.entries(byApproach).map(([a, ps]) =>
        ps.map((p, i) => {
          const an = ANCHOR[a];
          const x = an.x + i * an.dx;
          return <Head key={p.number} x={x} y={an.y} l={lamp(p.number, state)} label={`F${p.number}`}
            flashing={!!state.flashing} />;
        }))}
      {/* compass */}
      <text x={143} y={14} className="compass">K</text>
      <text x={143} y={282} className="compass">G</text>
      <text x={8} y={147} className="compass">B</text>
      <text x={278} y={147} className="compass">D</text>
    </svg>
  );
}
