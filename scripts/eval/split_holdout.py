"""把合并评测集划分为 dev 与 holdout 两份，holdout 锁定只用于最终验收。

防泄漏原则：
- 同族（group）查询整族同去向，绝不跨 dev/holdout 拆开——否则近义改写会泄漏。
- 作者已划定的验收分区直接沿用：v1 的 release、v2 的 holdout 一律进 holdout；
  v1/v2 的 dev 留在 dev。
- 其余无作者划分的来源（legacy/v3sem/v3ml）按 group 的 md5 确定性取约 1/4 进 holdout，
  同一 group 的所有条目同去向。确定性意味着可复现，不随运行变化。

用法：python -m scripts.eval.split_holdout \
        --merged eval/v4/product_retrieval_merged.jsonl \
        --dev eval/v4/product_retrieval_dev.jsonl \
        --holdout eval/v4/product_retrieval_holdout.jsonl
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
from pathlib import Path

# 作者已划定为验收用途的 (来源, source_split)
_AUTHOR_HOLDOUT = {("v1", "release"), ("v2", "holdout")}
_AUTHOR_DEV = {("v1", "dev"), ("v2", "dev")}
_HOLDOUT_BUCKET = 0  # md5(group) % 4 == 0 → holdout（约 25%）


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assign(case: dict) -> str:
    tag = (case["source"], case.get("source_split"))
    if tag in _AUTHOR_HOLDOUT:
        return "holdout"
    if tag in _AUTHOR_DEV:
        return "dev"
    digest = hashlib.md5(case["group"].encode("utf-8")).hexdigest()
    return "holdout" if int(digest, 16) % 4 == _HOLDOUT_BUCKET else "dev"


def split(cases: list[dict]) -> tuple[list[dict], list[dict]]:
    # 先按 group 决定去向，保证整族一致（即便作者 split 在同族内不一致，也以多数/holdout 优先）
    group_dest: dict[str, str] = {}
    for case in cases:
        dest = _assign(case)
        # 同族若出现 holdout，则整族 holdout（宁可多锁，不可泄漏）
        if group_dest.get(case["group"]) == "holdout" or dest == "holdout":
            group_dest[case["group"]] = "holdout"
        else:
            group_dest[case["group"]] = "dev"
    dev, holdout = [], []
    for case in cases:
        case = {**case, "split": group_dest[case["group"]]}
        (holdout if case["split"] == "holdout" else dev).append(case)
    return dev, holdout


def _summary(cases: list[dict]) -> dict:
    by_source = collections.Counter(c["source"] for c in cases)
    return {"total": len(cases), "groups": len({c["group"] for c in cases}),
            "empty_cases": sum(1 for c in cases if c["expected_empty"]), "by_source": dict(by_source)}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--merged", type=Path, default=Path("eval/v4/product_retrieval_merged.jsonl"))
    parser.add_argument("--dev", type=Path, default=Path("eval/v4/product_retrieval_dev.jsonl"))
    parser.add_argument("--holdout", type=Path, default=Path("eval/v4/product_retrieval_holdout.jsonl"))
    args = parser.parse_args(argv)

    cases = _load(args.merged)
    dev, holdout = split(cases)
    # 断言：无 group 跨分区
    dev_groups = {c["group"] for c in dev}
    holdout_groups = {c["group"] for c in holdout}
    leaked = dev_groups & holdout_groups
    assert not leaked, f"group 泄漏: {leaked}"

    for path, rows in ((args.dev, dev), (args.holdout, holdout)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in rows), encoding="utf-8")
    print(json.dumps({"dev": _summary(dev), "holdout": _summary(holdout),
                      "leaked_groups": len(leaked)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
