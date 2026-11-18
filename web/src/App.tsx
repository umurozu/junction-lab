import { useCallback, useEffect, useRef, useState } from "react";
import { api, auth, connectLive, EventRow, HistoryPoint, Junction, LiveMsg } from "./api";
import Login from "./components/Login";
import MapView from "./components/MapView";
import JunctionList from "./components/JunctionList";
import JunctionDetail from "./components/JunctionDetail";
import EventList from "./components/EventList";

export type User = { username: string; role: string };

export default function App() {
  const [user, setUser] = useState<User | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    if (!auth.token) { setChecked(true); return; }
    api.me().then(setUser).catch(() => auth.set(null)).finally(() => setChecked(true));
  }, []);

  if (!checked) return null;
  if (!user) return <Login onLogin={setUser} />;
  return <Dashboard user={user} onLogout={() => { auth.set(null); setUser(null); }} />;
}

function Dashboard({ user, onLogout }: { user: User; onLogout: () => void }) {
  const [junctions, setJunctions] = useState<Record<number, Junction>>({});
  const [events, setEvents] = useState<EventRow[]>([]);
  const [selected, setSelected] = useState<number | null>(null);
  const [live, setLive] = useState(false);
  const history = useRef<Record<number, HistoryPoint[]>>({});
  const [tick, setTick] = useState(0);

  useEffect(() => {
    api.events().then(setEvents).catch(() => {});
    const stop = connectLive((m: LiveMsg) => {
      if (m.type === "snapshot") {
        setJunctions(Object.fromEntries(m.junctions.map((j) => [j.id, j])));
      } else if (m.type === "state") {
        setJunctions((prev) => prev[m.id]
          ? { ...prev, [m.id]: { ...prev[m.id], online: true, latency_ms: m.latency_ms, state: m.state } }
          : prev);
        const h = (history.current[m.id] ??= []);
        h.push({ t: m.t, g: m.state.greens, y: m.state.yellows, ry: m.state.red_yellows, f: m.state.flashing });
        if (h.length > 180) h.shift();
        setTick((t) => t + 1);
      } else if (m.type === "junction") {
        setJunctions((prev) => ({ ...prev, [m.junction.id]: m.junction }));
      } else if (m.type === "event") {
        setEvents((prev) => [m.event, ...prev].slice(0, 200));
      } else if (m.type === "counts") {
        setJunctions((prev) => prev[m.id] ? { ...prev, [m.id]: { ...prev[m.id], volume_total: m.totals } } : prev);
      }
    }, setLive);
    return stop;
  }, []);

  const seedHistory = useCallback((id: number, h: HistoryPoint[]) => {
    if (!history.current[id]?.length) history.current[id] = h.slice(-180);
  }, []);

  const list = Object.values(junctions).sort((a, b) => a.id - b.id);
  const sel = selected != null ? junctions[selected] : undefined;
  const alarms = list.filter((j) => j.state.alarm_names?.length).length;
  const online = list.filter((j) => j.online).length;

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <svg width="20" height="20" viewBox="0 0 32 32" aria-hidden><rect x="9" y="1" width="14" height="30" rx="4" fill="currentColor" opacity=".25"/><circle cx="16" cy="8" r="4" fill="var(--red)"/><circle cx="16" cy="16" r="4" fill="var(--amber)"/><circle cx="16" cy="24" r="4" fill="var(--green)"/></svg>
          <span>junction-lab</span><small>Traffic Management Center</small>
        </div>
        <div className="kpis">
          <span className="kpi"><b>{online}</b>/{list.length} online</span>
          <span className={`kpi ${alarms ? "bad" : ""}`}><b>{alarms}</b> alarm</span>
          <span className={`kpi ${live ? "" : "bad"}`}><i className={`dot ${live ? "ok" : "offline"}`} />{live ? "Live" : "Disconnected"}</span>
        </div>
        <div className="user">
          <span>{user.username} <small>({user.role === "operator" ? "operator" : "viewer"})</small></span>
          <button className="ghost" onClick={onLogout}>Log out</button>
        </div>
      </header>

      <main className="layout">
        <section className="map-col">
          <MapView junctions={list} selected={selected} onSelect={setSelected} />
          <JunctionList junctions={list} selected={selected} onSelect={setSelected} />
        </section>
        <section className="detail-col">
          {sel ? (
            <JunctionDetail key={sel.id} junction={sel} user={user} history={history.current[sel.id] ?? []}
              tick={tick} seedHistory={seedHistory} onClose={() => setSelected(null)} />
          ) : (
            <div className="empty card">
              <p>Select a junction on the map or in the list for details and control.</p>
            </div>
          )}
          <EventList events={sel ? events.filter((e) => e.junction_id === sel.id) : events}
            junctions={junctions} />
        </section>
      </main>
    </div>
  );
}
