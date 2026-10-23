from unittest.mock import AsyncMock

import pytest

from app.infrastructure.vector.milvus_product_index import MilvusProductIndex


def _index(search_ef: int) -> tuple[MilvusProductIndex, AsyncMock]:
    # 绕过 __init__，避免单测连接真实 Milvus
    index = MilvusProductIndex.__new__(MilvusProductIndex)
    client = AsyncMock()
    client.search.return_value = [[{"entity": {"product_id": "P1"}, "distance": 0.9}]]
    index._async, index._collection, index._search_ef, index._loaded = client, "c", search_ef, True
    return index, client


@pytest.mark.parametrize("top_n, expected_ef", [(64, 128), (128, 128), (256, 256)])
async def test_search_ef_never_below_limit(top_n, expected_ef):
    # HNSW 要求 ef >= limit；补召回翻倍到 256 时若沿用 ef=128，服务端会直接报错
    index, client = _index(search_ef=128)
    hits = await index._search([0.0], top_n, "true")
    assert client.search.await_args.kwargs["search_params"]["params"]["ef"] == expected_ef
    assert client.search.await_args.kwargs["limit"] == top_n
    assert [h.product_id for h in hits] == ["P1"]
