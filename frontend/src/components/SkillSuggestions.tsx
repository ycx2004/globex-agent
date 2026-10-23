import { useCallback, useEffect, useRef, useState } from "react";
import Icon from "./Icon";
import type { WorkspaceRequest } from "./BuyerWorkspace";
import "./skillSuggestions.css";

export type SkillSuggestion = {
  id: string; session_id: string; run_id: string; decision: "create" | "update";
  target_skill_id: string | null; title: string; description: string; body: string;
  reason: string; signals: string[]; created_at: number;
};

// 空闲整理由服务端在本轮完成约 3 分钟后进行，页面在后台或关闭都不影响；前端只读取结果。
export const POLL_INTERVAL_MS = 30_000;
export const POLL_WINDOW_MS = 600_000;
const SIGNAL_LABEL: Record<string, string> = { explicit: "你说过以后照此办理", correction: "你纠正过做法", repeated: "多次提到同一要求", manual: "你主动发起" };

function SuggestionCard({ item, request, busy, onResolved }: {
  item: SkillSuggestion; request: WorkspaceRequest; busy: boolean; onResolved: (id: string, saved: boolean) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(item.title), [description, setDescription] = useState(item.description), [body, setBody] = useState(item.body);
  const [saving, setSaving] = useState(false), [error, setError] = useState("");
  const act = async (path: "accept" | "dismiss") => {
    setSaving(true); setError("");
    try {
      await request(`/skill-suggestions/${encodeURIComponent(item.id)}/${path}`, "POST",
        path === "accept" ? { title: title.trim(), description: description.trim(), body: body.trim() } : undefined);
      onResolved(item.id, path === "accept");
    } catch (reason) { setError(reason instanceof Error ? reason.message : "未能处理，请稍后重试"); }
    finally { setSaving(false); }
  };
  const disabled = busy || saving;
  return <article className="skill-suggestion" aria-label={"Skill 建议：" + item.title}>
    <header><Icon name="leaf" /><div>
      <strong>{item.decision === "update" ? "可以更新你的 Skill" : "可以把这次的做法记成 Skill"}</strong>
      <small>{item.reason || item.signals.map(s => SIGNAL_LABEL[s] ?? s).join(" · ")}</small></div></header>
    {editing ? <div className="skill-suggestion-editor">
      <label>标题<input value={title} maxLength={120} onChange={e => setTitle(e.target.value)} /></label>
      <label>适用场景<input value={description} maxLength={400} onChange={e => setDescription(e.target.value)} /></label>
      <label>做法<textarea value={body} maxLength={12000} rows={6} onChange={e => setBody(e.target.value)} /></label>
    </div> : <div className="skill-suggestion-preview">
      <h3>{title}</h3><p>{description}</p><pre>{body}</pre></div>}
    {error && <p role="alert" className="skill-suggestion-error">{error}</p>}
    <footer>
      <button className="primary" disabled={disabled || !title.trim() || !description.trim() || !body.trim()} onClick={() => void act("accept")}>
        {saving ? "正在保存…" : item.decision === "update" ? "确认更新" : "保存 Skill"}</button>
      <button disabled={disabled} onClick={() => setEditing(v => !v)}>{editing ? "收起编辑" : "编辑"}</button>
      <button disabled={disabled} onClick={() => void act("dismiss")}>忽略</button>
    </footer>
  </article>;
}

/** 对话内建议：服务端在本轮空闲后整理，这里只展示结果；不弹窗、不阻塞输入，也可手动整理。 */
export function ConversationSkillSuggestion({ sessionId, runKey, running, busy, request, onSkillsChanged }: {
  sessionId: string; runKey: string; running: boolean; busy: boolean; request: WorkspaceRequest; onSkillsChanged: () => Promise<void>;
}) {
  const [items, setItems] = useState<SkillSuggestion[]>([]);
  const [notice, setNotice] = useState(""), [checking, setChecking] = useState(false);
  const generation = useRef(0);
  const load = useCallback(async () => {
    const current = generation.current;
    const data = await request("/skill-suggestions?session_id=" + encodeURIComponent(sessionId)).catch(() => null);
    if (current === generation.current && data && Array.isArray(data.suggestions)) setItems(data.suggestions as SkillSuggestion[]);
  }, [request, sessionId]);
  const analyze = useCallback(async () => {
    const current = generation.current;
    setChecking(true); setNotice("");
    try {
      const data = await request("/skill-suggestions/analyze", "POST", { session_id: sessionId, trigger: "manual" });
      if (current !== generation.current) return;
      if (data.suggestion) setItems([data.suggestion as SkillSuggestion]);
      else setNotice(data.status === "quota" ? "今天的整理次数已用完，明天再试。" : "这次对话里没有找到值得长期复用的做法。");
    } catch (reason) {
      if (current === generation.current) setNotice(reason instanceof Error ? reason.message : "暂时没能整理，请稍后重试。");
    } finally { if (current === generation.current) setChecking(false); }
  }, [request, sessionId]);
  useEffect(() => { generation.current++; setItems([]); setNotice(""); void load(); }, [load]);
  // 本轮结束后的一段时间内定期读取服务端整理结果；回到前台时立即补读一次。
  useEffect(() => {
    if (running || !runKey) return;
    const started = Date.now();
    const timer = window.setInterval(() => {
      if (Date.now() - started > POLL_WINDOW_MS) window.clearInterval(timer);
      else if (document.visibilityState !== "hidden") void load();
    }, POLL_INTERVAL_MS);
    const onVisible = () => { if (document.visibilityState === "visible") void load(); };
    document.addEventListener("visibilitychange", onVisible);
    return () => { window.clearInterval(timer); document.removeEventListener("visibilitychange", onVisible); };
  }, [running, runKey, load]);
  if (running || !runKey) return null;
  return <div className="conversation-skill-suggestion">
    {items.map(item => <SuggestionCard key={item.id} item={item} request={request} busy={busy}
      onResolved={(id, saved) => { setItems(list => list.filter(x => x.id !== id)); setNotice(saved ? "已保存到“我的 Skill”。" : "已忽略，30 天内不会再提相同建议。"); if (saved) void onSkillsChanged(); }} />)}
    {!items.length && <button className="skill-suggestion-trigger" disabled={busy || checking} onClick={() => void analyze()}>
      <Icon name="leaf" />{checking ? "正在整理…" : "沉淀为 Skill"}</button>}
    {notice && <span className="skill-suggestion-notice" role="status">{notice}</span>}
  </div>;
}

/** “我的 Skill”页里的待确认建议，错过对话卡片也能在这里处理。 */
export function PendingSkillSuggestions({ request, busy, onSaved }: { request: WorkspaceRequest; busy: boolean; onSaved: () => void }) {
  const [items, setItems] = useState<SkillSuggestion[]>([]);
  useEffect(() => {
    let alive = true;
    void request("/skill-suggestions").then(data => { if (alive && Array.isArray(data.suggestions)) setItems(data.suggestions as SkillSuggestion[]); }).catch(() => undefined);
    return () => { alive = false; };
  }, [request]);
  if (!items.length) return null;
  return <section className="pending-skill-suggestions" aria-label="待确认的 Skill 建议">
    <h2>有 {items.length} 条建议</h2>
    {items.map(item => <SuggestionCard key={item.id} item={item} request={request} busy={busy}
      onResolved={(id, saved) => { setItems(list => list.filter(x => x.id !== id)); if (saved) onSaved(); }} />)}
  </section>;
}
