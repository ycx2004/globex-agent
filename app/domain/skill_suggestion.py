"""对话后 Skill 建议：信号判断、提案结构与入库前安全扫描（纯函数，无 IO）。

借鉴 Hermes 的后台复盘：有信号才提炼、优先修补已有 Skill、写入前扫描；
不同点：草稿只是建议，必须买家确认才写入个人 Skill，默认倾向不提。
"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# 买家明确要求复用：“以后都这样”“记住这个做法”。
_EXPLICIT = re.compile(r"以后|下次|每次|每回|总是|一直这样|都这样|按这个|照这个|记住|养成|规则|习惯|"
                       r"always|next time|every time|from now on|remember", re.I)
# 纠正上一步：“不对，应该先看…”“换成按单价排”。
_CORRECTION = re.compile(r"不对|不是这样|搞错|错了|应该先|应该按|换成|改成|重新(?:比|排|筛|找)|而是|"
                         r"that's wrong|instead|actually", re.I)
# 多步流程表述：“先…再…”“按…排序”“对比…”。
_PROCEDURE = re.compile(r"先.{1,40}(?:再|然后|之后|最后)|第一步|步骤|流程|按.{1,12}(?:排序|排|比较|筛)|"
                        r"对比|比较|比价|筛掉|排除|first .{1,40} then", re.I)

SIGNAL_KINDS = ("explicit", "correction", "procedure")


def detect_signals(user_texts: list[str]) -> list[str]:
    """只看买家自己的话；返回命中的信号种类，空列表表示不值得调用模型。"""
    joined = "\n".join(user_texts[-8:])
    found = []
    for name, pattern in zip(SIGNAL_KINDS, (_EXPLICIT, _CORRECTION, _PROCEDURE)):
        if pattern.search(joined):
            found.append(name)
    return found


def worth_auto_suggest(user_texts: list[str], tool_calls: int) -> list[str]:
    """自动触发门槛：至少两轮买家发言、做过实际检索，且出现至少一类信号。"""
    if len([t for t in user_texts if t.strip()]) < 2 or tool_calls < 1:
        return []
    return detect_signals(user_texts)


class SkillProposal(BaseModel):
    """提炼模型的结构化输出；decision=none 是最常见的正确答案。"""
    model_config = ConfigDict(extra="forbid")
    decision: Literal["create", "update", "none"]
    kind: Literal["procedure", "preference", "one_off", "chitchat"]
    reason: str = Field(min_length=1, max_length=300)
    target_skill_id: str | None = Field(default=None, max_length=128)
    title: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=200)
    body: str | None = Field(default=None, max_length=2000)


# Skill 会注入后续 Prompt：拦截指令覆盖、外链、越权操作和残留个人信息占位符。
_UNSAFE = [
    (re.compile(r"https?://|www\.", re.I), "包含外部链接"),
    (re.compile(r"忽略.{0,8}(?:指令|规则|系统|要求|限制)|无视.{0,8}(?:指令|规则)|ignore (?:all|previous|the above)|"
                r"system prompt|系统提示|开发者模式|jailbreak", re.I), "包含指令覆盖语句"),
    (re.compile(r"[a-z]+_tool\b|调用.{0,6}工具|执行.{0,6}(?:脚本|代码|命令)", re.I), "包含工具或脚本调用要求"),
    (re.compile(r"(?:直接|自动|立即)(?:下单|付款|支付|购买|取消订单)|(?:无需|不用|跳过|不必)(?:确认|审批|批准)", re.I), "要求绕过确认或自动交易"),
    (re.compile(r"\[(?:姓名|地址|手机号|邮箱|身份证|网址|密钥|邮编|长数字)\]"), "残留个人信息"),
    (re.compile(r"\b(?:sk|pk)-[\w.-]{6,}"), "疑似密钥"),
]


def safety_issues(*texts: str) -> list[str]:
    joined = "\n".join(t or "" for t in texts)
    return [label for pattern, label in _UNSAFE if pattern.search(joined)]


def _bigrams(text: str) -> set[str]:
    folded = re.sub(r"\s+", "", (text or "").casefold())
    return {folded[i:i + 2] for i in range(len(folded) - 1)} or ({folded} if folded else set())


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard，用于提炼后的重复兜底；不依赖向量服务。"""
    left, right = _bigrams(a), _bigrams(b)
    return len(left & right) / len(left | right) if left and right else 0.0


def fingerprint(title: str) -> str:
    """丢弃记录的指纹：同名建议在冷却期内不再提出。"""
    return re.sub(r"[\s\W_]+", "", (title or "").casefold())[:60]
