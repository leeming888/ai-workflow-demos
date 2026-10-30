#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地工作流同步到 WorkBuddy 资料库。

同步策略（不覆盖、不丢历史）：
  1. 读取资料库工作流文件夹下的节点清单，建立"标题 -> nodeId"映射。
  2. 对每个本地 md：
       - 资料库无同名节点 -> 直接 create_doc 新建。
       - 资料库已有同名节点 -> 先 move-node 到 "_历史版本" 文件夹并改名加版本后缀，
         再 create_doc 新建标准名。历史可回滚，标准名永远指向最新版。
  3. 输出同步报告 JSON，供自动化读取。

用法:
  python sync_to_library.py                 # 同步全部
  python sync_to_library.py --only 99 04a   # 只同步指定前缀
  python sync_to_library.py --dry-run       # 只报告不执行
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ---------- 常量 ----------
# 优先读同目录 config.py（用户自己的配置，不会进版本库）
# 没有就退回 config.example.py 的默认值，方便先跑通看效果
_CONFIG_FILE = Path(__file__).parent / "config.py"
_EXAMPLE_FILE = Path(__file__).parent / "config.example.py"

_cfg = {}
for _f in (_CONFIG_FILE, _EXAMPLE_FILE):
    if _f.exists():
        _exec(compile(_f.read_text(encoding="utf-8"), str(_f), "exec"), _cfg)
        break

PYTHON = _cfg.get("PYTHON", r"C:\Users\yourname\.workbuddy\binaries\python\versions\3.13.12\python.exe")
SKILL = Path(_cfg.get("SKILL", r"D:\workbuddy\resources\app.asar.unpacked\resources\plugins\workbuddy-builtin\skills\library"))
SPACE_API = SKILL / "space_api.py"
CREATE_DOC = SKILL / "doc" / "create_doc.py"
SCAN = Path(__file__).parent / "scan_sensitive.py"

SPACE_ID = _cfg.get("SPACE_ID", "your-space-id")
ROOT_ID = _cfg.get("ROOT_ID", "your-root-folder-id")            # 主目录
ARCHIVE_ID = _cfg.get("ARCHIVE_ID", "your-archive-folder-id")  # 历史版本目录
HISTORY_ID = _cfg.get("HISTORY_ID", "your-history-folder-id")  # 二级历史

LOCAL_DIR = Path(_cfg.get("LOCAL_ROOT", r"D:\your-workspace\docs"))
REPORT_PATH = Path(_cfg.get("LOCAL_ARCHIVE", str(LOCAL_DIR))) / "_sync_report.json"

# 本地文件名 -> 资料库标题（资料库里用空格替代下划线）
SKIP_FILES = {"00_工作流总览.md"}


def run(cmd: list[str]) -> tuple[int, str]:
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def title_of(stem: str) -> str:
    """00_工作流总览 -> 00 工作流总览；98_能力体检报告_20261016 -> 98 能力体检报告 20261016"""
    t = re.sub(r"_(\d{8})$", r" \1", stem)   # 尾部日期后缀用空格
    return t.replace("_", " ", 1)            # 首个下划线用空格


def list_nodes(parent_id: str) -> list[dict]:
    code, out = run([
        PYTHON, str(SPACE_API), "space.workspace.list-nodes",
        "--space-id", SPACE_ID, "--parent-node-id", parent_id, "--limit", "200",
    ])
    if code != 0 or not out.strip():
        print(f"[FAIL] list-nodes {parent_id}: {out[:400]}")
        return []
    try:
        return json.loads(out.strip())["data"]["nodes"]
    except Exception as e:
        print(f"[FAIL] parse list-nodes: {e} :: {out[:400]}")
        return []


def scan(md: Path) -> tuple[bool, str]:
    """上传前敏感信息闸门。返回 (是否通过, 说明)。"""
    if not SCAN.exists():
        return True, "(扫描器不存在，跳过)"
    code, out = run([PYTHON, str(SCAN), str(md), "--strict"])
    if code == 0:
        return True, ""
    return False, out.strip().replace("\n", " ")[:300]


