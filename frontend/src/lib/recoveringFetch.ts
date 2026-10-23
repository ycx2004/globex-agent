import type { HttpAgentConfig } from "@ag-ui/client";

type Fetcher = NonNullable<HttpAgentConfig["fetch"]>;
type Options = {
  runId: string;
  eventsUrl: string;
  resume?: boolean;
  onReconnect: () => void;
  onConnected: () => void;
  retries?: number;
  retryDelay?: number;
  /** 建连或读流超过该时长无任何字节（含 keep-alive 注释）即视为连接僵死；服务端每 15 秒发 keep-alive。 */
  stallTimeout?: number;
};

/** 保留官方 SDK 的协议校验/归约器；传输层只处理游标、重复帧与断线重连。 */
export function recoveringFetch(fetcher: Fetcher, options: Options): Fetcher {
  return async (input, init) => {
    const signal = init?.signal;
    let cursor = 0, terminal = false, journaled = options.resume === true, closed = false;
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    const headers = new Headers(init?.headers);
    headers.set("Accept", "text/event-stream");
    const stall = options.stallTimeout ?? 30000;
    // 每条连接一个控制器：外部 signal 取消时跟随中止；僵死时只中止本连接，再走 GET 恢复。
    let link: AbortController | undefined;
    const connect = (call: (connection: AbortSignal) => Promise<Response>) => {
      const current = new AbortController();
      link = current;
      const follow = () => current.abort(signal?.reason);
      if (signal?.aborted) current.abort(signal.reason);
      else signal?.addEventListener("abort", follow, { once: true });
      const timer = setTimeout(() => current.abort(new DOMException("连接超时", "TimeoutError")), stall);
      return call(current.signal).finally(() => clearTimeout(timer));
    };
    const reconnect = () => connect((connection) => fetcher(`${options.eventsUrl}&after=${cursor}`, {
      method: "GET", signal: connection, headers,
    }));
    const read = (source: ReadableStreamDefaultReader<Uint8Array>) => new Promise<ReadableStreamReadResult<Uint8Array>>((resolve, reject) => {
      const timer = setTimeout(() => { link?.abort(); reject(new Error("事件流长时间无数据，重新连接")); }, stall);
      source.read().then((value) => { clearTimeout(timer); resolve(value); }, (error) => { clearTimeout(timer); reject(error); });
    });
    let response: Response;
    try {
      response = await (options.resume ? reconnect() : connect((connection) => fetcher(input, { ...init, signal: connection })));
    } catch (error) {
      if (signal?.aborted) throw error;
      options.onReconnect();
      response = await reconnect();
      journaled = true;
    }
    if (!response.ok || !response.body) return response;
    const encoder = new TextEncoder();
    const body = new ReadableStream<Uint8Array>({
      async start(controller) {
        let attempts = 0;
        try {
          while (!closed) {
            let buffer = "";
            const decoder = new TextDecoder();
            try {
              reader = response.body!.getReader();
              while (!closed) {
                const chunk = await read(reader);
                if (chunk.done) break;
                buffer = (buffer + decoder.decode(chunk.value, { stream: true })).replace(/\r\n/g, "\n");
                let boundary: number;
                while ((boundary = buffer.indexOf("\n\n")) >= 0) {
                  const frame = buffer.slice(0, boundary);
                  buffer = buffer.slice(boundary + 2);
                  const data = frame.split("\n").filter((line) => line.startsWith("data:")).map((line) => line.slice(5).trimStart()).join("\n");
                  if (!data) continue;
                  const event = JSON.parse(data) as { type?: string };
                  const id = frame.split("\n").find((line) => line.startsWith("id:"))?.slice(3).trim();
                  if (id) {
                    journaled = true;
                    const prefix = `${options.runId}:`;
                    if (!id.startsWith(prefix) || !/^\d+$/.test(id.slice(prefix.length))) throw new Error("事件游标不属于当前运行");
                    const sequence = Number(id.slice(prefix.length));
                    if (!Number.isSafeInteger(sequence)) throw new Error("事件游标无效");
                    if (sequence <= cursor) continue;
                    if (sequence !== cursor + 1) throw new Error("事件顺序有缺口，重新读取持久日志");
                    cursor = sequence;
                    attempts = 0;
                  } else if (journaled) throw new Error("恢复流缺少持久事件游标");
                  terminal = event.type === "RUN_FINISHED" || event.type === "RUN_ERROR";
                  options.onConnected();
                  controller.enqueue(encoder.encode(`${frame}\n\n`));
                }
              }
              if (terminal) { controller.close(); return; }
              if (!journaled) throw new Error("当前连接不支持持久恢复，且缺少终态事件");
              throw new Error("事件流提前结束");
            } catch (error) {
              await reader?.cancel().catch(() => {});
              reader = undefined;
              if (closed || signal?.aborted) throw error;
              if (!journaled || ++attempts > (options.retries ?? 8)) throw error;
              options.onReconnect();
              await new Promise<void>((resolve, reject) => {
                const abort = () => { clearTimeout(timer); reject(new DOMException("已断开订阅", "AbortError")); };
                const timer = setTimeout(() => { signal?.removeEventListener("abort", abort); resolve(); }, Math.min(2000, (options.retryDelay ?? 200) * attempts));
                signal?.addEventListener("abort", abort, { once: true });
                if (signal?.aborted) abort();
              });
              // 恢复请求本身也可能断网；仍只 GET 日志，绝不重新 POST 模型执行。
              while (true) {
                try {
                  response = await reconnect();
                  if ([401, 403, 404, 409, 422].includes(response.status)) throw new Error(`HTTP ${response.status}`);
                  if (!response.ok || !response.body) throw new Error(`HTTP ${response.status}`);
                  break;
                } catch (reconnectError) {
                  if (signal?.aborted || /HTTP (401|403|404|409|422)/.test(String(reconnectError)) || ++attempts > (options.retries ?? 8)) throw reconnectError;
                  await new Promise((resolve) => setTimeout(resolve, Math.min(2000, (options.retryDelay ?? 200) * attempts)));
                  if (closed || signal?.aborted) throw new DOMException("已断开订阅", "AbortError");
                }
              }
            }
          }
        } catch (error) {
          if (!closed) controller.error(error);
        } finally {
          await reader?.cancel().catch(() => {});
        }
      },
      async cancel() { closed = true; await reader?.cancel().catch(() => {}); },
    });
    return new Response(body, { status: response.status, headers: response.headers });
  };
}
