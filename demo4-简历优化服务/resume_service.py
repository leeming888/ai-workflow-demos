#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
resume_service.py · AI 简历优化服务（MiniMax M3 单模型版）

实测结论（2026-10-30）：
  · 走 Anthropic 端点 https://api.minimaxi.com/anthropic/v1/messages → 无 <think> 污染
  · 走 OpenAI 端点 https://api.minimaxi.com/v1/chat/completions → <think> 占 90%+ token，必须剥离
  · 三步全流程：1.9 分钟，API 成本 0.05 元，产出 8500 字

用法：
  python resume_service.py --input 简历.md --output 输出目录
  python resume_service.py --input 简历.md --jd "岗位JD" --output 输出目录

产出：
  输出目录/成品1_简历终稿.md
  输出目录/成品2_自检意见.md
  输出目录/成品3_面试追问题清单.md
  输出目录/运行报告.md   （耗时 / tokens / 成本）
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────────
# API 凭据优先级：命令行 > 环境变量 MINIMAX_API_KEY > ANTHROPIC_AUTH_TOKEN
ANTHROPIC_BASE = "https://api.minimaxi.com/anthropic/v1/messages"
OPENAI_BASE = "https://api.minimaxi.com/v1/chat/completions"
MODEL = "MiniMax-M3"

# 价格（每百万 token，MiniMax 官方 2026-06 起）
PRICE_IN = 0.30
PRICE_OUT = 1.20


def strip_think(s: str) -> str:
    """剥离 <think> 块（OpenAI 端点必须用；Anthropic 端点一般不需要）"""
    s = re.sub(r"<think>.*?</think>", "", s, flags=re.S | re.I)
    s = re.sub(r"<think>.*$", "", s, flags=re.S | re.I)
    s = re.sub(r"</?think>", "", s, flags=re.I)
    return s.strip()


class M3:
    def __init__(self, key: str, endpoint: str = "anthropic"):
        self.key = key
        self.endpoint = endpoint
        self.total_in = 0
        self.total_out = 0
        self.total_time = 0.0

    def call(self, prompt: str, max_tokens: int = 6000) -> str:
        if self.endpoint == "anthropic":
            body = {
                "model": MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "Content-Type": "application/json",
                "x-api-key": self.key,
                "anthropic-version": "2023-06-01",
            }
            url = ANTHROPIC_BASE
        else:
            body = {
                "model": MODEL,
                "max_tokens": max_tokens,
                "messages": [{"role": "user", "content": prompt}],
            }
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            }
            url = OPENAI_BASE

        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers
        )
        t0 = time.time()
        with urllib.request.urlopen(req, timeout=900) as r:
            d = json.loads(r.read().decode("utf-8"))
        dt = time.time() - t0

        if self.endpoint == "anthropic":
            raw = "".join(
                b.get("text", "") for b in d.get("content", []) if b.get("type") == "text"
            )
            u = d.get("usage", {})
            self.total_in += u.get("input_tokens", 0)
            self.total_out += u.get("output_tokens", 0)
        else:
            raw = d["choices"][0]["message"].get("content", "") or ""
            u = d.get("usage", {})
            self.total_in += u.get("prompt_tokens", 0)
            self.total_out += u.get("completion_tokens", 0)

        self.total_time += dt
        clean = strip_think(raw)
        print(
            f"    {dt:.1f}s  in={u.get('input_tokens', u.get('prompt_tokens', 0))} "
            f"out={u.get('output_tokens', u.get('completion_tokens', 0))} "
            f"raw={len(raw)} clean={len(clean)}",
            file=sys.stderr,
        )
        return clean


# ── 三步提示词 ──────────────────────────────────────────────────────────
P_STEP1 = """你是资深简历顾问。把下面这份中国求职者的原始简历，重写为专业、ATS友好、量化导向的简历。

硬性要求：
1. 每条经历用「动词开头 + 量化结果 + 业务影响」结构
2. 原文没有的数据，用【待补充：XXX】占位，绝不编造数字
3. 删除空泛自我评价，改为具体能力证据
4. 保留真实信息：姓名、年龄、城市、联系方式、教育、工作时间线
5. 只输出 Markdown 正文，不要任何解释、前言、结语
{target}
原始简历：
{resume}
"""

