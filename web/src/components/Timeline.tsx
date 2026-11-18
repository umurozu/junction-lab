import { HistoryPoint, Phase } from "../api";

const WINDOW = 120; // seconds

/** Signal timing diagram of the last two minutes. */
export default function Timeline({ phases, history }: { phases: Phase[]; history: HistoryPoint[] }) {
  const W = 560, rowH = 16, gap = 6, left = 34;
  const H = phases.length * (rowH + gap) + 18;
  const now = history.length ? history[history.length - 1].t : 0;
  const x = (t: number) => left + ((t - (now - WINDOW)) / WINDOW) * (W - left);
  const pts = history.filter((h) => h.t >= now - WINDOW);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="timeline" role="img" aria-label="Signal timing diagram">
      {phases.map((p, row) => {
        const y = row * (rowH + gap);
        return (
          <g key={p.number}>
            <text x={0} y={y + rowH - 4} className="tl-label">F{p.number}</text>
            <rect x={left} y={y} width={W - left} height={rowH} className="tl-bg" />
            {pts.map((h, i) => {
              const next = pts[i + 1]?.t ?? h.t + 1;
              const cls = h.f ? "tl-flash" : h.g.includes(p.number) ? "tl-green"
                : h.y.includes(p.number) ? "tl-amber" : h.ry.includes(p.number) ? "tl-ry" : "tl-red";
              return <rect key={i} x={x(h.t)} y={y} width={Math.max(0.5, x(next) - x(h.t))} height={rowH}
                className={cls} />;
            })}
          </g>
        );
      })}
      {[0, 30, 60, 90, 120].map((s) => (
        <text key={s} x={x(now - WINDOW + s)} y={H - 2} className="tl-tick" style={{ textAnchor: s === 120 ? "end" : "middle" }}>
          {s === 120 ? "now" : `-${120 - s}s`}
        </text>
      ))}
    </svg>
  );
}
