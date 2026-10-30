#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自动化巡检 —— 检查 9 个自动化最近是否真的产出了东西。

背景：weekly_review.py 曾静默失败 2 周而没人发现，因为失败在一个没人看的地方。
这个脚本把「产出新鲜度」变成可检查的硬指标。

用法：
  python check_automations.py            # 输出到终端 + 写 dashboard.md
  python check_automations.py --quiet    # 只写文件不打印
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

# ── 配置 ────────────────────────────────────────────────────────────────
# 优先读同目录 config.py（用户自己的配置，不进版本库）
_CONFIG_FILE = Path(__file__).parent / "config.py"
_EXAMPLE_FILE = Path(__file__).parent / "config.example.py"
_cfg = {}
for _f in (_CONFIG_FILE, _EXAMPLE_FILE):
    if _f.exists():
        _exec(compile(_f.read_text(encoding="utf-8"), str(_f), "exec"), _cfg)
        break

WORKSPACE = Path(_cfg.get("WORKSPACE", r"D:\your-workspace"))
REPORTS = WORKSPACE / "reports"
STATUS_DIR = WORKSPACE / "reports" / "status"
DASHBOARD = STATUS_DIR / "dashboard.md"
STATUS_JSON = STATUS_DIR / "automation_status.json"

PYTHON = _cfg.get("PYTHON", r"C:\Users\yourname\.workbuddy\binaries\python\versions\3.13.12\python.exe")
SPACE_API = Path(_cfg.get("SKILL", r"D:\workbuddy\resources\app.asar.unpacked\resources\plugins\workbuddy-builtin\skills\library")) / "space_api.py"
LIB_SPACE_ID = _cfg.get("SPACE_ID", "your-space-id")
ROOT_ID = _cfg.get("ROOT_ID", "your-root-folder-id")
ARCHIVE_ID = _cfg.get("ARCHIVE_ID", "your-archive-folder-id")

# 额外要监控的工作区（产出不在主工作区时，在这里补上 —— 这正是「覆盖缺口」的来源）
EXTRA_WORKSPACES = _cfg.get("EXTRA_WORKSPACES", [])

# 2026-10-21 新增：接单工作流在**另一个工作区**，
# 产出完全不在本工作区里。此前云端已多出这 2 个 ACTIVE 自动化，但面板完全没跟，
# 属于 10-17 修过的同一类「覆盖缺口」——漏监控比误报更危险，因为它永远显示一切正常。
SIDE_WS = Path(EXTRA_WORKSPACES[0]) if len(EXTRA_WORKSPACES) > 0 else Path(r"D:\your-workspace-2")

# 2026-10-30 新增：付费产品自动化，
# 产出既不在本工作区、也不在接单工作区，而在**第三个工作区**下 —— 且目录在首跑前根本不存在。
# 这是 10-17 / 10-21 两次修过的「覆盖缺口」第三次复发：
# 云端多了一个 ACTIVE，脚本没跟 → 它出问题时巡检永远看不见。
PAY_WS = Path(EXTRA_WORKSPACES[1]) if len(EXTRA_WORKSPACES) > 1 else Path(r"D:\your-workspace-3")

# 约定周期（小时）。超过 = 该次周期没跑
EXPECTED_HOURS = {
    "AI 视频资料库每日更新": 30,
    "每日 AI 新闻推送": 30,
    "每日一个为什么": 30,
    "资料库周度健康度监控": 24 * 9,
    "资料库周度内容灌入": 24 * 9,
    "资料库周报 - 周日晚 22:30": 24 * 9,
    "AI 工具方法论资料库周更": 24 * 9,
    "工作流守护 - 每月校验": 24 * 35,
    # 2026-10-17 修正：schtasks 实测 AIWeeklyReview 是「每周 SUN 22:00」，
    # 不是每天。原按 24h 判会永远显示 ✅，掩盖真漏跑。
    "AIWeeklyReview (Windows)": 24 * 9,
    # 2026-10-17 新增：此前漏监控，云端 10 个 ACTIVE 但面板只跟 9 个。
    "工作流自动追加（每日 21:00）": 30,
    # 2026-10-21 新增：接单工作流的 2 个每日自动化（9:00 出帖 / 21:00 复盘）。
    "每日接单贴自动生成": 30,
    "每日接单复盘自动生成": 30,
    # 2026-10-30 新增：明哥 10-30 04:05 新建的「AI赛道情报周报」（每周一 09:30）。
    # 漏跟踪 = 出问题时巡检永远看不见（比误报危险）。
    "AI赛道情报周报 · 每周一生成": 24 * 9,
}

# 2026-10-22 新增：已知调度但还没首跑的自动化，写真实的首跑时间点。
# 「工作流守护 - 每月校验」= 每月 1 号 10:00 → 首跑 2026-11-01 10:00。
# 用来替代"约 840h 后"这种会误导人的粗算（见 main() 里 note 的生成逻辑）。
FIRST_RUN = {
    "工作流守护 - 每月校验": "2026-11-01 10:00",
    # 2026-10-30 新增：明哥 10-30 04:05 新建的付费产品周报，
    # 每周一 09:30 → 首跑 2026-11-03 09:30。
    "AI赛道情报周报 · 每周一生成": "2026-11-03 09:30",
}

# 2026-10-23 新增：每日型自动化的**实际调度时刻**（小时）。
# 判定「漏跑日」时必须按调度时刻判断机器是否开着，而不是按「当天开过机没有」——
# 机器 10-20 是 14:25 关机，当天 9:00 的活照跑，但 18:30 / 21:00 的活不可能跑。
# 只看「当天开过机」会把关机后的时段误判成漏跑。
# 调度抖动按日志最大 ~1.5h 算，取 3h 宽限窗口。
SCHEDULE_HOUR = {
    "AI 视频资料库每日更新": 9,
    "每日 AI 新闻推送": 9,
    "每日一个为什么": 18,
    "工作流自动追加（每日 21:00）": 21,
    "每日接单贴自动生成": 9,
    "每日接单复盘自动生成": 21,
}

# 各自动化产出应该出现在哪
PRODUCERS = {
    # 2026-10-19 修正：原注释说「直接改资料库工作流，无本地文件」是错的——
    # 「AI 视频资料库每日更新」实际有本地缓存 `community_research/YYYY-MM-DD/已调研-社区_*.md`，
    # 原配置不认领导致每次巡检都 ⚠️ 误报（10-17/18/19 连续三天都误报）。
    # 修法：跟 PROJucer → `community_research/`，按文件名模式只认自己的 md。
    "AI 视频资料库每日更新": WORKSPACE / "project_distill" / "community_research",
    "每日 AI 新闻推送": REPORTS / "ai_news",
    "每日一个为什么": REPORTS / "why",
    "资料库周度健康度监控": REPORTS / "health",
    "资料库周度内容灌入": WORKSPACE / "project_distill" / "cards",
    "资料库周报 - 周日晚 22:30": REPORTS / "weekly",
    # 2026-10-21 21:30 修正：原为 None → 面板永远显示「⏳ 待首跑」，
    # 即使它今天（周三）21:00 真的跑了、真的写了 2 篇 ［已调研］ 卡片。
    # 这类「明明跑了却显示没跑」= 假阴性，同样是静默失败（会让人以为还没到时候就不管）。
    "AI 工具方法论资料库周更": WORKSPACE / "project_distill" / "cards",
    "工作流守护 - 每月校验": None,
    "AIWeeklyReview (Windows)": REPORTS / "weekly",
    "工作流自动追加（每日 21:00）": WORKSPACE / "project_distill" / "reports" / "auto_update",
    # 2026-10-21 新增：接单工作流产出（另一工作区）
    "每日接单贴自动生成": SIDE_WS / "data" / "posts",
    "每日接单复盘自动生成": SIDE_WS / "data",
    # 2026-10-30 新增：付费产品周报自动化，产出在**第三个工作区**的
    # 「赚钱执行包_v3.1/周报/」下。
    "AI赛道情报周报 · 每周一生成": PAY_WS / "赚钱执行包_v3.1" / "周报",
}

# 2026-10-17 修正的关键缺陷：
# 「资料库周报」和「AIWeeklyReview」都写 reports/weekly/，共用一个目录。
# 原实现用 latest_mtime(整目录) → 一个自动化的产出会让另一个「借到」✅，
# 真正漏跑的那个永远看不出来。现在按文件名模式分别认领各自产出。
    # 2026-10-19 增补：「AI 视频资料库每日更新」文件名有 `已调研-社区_` 前缀
    # （10-19 起部分产出还套了 `[...]` 方括号，但 glob 不区分），按此模式只认自己。
    # 2026-10-24 修正：原 glob `*已调研-社区*.md` 只认 10-18 起的产出，
    # **漏掉 10-17 的 `社区调研_*.md`（2 篇）**。后果不是「少认一个文件」——
    # 而是把该自动化的「首次产出日」算成 10-18，10-17 被 floor 启发式
    # （早于首次产出不算漏跑）**永久豁免**，漏跑检测对它整体失效。
    # 改为 `*调研*.md` 后 16 个文件全覆盖（10-17~10-24 每天 2 篇）。
PRODUCER_GLOBS = {
    "AIWeeklyReview (Windows)": "weekly_review_*.md",
    "资料库周报 - 周日晚 22:30": "library_weekly_*.md",
    "AI 视频资料库每日更新": "*调研*.md",
    # 2026-10-21 新增：接单复盘认领 data/ 整个目录，而 data/posts/ 是出帖自动化的产出，
    # 不加 glob 的话出帖跑一次就能让复盘「借到」✅，真漏跑的看不出来（同目录互借的老问题）。
    "每日接单复盘自动生成": "daily_report_*.md",
    "每日接单贴自动生成": "*.md",
    # 2026-10-21 21:30 修正：**cards/ 目录两个自动化共用**。
    # 「资料库周度内容灌入」写 card_YYYY-MM-DD_主题.md；
    # 「AI 工具方法论资料库周更」写 ［已调研］主题 YYYY-MM-DD.md。
    # 原先两边都不加 glob → 认领整个 cards/ → 谁先跑另一个都「借到」✅。
    # 实测后果：周更今天 21:02 真的跑了（2 篇 ［已调研］ 卡片），
    # 但面板给它显示「⏳ 待首跑」，同时「周度灌入」却因为借到周更的产出显示 ✅——
    # 一个假阴性 + 一个假阳性，正好互相掩盖。
    "资料库周度内容灌入": "card_*.md",
    "AI 工具方法论资料库周更": "*已调研*.md",
}


