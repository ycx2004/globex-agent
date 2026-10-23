import { useEffect, useState, type FormEvent } from "react";
import App from "./App";
import { clearSession, fetchAuthState, login, type AuthState } from "./lib/auth";
import { resolveBuyerId } from "./lib/buyerIdentity";
import "./components/learningReview.css";

type Phase = { kind: "loading" } | { kind: "login" } | { kind: "error"; message: string } | { kind: "ready"; auth: AuthState };

export function LoginPage({ onLoggedIn }: { onLoggedIn: (auth: AuthState) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true); setError("");
    try { onLoggedIn(await login(username.trim(), password)); }
    catch (err) { setError(err instanceof Error ? err.message : "登录失败"); setPassword(""); }
    finally { setBusy(false); }
  };
  return (
    <main className="login-page">
      <form className="login-card" onSubmit={submit} aria-labelledby="login-title">
        <span className="login-brand">Globex</span>
        <h1 id="login-title">登录环球好物</h1>
        <p>账号由管理员在服务器创建，页面不提供注册。</p>
        <label>账号<input value={username} onChange={e => setUsername(e.target.value)} autoComplete="username" maxLength={128} required autoFocus /></label>
        <label>口令<input type="password" value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" maxLength={256} required /></label>
        {error && <p className="login-error" role="alert">{error}</p>}
        <button type="submit" disabled={busy || !username.trim() || !password}>{busy ? "登录中…" : "登录"}</button>
      </form>
    </main>
  );
}

export default function AuthGate() {
  const [phase, setPhase] = useState<Phase>({ kind: "loading" });
  const check = () => {
    setPhase({ kind: "loading" });
    fetchAuthState()
      .then(auth => {
        if (!auth) { clearSession(); setPhase({ kind: "login" }); return; }
        // 本地缓存的买家与令牌主体不一致时以令牌为准，否则所有请求都会 403。
        if (auth.mode === "hmac" && resolveBuyerId() !== auth.buyerId) {
          clearSession(); setPhase({ kind: "login" }); return;
        }
        setPhase({ kind: "ready", auth });
      })
      .catch(err => setPhase({ kind: "error", message: err instanceof Error ? err.message : "身份服务暂时不可用" }));
  };
  useEffect(check, []);
  if (phase.kind === "loading") return <main className="login-page"><p className="login-status" role="status">正在确认登录状态…</p></main>;
  if (phase.kind === "error") return <main className="login-page"><div className="login-card"><p className="login-error" role="alert">{phase.message}</p><button onClick={check}>重试</button></div></main>;
  // 登录后整页刷新，让会话客户端以新身份与令牌重新初始化。
  if (phase.kind === "login") return <LoginPage onLoggedIn={() => window.location.reload()} />;
  const auth = phase.auth;
  return <App auth={auth.mode === "hmac" ? { buyerId: auth.buyerId, isLearningAdmin: auth.isLearningAdmin,
    onLogout: () => { clearSession(); window.location.reload(); } } : undefined} />;
}
