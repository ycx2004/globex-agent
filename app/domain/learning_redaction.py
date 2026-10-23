"""学习证据的规则脱敏：把个人信息替换为占位符后再进入提炼，不再因出现敏感词而整段跳过。

规则只能覆盖已知值、明确语境和固定格式；自由书写的姓名/地址可能漏网，
因此证据标记为 rule_redacted，发布前仍须管理员人工审核。只记录各类替换次数，不保留原值。
"""
from __future__ import annotations

import json
import re
from collections import Counter

NAME, ADDRESS, PHONE, EMAIL, ID_CARD, URL, SECRET, POSTAL, NUMBER = (
    "[姓名]", "[地址]", "[手机号]", "[邮箱]", "[身份证]", "[网址]", "[密钥]", "[邮编]", "[长数字]")

# 结构化字段（下单工具参数、会话状态）中的已知值：精确替换其在文本中的所有出现。
KNOWN_FIELDS = {
    "recipient_name": NAME, "receiver_name": NAME, "contact_name": NAME, "consignee": NAME,
    "address_line": ADDRESS, "address": ADDRESS, "street": ADDRESS,
    "phone": PHONE, "mobile": PHONE, "email": EMAIL, "postal_code": POSTAL, "id_number": ID_CARD,
}

_CJK_NAME = r"[一-龥·]{2,4}"
_STOP = r"[^\s，,。；;！!？?、\n（）()]"
# 明确语境：只在自我介绍/收件信息等提示词之后替换，避免“我是敏感肌”之类误伤。
_CONTEXT_RULES = [
    (re.compile(r"(我叫|我的名字是|我的名字叫|本人姓名|(?:姓名|收件人|收货人|联系人)\s*[:：是为]?)\s*(" + _CJK_NAME + ")"), NAME),
    (re.compile(r"((?:my name is|name\s*[:：]|recipient\s*[:：])\s*)([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+){0,2})", re.I), NAME),
    (re.compile(r"((?:收货地址|收件地址|详细地址|地址|寄到|送到|邮寄到|配送到)\s*[:：是为]?\s*)(" + _STOP + "{4,80})"), ADDRESS),
]
# 固定格式：先替换长的/特征强的，避免手机号被拆进长数字。
_FORMAT_RULES = [
    (re.compile(r"\b(?:sk|pk)-[\w.-]{6,}"), SECRET),
    (re.compile(r"https?://\S+"), URL),
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), EMAIL),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), ID_CARD),
    (re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d[-\s]?\d{4}[-\s]?\d{4}(?!\d)"), PHONE),
    (re.compile(r"(?<!\d)0\d{2,3}-\d{7,8}(?!\d)"), PHONE),
    # 结构化中文地址：省/市/区县 + 路街巷 + 门牌，即使没有“地址”提示词也替换。
    (re.compile(r"[一-龥]{2,12}(?:省|市|区|县)[一-龥A-Za-z\d-]{0,30}?(?:路|街|大道|巷|弄|村)"
                r"[一-龥A-Za-z\d-]{0,20}?(?:号|室|栋|幢|单元|楼)"), ADDRESS),
    (re.compile(r"(?<![\w-])\d{7,}(?![\w-])"), NUMBER),
]


def known_values(*sources) -> dict[str, str]:
    """从工具参数/状态里递归收集个人信息字段的值 → 占位符。"""
    found: dict[str, str] = {}

    def walk(value, parent_key=""):
        if isinstance(value, dict):
            for key, item in value.items():
                walk(item, str(key).casefold())
        elif isinstance(value, list):
            for item in value:
                walk(item, parent_key)
        elif isinstance(value, str):
            placeholder = KNOWN_FIELDS.get(parent_key)
            text = value.strip()
            if placeholder and len(text) >= 2:
                found.setdefault(text, placeholder)
    for source in sources:
        if isinstance(source, str):
            try:
                source = json.loads(source)
            except ValueError:
                continue
        walk(source)
    return found


_ADDRESS_HINT = re.compile(r"\d|省|市|区|县|镇|乡|村|路|街|道|巷|弄|号|栋|幢|室|单元|小区|大厦|\b(?:street|st|road|rd|ave|avenue)\b", re.I)


def context_values(*texts: str) -> dict[str, str]:
    """按语境识别姓名/地址；对同一轮的问题和回答一起收集，问题里自报的姓名在回答里也会被替换。"""
    found: dict[str, str] = {}
    for text in texts:
        for pattern, placeholder in _CONTEXT_RULES:
            for match in pattern.finditer(text):
                value = match.group(2).strip()
                # 地址须带门牌/行政区等特征，避免“地址稍后再填”之类误伤。
                if not value or value.startswith("[") or (placeholder == ADDRESS and not _ADDRESS_HINT.search(value)):
                    continue
                found.setdefault(value, placeholder)
    return found


def redact(text: str, known: dict[str, str] | None = None) -> tuple[str, Counter]:
    counts: Counter = Counter()
    known = {**context_values(text), **(known or {})}

    for value in sorted(known, key=len, reverse=True):
        hits = text.count(value)
        if hits:
            text = text.replace(value, known[value])
            counts[known[value]] += hits

    for pattern, placeholder in _FORMAT_RULES:
        text, hits = pattern.subn(placeholder, text)
        if hits:
            counts[placeholder] += hits
    return text, counts