def read_cloud_automation_status() -> dict[str, dict]:
    """读云端自动化状态（ACTIVE / PAUSED），返回 {name: {...}}。

    2026-10-27 新增：此前脚本用**硬编码名单**跟踪自动化，完全不知道
    「明哥手动暂停了某个自动化」这件事。结果：
    「每日接单贴自动生成」「每日接单复盘自动生成」在 10-26 23:00:50/54
    被人为暂停 → 调度器根本不再 dispatch 它们 → 产出自然停在 10-26，
    面板把它们判成「⚠️ 偏旧 / 漏跑」。

    这正是本任务要消灭的**假信号**：越权（明哥主动决定不做）和故障
    （应该跑却没跑）被混为一谈，会逼着巡检去「补救」一个明哥主动关掉的东西。
    而真正该报的情况——「明哥以为它在跑、其实被暂停了」——反而看不见。

    状态来源：`C:\\Users\\lee\\.workbuddy\\workbuddy.db` 的 `automations` 表。
    读不到时返回空 dict（**不猜、不阻断**），由调用方决定降级行为。
    """
    db = Path.home() / ".workbuddy" / "workbuddy.db"
    if not db.exists():
        return {}
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute("SELECT name,status,rrule,updated_at,next_run_at FROM automations "
                    "WHERE deleted_at IS NULL")
        out: dict[str, dict] = {}
        for name, status, rrule, updated_at, next_run_at in cur.fetchall():
            def _ts(v):
                if not v:
                    return None
                try:
                    return datetime.fromtimestamp(int(v) / 1000).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    return None
            out[name] = {
                "status": status,
                "rrule": rrule,
                "updated_at": _ts(updated_at),
                "next_run_at": _ts(next_run_at),
            }
        con.close()
        return out
    except Exception:
        return {}


class LibraryReadError(RuntimeError):
    """资料库读取失败（编码/网络/API）。

    2026-10-21 21:30 新增：资料库节点读取一旦失败，**绝不能**当成「0 个节点」——
    那是本脚本最容易骗过人的静默失败：面板会显示文档数暴跌，但自动化状态全绿。
    必须显式抛错，让异常清单标出来。
    """


def latest_mtime_matched(folder: Path | None, pattern: str | None) -> datetime | None:
    """只认领属于该自动化的文件，避免共用目录时互相借产出。"""
    if folder is None or not folder.exists():
        return None
    if pattern:
        files = [f for f in folder.rglob(pattern) if f.is_file()]
    else:
        files = [f for f in folder.rglob("*") if f.is_file()]
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files))


def all_mtimes_matched(folder: Path | None, pattern: str | None) -> list[datetime]:
    """该自动化**所有**产出文件的时间（倒序）。

    2026-10-23 新增：单看「最近一次产出」有个结构性盲区——
    只要后来某天跑成功一次，之前漏掉的日子就被完全掩盖。
    10-19 的「每日 AI 新闻推送」就是这样：当天 run failed
    ([CANCELLED] unknown_interrupted)，之后 10-20 补跑成功，
    面板从 10-20 起就一直显示 ✅，**整整 4 天没人知道 10-19 缺档**。
    面板要能发现「某个该跑的日子没跑」，就必须拿到全部产出时间，
    而不只是最新的那一个。
    """
    if folder is None or not folder.exists():
        return []
    if pattern:
        files = [f for f in folder.rglob(pattern) if f.is_file()]
    else:
        files = [f for f in folder.rglob("*") if f.is_file()]
    return sorted((datetime.fromtimestamp(f.stat().st_mtime) for f in files),
                  reverse=True)


AUTOMATION_LOG = Path(r"C:\Users\lee\.workbuddy\logs\automation.log")


def check_scheduler_failures(lookback_days: int = 5) -> list[dict]:
    """从调度器日志里捞「run failed」记录。

    2026-10-23 新增：本面板之前**完全没用过调度器日志**，
    而那才是判断「自动化到底跑没跑、跑成功没有」的第一手证据。

    为什么必须加这个源：面板看的是「产出文件新鲜度」，
    而调度器日志记的是「调度器视角的执行结果」。
    两者的差集就是静默失败：
    - 10-19「每日 AI 新闻推送」run failed（[CANCELLED] unknown_interrupted），
      但 10-20 补跑成功 → 面板从 10-20 起一直 ✅，缺口被完全掩盖 4 天。
    - 10-18 / 10-19 **巡检自动化自己**连续两次 run failed，
      但看板「巡检自身」检查只看上一轮面板的时间差 → 同样看不见。

    结论：产出新鲜度回答「最近有没有产出」，
    调度器日志回答「有没有跑失败过」。两个都要看。
    """
    out: list[dict] = []
    if not AUTOMATION_LOG.exists():
        return out
    try:
        raw = AUTOMATION_LOG.read_bytes()
    except Exception:
        return out
    text = raw.decode("utf-8", errors="replace")
    cutoff = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")

    # 日志里的 run failed 行**不带 name=**，只有 id。
    # 名字只能从 "run start: id=..., name=..." 行反查。
    id2name: dict[str, str] = {}
    for line in text.splitlines():
        if "run start:" not in line:
            continue
        m = re.search(r"run start: id=([0-9a-f-]{36}), name=([^,]+)", line)
        if m:
            id2name[m.group(1)] = m.group(2).strip()

    seen: set[tuple[str, str]] = set()
    for line in text.splitlines():
        if "run failed for" not in line:
            continue
        ts = line[:19]
        if len(ts) < 19 or ts[:10] < cutoff:
            continue
        m = re.search(r"run failed for ([0-9a-f-]{36})[^:]*:\s*\[([A-Z]+)\]\s*([^:]*)", line)
        if not m:
            continue
        aid, code, reason = m.group(1), m.group(2), m.group(3)
        # 日志时间戳是 UTC（结尾带 Z），本机是 UTC+8 → 转成本地时间再显示
        try:
            local = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)
            ts_local = local.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts_local = ts.replace("T", " ")
        key = (ts_local[:16], aid[:8])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ts": ts_local,
            "id": aid[:8],
            "name": id2name.get(aid, ""),
            "code": code,
            "reason": reason.strip()[:120],
        })
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out


