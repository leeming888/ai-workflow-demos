#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""敏感信息扫描 —— 上传资料库前必须过这一关。

为什么需要：多个自动化 prompt 里都写了「不入金：API Key / 账号 / 凭证」，
但那是靠模型自觉。漏一次就是永久留在资料库里，撤不干净。
这是纯正则脚本，比在 8 个 prompt 里写十遍「不许写」可靠。

用法：
  python scan_sensitive.py <file.md>          # 扫描单个文件
  python scan_sensitive.py --dir <目录>        # 扫描整个目录
  python scan_sensitive.py --strict <file>     # 发现敏感就 exit 1（给自动化用）

退出码：
  0 = 干净
  1 = 发现敏感内容
  2 = 文件不存在 / 参数错
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# 高置信度模式：命中就直接报，不看白名单（宁可误报，漏报代价是永久泄露）
# 理由：这些格式足够特异，误报代价只是让人扫一眼
HIGH_CONFIDENCE = [
    (r"sk-ant-[A-Za-z0-9\-_]{20,}", "Anthropic API Key"),
    (r"sk-[A-Za-z0-9]{20,}", "OpenAI 风格 API Key"),
    (r"ghp_[A-Za-z0-9]{36}", "GitHub Personal Access Token"),
    (r"gho_[A-Za-z0-9]{36}", "GitHub OAuth Token"),
    (r"github_pat_[A-Za-z0-9_]{50,}", "GitHub Fine-grained Token"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key ID"),
    (r"AIza[0-9A-Za-z\-_]{35}", "Google API Key"),
    (r"xox[baprs]-[0-9A-Za-z\-]{10,}", "Slack Token"),
    (r"eyJ[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}\.[A-Za-z0-9\-_]{10,}", "JWT"),
    (r"(?<!\d)[1-9]\d{16}[0-9Xx](?!\d)", "疑似身份证号"),
    (r"(?i)ima[-_]?(api[_-]?key|token|client[_-]?id)\s*[:=]\s*[A-Za-z0-9\-_]{12,}", "ima 凭证"),
]

# 中国大陆身份证校验位（ISO 7064:1983 MOD 11-2）
# 用途：18 位数字串要成为「身份证号」，末位校验位必须对得上。
# 不加这层校验时，微信文章 ID / 订单号 / 视频 ID 等 18 位纯数字 URL 会被误报
# （2026-10-28 实测：https://www.tahou.com/article/211769778362680325 被判「疑似身份证号」，
#  而该串校验位算出来是 2 不是 5 → 根本不是身份证号）。
# 加了校验层之后：真身份证（校验位必然正确）照拦；纯数字 URL ID 放行。
_ID_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_ID_CHECK_CHARS = "10X98765432"


def is_valid_cn_id(s: str) -> bool:
    """校验 18 位中国身份证：长度 + 前 17 位数字 + MOD 11-2 校验位。"""
    if len(s) != 18 or not s[:17].isdigit():
        return False
    total = sum(int(s[i]) * _ID_WEIGHTS[i] for i in range(17))
    return _ID_CHECK_CHARS[total % 11] == s[17].upper()


# 低置信度模式：白名单放过（含"你的/示例/待填"等词就跳过）
PATTERNS: list[tuple[str, str]] = [
    (r"(?i)(api[_-]?key|apikey|secret[_-]?key|access[_-]?token|client[_-]?secret)"
     r"\s*[:=]\s*[\"']?[A-Za-z0-9\-_]{16,}[\"']?", "密钥赋值语句"),
    (r"(?i)(password|passwd|pwd)\s*[:=]\s*[\"']?[^\s\"']{6,}[\"']?", "密码赋值"),
    (r"(?i)bearer\s+[A-Za-z0-9\-_\.]{20,}", "Bearer Token"),
    (r"(?<!\d)1[3-9]\d{9}(?!\d)", "疑似手机号"),
    (r"\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b", "邮箱地址"),
]

# 允许出现的场景（会命中的行直接跳过，避免误伤）
ALLOW_PATTERNS = [
    r"(?i)client[_-]?id\s*[:=]\s*[\"']?$",          # 空值
    r"(?i)<[^>]*(key|token|secret|密码)[^>]*>",     # 占位符
    r"(?i)(你的|示例|example|placeholder|xxx+|\*\*\*+|待填|已废弃|作废)",
    r"^\s*[:\-\|>#\s]*$",
    r"(?i)nodeId|spaceId|folderId|discussionId|clientTempToken|appId|unionId|openId",
]

# 邮箱里的常见公共域不算敏感（但仍列出，供参考）
EMAIL_DOMAINS = {
    "example.com", "example.org", "test.com", "localhost",
    "github.com", "qq.com", "tencent.com", "anthropic.com",
    "openai.com", "google.com", "microsoft.com",
}


def _mask(frag: str) -> str:
    return frag[:6] + "..." + frag[-4:] if len(frag) > 14 else frag[:3] + "..."


def scan_text(text: str) -> list[tuple[str, str, int]]:
    """返回 [(说明, 匹配片段, 行号)]"""
    hits: list[tuple[str, str, int]] = []
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
        seen_desc = set()

        # 第一轮：高置信度，不受白名单影响
        for pat, desc in HIGH_CONFIDENCE:
            m = re.search(pat, line)
            if not m or desc in seen_desc:
                continue
            # 身份证号：额外过一遍 MOD 11-2 校验位，挡掉 18 位纯数字 URL ID 的误报
            if desc == "疑似身份证号" and not is_valid_cn_id(m.group(0)):
                continue
            hits.append((desc, _mask(m.group(0)), i))
            seen_desc.add(desc)

        # 第二轮：低置信度，整行是占位/示例才跳过
        if any(re.search(a, line) for a in ALLOW_PATTERNS):
            continue
        for pat, desc in PATTERNS:
            m = re.search(pat, line)
            if not m or desc in seen_desc:
                continue
            frag = m.group(0)
            if "邮箱地址" in desc:
                dom = frag.rsplit("@", 1)[-1].lower()
                if dom in EMAIL_DOMAINS:
                    continue
            hits.append((desc, _mask(frag), i))
            seen_desc.add(desc)

    return hits


def scan_file(path: Path) -> list[tuple[str, str, int]]:
    return scan_text(path.read_text(encoding="utf-8", errors="ignore"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", nargs="?", help="要扫描的文件")
    ap.add_argument("--dir", help="扫描整个目录下的 .md 文件")
    ap.add_argument("--strict", action="store_true", help="发现敏感则 exit 1")
    args = ap.parse_args()

    files: list[Path] = []
    if args.dir:
        files = sorted(Path(args.dir).rglob("*.md"))
    elif args.path:
        p = Path(args.path)
        if not p.exists():
            print(f"文件不存在: {p}", file=sys.stderr)
            return 2
        files = [p]
    else:
        ap.print_help()
        return 2

    total_hits = 0
    report: list[str] = []

    for f in files:
        hits = scan_file(f)
        if not hits:
            continue
        total_hits += len(hits)
        report.append(f"\n【{f}】")
        for desc, frag, line_no in hits:
            report.append(f"  第 {line_no} 行 · {desc} · {frag}")

    if total_hits == 0:
        print(f"✅ 扫描 {len(files)} 个文件，未发现敏感信息")
        return 0

    print(f"❌ 发现 {total_hits} 处疑似敏感信息（已打码）：")
    print("\n".join(report))
    print("\n处理建议：")
    print("  1. 把真实值替换为 <占位符> 后再上传")
    print("  2. 敏感内容只留本地 md，不要进资料库")
    print("  3. 如果值已经泄露过，去对应平台吊销重发")

    return 1 if args.strict else 0


if __name__ == "__main__":
    sys.exit(main())