P_STEP2 = """你是挑剔的 HR 招聘负责人。挑刺下面这份简历，逐条指出问题和改法。

输出格式（严格遵守）：
## 问题 N：<一句话概括问题>
- 位置：<哪一句>
- 现状：<原文引用>
- 改成：<具体怎么改>

先给一段「总体判断」（3-5 句），再列问题。至少列 8 个具体问题。

简历：
{resume}
"""

P_STEP3 = """基于下面的简历和自检意见，生成「面试官最可能追问的问题清单」。

按 5 个维度分组，每组 3-4 个问题：
1. 数据真实性追问
2. 业务决策追问
3. 能力深度追问
4. 动机与稳定性追问
5. 离职原因与空窗期追问

每个问题要具体到能让他答不上来就露馅的程度（如要求给出具体账号/链接/时间/数字）。

简历：
{resume}

自检意见：
{review}
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="原始简历 md 文件")
    ap.add_argument("--output", required=True, help="输出目录")
    ap.add_argument("--jd", default="", help="目标岗位 JD（可选）")
    ap.add_argument("--key", default="", help="API Key（默认读环境变量）")
    ap.add_argument(
        "--endpoint",
        default="anthropic",
        choices=["anthropic", "openai"],
        help="anthropic=无 think 污染（推荐）；openai=需剥离",
    )
    args = ap.parse_args()

    key = args.key or os.environ.get("MINIMAX_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not key:
        print("错误：未找到 API Key。请设置环境变量 MINIMAX_API_KEY 或 ANTHROPIC_AUTH_TOKEN，"
              "或用 --key 传入。", file=sys.stderr)
        sys.exit(2)

    src = Path(args.input)
    if not src.exists():
        print(f"错误：输入文件不存在 {src}", file=sys.stderr)
        sys.exit(2)

    outdir = Path(args.output)
    outdir.mkdir(parents=True, exist_ok=True)

    resume = src.read_text(encoding="utf-8")
    target = f"\n目标岗位 JD（优先对齐）：\n{args.jd}\n" if args.jd else ""

    m = M3(key, args.endpoint)

    print("[1/3] 出稿...", file=sys.stderr)
    o1 = m.call(P_STEP1.format(resume=resume, target=target))
    (outdir / "成品1_简历终稿.md").write_text(o1, encoding="utf-8")

    print("[2/3] 自检...", file=sys.stderr)
    o2 = m.call(P_STEP2.format(resume=o1))
    (outdir / "成品2_自检意见.md").write_text(o2, encoding="utf-8")

    print("[3/3] 追问清单...", file=sys.stderr)
    o3 = m.call(P_STEP3.format(resume=o1, review=o2))
    (outdir / "成品3_面试追问题清单.md").write_text(o3, encoding="utf-8")

    cost = m.total_in / 1e6 * PRICE_IN + m.total_out / 1e6 * PRICE_OUT
    report = f"""# 运行报告

| 项 | 值 |
|---|---|
| 模型 | {MODEL} |
| 端点 | {args.endpoint} |
| 总耗时 | {m.total_time:.1f}s（{m.total_time/60:.1f} 分钟） |
| input tokens | {m.total_in} |
| output tokens | {m.total_out} |
| API 成本 | ${cost:.6f} ≈ {cost*7.2:.4f} 元 |
| 产出字数 | {len(o1)} + {len(o2)} + {len(o3)} = {len(o1)+len(o2)+len(o3)} |

## 人工必须做的（AI 替代不了）

1. **填掉所有【待补充：XXX】** —— 填不上就删掉整条，不要留占位符
2. **逐条核对事实** —— AI 会把"参与"写成"负责"，必须改回真实程度
3. **检查时间线** —— 有空窗期/频繁跳动，面试官第一问就是这个
4. **替换脱敏信息** —— 姓名/电话/邮箱/学校要真实
"""
    (outdir / "运行报告.md").write_text(report, encoding="utf-8")

    print(f"\n完成 → {outdir}", file=sys.stderr)
    print(f"耗时 {m.total_time:.1f}s，成本 ${cost:.6f} ≈ {cost*7.2:.4f} 元", file=sys.stderr)


if __name__ == "__main__":
    main()
