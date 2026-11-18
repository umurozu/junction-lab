export type State = {
  greens: number[]; yellows: number[]; reds: number[]; red_yellows: number[];
  walks: number[]; ped_clears: number[]; veh_calls: number[];
  pattern: number; mode: number; mode_name: string; stage: number;
  interval: number; interval_name: string; alarms: number; alarm_names: string[];
  flashing: boolean; cycle: number;
  // live traffic model, keyed by phase number (as string)
  queues: Record<string, number>; served: Record<string, number>;
  peds_waiting: Record<string, number>; peds_crossed: Record<string, number>;
};

export type Junction = {
  id: number; name: string; preset: string; lat: number; lon: number;
  online: boolean; latency_ms: number; last_seen: string | null; heartbeat_age_s: number | null;
  state: Partial<State>; volume_total: Record<string, number>;
};

export type Phase = { number: number; name: string; approach: "N" | "E" | "S" | "W"; has_ped: boolean;
  min_green: number; max_green: number };
export type Stage = { name: string; phases: number[] };
export type Pattern = { number: number; name: string; greens: number[] | null };
export type HistoryPoint = { t: number; g: number[]; y: number[]; ry: number[]; f: boolean };
export type JunctionDetail = Junction & { phases: Phase[]; stages: Stage[]; patterns: Pattern[];
  history: HistoryPoint[] };

export type EventRow = { id: number; ts: string; junction_id: number; kind: string;
  severity: "info" | "warning" | "critical"; source: string; message: string };

export type CountBin = { t: string; veh_per_hour: Record<string, number> };

const TOKEN_KEY = "jl_token";

export const auth = {
  get token(): string | null {
    try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
  },
  set(token: string | null) {
    try { token ? localStorage.setItem(TOKEN_KEY, token) : localStorage.removeItem(TOKEN_KEY); } catch { /* */ }
  },
};

export class ApiError extends Error {
  constructor(public status: number, message: string) { super(message); }
}

async function req<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", ...(auth.token ? { Authorization: `Bearer ${auth.token}` } : {}) },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (res.status === 401) auth.set(null);
  if (!res.ok) {
    let msg = res.statusText;
    try { const j = await res.json(); msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch { /* */ }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export const api = {
  login: (username: string, password: string) =>
    req<{ access_token: string; role: string; username: string }>("POST", "/api/auth/login", { username, password }),
  me: () => req<{ username: string; role: string }>("GET", "/api/auth/me"),
  junctions: () => req<Junction[]>("GET", "/api/junctions"),
  junction: (id: number) => req<JunctionDetail>("GET", `/api/junctions/${id}`),
  events: (junctionId?: number) =>
    req<EventRow[]>("GET", `/api/events?limit=60${junctionId ? `&junction_id=${junctionId}` : ""}`),
  counts: (id: number) => req<CountBin[]>("GET", `/api/junctions/${id}/counts?minutes=60&bucket=1`),
  setPattern: (id: number, pattern: number) => req("POST", `/api/junctions/${id}/pattern`, { pattern }),
  preempt: (id: number, stage: number) => req("POST", `/api/junctions/${id}/preempt`, { stage }),
  fault: (id: number, code: number, phase: number) => req("POST", `/api/junctions/${id}/faults`, { code, phase }),
  clearFaults: (id: number) => req("DELETE", `/api/junctions/${id}/faults`),
  ped: (id: number, phase: number) => req("POST", `/api/junctions/${id}/ped/${phase}`),
};

export type LiveMsg =
  | { type: "snapshot"; junctions: Junction[] }
  | { type: "state"; id: number; online: boolean; latency_ms: number; state: State; t: number }
  | { type: "junction"; junction: Junction }
  | { type: "event"; event: EventRow }
  | { type: "counts"; id: number; t: string; volumes: Record<string, number>; totals: Record<string, number> };

export function connectLive(onMsg: (m: LiveMsg) => void, onStatus: (ok: boolean) => void): () => void {
  let ws: WebSocket | null = null;
  let stopped = false;
  let retry = 500;
  const open = () => {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws/live?token=${encodeURIComponent(auth.token ?? "")}`);
    ws.onopen = () => { retry = 500; onStatus(true); };
    ws.onmessage = (e) => onMsg(JSON.parse(e.data));
    ws.onclose = () => {
      onStatus(false);
      if (!stopped) setTimeout(open, (retry = Math.min(retry * 2, 8000)));
    };
  };
  open();
  return () => { stopped = true; ws?.close(); };
}

export const MODE_LABEL: Record<string, string> = {
  FIXED: "Fixed time", ACTUATED: "Actuated", FLASH: "Amber flash", STARTUP: "Startup", "?": "—",
};
export const INTERVAL_LABEL: Record<string, string> = {
  GREEN: "Green", YELLOW: "Amber", ALL_RED: "All red", RED_YELLOW: "Red + amber", FLASH: "Flash",
  STARTUP: "Startup",
};
export const ALARM_LABEL: Record<string, string> = {
  CONFLICT: "Conflict", RED_LAMP_OUT: "Red lamp failure", DETECTOR_FAULT: "Detector fault",
  POWER_FAIL: "Mains power failure", COMM_LOSS: "Communication loss",
};

export function junctionStatus(j: Junction): "offline" | "alarm" | "flash" | "ok" {
  if (!j.online) return "offline";
  if (j.state.alarm_names?.length) return "alarm";
  if (j.state.flashing) return "flash";
  return "ok";
}
