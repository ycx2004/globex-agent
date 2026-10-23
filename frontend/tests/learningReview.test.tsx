// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LearningReview from "../src/components/LearningReview";
import { LoginPage } from "../src/AuthGate";
import { TOKEN_KEY } from "../src/lib/auth";

let host: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
  localStorage.clear();
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const hash = "a".repeat(64);
function candidate(status: string) {
  return { id: "c1", content_hash: hash, revision: 1, status, created_at: 1, updated_at: 1,
    document: { title: "按收货期限选购", applies_when: "有期限时", steps: ["先确认期限"], exceptions: ["无期限时不追问"], keywords: ["出行"], scope: "shopping" },
    validation: status === "captured" ? null : { validator_version: "v", checks: [{ case_id: "p", kind: "positive", passed: true }, { case_id: "n", kind: "negative", passed: true }] },
    pending_check: status === "captured" ? { passed: false, cases: [{ case_id: "n", kind: "negative", passed: false, reason: "关键词过宽：订单" }] } : undefined };
}
function overview(status: string) {
  return { summary: { learning_jobs: [{ status: "pending", count: 3 }], strategy_candidates: [{ status, count: 1 }] },
    candidates: [candidate(status)], running: false, running_since: null, last_run: null, schedule_timezone: "Asia/Shanghai" };
}
const button = (text: string) => [...host.querySelectorAll("button")].find(b => b.textContent === text) as HTMLButtonElement | undefined;
async function type(el: HTMLTextAreaElement | HTMLInputElement, value: string) {
  const proto = el instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  await act(async () => {
    Object.getOwnPropertyDescriptor(proto, "value")!.set!.call(el, value);
    el.dispatchEvent(new Event("input", { bubbles: true }));
  });
}

it("批准需要审核意见，并携带精确哈希与版本", async () => {
  const request = vi.fn(async (path: string) => path === "/learning-review" ? overview("validated") : { status: "approved" });
  await act(async () => root.render(<LearningReview request={request} />));
  expect(host.textContent).toContain("每天 0 点");
  expect(host.textContent).toContain("待审核");
  expect(button("批准")!.disabled).toBe(true);
  expect(button("发布（90 天有效）")).toBeUndefined();
  await type(host.querySelector("textarea")!, "正反例合理");
  await act(async () => button("批准")!.click());
  expect(request).toHaveBeenCalledWith("/learning-review/candidates/c1/review", "POST",
    { accepted: true, expected_hash: hash, expected_revision: 1, note: "正反例合理" });
});

it("未通过校验的候选展示原因，只能重新校验或驳回", async () => {
  const request = vi.fn(async () => overview("captured"));
  await act(async () => root.render(<LearningReview request={request} />));
  expect(host.textContent).toContain("关键词过宽：订单");
  expect(button("批准")).toBeUndefined();
  expect(button("重新校验")).toBeTruthy();
});

it("已批准的候选才出现发布按钮", async () => {
  const request = vi.fn(async (path: string) => path === "/learning-review" ? overview("approved") : {});
  await act(async () => root.render(<LearningReview request={request} />));
  await act(async () => root.render(<LearningReview request={request} />));
  (host.querySelector('[role="tab"]:nth-child(2)') as HTMLButtonElement).click();
  await act(async () => {});
  await act(async () => button("发布（90 天有效）")!.click());
  expect(request).toHaveBeenCalledWith("/learning-review/candidates/c1/publish", "POST", { expected_hash: hash, expires_days: 90 });
});

it("登录成功保存令牌并以账号作为买家身份", async () => {
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ access_token: "tok", buyer_id: "ycx", is_learning_admin: true }), { status: 200 }));
  vi.stubGlobal("fetch", fetcher);
  const done = vi.fn();
  await act(async () => root.render(<LoginPage onLoggedIn={done} />));
  const [user, pass] = [...host.querySelectorAll("input")];
  await type(user, "ycx"); await type(pass, "secret-password");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(localStorage.getItem(TOKEN_KEY)).toBe("tok");
  expect(localStorage.getItem("globex.buyer-id-override")).toBe("ycx");
  expect(done).toHaveBeenCalledWith({ mode: "hmac", buyerId: "ycx", isLearningAdmin: true });
});

it("登录失败显示服务端提示且不保存令牌", async () => {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "账号或口令错误" }), { status: 401 })));
  await act(async () => root.render(<LoginPage onLoggedIn={vi.fn()} />));
  const [user, pass] = [...host.querySelectorAll("input")];
  await type(user, "ycx"); await type(pass, "wrong");
  await act(async () => host.querySelector("form")!.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
  expect(host.textContent).toContain("账号或口令错误");
  expect(localStorage.getItem(TOKEN_KEY)).toBeNull();
});

describe("lastRunText", () => {
  it("空队列时说明没有待处理任务，而不是显示 0 个任务", async () => {
    const { lastRunText } = await import("../src/components/LearningReview");
    const text = lastRunText({ trigger: "schedule", started_at: 1, finished_at: 2, jobs: {}, validated: 0, validation_failed: 0 });
    expect(text).toContain("队列为空");
    expect(text).not.toContain("0 个任务");
    expect(text).not.toContain("。。");
  });
  it("有任务但无候选时列出去向", async () => {
    const { lastRunText } = await import("../src/components/LearningReview");
    const text = lastRunText({ trigger: "manual", started_at: 1, finished_at: 2,
      jobs: { skipped_insufficient_or_excluded_evidence: 10, diagnostic_saved: 2 }, validated: 0, validation_failed: 0 });
    expect(text).toContain("处理 12 个任务：证据不足或不允许学习 10，失败会话存为诊断样本 2。");
    expect(text).toContain("本次没有产生策略候选");
    expect(text).not.toContain("。。");
  });
  it("累计口径列出历史任务去向", async () => {
    const { totalsText } = await import("../src/components/LearningReview");
    const text = totalsText({ total: 15, pending: 0, last_processed_at: 1_761_230_400,
      outcomes: { skipped_insufficient_or_excluded_evidence: 10, skipped_reviewer_not_supported: 3, diagnostic_saved: 2 } });
    expect(text).toContain("累计入队 15 个任务，已处理 15（证据不足或不允许学习 10，提炼模型判定不足以成策略 3，失败会话存为诊断样本 2），待处理 0");
    expect(text).toContain("最近一次处理于");
  });
});
