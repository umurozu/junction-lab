import { FormEvent, useState } from "react";
import { api, auth } from "../api";
import type { User } from "../App";

export default function Login({ onLogin }: { onLogin: (u: User) => void }) {
  const [username, setUsername] = useState("operator");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const r = await api.login(username, password);
      auth.set(r.access_token);
      onLogin({ username: r.username, role: r.role });
    } catch (err) {
      setError(err instanceof Error ? err.message : "login failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card login-card" onSubmit={submit}>
        <h1>junction-lab</h1>
        <p className="muted">Traffic Management Center</p>
        <label>Username<input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setPassword(e.target.value)} /></label>
        {error && <div className="error">{error}</div>}
        <button disabled={busy}>{busy ? "…" : "Log in"}</button>
        <p className="hint">Demo: operator / operator123 · viewer / viewer123</p>
      </form>
    </div>
  );
}
