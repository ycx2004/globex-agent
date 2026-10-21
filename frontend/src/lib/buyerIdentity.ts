/** 本机私人部署的买家身份：默认 ycx，可在页面侧栏点击用户名修改。 */
export const DEFAULT_BUYER_ID = "ycx";
const OVERRIDE_KEY = "globex.buyer-id-override";

export function resolveBuyerId(): string {
  try {
    const override = window.localStorage.getItem(OVERRIDE_KEY)?.trim();
    if (override) return override;
  } catch {
    /* 存储受限时使用构建配置或默认身份。 */
  }
  return (import.meta.env.VITE_BUYER_ID as string | undefined) || DEFAULT_BUYER_ID;
}

export function setBuyerIdOverride(id: string | null): void {
  try {
    const value = id?.trim();
    if (value) window.localStorage.setItem(OVERRIDE_KEY, value);
    else window.localStorage.removeItem(OVERRIDE_KEY);
  } catch {
    /* 存储受限时忽略，下次启动仍用默认身份。 */
  }
}
