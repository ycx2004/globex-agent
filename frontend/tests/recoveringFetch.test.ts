import { expect, it } from "vitest";
import { recoveringFetch } from "../src/lib/recoveringFetch";

const frame = (runId: string, seq: number, event: Record<string, unknown>) => `id: ${runId}:${seq}\ndata: ${JSON.stringify(event)}\n\n`;
const encoder = new TextEncoder();

// 只吐出首帧后永远挂起的流，模拟 HTTP/3 连接中途僵死；被中止时才报错。
const hanging = (first: string, signal?: AbortSignal | null) => new Response(new ReadableStream<Uint8Array>({
  start(controller) {
    controller.enqueue(encoder.encode(first));
    signal?.addEventListener("abort", () => controller.error(new DOMException("aborted", "AbortError")));
  },
}), { headers: { "Content-Type": "text/event-stream" } });

const drain = async (response: Response) => {
  const reader = response.body!.getReader(); const decoder = new TextDecoder(); let text = "";
  while (true) { const chunk = await reader.read(); if (chunk.done) return text; text += decoder.decode(chunk.value); }
};

it("读流僵死超时后只 GET 恢复，从游标续读到终态", async () => {
  const calls: Array<{ method: string; url: string }> = [];
  const fetcher = async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ method: init?.method ?? "GET", url: String(url) });
    if (init?.method === "POST") return hanging(frame("r1", 1, { type: "RUN_STARTED" }), init.signal);
    return new Response(frame("r1", 2, { type: "TEXT_MESSAGE_START" }) + frame("r1", 3, { type: "RUN_FINISHED" }));
  };
  let reconnects = 0;
  const wrapped = recoveringFetch(fetcher, { runId: "r1", eventsUrl: "/events?buyer_id=ycx", onReconnect: () => { reconnects++; }, onConnected: () => {}, retryDelay: 1, stallTimeout: 30 });
  const text = await drain(await wrapped("/run", { method: "POST", body: "{}" }));
  expect(text).toContain("RUN_FINISHED");
  expect(reconnects).toBe(1);
  expect(calls.filter((call) => call.method === "POST")).toHaveLength(1);
  expect(calls[1].url).toBe("/events?buyer_id=ycx&after=1");
});

it("恢复请求一直无响应时有限重试后报错，不无限停在恢复中", async () => {
  let gets = 0;
  const fetcher = async (_url: RequestInfo | URL, init?: RequestInit) => {
    if (init?.method === "POST") return hanging(frame("r2", 1, { type: "RUN_STARTED" }), init.signal);
    gets++;
    // 建连本身挂起，只能靠超时中止
    return new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
  };
  const wrapped = recoveringFetch(fetcher, { runId: "r2", eventsUrl: "/events?buyer_id=ycx", onReconnect: () => {}, onConnected: () => {}, retries: 2, retryDelay: 1, stallTimeout: 20 });
  await expect(drain(await wrapped("/run", { method: "POST", body: "{}" }))).rejects.toBeTruthy();
  expect(gets).toBeGreaterThanOrEqual(1);
  expect(gets).toBeLessThanOrEqual(2);
});