def reconcile_failures_with_output(fails: list[dict]) -> list[dict]:
    """把「调度器失败」和「产出」两个视角对账。

    2026-10-30 新增（本轮实测抓到的矛盾信号）：
    面板上「AI 视频资料库每日更新」显示 ✅ 正常（10-30 03:57 有产出），
    但调度器日志同一时刻记的是 `run finished ... success=false` +
    `run failed ... [CANCELLED] unknown_interrupted`。
    **两个视角互相打架，且都没有解释** —— 读的人只能二选一信一个。

    实证核对：这次失败**发生在产出之后**（2 篇卡片 03:54/03:57 落盘并已入库资料库，
    automation memory 也写了），调度器随后判定中断 → 属于「做完但收尾失败」。

    对账口径：失败时刻之后，该自动化名下**有没有新产出**。
      - 有 → 「部分完成（产出在先，失败在后）」→ 不重跑，但要显式说明
      - 无 → 真失败 → 需要人工重跑
    """
    out: list[dict] = []
    for f in fails:
        name = f.get("name") or ""
        ckey = name.replace(" (Windows)", "")
        folder = PRODUCERS.get(name) or PRODUCERS.get(ckey)
        if folder is None or not folder.exists():
            out.append({**f, "reconcile": "无法对账（未跟踪该自动化）",
                        "has_output": None})
            continue
        try:
            ftime = datetime.strptime(f["ts"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            out.append({**f, "reconcile": "无法对账（时间解析失败）", "has_output": None})
            continue
        pattern = PRODUCER_GLOBS.get(name) or PRODUCER_GLOBS.get(ckey)
        mts = all_mtimes_matched(folder, pattern)
        after = [m for m in mts if m >= ftime - timedelta(minutes=5)]
        if after:
            out.append({**f, "reconcile": "部分完成（产出在先，失败在后）",
                        "has_output": True,
                        "output_at": max(after).strftime("%Y-%m-%d %H:%M")})
        else:
            out.append({**f, "reconcile": "真失败（失败后无产出）", "has_output": False})
    return out


def self_covered_slots() -> tuple[set[datetime], list[datetime]]:
    """巡检自动化**已覆盖**的调度槽（返回 (槽集合, 实际 run start 列表)）。

    2026-10-30 新增（本轮实测抓到的假信号）：
    「上一轮面板距今 0.3h，未中断」—— **这是错的**。
    巡检自动化自己从 10-28 21:40 之后就没跑过（机器关机到 10-30 03:49），
    但面板文件在 10-30 03:52 被**另一个自动化**（工作流自动追加）当作诊断工具跑了一次
    → 面板被覆盖 → 「面板年龄」这个代理指标失真 → 巡检自己漏跑反而报 ✅。

    教训：**面板文件不是「巡检自己跑过」的可靠证据**（任何人都能写它），
    调度器日志里的 run start 才是权威。

    ⚠️ 关键实现细节（第一次写错了，教训记在这）：
    不能用「run start 的时刻」去和「当天 21:40 整点」直接比大小——
    调度抖动让实际 run start 落在 21:40:31 / 21:40:47，
    整点 21:40:00 反而**早于**它 → 同一天会被误判成「漏跑」。
    正确做法：读 dispatch 行里的 `nextRunAt=`（那是**被补偿的调度槽**本身），
    用它来判定「哪个槽被覆盖了」，从而天然支持开机后的补偿触发。
    """
    if not AUTOMATION_LOG.exists():
        return set(), []
    try:
        text = AUTOMATION_LOG.read_bytes().decode("utf-8", errors="replace")
    except Exception:
        return set(), []
    self_id = "2c3f44e0-7b6a-4f8b-a32d-540589123a28"
    covered: set[datetime] = set()
    starts: list[datetime] = []
    for line in text.splitlines():
        if self_id not in line:
            continue
        ts = line[:19]
        try:
            local = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)
        except ValueError:
            continue
        if "run start:" in line:
            starts.append(local)
        if "dispatch automation" in line:
            m = re.search(r"nextRunAt=(\d+)", line)
            if m:
                covered.add(datetime.fromtimestamp(int(m.group(1)) / 1000))
    return covered, sorted(starts)


def self_missed_slots() -> dict:
    """巡检自动化自己漏掉的调度点。

    逐日核对 21:40 调度点：
      - 该槽**已被某次 run 覆盖**（含开机后的补偿触发）→ 正常
      - 未覆盖 + 机器开着 → ❌ 真漏跑（巡检自己失效 = 所有异常都看不见）
      - 未覆盖 + 机器关着 → 合理漏跑（关机），不算故障
    """
    covered, starts = self_covered_slots()
    if len(starts) < 2:
        return {"checked": False, "reason": "历史 run 记录不足"}
    slot_hour, slot_min = 21, 40
    # 从最早一次 run 覆盖的槽，到今天，逐日检查
    first_slot = min(covered) if covered else starts[0]
    out: list[dict] = []
    d = first_slot.replace(hour=slot_hour, minute=slot_min, second=0, microsecond=0)
    now = datetime.now()
    guard = 0
    while d <= now and guard < 120:
        guard += 1
        # 该槽对应的整点时间（nextRunAt 是毫秒时间戳，秒位可能带抖动）
        slot = d.replace(second=0, microsecond=0)
        matched = any(abs((c - slot).total_seconds()) <= 3600 for c in covered)
        if not matched:
            on = was_machine_on(slot)
            out.append({
                "slot": slot.strftime("%Y-%m-%d %H:%M"),
                "machine_on": on,
                "verdict": "真漏跑" if on else "合理漏跑（关机）",
            })
        d += timedelta(days=1)
    missed_real = [x for x in out if x["machine_on"]]
    missed_ok = [x for x in out if not x["machine_on"]]
    return {
        "checked": True,
        "last_start": starts[-1].strftime("%Y-%m-%d %H:%M:%S"),
        "prev_start": starts[-2].strftime("%Y-%m-%d %H:%M:%S"),
        "gap_h": round((starts[-1] - starts[-2]).total_seconds() / 3600, 1),
        "missed_real": missed_real,
        "missed_ok": missed_ok,
        "all_slots": out,
    }


def check_self_crash(grace_minutes: int = 90) -> dict | None:
    """检测「巡检自动化自己上一轮是不是崩了」。

    2026-10-28 新增（本轮实测抓到的真凶）：
    脚本首跑时 `check_git()` 抛 FileNotFoundError（git.exe 找不到）→ **整轮巡检崩溃** →
    dashboard.md 不更新。但原有的「巡检自身」自检只看**旧面板年龄**，
    48h 阈值内一律报 ✅ → **崩溃被完全掩盖**，而且会连续掩盖两轮
    （第 2 天旧面板 ~47.8h 仍 < 48h）。

    危害等级：比任何一个自动化漏跑都高 —— 巡检本身失效 = 所有自动化的异常都看不见。

    判据（直接读调度器日志，不依赖面板文件）：
    找到本自动化最近一次 `run start`，若其后**没有**对应的 `run finished`，
    说明那次运行没有正常收尾 → 崩了 / 被打断了。

    ⚠️ 2026-10-28 追加修正（避免假阳性）：
    **当前这一轮自己也没有 run finished**（因为脚本就是在这次 run 里跑的，
    日志要等整个会话结束才写）→ 不加区分就会把「正在跑」报成「崩了」。
    所以加**宽限期**：最近一次 run start 距今不足 grace_minutes → 判为
    「进行中」而非「崩溃」。实测历史：正常一轮跑 8–25 分钟，
    崩溃/中断的间隔远超 90 分钟 → 宽限 90 分钟不会漏掉真崩溃。

    注意：日志时间戳是 UTC，本机 UTC+8。
    """
    if not AUTOMATION_LOG.exists():
        return None
    try:
        text = AUTOMATION_LOG.read_bytes().decode("utf-8", errors="replace")
    except Exception:
        return None

    self_id = "2c3f44e0-7b6a-4f8b-a32d-540589123a28"
    starts: list[str] = []
    finished_at: list[str] = []
    for line in text.splitlines():
        if self_id not in line:
            continue
        if "run start:" in line:
            starts.append(line[:19])
        elif "run finished:" in line:
            finished_at.append(line[:19])

    if not starts:
        return None

    last_start = starts[-1]
    # 该次 run start 之后没有任何 run finished → 没有正常收尾
    if any(f > last_start for f in finished_at):
        return None

    try:
        local = datetime.strptime(last_start, "%Y-%m-%dT%H:%M:%S") + timedelta(hours=8)
    except ValueError:
        return None
    ts_local = local.strftime("%Y-%m-%d %H:%M:%S")
    age_min = (datetime.now() - local).total_seconds() / 60

    if age_min < grace_minutes:
        # 本次 run 仍在进行中 —— 不是崩溃
        return {"state": "running", "last_start_local": ts_local,
                "age_min": round(age_min, 1)}

    return {"state": "crashed", "last_start_local": ts_local,
            "last_start_utc": last_start, "finished_after": False,
            "age_min": round(age_min, 1)}


def machine_uptime_windows(days: int = 10) -> list[tuple[datetime, datetime]]:
    """机器的开机时间窗 [(开机, 关机), ...]。

    2026-10-23 新增：判定「漏跑」之前必须先排除「机器没开机」。
    2026-10-21 那轮 memory 记的是「10-20 全天关机」，实测事件日志发现
    机器是 **10-20 14:25 才关机**，当天 9:00 的自动化全都正常跑了。
    按「整天关机」去理解，会把真漏跑和合理漏跑一起放过。
    """
    if _UPTIME_CACHE["windows"] is not None:
        return _UPTIME_CACHE["windows"]
    wins: list[tuple[datetime, datetime]] = []
    try:
        import os
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        ps = ("Get-WinEvent -FilterHashtable @{LogName='System'; Id=6005,6006} "
              "-MaxEvents 60 -ErrorAction SilentlyContinue | "
              "ForEach-Object { $_.TimeCreated.ToString('yyyy-MM-dd HH:mm:ss') + '|' + $_.Id }")
        p = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           capture_output=True, env=env, timeout=90)
        raw = p.stdout.decode("utf-8", errors="replace")
        events: list[tuple[datetime, int]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            ts, _, eid = line.partition("|")
            try:
                events.append((datetime.strptime(ts.strip(), "%Y-%m-%d %H:%M:%S"), int(eid.strip())))
            except ValueError:
                continue
        events.sort()
        # 事件按时间正序：6005=开机启动日志服务，6006=关机停止日志服务
        boot: datetime | None = None
        for ts, eid in events:
            if eid == 6005:
                boot = ts
            elif eid == 6006 and boot is not None:
                wins.append((boot, ts))
                boot = None
        if boot is not None:
            wins.append((boot, datetime.now()))
    except Exception:
        wins = []
    _UPTIME_CACHE["windows"] = wins
    return wins


_UPTIME_CACHE: dict = {"windows": None}


def was_machine_on(dt: datetime) -> bool:
    """该时刻机器是否处于开机状态（用来区分「真漏跑」和「合理漏跑」）。"""
    wins = machine_uptime_windows()
    if not wins:
        return True  # 读不到开机记录 → 不据此判漏跑，避免误报
    return any(b <= dt <= e for b, e in wins)


def all_mtimes_matched(folder: Path | None, pattern: str | None) -> list[datetime]:
    """该自动化**所有**产出文件的时间（倒序）。

    2026-10-23 新增：单看「最近一次产出」有个结构性盲区——
    只要后来某天跑成功一次，之前漏掉的日子就被完全掩盖。
    10-19 的「每日 AI 新闻推送」就是这样：当天 run failed
    ([CANCELLED] unknown_interrupted)，之后 10-20 补跑成功，
    面板从 10-20 起就一直显示 ✅，**整整 4 天没人知道 10-19 缺档**。
    面板要能发现「某个该跑的日子没跑」，就必须拿到全部产出时间，
    而不只是最新的那一个。
    """
    if folder is None or not folder.exists():
        return []
    if pattern:
        files = [f for f in folder.rglob(pattern) if f.is_file()]
    else:
        files = [f for f in folder.rglob("*") if f.is_file()]
    return sorted((datetime.fromtimestamp(f.stat().st_mtime) for f in files),
                  reverse=True)


def produced_days_of(folder: Path | None, pattern: str | None) -> set[str]:
    """该自动化**已覆盖**的日期集合。

    2026-10-23 新增：日期要同时看 **mtime** 和 **文件名/目录名**。
    原因：补跑场景下两者会错位——
    `reports/auto_update/2026-10-20.md` 是 10-21 13:21 补跑生成的，
    文件名代表「10-20 那一期」，mtime 是「10-21」。
    只看 mtime 会把 10-20 误判成没跑；只看文件名又认不出当天实时产出的文件。
    两个都收，取并集。

    2026-10-24 新增：**产出目录可以不在本工作区**。
    「每日 AI 新闻推送」在 10-16 迁移过工作区，09-28~10-12 的产出
    落在 `_escaped_workspaces/automation-2026-09-28-08-14-42/outputs/`，
    不在 `reports/ai_news/`。只看当前目录 → 那 13 天会被当成「从没跑过」。
    → 加扫**历史工作区**，按日期并集。
    """
    if folder is None or not folder.exists():
        return set()
    if pattern:
        files = [f for f in folder.rglob(pattern) if f.is_file()]
    else:
        files = [f for f in folder.rglob("*") if f.is_file()]
    days: set[str] = set()
    for f in files:
        days.add(datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"))
        for token in (f.name, str(f.parent.name)):
            m = re.search(r"(20\d{2}-\d{2}-\d{2})", token)
            if m:
                days.add(m.group(1))
    return days


# 2026-10-24 新增：历史工作区里的产出目录（工作区迁移 / 路径整改前的落点）。
# 巡检只看当前工作区 → 迁移前的产出全部「人间蒸发」，会被误判成长期漏跑。
# 这里显式登记已知的迁移落点，把它们的日期并进 produced_days。
LEGACY_OUTPUT_DIRS: list[tuple[Path, str | None]] = [
    # 「每日 AI 新闻推送」09-28~10-12 的产出（文件名 AI动态日报-YYYY-MM-DD.md，注意是短横线）
    (Path(r"D:\Workbuddy工作目录\_escaped_workspaces\automation-2026-09-28-08-14-42\outputs"), "AI动态日报-*.md"),
    (Path(r"D:\Workbuddy工作目录\_install_dir_escape_backup\automation-2026-09-28-08-14-42\outputs"), "AI动态日报-*.md"),
]


def legacy_produced_days() -> set[str]:
    """历史工作区里已产出过的日期（并集）。"""
    days: set[str] = set()
    for folder, pattern in LEGACY_OUTPUT_DIRS:
        if not folder.exists():
            continue
        try:
            if pattern:
                files = [f for f in folder.rglob(pattern) if f.is_file()]
            else:
                files = [f for f in folder.rglob("*") if f.is_file()]
        except Exception:
            continue
        for f in files:
            days.add(datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d"))
            for token in (f.name, str(f.parent.name)):
                m = re.search(r"(20\d{2}-\d{2}-\d{2})", token)
                if m:
                    days.add(m.group(1))
    return days


def scheduler_says_success_but_no_output(
        lookback_days: int = 7) -> list[dict]:
    """调度器记 success=true，但当天**找不到任何产出**的日期。

    2026-10-24 新增：这是最隐蔽的一类静默失败——
    调度器日志明确写了 `run finished ... success=true`，
    说明「任务跑完了、没报错」，但产出文件根本不存在。
    产出新鲜度视角**永远看不见**（因为它在算「最近一次产出」，
    而这一天的产出压根不存在，只能靠别天的产出把日期填上）；
    调度器失败视角**也看不见**（因为它压根没失败）。

    实测（本机真实数据）：
    - 「每日 AI 新闻推送」10-13 / 10-14：调度器 success=true，
      但全盘搜不到当天日报文件（10-12 之后直接跳到 10-17）。
    - 「每日一个为什么」09-29~10-16：调度器 success=true，
      产出只以「已用主题」列表形式记在旧工作区的 memory 里，
      没有独立 md 文件 → 同样零产出。

    判定口径（刻意保守，避免误报）：
    1. 只看**每日型**自动化（周/月级粒度不匹配）；
    2. 只报「调度器有 success=true 且机器开着」的日期；
    3. 必须**同时**在当前工作区和历史工作区都找不到产出；
    4. 产出目录已知在迁移前被改过路径的自动化
       （如「每日一个为什么」）**一律不报**——它的产出形态本来就不是
       独立 md 文件，报了就是误报。
    """
    if not AUTOMATION_LOG.exists():
        return []
    try:
        raw = AUTOMATION_LOG.read_bytes()
    except Exception:
        return []
    text = raw.decode("utf-8", errors="replace")

    # 只处理这些自动化的「成功但零产出」——
    # 它们在迁移后有明确的独立 md 产出目录，判定可靠。
    tracked = {
        "每日 AI 新闻推送": (REPORTS / "ai_news", "ai_news_*.md"),
        "AI 视频资料库每日更新": (WORKSPACE / "project_distill" / "community_research", "*调研*.md"),
    }
    # 这些自动化的产出形态不是独立 md（记在 memory 主题列表里），不做判定。
    skip = {"每日一个为什么"}

    finished: dict[str, dict[str, datetime]] = {k: {} for k in tracked}
    for m in re.finditer(
            r"(\S+?)\s+\[INFO\] \[LocalAutomationScheduler\] run finished: "
            r"id=[0-9a-f-]{36}, name=([^,]+), success=true", text):
        ts_raw, name = m.groups()
        name = name.strip()
        if name in skip or name not in finished:
            continue
        try:
            dt = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)
        except Exception:
            continue
        ds = dt.strftime("%Y-%m-%d")
        # 同一天多次运行 → 取最后一次
        prev = finished[name].get(ds)
        if prev is None or dt > prev:
            finished[name][ds] = dt

    legacy = legacy_produced_days()
    out: list[dict] = []
    cutoff = (datetime.now() - timedelta(days=lookback_days)).date()
    for name, days in finished.items():
        folder, pattern = tracked[name]
        local_days = produced_days_of(folder, pattern)
        for ds, dt in sorted(days.items()):
            try:
                d = datetime.strptime(ds, "%Y-%m-%d").date()
            except ValueError:
                continue
            if d < cutoff:
                continue
            if ds in local_days or ds in legacy:
                continue
            # 机器在**实际运行时刻**开着才可能是真问题（关机日 → 合理不产出）。
            # 不能用「当天中午」判断：机器可能上午开机、下午关机，
            # 那天下午跑的活照样能跑成。实测 10-14 13:54 就跑过。
            if not was_machine_on(dt):
                continue
            out.append({"automation": name, "date": ds, "run_at": dt.strftime("%Y-%m-%d %H:%M")})
    out.sort(key=lambda r: (r["date"], r["automation"]))
    return out


def missed_days(folder: Path | None, pattern: str | None,
                expected_hours: int, schedule_hour: int | None = None,
                lookback_days: int = 7) -> list[str]:
    """找出「该跑但没跑」的日子。

    2026-10-23 新增：见 all_mtimes_matched 的说明——
    面板原本只看最新产出，导致**中间漏掉的日子被掩盖**。
    这里按产出日期逐日核对，只报「机器开着、且到点了、却没有任何产出」的日子。

    关机判定按**调度时刻**而不是「当天开过机没有」：
    机器 10-20 14:25 关机 → 当天 9:00 的活跑了（算正常），
    18:30 / 21:00 的活没跑（算合理漏跑，不报）。
    """
    if expected_hours > 48:
        return []  # 周/月级自动化不做逐日核对，粒度不匹配
    mts = all_mtimes_matched(folder, pattern)
    if not mts:
        return []
    produced_days = produced_days_of(folder, pattern)
    latest = mts[0]
    earliest = mts[-1]
    now = datetime.now()
    h = schedule_hour if schedule_hour is not None else 12
    out: list[str] = []
    start = latest.strftime("%Y-%m-%d")
    floor = earliest.strftime("%Y-%m-%d")
    for i in range(lookback_days + 1):
        d = datetime.strptime(start, "%Y-%m-%d") - timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        # 早于该自动化**第一次产出**的日子不算漏跑——
        # 那时它可能还没建、或产出目录还不存在（不是漏跑，是没开始）。
        if ds < floor:
            break
        if ds in produced_days:
            continue
        due = d.replace(hour=h, minute=0)
        # 到点 + 宽限窗口都还没过去 → 还不能判漏跑（今天这一期可能还没到点/还在跑）
        if now < due + timedelta(hours=3):
            continue
        # 该调度时刻机器是否开着？开着 → 真漏跑；没开 → 合理漏跑，不报。
        if was_machine_on(due) or was_machine_on(due + timedelta(hours=3)):
            out.append(ds)
    return sorted(out)



def latest_mtime(folder: Path | None) -> datetime | None:
    if folder is None or not folder.exists():
        return None
    files = [f for f in folder.rglob("*") if f.is_file()]
    if not files:
        return None
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files))


