// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { ConversationSkillSuggestion, PendingSkillSuggestions, POLL_INTERVAL_MS, POLL_WINDOW_MS } from "../src/components/SkillSuggestions";

let host: HTMLDivElement, root: Root;
beforeEach(() => {
  (globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;
  host = document.createElement("div"); document.body.append(host); root = createRoot(host);
});
afterEach(async () => { await act(async () => root.unmount()); host.remove(); vi.useRealTimers(); vi.restoreAllMocks(); });

const suggestion = { id: "g1", session_id: "s1", run_id: "r1", decision: "create", target_skill_id: null, title: "按单价比价",
  description: "比较同类商品时", body: "1. 先换算单价", reason: "你纠正过比较方式", signals: ["correction"], created_at: 1 };
const button = (text: string) => [...host.querySelectorAll("button")].find(b => b.textContent?.includes(text)) as HTMLButtonElement | undefined;
const flush = () => act(async () => { await Promise.resolve(); await Promise.resolve(); });

it("前端不再发起空闲分析，只轮询服务端结果；卡片非阻塞，确认后才保存", async () => {
  vi.useFakeTimers();
  let ready = false;
  const request = vi.fn(async (path: string) => path.startsWith("/skill-suggestions?") ? { suggestions: ready ? [suggestion] : [] }
    : { suggestion: { ...suggestion, status: "accepted" } });
  const changed = vi.fn(async () => undefined);
  await act(async () => root.render(<ConversationSkillSuggestion sessionId="s1" runKey="r1" running={false} busy={false} request={request} onSkillsChanged={changed} />));
  await flush();
  expect(host.textContent).not.toContain("可以把这次的做法记成 Skill");
  ready = true;   // 服务端空闲整理已产出建议
  await act(async () => { vi.advanceTimersByTime(POLL_INTERVAL_MS); }); await flush();
  expect(request).not.toHaveBeenCalledWith("/skill-suggestions/analyze", "POST", expect.anything());
  expect(host.querySelector("[role=dialog]")).toBeNull();
  expect(host.textContent).toContain("可以把这次的做法记成 Skill");
  await act(async () => button("保存 Skill")!.click()); await flush();
  expect(request).toHaveBeenCalledWith("/skill-suggestions/g1/accept", "POST", { title: "按单价比价", description: "比较同类商品时", body: "1. 先换算单价" });
  expect(changed).toHaveBeenCalled();
  expect(host.textContent).toContain("已保存到“我的 Skill”");
});

it("轮询窗口结束后停止读取", async () => {
  vi.useFakeTimers();
  const request = vi.fn(async () => ({ suggestions: [] }));
  await act(async () => root.render(<ConversationSkillSuggestion sessionId="s1" runKey="r1" running={false} busy={false} request={request} onSkillsChanged={async () => undefined} />));
  await act(async () => { vi.advanceTimersByTime(POLL_WINDOW_MS + POLL_INTERVAL_MS * 2); }); await flush();
  const count = request.mock.calls.length;
  await act(async () => { vi.advanceTimersByTime(POLL_INTERVAL_MS * 5); }); await flush();
  expect(request.mock.calls.length).toBe(count);
});

it("运行中不显示也不轮询；手动整理无结果时给出说明", async () => {
  vi.useFakeTimers();
  const request = vi.fn(async (path: string) => path.startsWith("/skill-suggestions?") ? { suggestions: [] } : { status: "declined", suggestion: null });
  await act(async () => root.render(<ConversationSkillSuggestion sessionId="s1" runKey="r1" running busy={false} request={request} onSkillsChanged={async () => undefined} />));
  await act(async () => { vi.advanceTimersByTime(POLL_INTERVAL_MS * 4); });
  expect(host.textContent).toBe("");
  expect(request.mock.calls.length).toBe(1);   // 只有挂载时读取一次
  await act(async () => root.render(<ConversationSkillSuggestion sessionId="s1" runKey="r1" running={false} busy={false} request={request} onSkillsChanged={async () => undefined} />));
  await act(async () => button("沉淀为 Skill")!.click()); await flush();
  expect(request).toHaveBeenCalledWith("/skill-suggestions/analyze", "POST", { session_id: "s1", trigger: "manual" });
  expect(host.textContent).toContain("没有找到值得长期复用的做法");
});

it("Skill 页列出待确认建议，可编辑后保存或忽略", async () => {
  const request = vi.fn(async (path: string, _m?: string, _b?: Record<string, unknown>) => path === "/skill-suggestions" ? { suggestions: [suggestion, { ...suggestion, id: "g2", decision: "update", target_skill_id: "k1" }] } : { suggestion: {} });
  const saved = vi.fn();
  await act(async () => root.render(<PendingSkillSuggestions request={request} busy={false} onSaved={saved} />)); await flush();
  expect(host.textContent).toContain("有 2 条建议");
  expect(button("确认更新")).toBeTruthy();
  await act(async () => button("编辑")!.click());
  const input = host.querySelector("input") as HTMLInputElement;
  await act(async () => { Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(input, "单价优先"); input.dispatchEvent(new Event("input", { bubbles: true })); });
  await act(async () => button("保存 Skill")!.click()); await flush();
  expect(request).toHaveBeenCalledWith("/skill-suggestions/g1/accept", "POST", expect.objectContaining({ title: "单价优先" }));
  expect(saved).toHaveBeenCalledTimes(1);
  await act(async () => button("忽略")!.click()); await flush();
  expect(request).toHaveBeenCalledWith("/skill-suggestions/g2/dismiss", "POST", undefined);
  expect(host.textContent).toBe("");
});
