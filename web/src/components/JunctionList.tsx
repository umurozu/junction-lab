import { ALARM_LABEL, Junction, junctionStatus, MODE_LABEL } from "../api";

export default function JunctionList({ junctions, selected, onSelect }: {
  junctions: Junction[]; selected: number | null; onSelect: (id: number) => void;
}) {
  return (
    <div className="jlist">
      {junctions.map((j) => {
        const st = junctionStatus(j);
        const s = j.state;
        return (
          <button key={j.id} className={`card jitem ${selected === j.id ? "sel" : ""}`} onClick={() => onSelect(j.id)}>
            <div className="jitem-head">
              <i className={`dot ${st}`} />
              <b>{j.name}</b>
              <span className="muted mono">#{j.id}</span>
            </div>
            <div className="jitem-body">
              {j.online ? (
                <>
                  <span>{MODE_LABEL[s.mode_name ?? "?"]}</span>
                  <span className="muted">stage {s.stage || "—"}</span>
                  <span className="muted mono">{j.latency_ms.toFixed(1)} ms</span>
                </>
              ) : <span className="muted">Offline</span>}
            </div>
            {!!s.alarm_names?.length && (
              <div className="alarm-chip">{s.alarm_names.map((a) => ALARM_LABEL[a] ?? a).join(", ")}</div>
            )}
          </button>
        );
      })}
    </div>
  );
}