def check_disk() -> dict:
    import shutil
    total, used, free = shutil.disk_usage("C:\\")
    return {
        "total_gb": round(total / 1024**3, 1),
        "used_gb": round(used / 1024**3, 1),
        "free_gb": round(free / 1024**3, 1),
        "free_pct": round(free / total * 100, 1),
    }


# ── C 盘 AI 工具占用巡检 ────────────────────────────────────────────────
# 背景：2026-10-17 发现 Codex runtimes 堆了 14 个 hash 版本共 4.84GB，
# 且「删除」走回收站不清空就不释放空间。这两项必须持续盯。
CUA_NODE = Path(r"C:\Users\lee\AppData\Local\OpenAI\Codex\runtimes\cua_node")
RECYCLE = Path(r"C:\$Recycle.Bin")
AI_TOOL_DIRS = {
    "OpenAI\\Codex": Path(r"C:\Users\lee\AppData\Local\OpenAI\Codex"),
    ".codex": Path(r"C:\Users\lee\.codex"),
    ".cache\\codex-runtimes": Path(r"C:\Users\lee\.cache\codex-runtimes"),
    ".workbuddy": Path(r"C:\Users\lee\.workbuddy"),
}
RUNTIME_WARN_COUNT = 2   # 超过 2 个版本就值得看一眼
RECYCLE_WARN_GB = 1.0    # 回收站超过 1GB 说明有「删了没释放」的隐患

# WorkBuddy 自身会无限增长的日志/trace（2026-10-17 实测：logs 一个月涨到 618MB）
WB_LOGS = Path(r"C:\Users\lee\.workbuddy\logs")
WB_TRACES = Path(r"C:\Users\lee\.workbuddy\traces")
WB_GROWTH_WARN_MB = 500  # 单个日志目录超过 500MB 就该清了


def dir_size_gb(p: Path) -> float:
    if not p.exists():
        return 0.0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return round(total / 1024**3, 2)


def check_cua_runtimes() -> dict:
    """Codex cua_node 历史版本堆积检查（只报告，不自动删）。"""
    if not CUA_NODE.exists():
        return {"count": 0, "total_gb": 0.0, "versions": [], "reclaimable_gb": 0.0,
                "status": "✅ 正常", "note": "目录不存在（可能 Codex 已更新结构）"}
    dirs = sorted(
        [d for d in CUA_NODE.iterdir() if d.is_dir()],
        key=lambda d: d.stat().st_mtime, reverse=True)
    versions = []
    for d in dirs:
        versions.append({
            "name": d.name,
            "mb": round(dir_size_gb(d) * 1024, 1),
            "date": datetime.fromtimestamp(d.stat().st_mtime).strftime("%m-%d"),
        })
    total = round(sum(v["mb"] for v in versions) / 1024, 2)
    # 保留最新一个，其余算可回收
    reclaim = round(sum(v["mb"] for v in versions[1:]) / 1024, 2)
    if len(versions) <= RUNTIME_WARN_COUNT:
        status, note = "✅ 正常", f"{len(versions)} 个版本，符合预期"
    elif len(versions) <= 5:
        status, note = "⚠️ 偏多", f"{len(versions)} 个版本，建议只留最新"
    else:
        status, note = "❌ 堆积", f"{len(versions)} 个版本，{reclaim} GB 可回收"
    return {"count": len(versions), "total_gb": total, "versions": versions[:6],
            "reclaimable_gb": reclaim, "status": status, "note": note}


def check_recycle_bin() -> dict:
    """回收站体积检查 —— 删除后不清空等于没删。"""
    gb = dir_size_gb(RECYCLE)
    if gb < RECYCLE_WARN_GB:
        status, note = "✅ 正常", f"{gb} GB"
    else:
        status, note = "⚠️ 需清空", f"{gb} GB 未释放，先 Clear-RecycleBin -Force"
    return {"size_gb": gb, "status": status, "note": note}


def check_wb_logs() -> dict:
    """WorkBuddy 自身日志/trace 增长检查（只报告，不自动删）。"""
    out = {}
    total = 0.0
    worst = None
    for label, base in (("logs", WB_LOGS), ("traces", WB_TRACES)):
        gb = dir_size_gb(base)
        total += gb
        entry = {"size_gb": gb, "status": "✅ 正常",
                 "note": f"{round(gb * 1024)} MB"}
        if gb * 1024 > WB_GROWTH_WARN_MB:
            entry["status"] = "⚠️ 偏大"
            entry["note"] = (f"{round(gb * 1024)} MB，超过 {WB_GROWTH_WARN_MB}MB "
                             f"阈值，可只保留最近 2–3 天")
        out[label] = entry
        if gb > (worst or 0):
            worst = gb
    status = "⚠️ 偏大" if any(e["status"].startswith("⚠️") for e in out.values()) else "✅ 正常"
    return {"total_gb": round(total, 2), "status": status,
            "detail": out,
            "note": f"合计 {round(total * 1024)} MB，可只保留最近 2–3 天"}


def check_ai_disk() -> dict:
    dirs = {}
    for name, p in AI_TOOL_DIRS.items():
        dirs[name] = dir_size_gb(p)
    total = round(sum(dirs.values()), 2)
    return {
        "dirs": dirs,
        "total_gb": total,
        "runtime": check_cua_runtimes(),
        "recycle": check_recycle_bin(),
        "wb_logs": check_wb_logs(),
    }


def prev_dashboard_age_h() -> float | None:
    """上一轮巡检面板距今多少小时。

    2026-10-21 新增：10-20 的 21:30 巡检没跑（机器当时没开机），
    面板就静静停在 10-19 21:54 —— **没人收到任何告警**。
    这恰恰是本脚本要防的「静默失败」，只是这次发生在脚本自己身上：
    如果没人主动来看这个面板，「巡检没跑」和「自动化没产出」长得一模一样。

    所以这里读旧面板的生成时间，超过 48h 就在面板里显式标出来。
    """
    if not DASHBOARD.exists():
        return None
    try:
        first = DASHBOARD.read_text(encoding="utf-8").splitlines()
        for line in first[:6]:
            if line.startswith("> 生成时间："):
                prev = datetime.strptime(line.split("：", 1)[1].strip(), "%Y-%m-%d %H:%M")
                return round((datetime.now() - prev).total_seconds() / 3600, 1)
    except Exception:
        return None
    return None


