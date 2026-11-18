import { useEffect, useState } from "react";
import { ALARM_LABEL, api, HistoryPoint, INTERVAL_LABEL, Junction, JunctionDetail as Detail, MODE_LABEL } from "../api";
import type { User } from "../App";
import Intersection from "./Intersection";
import Timeline from "./Timeline";
import CountsChart from "./CountsChart";

const FAULTS = [
  { code: 2, label: "Red lamp failure" },
  { code: 1, label: "Conflict (welded relay)" },
  { code: 3, label: "Stuck detector" },
  { code: 4, label: "Mains power failure (UPS)" },
];

export default function JunctionDetail({ junction, user, history, tick, seedHistory, onClose }: {
  junction: Junction; user: User; history: HistoryPoint[]; tick: number;
  seedHistory: (id: number, h: HistoryPoint[]) => void; onClose: () => void;
}) {
  const [meta, setMeta] = useState<Detail | null>(null);
  const [busy, setBusy] = useState(false);
  const [toast, setToast] = useState<{ ok: boolean; text: string } | null>(null);
  const [fault, setFault] = useState(2);
  const [faultPhase, setFaultPhase] = useState(2);
  const operator = user.role === "operator";
  const s = junction.state;
  void tick;

  useEffect(() => {
    api.junction(junction.id).then((d) => { setMeta(d); seedHistory(d.id, d.history); }).catch(() => {});
  }, [junction.id, seedHistory]);

  async function run(label: string, fn: () => Promise<unknown>) {
    setBusy(true);
    try {
      await fn();
      setToast({ ok: true, text: `${label}: sent` });
    } catch (e) {
      setToast({ ok: false, text: `${label}: ${e instanceof Error ? e.message : "hata"}` });
    } finally {
      setBusy(false);
      setTimeout(() => setToast(null), 3500);
    }
  }

  if (!meta) return <div className="card detail"><p className="muted">Loading…</p></div>;
  const curPattern = meta.patterns.find((p) => p.number === s.pattern);

  return (
    <div className="card detail">
      <div className="detail-head">
        <div>
          <h2>{junction.name}</h2>
          <span className="muted small">
            Device #{junction.id} · {meta.stages.length} stages · {meta.phases.length} phases ·{" "}
            {junction.online ? `SNMP ${junction.latency_ms.toFixed(1)} ms` : "offline"}
          </span>
        </div>
        <button className="ghost" onClick={onClose} aria-label="Close">✕</button>
      </div>

      <div className="detail-grid">
        <Intersection key={junction.id} phases={meta.phases} state={s} />
        <div className="status">
          <dl>
            <dt>Mode</dt><dd className={s.flashing ? "warn" : ""}>{MODE_LABEL[s.mode_name ?? "?"]}</dd>
            <dt>Plan</dt><dd>{curPattern?.name ?? s.pattern} <span className="muted mono">#{s.pattern}</span></dd>
            <dt>Stage</dt><dd>{s.stage ? meta.stages[s.stage - 1]?.name : "—"}</dd>
            <dt>Interval</dt><dd>{INTERVAL_LABEL[s.interval_name ?? ""] ?? "—"}</dd>
            <dt>Cycle</dt><dd className="mono">{s.cycle ?? 0}</dd>
            <dt>Calls</dt><dd className="mono">{s.veh_calls?.length ? s.veh_calls.map((p) => `F${p}`).join(" ") : "—"}</dd>
          </dl>
          {!!s.alarm_names?.length && (
            <div className="alarm-box">
              <b>Active alarm</b>
              {s.alarm_names.map((a) => <div key={a}>{ALARM_LABEL[a] ?? a}</div>)}
            </div>
          )}
        </div>
      </div>

      <h3>Signal timing diagram <span className="muted small">(last 2 min, SNMP polled every 1 s)</span></h3>
      <Timeline phases={meta.phases} history={history} />

      <h3>Control {!operator && <span className="muted small">(viewer role: read-only)</span>}</h3>
      <fieldset disabled={!operator || busy || !junction.online} className="controls">
        <div className="row">
          <span className="ctl-label">Plan <small className="muted">NTCIP SET</small></span>
          {meta.patterns.map((p) => (
            <button key={p.number} className={`chip ${s.pattern === p.number ? "active" : ""} ${p.number === 255 ? "warnbtn" : ""}`}
              title={p.greens ? `Green times: ${p.greens.join(" / ")} sn` : undefined}
              onClick={() => run(p.name, () => api.setPattern(junction.id, p.number))}>
              {p.name}
            </button>
          ))}
        </div>
        <div className="row">
          <span className="ctl-label">Emergency preemption <small className="muted">JLP</small></span>
          {meta.stages.map((st, i) => (
            <button key={i} className="chip" onClick={() => run(`Preempt ${st.name}`, () => api.preempt(junction.id, i))}>
              {st.name}
            </button>
          ))}
        </div>
        <div className="row">
          <span className="ctl-label">Pedestrian button</span>
          {meta.phases.filter((p) => p.has_ped).map((p) => (
            <button key={p.number} className="chip" onClick={() => run(`Pedestrian F${p.number}`, () => api.ped(junction.id, p.number))}>
              F{p.number} {p.name}
            </button>
          ))}
        </div>
        <div className="row">
          <span className="ctl-label">Fault simulation</span>
          <select value={fault} onChange={(e) => setFault(+e.target.value)}>
            {FAULTS.map((f) => <option key={f.code} value={f.code}>{f.label}</option>)}
          </select>
          <select value={faultPhase} onChange={(e) => setFaultPhase(+e.target.value)}>
            {meta.phases.map((p) => <option key={p.number} value={p.number}>F{p.number}</option>)}
          </select>
          <button className="chip warnbtn" onClick={() => run("Fault", () => api.fault(junction.id, fault, faultPhase))}>Apply</button>
          <button className="chip" onClick={() => run("Clear faults", () => api.clearFaults(junction.id))}>Clear</button>
        </div>
      </fieldset>
      {toast && <div className={`toast ${toast.ok ? "ok" : "bad"}`}>{toast.text}</div>}

      <h3>Detector volumes</h3>
      <CountsChart id={junction.id} phases={meta.phases} />
    </div>
  );
}
