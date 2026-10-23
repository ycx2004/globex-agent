"""把仓库内各版商品检索标注集合并成一份扩容评测集。

只做字段归一与去重，不改任何金标：
- 每条保留来源文件、来源 id、来源 split（v2 的 holdout 记在 source_split，不写入 split，
  以免被 run_product_recall 的 dev/release 选集误用）。
- kind 统一为 "<来源>:<原 kind>"，报告可按来源分组。
- 多语言集同一查询按平台/语料语言子池各有一份金标，全目录评测时按 (query, ship_to) 取并集。
- 不同来源出现完全相同的 (query, 约束, 金标) 时只保留第一条。

用法：python -m scripts.eval.build_merged_retrieval --output eval/v4/product_retrieval_merged.jsonl
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

SOURCES = (
    ("legacy", Path("eval/product_recall.jsonl")),
    ("v1", Path("eval/v1/product_retrieval.jsonl")),
    ("v2", Path("eval/v2/product_retrieval.jsonl")),
    ("v3sem", Path("eval/v3/semantic_paraphrases.jsonl")),
    ("v3ml", Path("eval/v3/multilingual_retrieval.jsonl")),
)
# run_product_recall 实际读取的检索约束字段
_SPEC_FIELDS = (
    "category", "price_max_major", "ship_to", "target_currency",
    "excluded_material_tags", "required_material_tags",
)


def _load(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _kind(source: str, row: dict) -> str:
    # 各版本描述用例类型的字段不同，统一取最贴近“查询类型”的那个
    raw = row.get("kind") or row.get("query_kind") or row.get("mode") or "lexical"
    return f"{source}:{raw}"


def _group(source: str, row: dict) -> str:
    """同族标识：同一模板/同义改写的变体共享一个 group，划 dev/holdout 时整族同去向，防止泄漏。

    各版本的分组字段不同，取最能代表“同一来源需求”的那个；都没有时回退到本条 id（自成一族）。
    """
    for field in ("pair_id", "reference_case_id", "group_id", "template_family"):
        if row.get(field):
            return f"{source}:{field}:{row[field]}"
    if row.get("family") not in (None, ""):
        return f"{source}:family:{row['family']}"
    # 无分组字段（如 legacy）：每条查询自成一族
    return f"{source}:id:{row.get('id') or 'q:'+row['query']}"


def normalize(source: str, index: int, row: dict) -> dict:
    case = {
        "id": f"{source}-{row.get('id') or f'line-{index}'}",
        "query": row["query"],
        "kind": _kind(source, row),
        "source": source,
        "source_id": row.get("id") or f"line-{index}",
        "source_split": row.get("split"),
        "group": _group(source, row),
        "expected_empty": bool(row.get("expected_empty")),
    }
    for field in _SPEC_FIELDS:
        if row.get(field) not in (None, [], ""):
            case[field] = row[field]
    case.setdefault("target_currency", "CNY")
    if row.get("relevant_canonical_ids"):
        case["relevant_canonical_ids"] = list(row["relevant_canonical_ids"])
    case["relevant"] = list(row.get("relevant") or row.get("relevant_product_ids") or [])
    for extra in ("query_language", "mode", "platform", "corpus_language"):
        if extra in row:
            case[extra] = row[extra]
    return case


def _dedupe_key(case: dict) -> str:
    gold = sorted(case.get("relevant_canonical_ids") or case["relevant"])
    spec = {field: case.get(field) for field in _SPEC_FIELDS}
    return json.dumps([case["query"], spec, gold, case["expected_empty"]], ensure_ascii=False, sort_keys=True)


def _union_multilingual(rows: list[dict]) -> list[dict]:
    """多语言集的金标只覆盖单个平台+语料语言子池；全目录检索时同一查询应以并集为金标。

    原集里一个 canonical 在某个子池无货/不可售，会被该子池金标排除，但在别的子池可售；
    全目录评测里它就是相关商品，取并集才不会把真实相关项判成误召回。
    """
    groups: dict[tuple[str, str], dict] = {}
    for row in rows:
        key = (row["query"], row.get("ship_to") or "")
        group = groups.get(key)
        if group is None:
            groups[key] = group = {**row, "relevant_canonical_ids": [], "relevant_product_ids": [],
                                   "id": row["id"], "platform": [], "corpus_language": []}
        for field, value in (("relevant_canonical_ids", row["relevant_canonical_ids"]),
                             ("relevant_product_ids", row["relevant_product_ids"]),
                             ("platform", [row["platform"]]), ("corpus_language", [row["corpus_language"]])):
            group[field].extend(v for v in value if v not in group[field])
        if row["mode"] == "monolingual":
            group["mode"] = "monolingual"  # 查询语言在目录里有同语种商品
    for group in groups.values():
        group["relevant_canonical_ids"].sort()
    return list(groups.values())


def build() -> tuple[list[dict], dict]:
    seen: set[str] = set()
    merged: list[dict] = []
    stats: dict[str, dict[str, int]] = {}
    for source, path in SOURCES:
        rows = _load(path)
        if source == "v3ml":
            rows = _union_multilingual(rows)
        kept = 0
        for index, row in enumerate(rows, 1):
            case = normalize(source, index, row)
            key = _dedupe_key(case)
            if key in seen:
                continue
            seen.add(key)
            merged.append(case)
            kept += 1
        stats[source] = {"source_rows": len(rows), "kept": kept}
    return merged, stats


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("eval/v4/product_retrieval_merged.jsonl"))
    args = parser.parse_args(argv)
    merged, stats = build()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("".join(json.dumps(c, ensure_ascii=False) + "\n" for c in merged), encoding="utf-8")
    print(json.dumps({"output": str(args.output), "total": len(merged), "sources": stats}, ensure_ascii=False))


if __name__ == "__main__":
    main()
