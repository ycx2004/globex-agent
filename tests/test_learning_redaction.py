"""学习证据规则脱敏：个人信息替换为占位符，正常选购文本不被误伤。"""
import json

from app.domain.learning_redaction import known_values, redact
from app.infrastructure.ag_ui_journal import AGUIJournal
from app.infrastructure.learning_store import LearningStore
from tests.test_learning_pipeline import run_body


def test_self_introduced_name_replaced_everywhere():
    text, counts = redact("我叫夏瑜程，帮我找背包。好的夏瑜程，已为你筛选。")
    assert "夏瑜程" not in text and text.count("[姓名]") == 2 and counts["[姓名]"] == 2


def test_known_order_fields_replaced_without_context_words():
    known = known_values(json.dumps({"shipping_address": {"recipient_name": "张小明", "address_line": "中山北路100号3栋",
                                                           "phone": "13812345678", "postal_code": "200000"}}))
    text, _ = redact("已为张小明下单，寄往中山北路100号3栋，联系 13812345678", known)
    assert text == "已为[姓名]下单，寄往[地址]，联系 [手机号]"


def test_formats_and_contextual_address():
    text, counts = redact("收货地址：上海市浦东新区世纪大道88号，邮箱 a.b@example.test，"
                          "身份证 11010519491231002X，电话 138-1234-5678，见 https://x.test/p，sk-abcdefgh123")
    for raw in ("世纪大道", "example.test", "11010519491231002X", "1234-5678", "https://", "sk-abc"):
        assert raw not in text
    assert {"[地址]", "[邮箱]", "[身份证]", "[手机号]", "[网址]", "[密钥]"} <= set(counts)


def test_ordinary_shopping_text_untouched():
    source = "我是敏感肌，想要不含酒精的面霜，预算 200 元以内，地址稍后再填，P1003 和 P1004 对比一下"
    text, counts = redact(source)
    assert text == source and not counts


async def test_runtime_evidence_redacts_instead_of_skipping(tmp_path):
    journal = AGUIJournal(tmp_path/"runs.db", learning_enabled=True)
    body = run_body(); body["messages"][-1]["content"] = "我叫夏瑜程，手机 13812345678，帮我挑背包"
    await journal.reserve(body, "b1", "owner")
    await journal.append("r1", "owner", [
        {"type": "TOOL_CALL_START", "toolCallId": "t1"}, {"type": "TOOL_CALL_END", "toolCallId": "t1"},
        {"type": "TOOL_CALL_START", "toolCallId": "t2"},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t2", "delta": '{"shipping_address":{"recipient_name":"李雷",'},
        {"type": "TOOL_CALL_ARGS", "toolCallId": "t2", "delta": '"address_line":"幸福路8号"}}'},
        {"type": "TOOL_CALL_END", "toolCallId": "t2"},
        {"type": "STATE_SNAPSHOT", "snapshot": {"products": [{"product_id": "P1003", "title": "背包"}]}},
        {"type": "TEXT_MESSAGE_START", "messageId": "a1"},
        {"type": "TEXT_MESSAGE_CONTENT", "messageId": "a1", "delta": "夏瑜程你好，可寄给李雷（幸福路8号）"},
        {"type": "TEXT_MESSAGE_END", "messageId": "a1"}, {"type": "RUN_FINISHED"}])
    store = LearningStore(journal.path)
    source = store.evidence(store.claim("worker"))
    blob = json.dumps(source, ensure_ascii=False)
    assert source["meaningful"] is True and source["privacy"].startswith("rule_redacted")
    for raw in ("夏瑜程", "13812345678", "李雷", "幸福路8号"):
        assert raw not in blob
    assert source["input"] == "我叫[姓名]，手机 [手机号]，帮我挑背包"
    assert source["redactions"] == {"[姓名]": 3, "[手机号]": 1, "[地址]": 1}
