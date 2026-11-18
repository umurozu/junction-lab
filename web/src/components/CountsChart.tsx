import { useEffect, useState } from "react";
import { api, CountBin, Phase } from "../api";

const PALETTE = ["#38bdf8", "#a78bfa", "#f472b6", "#34d399", "#fbbf24", "#fb7185"];

/** Detector volumes (veh/h) per phase, last 60 minutes in 1-minute bins. */
export default function CountsChart({ id, phases }: { id: number; phases: Phase[] }) {
  const [bins, setBins] = useState<CountBin[]>([]);
  useEffect(() => {
    let alive = true;
    // drop the current, still-filling minute so the line doesn't dive to zero at the right edge
    const load = () => api.counts(id).then((b) => alive && setBins(b.slice(0, -1))).catch(() => {});
    load();
    const t = setInterval(load, 15000);
    return () => { alive = false; clearInterval(t); };
  }, [id]);

  const W = 560, H = 140, left = 36, bottom = 18;
  const max = Math.max(200, ...bins.flatMap((b) => Object.values(b.veh_per_hour)));
  const x = (i: number) => left + (bins.length < 2 ? 0 : (i / (bins.length - 1)) * (W - left - 6));
  const y = (v: number) => H - bottom - (v / max) * (H - bottom - 6);

  return (
    <div>
      <div className="legend">
        {phases.map((p, i) => (
          <span key={p.number}><i style={{ background: PALETTE[i % PALETTE.length] }} />F{p.number} {p.name}</span>
        ))}
      </div>
      {bins.length < 2 ? (
        <p className="muted small">Collecting counts (detectors report every 10 s)…</p>
      ) : (
        <svg viewBox={`0 0 ${W} ${H}`} className="chart" role="img" aria-label="Detector volumes">
          {[0, 0.5, 1].map((f) => (
            <g key={f}>
              <line x1={left} x2={W} y1={y(max * f)} y2={y(max * f)} className="grid" />
              <text x={left - 4} y={y(max * f) + 3} className="axis" textAnchor="end">{Math.round(max * f)}</text>
            </g>
          ))}
          {phases.map((p, i) => (
            <polyline key={p.number} fill="none" stroke={PALETTE[i % PALETTE.length]} strokeWidth={2}
              points={bins.map((b, k) => `${x(k)},${y(b.veh_per_hour[String(p.number)] ?? 0)}`).join(" ")} />
          ))}
          <text x={left} y={H - 3} className="axis">{new Date(bins[0].t).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" })}</text>
          <text x={W} y={H - 3} className="axis" textAnchor="end">veh/h</text>
        </svg>
      )}
    </div>
  );
}
