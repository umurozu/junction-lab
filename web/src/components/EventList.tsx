import { EventRow, Junction } from "../api";

const SRC: Record<string, string> = { "snmp-trap": "SNMP trap", "snmp-poll": "SNMP", jlp: "JLP", api: "Operator" };

export default function EventList({ events, junctions }: { events: EventRow[]; junctions: Record<number, Junction> }) {
  return (
    <div className="card events">
      <h3>Event log</h3>
      <div className="event-scroll">
        {events.length === 0 && <p className="muted">No events yet.</p>}
        {events.map((e) => (
          <div key={e.id} className={`event ${e.severity}`}>
            <span className="mono muted">{new Date(e.ts).toLocaleTimeString("tr-TR")}</span>
            <span className="ev-j">{junctions[e.junction_id]?.name ?? `#${e.junction_id}`}</span>
            <span className="ev-msg">{e.message}</span>
            <span className="badge">{SRC[e.source] ?? e.source}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
