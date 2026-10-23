# 状态：已中止，结果无效

- 本目录请求的是 `typesafe/jev-router`，实际被路由到 `z-ai/glm-5.3-flash` 与 `deepseek/deepseek-v4.1-flash-1017`，并非用户指定的 Jev 1.14 结构化判断模型。
- 2026-10-23 按用户要求中止，已完成 164/300 条 query；保留原始记录，不用于任何指标结论。
- 改用 `typesafe/jev-1.14` 时：该模型只支持 `/api/alpha/decisions`（state + questions，choice 题型需 instructions + criteria），请求格式已通过校验，但 OpenRouter 返回 403 “This model is not available in your region”，未能执行。
