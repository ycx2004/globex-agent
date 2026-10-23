import { useCallback, useEffect, useRef, useState } from "react";
import "./learningReview.css";

type Request = (path: string, method?: string, body?: any) => Promise<any>;
type CheckCase = { case_id: string; kind: "positive" | "negative"; passed: boolean; reason?: string };
type Candidate = {
  id: string; content_hash: string; revision: number; status: string; reviewer?: string | null; review_note?: string | null;
  created_at: number; updated_at: number;
  document: { title: string; applies_when: string; steps: string[]; exceptions: string[]; keywords: string[]; scope: string };
  validation?: { validator_version: string; checks: CheckCase[] } | null;
  pending_check?: { passed: boolean; cases: CheckCase[] };
};
type RunReport = { trigger: string; started_at: number; finished_at: number; jobs: Record<string, number>; validated: number; validation_failed: number };
type Overview = {
  summary: Record<string, { status?: string; count: number }[]>; candidates: Candidate[];
  running: boolean; running_since: number | null; last_run: RunReport | null; schedule_timezone: string | null;
  run_history?: RunReport[]; job_totals?: JobTotals;
};
type JobTotals = { total: number; pending: number; outcomes: Record<string, number>; last_processed_at: number | null };

const STATUS_LABEL: Record<string, string> = {
  captured: "待校验", validated: "待审核", approved: "已批准", rejected: "已驳回", published: "已发布", withdrawn: "已撤回",
};
// 与 app/application/learning.py 的任务结果状态对应。
const JOB_LABEL: Record<string, string> = {
  candidate_captured: "产生候选",
  skipped_insufficient_or_excluded_evidence: "证据不足或不允许学习",
  skipped_reviewer_not_supported: "提炼模型判定不足以成策略",
  skipped_partition_conflict: "数据分区冲突跳过",
  diagnostic_saved: "失败会话存为诊断样本",
  retry_or_failed: "提炼出错待重试",
  cancelled: "已取消",
};
const FILTERS = [["todo", "待处理"], ["approved", "待发布"], ["published", "已发布"], ["all", "全部"]] as const;

function when(seconds?: number | null) {
  return seconds ? new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false }) : "—";
}
function count(summary: Overview["summary"] | undefined, table: string, status: string) {
  return summary?.[table]?.find(row => row.status === status)?.count ?? 0;
}

// 分项说明各任务去向，避免"任务清零但没有候选"时误以为流程失败。
export function lastRunText(run: RunReport) {
  const head = `上次${run.trigger === "schedule" ? "定时" : "手动"}运行：${when(run.finished_at)}。`;
  const total = Object.values(run.jobs).reduce((a, b) => a + b, 0);
  if (!total) return `${head}队列为空，没有新任务可处理，所以没有新候选。`;
  const parts = outcomeText(run.jobs);
  const checked = run.validated || run.validation_failed ? `自动校验通过 ${run.validated}，未通过 ${run.validation_failed}。` : "";
  const tail = run.jobs.candidate_captured ? "" : "本次没有产生策略候选，所以没有需要审批的内容。";
  return `${head}处理 ${total} 个任务：${parts}。${checked}${tail}`;
}

function outcomeText(jobs: Record<string, number>) {
  return Object.entries(jobs).map(([k, v]) => `${JOB_LABEL[k] ?? k} ${v}`).join("，");
}

// 累计口径来自任务表，覆盖上线前和历史文件之外的全部任务；空跑时能看出此前任务的去向。
export function totalsText(totals: JobTotals) {
  if (!totals.total) return "学习队列至今没有收到任务：需要有完成的对话才会入队。";
  const done = totals.total - totals.pending;
  const parts = outcomeText(totals.outcomes);
  const last = totals.last_processed_at ? `，最近一次处理于 ${when(totals.last_processed_at)}` : "";
  return `累计入队 ${totals.total} 个任务，已处理 ${done}${parts ? `（${parts}）` : ""}，待处理 ${totals.pending}${last}。`;
}