def upload(md: Path, title: str, parent_id: str) -> tuple[bool, str]:
    ok, msg = scan(md)
    if not ok:
        return False, f"🚫 敏感信息拦截，未上传：{msg}"
    code, out = run([
        PYTHON, str(CREATE_DOC),
        "--title", title,
        "--content-file", str(md),
        "--space-id", SPACE_ID,
        "--parent-id", parent_id,
    ])
    ok = "KS_DOC_CREATE" in out or "KS_USER_REPLY" in out
    nid = ""
    m = re.search(r"KS_DOC_CREATE\s+(\S+)", out)
    if m:
        nid = m.group(1)
    return ok, (nid or out[:300])


def archive_existing(node_id: str, base_title: str) -> tuple[bool, str]:
    """把旧节点移进历史文件夹并改名加版本后缀。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    new_title = f"{base_title}（旧版 {stamp}）"
    c1, o1 = run([PYTHON, str(SPACE_API), "space.workspace.move-node",
                  "--node-id", node_id, "--target-parent-id", HISTORY_ID])
    c2, o2 = run([PYTHON, str(SPACE_API), "space.workspace.rename-node",
                  "--node-id", node_id, "--title", new_title])
    return (c1 == 0 and c2 == 0), (o1[:150] + o2[:150])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", nargs="*", default=None,
                    help="只同步文件名前缀，如 99 04a")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-overview", action="store_true",
                    help="同时同步 00 总览")
    args = ap.parse_args()

    mds = sorted(LOCAL_DIR.glob("*.md"))
    targets = []
    for md in mds:
        stem = md.stem
        if stem in SKIP_FILES and not args.include_overview:
            continue
        if args.only and not any(stem.startswith(o) for o in args.only):
            continue
        targets.append(md)

    print(f"[INFO] 待同步 {len(targets)} 个文件")
    # 所有资料库节点（含"【已废弃】""（旧版 ...）"标记）都视为可归档的旧版
    existing = {n["title"].strip(): n for n in list_nodes(ROOT_ID)}
    stale = []
    for t, n in list(existing.items()):
        if t.startswith("【已废弃】") or "（旧版 " in t:
            stale.append(n)
    if stale:
        print(f"[INFO] 发现 {len(stale)} 个遗留旧版节点，将一并归档")
        if not args.dry_run:
            for n in stale:
                base = re.sub(r"^【已废弃】", "", n["title"]).strip()
                base = re.sub(r"（旧版[^）]*）\s*$", "", base).strip()
                ok_s, note_s = archive_existing(n["id"], base)
                print(f"  [{'OK ' if ok_s else 'ERR'}] 归档遗留: {n['title'][:40]} {note_s[:60]}")

    results = []
    for md in targets:
        title = title_of(md.stem)
        rec = {"file": md.name, "title": title, "action": "", "ok": False, "node": "", "note": ""}
        hit = existing.get(title)
        if hit:
            rec["action"] = "archive+recreate"
            if args.dry_run:
                rec["ok"] = True
                rec["note"] = f"将归档旧节点 {hit['id']}"
            else:
                ok_a, note_a = archive_existing(hit["id"], title)
                if not ok_a:
                    rec["note"] = f"归档失败: {note_a}"
                else:
                    ok_u, nid = upload(md, title, ROOT_ID)
                    rec["ok"] = ok_u
                    rec["node"] = nid if ok_u else ""
                    rec["note"] = note_a if ok_u else f"上传失败: {nid}"
        else:
            rec["action"] = "create"
            if args.dry_run:
                rec["ok"] = True
                rec["note"] = "资料库无同名节点，将新建"
            else:
                ok_u, nid = upload(md, title, ROOT_ID)
                rec["ok"] = ok_u
                rec["node"] = nid if ok_u else ""
                rec["note"] = "" if ok_u else f"上传失败: {nid}"
        results.append(rec)
        print(f"[{'OK ' if rec['ok'] else 'ERR'}] {rec['action']:16s} {rec['title']}"
              + (f"  -> {rec['node']}" if rec["node"] else "")
              + (f"  {rec['note'][:80]}" if rec["note"] else ""))

    report = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "dry_run": args.dry_run,
        "total": len(results),
        "ok": sum(1 for r in results if r["ok"]),
        "results": results,
    }
    if not args.dry_run:
        REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    print(f"\n[SUMMARY] {report['ok']}/{report['total']} 成功")
    return 0 if report["ok"] == report["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