def run_text(cmd: list[str], cwd: Path | None = None, utf8_stdout: bool = False) -> tuple[int, str]:
    """跑子进程并稳妥解码。

    2026-10-21 修正：原实现直接 `text=True, encoding="utf-8"`。
    但本机 `space_api.py` 的输出是 **GBK**（Windows 控制台默认编码），
    强行按 utf-8 解码会让 subprocess 的 reader 线程抛 UnicodeDecodeError，
    结果 `p.stdout` 变成 **None** —— 不是空字符串，是 None，
    于是 `p.stdout.strip()` 直接 AttributeError 崩掉整个巡检脚本。

    这类崩溃最阴：脚本一崩，dashboard.md 就停在**上一次**的内容，
    看上去一切正常，实际是「面板没更新」而不是「自动化没产出」——
    和本脚本要防的静默失败是同一类错误，只是方向反了。

    这里改成按字节捕获，再依次尝试 utf-8 → gbk → utf-8-sig → replace 兜底。

    2026-10-21 21:30 补充修正（**本轮实测抓到的真凶**）：
    `space_api.py` 的输出其实是 **UTF-8**（不是 GBK），但它走 `sys.stdout.write`，
    而 Windows 控制台默认编码是 GBK → 遇到标题里含 GBK 编不了的字符时
    `sys.stdout.write` 直接抛 UnicodeEncodeError。
    而 `_common.safe_print` 用的是 `except Exception: pass` ——
    **异常被吞掉，stdout 一个字节都不写，进程还 exit 0**。

    后果极其阴险：巡检脚本拿到空字符串 → 当成「这个目录 0 个节点」→
    资料库文档数从 68 掉到 26 → 面板显示「一切正常」。
    实际上资料库一个文档都没丢，**只是读取姿势被 GBK 编码卡住了**。

    修法：调 space_api.py 时强制 `PYTHONIOENCODING=utf-8`，
    让它写 UTF-8 字节，不受控制台编码影响。
    """
    env = None
    if utf8_stdout:
        import os
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, env=env)
    raw = p.stdout or b""
    for enc in ("utf-8", "gbk", "utf-8-sig"):
        try:
            return p.returncode, raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return p.returncode, raw.decode("utf-8", errors="replace")


def list_library_nodes() -> list[dict]:
    """列出资料库节点。

    2026-10-17 修正：原实现只查空间根目录，实测只返回 11 个节点，
    而真正的工作流文件夹（ROOT_ID）下有 28 个、任务结果归档（ARCHIVE_ID）下有 5 个。
    「直接写资料库型」自动化的产出几乎都在这两个子文件夹里 —— 只查根目录会
    漏掉它们，升级判定就会把真产出误判成「资料库也没有」→ 触发假补救。
    这里合并「根 + 工作流文件夹 + 任务结果归档」三处。
    """
    found: dict[str, dict] = {}
    errors: list[str] = []

    def fetch(parent_id: str | None) -> None:
        cmd = [PYTHON, str(SPACE_API), "space.workspace.list-nodes",
               "--space-id", LIB_SPACE_ID, "--limit", "200"]
        if parent_id:
            cmd += ["--parent-node-id", parent_id]
        rc, out = run_text(cmd, utf8_stdout=True)
        label = parent_id or "SPACE_ROOT"
        if rc != 0 or not out.strip():
            # 2026-10-21 21:30：空 stdout 曾经被当成「这个目录 0 个节点」静默吞掉，
            # 导致资料库文档数 68 → 26 而面板还显示一切正常。
            # 现在必须显式记为读取失败，不能当成「空」。
            errors.append(f"{label}: rc={rc} stdout为空")
            return
        try:
            for n in json.loads(out.strip())["data"]["nodes"]:
                found[n["id"]] = n
        except Exception as e:
            errors.append(f"{label}: 解析失败 {e}")

    fetch(None)
    fetch(ROOT_ID)
    fetch(ARCHIVE_ID)
    if errors:
        raise LibraryReadError("；".join(errors))
    return list(found.values())


def library_recent_nodes(nodes: list[dict], days: int = 7) -> list[dict]:
    """按 updatedAt 筛近 N 天有更新的节点。

    注意：list-nodes 返回的 updatedAt 是 **epoch 毫秒（int）**，不是 ISO 字符串。
    按 ISO 解析会静默失败成「0 条近期更新」，是最容易骗过巡检的错误。
    """
    cut = (datetime.now() - timedelta(days=days)).timestamp() * 1000
    out = []
    for n in nodes:
        v = n.get("updatedAt")
        try:
            if float(v) > cut:
                out.append(n)
        except (TypeError, ValueError):
            continue
    return out


def resolve_git() -> str | None:
    """找出可用的 git 可执行文件。

    2026-10-28 修正（本轮实测抓到的真凶）：WorkBuddy 更新后 PortableGit 布局变了 ——
    `git.exe` 现在只存在于 `PortableGit\\cmd\\` 和 `PortableGit\\mingw64\\bin\\`，
    而 Python 子进程继承的 Windows PATH 里**只有 `PortableGit\\usr\\bin\\`**，
    那个目录**根本没有 git.exe**（实测 `shutil.which("git")` = None）。

    危害：check_git() 直接 `subprocess.run(["git", ...])` → 抛 FileNotFoundError →
    **整个巡检脚本崩溃** → dashboard.md 不更新 → 但「巡检自身」自检只看旧面板年龄，
    48h 阈值内照样报 ✅ → **崩溃被完全掩盖**。
    这正是本任务立项要消灭的那类静默失败，却发生在自己身上。

    修法：显式按已知候选路径探测，找到就用绝对路径；找不到就返回 None
    —— **绝不让「git 缺失」变成「整个巡检崩溃」**。
    """
    import shutil as _shutil

    found = _shutil.which("git")
    if found:
        return found

    candidates = [
        # mingw64\bin 是**真身**（4.3MB）；cmd\git.exe 只是 47KB 的 wrapper，
        # 靠 PATH 找配套 DLL —— 在 PATH 被裁剪的子进程里可能起不来。
        # 所以真身优先，wrapper 兜底。
        Path(r"D:\workbuddy\resources\vendor\PortableGit\mingw64\bin\git.exe"),
        Path(r"D:\workbuddy\resources\vendor\PortableGit\cmd\git.exe"),
        Path(r"D:\workbuddy\resources\vendor\PortableGit\usr\bin\git.exe"),
        Path(r"C:\Program Files\Git\cmd\git.exe"),
        Path(r"C:\Program Files\Git\bin\git.exe"),
    ]
    for c in candidates:
        try:
            if c.exists():
                return str(c)
        except OSError:
            continue
    return None


def check_git() -> dict:
    d = WORKSPACE / "project_distill"
    git = resolve_git()
    if not git:
        # 2026-10-28：git 找不到 **不能**让巡检崩掉。
        # 降级为「未知」，面板上标注，别让一个非关键检查拖垮整轮巡检。
        return {"dirty_files": None, "last_commit": "(git 不可用，未检查)", "git_missing": True}
    try:
        _, out = run_text([git, "status", "--short"], cwd=d)
        dirty = [x for x in out.strip().split("\n") if x.strip()]
        _, out2 = run_text([git, "log", "--oneline", "-1"], cwd=d)
    except Exception as e:
        return {"dirty_files": None, "last_commit": f"(git 调用失败: {e})", "git_missing": True}
    return {"dirty_files": len(dirty), "last_commit": out2.strip(), "git_missing": False}


# 2026-10-25 新增：Windows 计划任务（schtasks）里**存在但脚本没跟踪**的自动化。
#
# 盲点背景：巡检脚本只跟踪「云端 ACTIVE 自动化 + 1 个已知 Windows 计划任务
# (AIWeeklyReview)」。但本机 schtasks 里还有 `KB_Index_Monthly_Verify`
# （2026-10-12 注册，每月 1 号 21:00）——它从未运行过、也从未被巡检发现。
# 只要不在跟踪名单里，它就永远隐形：报成功时报不出、漏跑时也看不出。
#
# 这类「注册了但没人看」的自动化 = 最纯粹的静默失败：
# 既没有 run failed 记录，也没有产出文件，两个视角都看不见。
def _extract_script_ref(cmdline: str) -> str:
    """从计划任务命令行里提取被执行的脚本路径。

    2026-10-28 修正：命令行有**两种形态**——
      A. `python "D:\\path\\script.py"`      （带引号）
      B. `python D:\\path\\script.py`        （不带引号，含空格路径）
    原实现只按双引号切，遇到 B 形态会把整条命令行当成路径 → 误报「脚本不存在」。
    这里改成：先按引号切；切不出就按空白切，取最后一个以脚本后缀结尾的 token。
    """
    if not cmdline:
        return ""
    for token in cmdline.split('"'):
        t = token.strip()
        if t.lower().endswith((".py", ".bat", ".cmd", ".ps1")):
            return t
    # 不带引号的形态：按空白切，取最后一个脚本后缀 token
    parts = cmdline.split()
    for t in reversed(parts):
        if t.lower().endswith((".py", ".bat", ".cmd", ".ps1")):
            return t
    return ""


def find_script_refs_in_raw(raw: bytes) -> list[str]:
    """在 schtasks 的**原始字节**里找脚本路径引用。

    2026-10-28 实测发现：schtasks 的 CSV 输出是**混合编码**——
      - 表头 / 系统字符串（主机名、任务名、计划类型…）是 **GBK**
      - 任务命令行里的**用户路径**（含中文目录名）是 **UTF-8**
    整体按 GBK 解 → 中文路径变乱码；整体按 UTF-8 解 → 直接 UnicodeDecodeError。
    而且 `csv.reader` 的引号处理还会把 `python.exe "D:/path/x.py"` 的引号吃掉，
    导致路径和命令粘成一条。

    → 解法：**只在原始字节层面**用正则找路径（不依赖任何整体解码），
    再对每个候选片段分别试 UTF-8 / GBK 解码，
    **以「该路径在磁盘上真实存在」为唯一验收标准**。
    这样既能识别中文路径，又能天然排除「命令粘成一条」的误判。

    正则排除空格，避免把 `python.exe "D:/path/x.py"` 整体当成一个路径。
    """
    import re as _re

    pat = _re.compile(
        rb"[A-Za-z]:\\[^\\\"'\',\r\n ]{0,300}?\.(?:py|bat|cmd|ps1)",
        _re.IGNORECASE)
    found: list[str] = []
    for m in pat.finditer(raw):
        blob = m.group(0)
        for enc in ("utf-8", "gbk"):
            try:
                s = blob.decode(enc)
            except UnicodeDecodeError:
                continue
            try:
                if Path(s).exists():
                    found.append(s)
                    break
            except OSError:
                continue
    return found