function RunHistory({ runs }: { runs: RunReport[] }) {
  if (!runs.length) return null;
  return (
    <details className="learning-history">
      <summary>历史运行记录（最近 {runs.length} 次）</summary>
      <ol>
        {[...runs].reverse().map(run => {
          const total = Object.values(run.jobs).reduce((a, b) => a + b, 0);
          return (
            <li key={`${run.started_at}-${run.trigger}`}>
              <time>{when(run.finished_at)}</time>
              <span>{run.trigger === "schedule" ? "定时" : "手动"}</span>
              <span>{total ? `处理 ${total} 个：${outcomeText(run.jobs)}` : "队列为空"}
                {run.validated || run.validation_failed ? `；校验通过 ${run.validated}，未通过 ${run.validation_failed}` : ""}</span>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

export default function LearningReview({ request }: { request: Request }) {
  const [data, setData] = useState<Overview | null>(null);
  const [error, setError] = useState("");
  const [busyId, setBusyId] = useState<string | null>(null);
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [filter, setFilter] = useState<(typeof FILTERS)[number][0]>("todo");
  const poll = useRef<number>();
  // 管理接口不需要 buyer_id，但 workspaceRequest 会附带，服务端忽略。
  const load = useCallback(async () => {
    try { setData(await request("/learning-review") as Overview); setError(""); }
    catch (err) { setError(err instanceof Error ? err.message : "读取失败"); }
  }, [request]);
  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    window.clearInterval(poll.current);
    if (data?.running) poll.current = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(poll.current);
  }, [data?.running, load]);

  const act = async (id: string, action: () => Promise<unknown>) => {
    setBusyId(id); setError("");
    try { await action(); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : "操作失败"); }
    finally { setBusyId(null); }
  };
  const note = (c: Candidate) => notes[c.id]?.trim() ?? "";
  const path = (c: Candidate, action: string) => `/learning-review/candidates/${encodeURIComponent(c.id)}/${action}`;
  const candidates = (data?.candidates ?? []).filter(c =>
    filter === "all" ? true : filter === "todo" ? c.status === "captured" || c.status === "validated" : c.status === filter);

  return (
    <section className="learning-review" aria-label="学习审核">
      <header className="learning-heading">
        <div>
          <span className="learning-eyebrow">LEARNING REVIEW</span>
          <h2>策略候选审核</h2>
          <p>{data?.schedule_timezone ? `每天 0 点（${data.schedule_timezone}）自动提炼并校验。` : "未开启定时提炼。"}系统从不自动批准或发布，候选需要你逐条确认。来源对话中的姓名、地址、手机号等已按规则替换为占位符（如 [姓名]），规则可能漏掉自由书写的个人信息，批准前请确认策略正文不含个人信息。</p>
        </div>
        <button className="learning-primary" disabled={!data || data.running}
          onClick={() => void act("run", () => request("/learning-review/run", "POST", {}))}>
          {data?.running ? "提炼进行中…" : "立即提炼并校验"}
        </button>
      </header>
      {error && <p className="learning-error" role="alert">{error}<button onClick={() => void load()}>刷新</button></p>}
      <div className="learning-stats" aria-label="学习队列概况">
        <div><span>待处理任务</span><strong>{count(data?.summary, "learning_jobs", "pending")}</strong></div>
        <div><span>待校验</span><strong>{count(data?.summary, "strategy_candidates", "captured")}</strong></div>
        <div><span>待审核</span><strong>{count(data?.summary, "strategy_candidates", "validated")}</strong></div>
        <div><span>已发布</span><strong>{count(data?.summary, "strategy_candidates", "published")}</strong></div>
      </div>
      {data?.last_run && <p className="learning-last-run">{lastRunText(data.last_run)}</p>}
      {data?.job_totals && <p className="learning-last-run">{totalsText(data.job_totals)}</p>}
      <RunHistory runs={data?.run_history ?? []} />
      <div className="learning-filters" role="tablist" aria-label="候选筛选">
        {FILTERS.map(([key, label]) => <button key={key} role="tab" aria-selected={filter === key} onClick={() => setFilter(key)}>{label}</button>)}
      </div>
      {!data && !error && <p className="learning-empty" role="status">正在读取候选…</p>}
      {data && !candidates.length && <p className="learning-empty">当前没有{FILTERS.find(([k]) => k === filter)?.[1]}的候选。</p>}
      <ol className="learning-list">
        {candidates.map(c => {
          const cases = c.validation?.checks ?? c.pending_check?.cases ?? [];
          const busy = busyId === c.id;
          return (
            <li key={c.id} className="learning-card">
              <div className="learning-card-head">
                <h3>{c.document.title}</h3>
                <span className={`learning-badge status-${c.status}`}>{STATUS_LABEL[c.status] ?? c.status}</span>
              </div>
              <p className="learning-applies"><strong>适用条件</strong>{c.document.applies_when}</p>
              <div className="learning-body">
                <div><strong>步骤</strong><ol>{c.document.steps.map((s, i) => <li key={i}>{s}</li>)}</ol></div>
                <div><strong>例外</strong><ul>{c.document.exceptions.map((s, i) => <li key={i}>{s}</li>)}</ul></div>
              </div>
              <p className="learning-keywords">触发关键词：{c.document.keywords.map(k => <code key={k}>{k}</code>)}</p>
              {!!cases.length && <ul className="learning-checks" aria-label="自动校验结果">
                {cases.map(item => <li key={item.case_id} className={item.passed ? "is-pass" : "is-fail"}>
                  {item.kind === "positive" ? "正例" : "反例"} · {item.passed ? "通过" : "未通过"}{item.reason ? `：${item.reason}` : ""}
                </li>)}
              </ul>}
              {c.review_note && <p className="learning-note">审核记录（{c.reviewer ?? "—"}）：{c.review_note}</p>}
              <p className="learning-meta">创建 {when(c.created_at)} · 版本 {c.content_hash.slice(0, 12)}</p>
              {(c.status === "captured" || c.status === "validated") && (
                <label className="learning-note-input">审核意见
                  <textarea value={notes[c.id] ?? ""} maxLength={1000} rows={2} placeholder="批准或驳回都需要填写理由"
                    onChange={e => setNotes(n => ({ ...n, [c.id]: e.target.value }))} />
                </label>
              )}
              <div className="learning-actions">
                {c.status === "captured" && <button disabled={busy} onClick={() => void act(c.id, () => request(path(c, "validate"), "POST", {}))}>重新校验</button>}
                {c.status === "validated" && <button className="learning-primary" disabled={busy || !note(c)}
                  onClick={() => void act(c.id, () => request(path(c, "review"), "POST", { accepted: true, expected_hash: c.content_hash, expected_revision: c.revision, note: note(c) }))}>批准</button>}
                {(c.status === "captured" || c.status === "validated") && <button disabled={busy || !note(c)}
                  onClick={() => void act(c.id, () => request(path(c, "review"), "POST", { accepted: false, expected_hash: c.content_hash, expected_revision: c.revision, note: note(c) }))}>驳回</button>}
                {c.status === "approved" && <button className="learning-primary" disabled={busy}
                  onClick={() => void act(c.id, () => request(path(c, "publish"), "POST", { expected_hash: c.content_hash, expires_days: 90 }))}>发布（90 天有效）</button>}
                {(c.status === "approved" || c.status === "published") && <button disabled={busy}
                  onClick={() => { const reason = window.prompt("撤回原因"); if (reason?.trim()) void act(c.id, () => request(path(c, "withdraw"), "POST", { reason: reason.trim() })); }}>撤回</button>}
              </div>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
