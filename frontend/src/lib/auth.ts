/** 严格签名模式的账号登录：令牌只保存在本机 localStorage，由服务端密钥签发。 */
import { setBuyerIdOverride } from "./buyerIdentity";

export const TOKEN_KEY = "globex.access-token";
export type AuthState =
  | { mode: "demo" }
  | { mode: "hmac"; buyerId: string; isLearningAdmin: boolean };

function apiBase(): string {
  return ((import.meta.env.VITE_API_BASE as string | undefined) ?? "").replace(/\/$/, "");
}

export function readToken(): string | null {
  try { return window.localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

export function clearSession(): void {
  try { window.localStorage.removeItem(TOKEN_KEY); } catch { /* 存储受限时忽略。 */ }
  setBuyerIdOverride(null);
}

/** 返回 null 表示需要登录；网络失败抛错，交给页面提示重试。 */
export async function fetchAuthState(fetcher: typeof fetch = globalThis.fetch): Promise<AuthState | null> {
  const token = readToken();
  const response = await fetcher(`${apiBase()}/commerce/auth/me`, {
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (response.status === 401) return null;
  if (!response.ok) throw new Error("身份服务暂时不可用");
  const data = await response.json();
  if (data.mode !== "hmac") return { mode: "demo" };
  return { mode: "hmac", buyerId: String(data.buyer_id), isLearningAdmin: data.is_learning_admin === true };
}

export async function login(username: string, password: string, fetcher: typeof fetch = globalThis.fetch): Promise<AuthState> {
  const response = await fetcher(`${apiBase()}/commerce/auth/login`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(typeof data.detail === "string" ? data.detail : response.status === 429 ? "尝试次数过多，请稍后再试" : "登录失败，请稍后重试");
  }
  window.localStorage.setItem(TOKEN_KEY, data.access_token);
  // 会话、收藏等都按 buyer_id 查询；登录账号即买家身份。
  setBuyerIdOverride(data.buyer_id);
  return { mode: "hmac", buyerId: data.buyer_id, isLearningAdmin: data.is_learning_admin === true };
}
