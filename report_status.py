#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
report_status.py — 日报「到底有没有真正成功」的跨进程契约

为什么需要它：
  daily_report.py 是 service.py 用 subprocess 拉起的独立子进程，两者之间只有
  退出码和落盘文件两个通道。旧版模型调用失败时 daily_report.py 仍然 return 0，
  于是 service.py 记成「今天日报已完成」，而报告正文里只有一句
  「（模型调用失败：HTTP Error 403）」—— 14 天里没有任何一处能看出这是故障。

  修法是让「成功 / 降级」成为**写在产物里的事实**，而不是靠退出码转述：
  写方在日报首行落一条标记，读方只看这条标记。定义只此一处，两端都 import 它。

标记形态（日报首行，HTML 注释，渲染后不可见）：

    <!-- daily-report:status=ok -->
    <!-- daily-report:status=degraded reason=llm_failed -->

读不到标记 = 未确认成功（旧格式文件、被截断的文件、根本没写出来），
调用方**不得**把它当成成功 —— 这正是本模块要堵掉的那条缝。
"""

# 状态取值
OK = "ok"                # 模型分析正常产出
DEGRADED = "degraded"    # 写出的是占位/统计版（模型调用失败、凭据缺失等）

# daily_report.py 的退出码。注意这是 daily_report.py 自己的码空间
# （0 成功 / 1 无数据 / 2 推送失败 / 3 降级），与 push_to_feishu.py 的
# 0/2/3/4 无关，两套不要互相比对。
EXIT_DEGRADED = 3

_PREFIX = "<!-- daily-report:status="
_SUFFIX = "-->"

# 标记只在首行；读取时最多看这么多字符，不必为一条标记读整份报告。
READ_LIMIT = 512


def marker(status, reason=""):
    """构造写在日报首行的标记（不含换行）。"""
    reason = str(reason or "").strip().replace(" ", "_")
    tail = f" reason={reason}" if reason else ""
    return f"{_PREFIX}{status}{tail} {_SUFFIX}"


def read_status(text):
    """从日报（或其开头片段）读出 (status, reason)；读不到返回 None。"""
    first = (text or "")[:READ_LIMIT].split("\n", 1)[0].strip()
    if not first.startswith(_PREFIX) or not first.endswith(_SUFFIX):
        return None
    parts = first[len(_PREFIX):-len(_SUFFIX)].split()
    if not parts:
        return None
    reason = ""
    for token in parts[1:]:
        if token.startswith("reason="):
            reason = token.split("=", 1)[1]
            break
    return parts[0], reason


def is_degraded(text):
    """这份日报是否**未确认成功**（含读不出标记的旧文件、空串）。"""
    result = read_status(text)
    return result is None or result[0] != OK


def read_head(path, limit=READ_LIMIT):
    """读日报开头片段；不存在或读不了返回 ""（= 无标记 = 未确认成功）。"""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read(limit)
    except OSError:
        return ""


def body(text):
    """去掉首行标记后的正文（推送/渲染用；标记是给机器读的）。"""
    if read_status(text) is None:
        return text
    return (text or "").split("\n", 1)[-1].lstrip("\n")
