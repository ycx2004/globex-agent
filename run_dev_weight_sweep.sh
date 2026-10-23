#!/bin/bash
# 在 docker-app 镜像的临时容器内跑三档权重扫描(dev 294 条)
set -euo pipefail
cd "$(dirname "$0")"

# 加载 .env
if [[ ! -f .env ]]; then echo ".env 缺失"; exit 1; fi
export $(grep -v '^#' .env | grep -v '^$' | xargs)

NET=docker_default
IMG=docker-app:latest
REPO_ROOT=$(pwd)
TS=$(date +%Y%m%d-%H%M%S)
OUT_HOST="${REPO_ROOT}/eval/verification/product-recall-20261022-dev3weights"
mkdir -p "$OUT_HOST"

echo "=== 三档权重扫描 (dev 294条) ==="
echo "镜像: $IMG,  网络: $NET"
echo "输出: $OUT_HOST/"
echo

# 通用 env(临时容器需要手动传依赖的环境变量)
COMMON_ENV=(
  -e LLM_BASE_URL="$LLM_BASE_URL"
  -e LLM_API_KEY="$LLM_API_KEY"
  -e LLM_MODEL="${LLM_MODEL:-qwen3-max}"
  -e EMBEDDING_MODEL="${EMBEDDING_MODEL:-text-embedding-v4}"
  -e RERANKER_BASE_URL="${RERANKER_BASE_URL:-}"
  -e RERANKER_MODEL="${RERANKER_MODEL:-}"
  -e RERANKER_MODE="${RERANKER_MODE:-http}"
  -e QDRANT_URL=http://qdrant:6333
  -e DATABASE_URL="${DATABASE_URL:-sqlite+aiosqlite:////app/data/globex.db}"
  -e REDIS_URL="${REDIS_URL:-redis://redis:6379/0}"
  -e SEMANTIC_CACHE_ENABLED=0
  -e RECALL_CANDIDATES="${RECALL_CANDIDATES:-32}"
  -e DATA_DIR=/app/data
)

# 三组权重
declare -A CONFIGS=(
  ["1v1"]="HYBRID_LEXICAL_WEIGHT=1 HYBRID_VECTOR_WEIGHT=1"
  ["1.5v1k"]="HYBRID_LEXICAL_WEIGHT=1 HYBRID_VECTOR_WEIGHT=1.5"
  ["2v1k"]="HYBRID_LEXICAL_WEIGHT=1 HYBRID_VECTOR_WEIGHT=2"
)

for label in 1v1 1.5v1k 2v1k; do
  out_dir="/tmp/recall_dev_${TS}_${label}"
  log="/tmp/recall_dev_${TS}_${label}.log"

  eval "${CONFIGS[$label]}"

  echo "[$label] HYBRID_LEXICAL_WEIGHT=$HYBRID_LEXICAL_WEIGHT  HYBRID_VECTOR_WEIGHT=$HYBRID_VECTOR_WEIGHT"
  echo "        输出: $out_dir  →  ${OUT_HOST}/${label}/"

  docker run -d --rm --name "recall_dev_${label}" \
    --network "$NET" \
    "${COMMON_ENV[@]}" \
    -e HYBRID_LEXICAL_WEIGHT="$HYBRID_LEXICAL_WEIGHT" \
    -e HYBRID_VECTOR_WEIGHT="$HYBRID_VECTOR_WEIGHT" \
    -v "${REPO_ROOT}:/repo:ro" \
    -v "${OUT_HOST}/${label}:${out_dir}" \
    -w /repo \
    "$IMG" \
    sh -c "python -m scripts.eval.run_product_recall \
      --dataset /repo/eval/v4/product_retrieval_dev.jsonl \
      --strategy hybrid_rerank \
      --report-dir ${out_dir} \
      >>${log} 2>&1; cat ${log}"
done

echo
echo "三档已后台启动，查进度:"
echo "  docker logs -f recall_dev_1v1"
echo "完成后报告自动写入: ${OUT_HOST}/*/  (挂载卷)"