def check_untracked_schtasks() -> list[dict]:
    """列出 schtasks 里与本项目相关、但不在脚本跟踪名单里的计划任务。"""
    import csv
    import io

    # 已知的、由本巡检脚本跟踪的 Windows 计划任务名
    tracked = {"AIWeeklyReview"}
    # 只有这些标记的任务才认为是「本项目的」
    project_markers = ("workbuddy", "Workbuddy", "WorkBuddy", "副业", "project_distill",
                       "weekly_review", "verify_kb", "kb_verify", "kb_index")

    try:
        p = subprocess.run(["schtasks", "/query", "/fo", "CSV", "/v"],
                           capture_output=True, timeout=120)
        raw = p.stdout or b""
        txt = raw.decode("gbk", errors="replace")
        # 2026-10-28：同时在**原始字节**层面扫脚本路径引用。
        # schtasks CSV 是混合编码（表头 GBK + 用户路径 UTF-8），
        # 单靠 csv.reader 拿到的命令列不可靠。
        raw_script_refs = find_script_refs_in_raw(raw)
    except Exception as e:
        return [{"name": "(读取失败)", "detail": str(e), "severity": "⚠️"}]

    try:
        rows = list(csv.reader(io.StringIO(txt)))
    except Exception as e:
        return [{"name": "(解析失败)", "detail": str(e), "severity": "⚠️"}]
    if not rows:
        return [{"name": "(schtasks 无输出)", "detail": "", "severity": "⚠️"}]

    hdr = rows[0]
    idx = {name: i for i, name in enumerate(hdr)}

    def col(row: list[str], key: str, default: str = "") -> str:
        i = idx.get(key)
        if i is None or i >= len(row):
            return default
        return row[i].strip()

    out: list[dict] = []
    for row in rows[1:]:
        if not row:
            continue
        tname = col(row, "任务名")
        if not tname:
            continue
        plain = tname.lstrip("\\")
        if plain in tracked:
            # 2026-10-28：被跟踪的计划任务同样要校验脚本存在性。
            # 「任务显示就绪」≠「任务能跑」——路径失效时只会在日志里留个坑。
            #
            # 校验方式：**原始字节层面**找路径引用（find_script_refs_in_raw）。
            # 该函数只返回**磁盘上确实存在**的路径 → 天然不会误判。
            # 不用 csv.reader 解析的命令列：schtasks 是混合编码（表头 GBK +
            # 用户路径 UTF-8）且引号被吃掉 → 命令与路径会粘成一条，误报率极高。
            #
            # 找该任务行区间：在原始字节里定位任务名，取其后的一段再扫。
            # 注意：局部变量名不能叫 idx —— 外层 idx 是「CSV 列名→下标」映射。
            row_pos = raw.find(plain.encode("utf-8"))
            if row_pos < 0:
                row_pos = raw.find(plain.encode("gbk", errors="ignore"))
            if row_pos >= 0:
                seg = raw[row_pos: row_pos + 400]
                found = find_script_refs_in_raw(seg)
                if found:
                    out.append({
                        "name": plain,
                        "last_run": col(row, "上次运行时间"),
                        "last_result": col(row, "上次结果"),
                        "next_run": col(row, "下次运行时间"),
                        "never_ran": False,
                        "script_ref": found[0],
                        "script_missing": False,
                        "severity": "⚠️",
                    })
            continue
        # 判定是否与本项目相关
        hay = " ".join([tname, col(row, "要运行的任务"), col(row, "注释"),
                         col(row, "起始于")])
        if not any(mk in hay for mk in project_markers):
            continue
        last_run = col(row, "上次运行时间")
        last_res = col(row, "上次结果")
        next_run = col(row, "下次运行时间")
        # 1999/11/30 = 从未运行过
        never = "1999" in last_run or last_res == "267011"
        # 2026-10-28 新增：校验任务要执行的脚本**是否还存在**。
        # 起因：run_weekly_review.bat 指向的 weekly_review.py 在工作区迁移后
        # 路径已失效，但任务本身仍显示「就绪」→ 到点必失败，且失败原因
        # 只落在日志里。**「任务存在」不等于「任务能跑」。**
        #
        # 校验方式：原始字节扫描该任务行区间（find_script_refs_in_raw）。
        # 若扫描到「磁盘上确实存在」的脚本 → 路径有效。
        # 若扫描不到任何存在的脚本，但任务行里**提到了**某个脚本路径
        # （如已失效的旧路径）→ 判定「脚本失效」。
        row_pos2 = raw.find(plain.encode("utf-8"))
        if row_pos2 < 0:
            row_pos2 = raw.find(plain.encode("gbk", errors="ignore"))
        seg = raw[row_pos2: row_pos2 + 400] if row_pos2 >= 0 else b""
        found_ok = find_script_refs_in_raw(seg)
        script_ref = found_ok[0] if found_ok else ""
        script_missing = False
        if not found_ok:
            # 回退：找该任务行里提到的脚本路径（可能已不存在）
            import re as _re
            pat = _re.compile(
                rb"[A-Za-z]:\\[^\\\"'\',\r\n ]{0,300}?\.(?:py|bat|cmd|ps1)",
                _re.IGNORECASE)
            for m in pat.finditer(seg):
                for enc in ("utf-8", "gbk"):
                    try:
                        s = m.group(0).decode(enc)
                    except UnicodeDecodeError:
                        continue
                    script_ref = script_ref or s
                    try:
                        if not Path(s).exists():
                            script_missing = True
                    except OSError:
                        script_missing = True
                    break
                if script_ref:
                    break
        out.append({
            "name": plain,
            "last_run": last_run,
            "last_result": last_res,
            "next_run": next_run,
            "never_ran": never,
            "script_ref": script_ref,
            "script_missing": script_missing,
            "severity": "❌" if (never or script_missing) else "⚠️",
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    now = datetime.now()
    rows = []
    prev_age_h = prev_dashboard_age_h()
    # 2026-10-28 新增：自检自己上一轮是不是崩了（读调度器日志，不依赖面板文件）
    self_crash = check_self_crash()
    # 2026-10-30 新增：巡检自己漏跑了哪些调度点（同样只认调度器日志）。
    # 面板文件会被别的自动化覆盖 → 不能当「巡检跑过」的证据（10-30 实测踩过）。
    self_slots = self_missed_slots()

    # 2026-10-27 新增：读云端真实状态。人为 PAUSED 的自动化**不算异常**——
    # 明哥主动关掉的东西，产出停更是预期行为，判成「漏跑」是假信号。
    cloud = read_cloud_automation_status()
    paused_rows: list[dict] = []

    for name, folder in PRODUCERS.items():
        expected = EXPECTED_HOURS.get(
            name.replace(" (Windows)", ""), EXPECTED_HOURS.get(name, 24))
        # PAUSED 判定：优先按自动化名匹配；「AIWeeklyReview (Windows)」
        # 是 Windows 计划任务，不在云端名单里 → 不参与 PAUSED 判定。
        ckey = name.replace(" (Windows)", "")
        cstat = cloud.get(ckey, {})
        is_paused = cstat.get("status") == "PAUSED"

        mt = latest_mtime_matched(folder, PRODUCER_GLOBS.get(name))
        # 2026-10-23 新增：逐日核对「该跑却没跑」的日子。
        # 只看最新产出天数有结构性盲区：漏跑日后只要有一次补跑成功，
        # 缺口就被完全掩盖（10-19 AI 日报就是这样被掩盖了 4 天）。
        # 2026-10-27：PAUSED 的自动化不做漏跑判定（暂停后本就不该有产出）。
        gaps = [] if is_paused else missed_days(
            folder, PRODUCER_GLOBS.get(name), expected,
            schedule_hour=SCHEDULE_HOUR.get(name))
        # 尚未到第一次执行时间（按创建时间 2026-10-17 算起）→ 不算异常
        not_due_yet = False
        if mt is None:
            # 2026-10-30 修正：原来硬编码「创建于 2026-10-17」，对**之后新建**的
            # 自动化会把「还没到点」误判成「⚠️ 到点未产出」。
            # 实测：明哥 10-30 04:05 新建的「AI赛道情报周报」首跑是 11-03，
            # 按硬编码口径会立刻报 ⚠️ —— 又是一个假信号。
            # 现在优先用 FIRST_RUN 登记的真实首跑时间，其次用云端 created_at。
            created = None
            first = FIRST_RUN.get(name)
            if first:
                try:
                    dt_first = datetime.strptime(first, "%Y-%m-%d %H:%M")
                    created = dt_first
                except ValueError:
                    created = None
            if created is None:
                c_created = cstat.get("created_at")
                if isinstance(c_created, (int, float)) and c_created > 0:
                    created = datetime.fromtimestamp(c_created / 1000)
                else:
                    created = datetime(2026, 10, 17)
            elapsed_h = (now - created).total_seconds() / 3600
            not_due_yet = elapsed_h < expected

        if mt is None:
            age_h = None
            if not_due_yet:
                status = "⏳ 待首跑"
                # 2026-10-22 修正：原 note 直接印 expected（840h = 35 天），
                # 但「工作流守护 - 每月校验」实际首跑是 11-01 10:00（约 228h 后），
                # 印 840h 会让人以为「还要等 35 天」，其实是「10 天后」。
                # 这里改成按真实调度推算的首跑时间点。
                first = FIRST_RUN.get(name)
                if first:
                    dt_first = datetime.strptime(first, "%Y-%m-%d %H:%M")
                    h_left = (dt_first - now).total_seconds() / 3600
                    if h_left > 0:
                        note = (f"还没到首次执行时间（{first} 首跑，约 {h_left:.0f}h 后）")
                    else:
                        note = f"首跑时间 {first} 已过但无产出"
                        status = "⚠️ 无产出"
                else:
                    note = f"还没到首次执行时间（约 {expected}h 后）"
            elif folder is not None and not folder.exists():
                status = "❌ 目录缺失"
                note = f"目录不存在: {folder}"
            else:
                status = "⚠️ 无产出"
                note = "到点未产出"
        else:
            age_h = (now - mt).total_seconds() / 3600
            if age_h <= expected:
                status = "✅ 正常"
            elif age_h <= expected * 2:
                status = "⚠️ 偏旧"
            else:
                status = "❌ 超期"
            note = mt.strftime("%m-%d %H:%M")
            # 2026-10-23 新增：最近产出是新的，但中间漏了日子 → 不能显示 ✅。
            # 这类「最新新鲜但历史有缺口」必须显式标出来，否则等于没报。
            if gaps:
                status = "⚠️ 有缺口"
                note += f"；漏跑 {', '.join(gaps)}"

        # 2026-10-27：人为暂停覆盖状态判定，但不隐藏事实——
        # 面板保留「最近产出」信息，只是不再把它算作异常。
        if is_paused:
            status = "⏸ 已暂停"
            note = f"{note}（人为暂停 {cstat.get('updated_at', '')}）"
            paused_rows.append({
                "automation": name,
                "last_output": mt.strftime("%m-%d %H:%M") if mt else "无",
                "paused_at": cstat.get("updated_at"),
                "rrule": cstat.get("rrule"),
            })

        rows.append({
            "automation": name,
            "expected_hours": expected,
            "age_hours": round(age_h, 1) if age_h is not None else None,
            "last_output": note,
            "status": status,
            "missed_days": gaps,
            "paused": is_paused,
        })

    disk = check_disk()
    git = check_git()
    ai = check_ai_disk()
    sched_fails = check_scheduler_failures(5)
    # 2026-10-30 新增：把「调度器失败」和「产出」对账——
    # 避免出现「面板说 ✅、调度器说失败」这种两边都解释不了的矛盾信号。
    sched_fails = reconcile_failures_with_output(sched_fails)
    # 2026-10-24 新增：调度器报 success=true 但零产出（最隐蔽的一类静默失败）。
    # 窗口取 30 天：这类失败比「run failed」更隐蔽，往往几周后才被发现。
    silent = scheduler_says_success_but_no_output(30)

    # 资料库读取失败 ≠ 0 个文档。2026-10-21 21:30 实测踩过：
    # space_api.py 写 stdout 时被 GBK 卡住 → safe_print 吞异常 → exit 0 + 空输出，
    # 面板就显示「26 个文档」而真相是「读不到」。
    # 这里把读取失败显式记成一条 ❌ 异常项，绝不静默当成空。
    lib_error = ""
    try:
        nodes = list_library_nodes()
        lib_docs = len(nodes)
        recent_lib = library_recent_nodes(nodes, 7)
    except LibraryReadError as e:
        lib_error = str(e)
        nodes = []
        lib_docs = -1
        recent_lib = []

    bad = [r for r in rows if r["status"].startswith(("❌", "⚠️"))]
    ok = [r for r in rows if r["status"].startswith(("✅", "⏳", "⏸"))]
    gap_rows = [r for r in rows if r.get("missed_days")]

    # 2026-10-27 新增：**覆盖缺口**检测——云端 ACTIVE 的自动化，
    # 脚本是否都在跟踪？漏跟踪 = 那个自动化出问题永远不会被发现
    # （比误报危险得多：面板会一直显示一切正常）。
    tracked_names = set(PRODUCERS.keys())
    cloud_active = {n for n, v in cloud.items() if v.get("status") == "ACTIVE"}
    # 巡检自身的产出就是 dashboard.md，它由「巡检自身」检查单独覆盖
    # （prev_dashboard_age_h），不能算进 PRODUCERS 的覆盖缺口。
    SELF_NAMES = {"自动化巡检 - 每天 21:30"}
    uncovered = sorted(cloud_active - tracked_names - SELF_NAMES)
    cloud_paused = sorted(n for n, v in cloud.items() if v.get("status") == "PAUSED")
    # 已跟踪但云端已删除的（僵尸跟踪项）
    stale = sorted(tracked_names - set(cloud.keys())
                   - {"AIWeeklyReview (Windows)"})
    disk_bad = [k for k in (ai["runtime"]["status"], ai["recycle"]["status"],
                            ai["wb_logs"]["status"])
                if k.startswith(("❌", "⚠️"))]
    # 2026-10-25 新增：Windows 计划任务漏监控检测。
    # 「注册了但没人看」的自动化 = 既没 run failed 记录、也没产出文件，
    # 两个视角都看不见 —— 最纯粹的静默失败。
    untracked_tasks = check_untracked_schtasks()

    lines = [
        "# 自动化运行状态面板",
        "",
        f"> 生成时间：{now.strftime('%Y-%m-%d %H:%M')}",
        f"> 自动生成，勿手改。下次刷新由「自动化巡检」定时任务负责。",
        "",
        "## 总览",
        "",
        f"| 指标 | 值 |",
        f"| --- | --- |",
        f"| 自动化总数 | {len(rows)} |",
        f"| 正常 / 待首跑 / 已暂停 | {len(ok)} |",
        f"| 异常（偏旧+超期） | {len(bad)} |",
        f"| 其中「人为暂停（非故障）」 | {len(paused_rows)} |",
        f"| 覆盖缺口（云端 ACTIVE 但未跟踪） | {len(uncovered)} |",
        f"| 其中「最新正常但历史漏跑」 | {len(gap_rows)} |",
        f"| 调度器记录到的失败（近 5 天） | {len(sched_fails)} |",
        f"| 调度器报成功但零产出 | {len(silent)} |",
        f"| 未被跟踪的 Windows 计划任务 | {len(untracked_tasks)} |",
        f"| 资料库文档总数 | {lib_docs if lib_docs >= 0 else '❌ 读取失败'}（根+工作流+归档） |",
        f"| 资料库近 7 天更新 | {len(recent_lib) if lib_docs >= 0 else '❌ 读取失败'} |",
        f"| Git 未提交文件 | {git['dirty_files']} |",
        f"| C 盘剩余 | {disk['free_gb']} GB（{disk['free_pct']}%） |",
        f"| AI 工具占用 | {ai['total_gb']} GB |",
        f"| Codex runtime 版本 | {ai['runtime']['count']} 个（{ai['runtime']['status']}） |",
        f"| 回收站未释放 | {ai['recycle']['size_gb']} GB（{ai['recycle']['status']}） |",
        f"| WorkBuddy 日志+trace | {round(ai['wb_logs']['total_gb'] * 1024)} MB（{ai['wb_logs']['status']}） |",
        "",
        "## 各自动化产出新鲜度",
        "",
        "| 自动化 | 约定周期 | 最近产出 | 状态 |",
        "| --- | --- | --- | --- |",
    ]
    for r in rows:
        exp = f"{r['expected_hours']}h" if r["expected_hours"] < 48 else \
              f"{r['expected_hours'] // 24}d"
        lines.append(f"| {r['automation']} | {exp} | {r['last_output']} | {r['status']} |")

    lines += [
        "",
        "## 异常清单",
        "",
    ]
    # 巡检自身静默：上一轮面板太久没刷新，说明「巡检自动化」自己漏跑了。
    # 不显式写出来的话，「面板旧」和「自动化正常」在结果里长得一模一样。
    # 2026-10-30 修正：**面板文件不是「巡检跑过」的可靠证据**——
    # 任何自动化都能把它覆盖掉（10-30 03:52「工作流自动追加」就顺手跑了一次脚本），
    # 于是「上一轮面板距今 0.3h，未中断」报了出来，而巡检自己从 10-28 起其实一直没跑。
    # 主判据换成调度器日志里巡检自己的 run start 间隔，面板年龄降级为辅助信息。
    if self_slots.get("checked"):
        gap_h = self_slots.get("gap_h", 0)
        missed_real = self_slots.get("missed_real", [])
        missed_ok = self_slots.get("missed_ok", [])
        if missed_real:
            slots = "、".join(x["slot"] for x in missed_real)
            lines.append(
                f"- ❌ **巡检自身漏跑**：调度点（每日 21:40）机器开着却没有巡检记录 —— "
                f"**{slots}**。巡检失效 = 所有自动化的异常都看不见，这是最高优先级故障。")
        elif gap_h > 48:
            lines.append(
                f"- ❌ **巡检自身静默**：上一次巡检在 {self_slots.get('prev_start')}，"
                f"距今 **{gap_h}h**（>48h）——「自动化巡检」本身漏跑了。")
        else:
            lines.append(
                f"- ✅ 巡检自身：上次 {self_slots.get('prev_start')} → 本次 "
                f"{self_slots.get('last_start')}，间隔 {gap_h}h，未漏跑。")
        if missed_ok:
            slots = "、".join(x["slot"] for x in missed_ok)
            lines.append(f"  - ℹ️ 其中 {slots} 机器关机（事件日志 6006）→ 合理漏跑，不算故障。")
        lines.append(
            f"  - ℹ️ 面板文件年龄 {prev_age_h}h（仅参考：面板会被其他自动化覆盖，不能当判据）。")
    elif prev_age_h is None:
        lines.append("- ❌ **巡检自身**：读不到上一轮面板生成时间，无法确认本脚本多久没跑。")
    elif prev_age_h > 48:
        lines.append(
            f"- ❌ **巡检自身静默**：上一轮面板距今 **{prev_age_h}h**（>48h）——"
            "「自动化巡检」自动化本身漏跑了，或机器当时未开机。")
    else:
        lines.append(f"- ✅ 巡检自身：上一轮面板距今 {prev_age_h}h，未中断。")

    # 2026-10-28 新增：巡检自身「上一轮崩了」检测。
    # 面板年龄 <48h 时上面那行照样报 ✅ —— 但脚本可能压根没跑完。
    # 这是比任何自动化漏跑都严重的问题：巡检失效 = 所有异常都看不见。
    if self_crash and self_crash.get("state") == "crashed":
        lines.append(
            f"- ❌ **巡检自身崩溃**：上一次巡检（{self_crash['last_start_local']}）"
            "**没有正常收尾**（调度器日志里有 run start、没有 run finished）——"
            "脚本中途抛异常，dashboard.md 未更新。"
            "⚠️ 面板年龄检测看不到这种情况（48h 内会误报 ✅），"
            "本条是唯一的发现途径。")
    elif self_crash and self_crash.get("state") == "running":
        lines.append(
            f"- 🔄 巡检自身：上一轮（{self_crash['last_start_local']}）仍在运行中"
            f"（{self_crash['age_min']:.0f} 分钟），尚未收尾。")

    if not bad:
        lines.append("无异常。所有自动化产出新鲜度都在预期范围内。")
    else:
        for r in bad:
            extra = f"，已 {r['age_hours']}h 未产出" if r["age_hours"] else ""
            lines.append(f"- {r['status']} **{r['automation']}**：{r['last_output']}{extra}")

    # 2026-10-27 新增：人为暂停的自动化单列——它**不是故障**，
    # 但必须可见，否则（a）不知道哪些是被主动关掉的，
    # （b）漏跑检测会把「已暂停」和「该跑没跑」混为一谈。
    if paused_rows:
        lines.append("")
        lines.append("### ⏸ 人为暂停的自动化（非故障，不需补救）")
        lines.append("")
        lines.append("> 这些自动化在云端状态是 `PAUSED`——**明哥主动暂停**。"
                     "调度器不会 dispatch 它们，产出停更是预期行为。")
        lines.append("> ⚠️ 反过来：如果明哥以为某个自动化还在跑、实际已被暂停，"
                     "那才是真问题，会在下面的「覆盖缺口」里体现。")
        lines.append("")
        lines.append("| 自动化 | 最近产出 | 暂停时间 | 调度规则 |")
        lines.append("| --- | --- | --- | --- |")
        for p in paused_rows:
            lines.append(f"| {p['automation']} | {p['last_output']} | "
                         f"{p['paused_at'] or ''} | {p['rrule'] or ''} |")
        lines.append("")

    # 2026-10-27 新增：覆盖缺口——云端 ACTIVE 但脚本没跟踪的自动化。
    # 这是「漏监控」，比误报危险：面板会一直显示一切正常。
    if uncovered:
        lines.append("")
        lines.append("### ❗ 覆盖缺口：云端 ACTIVE 但巡检未跟踪")
        lines.append("")
        lines.append("> 这些自动化在云端是 `ACTIVE`，但脚本的跟踪名单里没有它们 —— "
                     "**它们出问题时巡检永远看不见**。")
        lines.append("")
        for u in uncovered:
            lines.append(f"- **{u}** — 需要在 `PRODUCERS` 里补上产出目录与周期。")
        lines.append("")

    if stale:
        lines.append("")
        lines.append("### ⚠️ 僵尸跟踪项：脚本在跟，但云端已不存在")
        lines.append("")
        for s in stale:
            lines.append(f"- **{s}** — 云端已无此自动化，建议从跟踪名单移除。")
        lines.append("")


    # 2026-10-23 新增：单独列出「漏跑日」——这类问题原本被「最新产出新鲜」掩盖。
    # 10-19「每日 AI 新闻推送」就是这么被藏了 4 天：当天 run failed，之后补跑成功，
    # 面板就一直是 ✅。最近产出新鲜 ≠ 每天都跑到了。
    if gap_rows:
        lines.append("")
        lines.append("### ⚠️ 漏跑日（最近产出虽新，但中间有该跑没跑的日子）")
        lines.append("")
        for r in gap_rows:
            lines.append(
                f"- **{r['automation']}**：漏跑 {', '.join(r['missed_days'])}"
                f"（最近一次产出 {r['last_output'].split('；')[0]}，"
                "状态看似正常，实则中间有缺口）")
        lines.append("")
        lines.append("> 只报「机器开机、到点、却零产出」的日子；关机日已排除。")

    if lib_error:
        lines.append(
            f"- ❌ **资料库读取失败**：{lib_error} —— "
            "本次「资料库文档总数 / 近 7 天更新」不可信，"
            "请勿据此判断资料库是否丢文档（是读取姿势失败，不是数据没了）。")

    # 2026-10-23 新增：调度器视角的失败记录。
    # 产出新鲜度只能看出「最近有没有产出」，看不出「中间有没有跑失败过」。
    # 两者合起来才是完整的失败视图。
    if sched_fails:
        lines.append("")
        lines.append("### ⚠️ 调度器记录的失败（近 5 天）")
        lines.append("")
        lines.append("> 这部分**不看产出文件**，直接读调度器日志。"
                     "有些失败后面被补跑掩盖了，产出新鲜度完全看不出来。")
        lines.append("")
        lines.append("| 时间 | 自动化 | 类型 | 原因 | 与产出对账 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for f in sched_fails:
            extra = ""
            if f.get("reconcile"):
                extra = f["reconcile"]
                if f.get("output_at"):
                    extra += f"（产出 {f['output_at']}）"
            lines.append(f"| {f['ts']} | {f['name'] or f['id']} | {f['code']} | "
                         f"{f['reason']} | {extra} |")
        lines.append("")
        lines.append("> `[CANCELLED] unknown_interrupted` = 任务跑到一半被打断（常见于"
                     "机器关机 / 会话中断 / LLM 网关异常）；`[UNKNOWN] refusal` = LLM 接口返回 400。")
        lines.append("")
        lines.append("> 2026-10-30 新增「与产出对账」列：调度器说失败、但产出文件已落盘，"
                     "说明是**做完但收尾失败**（不重跑）；失败后确实零产出才是**真失败**（要重跑）。"
                     "两个视角互相矛盾时以这一列为准。")

    # 2026-10-24 新增：调度器说「成功」但产出不存在。
    # 这是最隐蔽的一类——失败视角看不见（没报错），产出视角也看不见（压根没文件）。
    if silent:
        lines.append("")
        lines.append("### ❗ 调度器报「成功」但零产出（近 30 天）")
        lines.append("")
        lines.append("> 调度器日志明确写了 `run finished ... success=true`（没报错），"
                     "但当天找不到任何产出文件。**失败视角看不见，产出视角也看不见。**")
        lines.append("")
        lines.append("| 自动化 | 日期 | 实际运行时刻 | 判定 |")
        lines.append("| --- | --- | --- | --- |")
        for s in silent:
            lines.append(f"| {s['automation']} | {s['date']} | {s.get('run_at', '')} | 真失败（调度器报成功但无产出） |")
        lines.append("")
        lines.append("> 已排除关机日；已跨当前工作区 + 历史工作区两处查找。")

    # 2026-10-25 新增：未被跟踪的 Windows 计划任务。
    if untracked_tasks:
        lines.append("")
        lines.append("### ❗ 未被巡检跟踪的 Windows 计划任务")
        lines.append("")
        lines.append("> 这些任务**注册在本机任务计划里**，但不在巡检脚本的跟踪名单上。"
                     "没有 run failed 记录，也不在任何产出目录里 —— "
                     "**失败视角和产出视角都看不见**，属于最彻底的静默失败。")
        lines.append("")
        lines.append("| 任务名 | 上次运行 | 上次结果 | 下次运行 | 状态 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for t in untracked_tasks:
            st = "❌ 从未运行过" if t.get("never_ran") else "⚠️ 有过运行但未跟踪"
            if t.get("script_missing"):
                st = f"❌ **脚本不存在**（{t.get('script_ref', '')}）"
            lines.append(f"| {t['name']} | {t.get('last_run', '')} | "
                         f"{t.get('last_result', '')} | {t.get('next_run', '')} | {st} |")
        lines.append("")
        lines.append("> 处理方式：确认它是否还需要；要就纳入跟踪，不要就注销任务。")

    for label, item in (("Codex runtime 堆积", ai["runtime"]),
                        ("回收站未释放", ai["recycle"]),
                        ("WorkBuddy 日志增长", ai["wb_logs"])):
        if item["status"].startswith(("❌", "⚠️")):
            lines.append(f"- {item['status']} **{label}**：{item['note']}")
    lines += [
        "",
        "## 磁盘",
        "",
        f"- C 盘：已用 {disk['used_gb']} / {disk['total_gb']} GB，剩余 **{disk['free_gb']} GB**（{disk['free_pct']}%）",
        "",
        "### AI 工具在 C 盘的占用",
        "",
        "> 任务产物在 D 盘，这里放的是 Codex / WorkBuddy 的运行时、日志、缓存和状态。",
        "",
        "| 目录 | 占用 |",
        "| --- | --- |",
    ]
    for name, gb in sorted(ai["dirs"].items(), key=lambda x: -x[1]):
        lines.append(f"| `{name}` | {gb} GB |")
    lines.append(f"| **合计** | **{ai['total_gb']} GB** |")

    rt = ai["runtime"]
    lines += [
        "",
        f"#### Codex runtime 版本检查 · {rt['status']}",
        "",
        "- 路径：`AppData\\Local\\OpenAI\\Codex\\runtimes\\cua_node`",
        f"- 版本数：**{rt['count']}** 个，占 {rt['total_gb']} GB —— {rt['note']}",
    ]
    if rt["versions"]:
        lines += ["", "| 版本 | 大小 | 日期 |", "| --- | --- | --- |"]
        for v in rt["versions"]:
            lines.append(f"| `{v['name']}` | {v['mb']} MB | {v['date']} |")
        if rt["reclaimable_gb"] > 0:
            lines += [
                "",
                f"> 可回收 **{rt['reclaimable_gb']} GB**（保留最新版本）。"
                "⚠️ 删除后**必须清空回收站**，否则空间不释放。",
            ]
    lines += [
        "",
        f"#### 回收站 · {ai['recycle']['status']}",
        "",
        f"- 未释放空间：**{ai['recycle']['size_gb']} GB**"
        + ("　—— 无残留，删除已真正释放空间。" if ai['recycle']['status'].startswith('✅')
           else f"　—— {ai['recycle']['note']}"),
        "",
        f"#### WorkBuddy 自身日志增长 · {ai['wb_logs']['status']}",
        "",
        "> 这是 WorkBuddy 程序自动写的运行记录，会无限增长。Codex 相关的目录**不要动**。",
        "",
        "| 目录 | 当前 | 状态 |",
        "| --- | --- | --- |",
    ]
    for k, v in ai["wb_logs"]["detail"].items():
        lines.append(f"| `.workbuddy\\{k}` | {v['note']} | {v['status']} |")
    lines += [
        f"| **合计** | **{round(ai['wb_logs']['total_gb'] * 1024)} MB** | |",
        "",
        "> 清法：只保留最近 2–3 天，更早的移到同盘隔离区或直接删（删完记得清空回收站）。",
        "",
        "## 版本控制",
        "",
        f"- 最近提交：`{git['last_commit'] or '无'}`",
        f"- 未提交改动：{git['dirty_files'] if git['dirty_files'] is not None else '未知（git 不可用）'} 个文件",
        "",
        "---",
        "",
        "## 怎么读这个面板",
        "",
        "| 标记 | 含义 | 要不要管 |",
        "| --- | --- | --- |",
        "| ✅ 正常 | 产出在预期周期内 | 不用管 |",
        "| ⏳ 待首跑 | 新建自动化，还没到第一次执行时间 | 不用管 |",
        "| ⏸ 已暂停 | 云端状态 PAUSED，明哥主动关掉 | **不用管**（非故障） |",
        "| ⚠️ 偏旧 | 超过约定周期但未到 2 倍 | 留意，可能漏跑了一次 |",
        "| ❌ 超期 | 超过约定周期 2 倍 | **要查**：看资料库对应文件夹有没有新文档 |",
        "",
        "> 本面板只反映本地文件新鲜度。直接写资料库的自动化，本地可能无文件，",
        "> 这种以 ❌ 超期为准（说明确实很久没产出）。",
        "",
        "> **2026-10-27 起**：面板会读云端真实状态（`workbuddy.db` 的 `automations` 表）。",
        "> 人为 `PAUSED` 的自动化标 ⏸ 且不计入异常——明哥主动关掉的东西，",
        "> 产出停更是预期行为，不该被当成漏跑去「补救」。",
        "",
    ]

    STATUS_DIR.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    DASHBOARD.write_text(content, encoding="utf-8")
    STATUS_JSON.write_text(json.dumps({
        "ts": now.isoformat(timespec="seconds"),
        "rows": rows,
        "disk": disk,
        "git": git,
        "library_docs": lib_docs,
        "library_recent_7d": len(recent_lib),
        "ai_disk": ai,
        # 2026-10-23 新增：调度器失败记录进 JSON，方便外部脚本/告警消费。
        # 「最新产出新鲜」不等于「每天都跑到了」——缺口就藏在 rows[].missed_days 里。
        "scheduler_failures": sched_fails,
        "success_but_no_output": silent,
        "missed_days_total": len(gap_rows),
        # 2026-10-27 新增：人为暂停 / 覆盖缺口
        "paused_automations": paused_rows,
        "coverage_gaps": uncovered,
        "stale_tracked": stale,
        "cloud_automation_status": cloud,
        # 2026-10-30 新增：巡检自己漏跑的调度点（权威来源是调度器日志，不是面板文件）。
        "self_missed_slots": self_slots,
        "machine_uptime_windows": [
            [b.strftime("%Y-%m-%d %H:%M"), e.strftime("%Y-%m-%d %H:%M")]
            for b, e in machine_uptime_windows()
        ],
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    if not args.quiet:
        # 2026-10-21 21:30 修正：面板里含 ⚠ ✅ ❌ 等字符，
        # Windows 控制台默认 GBK 编不了 → print 直接抛 UnicodeEncodeError
        # → 脚本 exit 1，看起来像「巡检失败」，实际文件已写好。
        # 用 errors="replace" 输出，绝不让打印失败污染退出码。
        try:
            print(content)
        except UnicodeEncodeError:
            import sys
            enc = getattr(sys.stdout, "encoding", None) or "utf-8"
            sys.stdout.write(content.encode(enc, errors="replace").decode(enc, errors="replace"))
            sys.stdout.write("\n")
    self_crashed = bool(self_crash and self_crash.get("state") == "crashed")
    print(f"\n[SUMMARY] 正常 {sum(1 for r in rows if r['status'].startswith('✅'))}"
          f" / 已暂停 {len(paused_rows)} / 异常 {len(bad)} / 磁盘问题 {len(disk_bad)}"
          + (f" / **自身崩溃 {1}**" if self_crashed else ""))
    # 2026-10-28：自身崩溃也算失败退出码 —— 巡检失效必须能被调用方看见
    return 1 if (bad or disk_bad or untracked_tasks or uncovered or self_crashed) else 0


if __name__ == "__main__":
    sys.exit(main())
