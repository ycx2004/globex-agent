# ESCI 池化补判复核：typesafe/jev-1.14

- 判官：OpenRouter `typesafe/jev-1.14-20261014`（7009/7009 返回模型一致），经服务器本机 mihomo 127.0.0.1:7890；逐 query-商品对单独请求，0 失败，耗时 455s，费用 0.1865 USD。
- 范围：基线与查询理解版 @16 结果并集 7009 对；其中 4141 对无人工标签用于补判，2868 对有人工标签用于盲判校准。
- 文件：`judge_jev114.py`（判官脚本）、`judgments.jsonl`（原始判定，含概率与置信度）、`rescore_jev114.py`、`rescore.log`（全部指标、混淆矩阵、配对 bootstrap）、`run.log`。
- 人工标签与 LLM 判定均为评测辅助，未经人工复核。
