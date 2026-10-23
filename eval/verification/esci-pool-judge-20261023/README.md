# ESCI 池化补判（2026-10-23）

- 判定池：基线 c64 k16（hybrid_only/hybrid_rerank）top16 并集中无人工 ESCI 标签的商品；`-extra-qu` 补判查询理解版 top16 残留 311 个。
- 判官：`qwen3.8-flash`，temperature=0，enable_thinking=false，每 query 一次调用；商品以别名盲测，混入至多 8 个已有人工标签商品做校准。
- 文件：`judgments.jsonl`（LLM 标签+校准对）、`pool_judge.py`、`rescore.py`、`rescore.json`、`calibration_and_paired.txt`、`run.log`。
- 重算规则：人工标签优先，缺失才用 LLM；ESCI nDCG 增益 E=1/S=0.1/C=0.01/I=0。
- 局限：与人工四分类一致率 59.7%；LLM 判 E 的精确率 0.789（相对人工），判官与在线模型同属 qwen 系，可能存在偏向。补判结果仅为评测辅助，非人工金标。
