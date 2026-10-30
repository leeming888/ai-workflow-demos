# 配置模板 —— 复制为 config.py 后填入自己的值
#
# 用法：
#   1. cp config.example.py config.py
#   2. 编辑 config.py 填入你自己的路径 / ID
#   3. 运行 check_automations.py
#
# ⚠️ config.py 已在 .gitignore 中，不要提交

# ── 你要监控的主工作区 ──────────────────────────────────────────────────
# 这个目录下面应该有 reports/ 和你的产出文件
WORKSPACE = r"D:\your-workspace"

# 额外要监控的工作区（可选，但强烈建议加）
#
# ⭐ 这一条是本系统最有价值的检查项。
# 真实事故：自动化跑在 A 工作区，但产出落在 B 工作区，
# 监控脚本只扫 A → 云端显示一切正常，实际已经静默失败 2 周。
# 凡是"自动化和产出不在同一个目录"的，都必须在这里补上。
EXTRA_WORKSPACES = [
    r"D:\your-workspace-2",
    r"D:\your-workspace-3",
]

# ── 运行环境 ────────────────────────────────────────────────────────────
PYTHON = r"C:\Users\yourname\.workbuddy\binaries\python\versions\3.13.12\python.exe"
SKILL = r"D:\workbuddy\resources\app.asar.unpacked\resources\plugins\workbuddy-builtin\skills\library"

# ── 云端知识库 ID（可选，不填就跳过云端对账）────────────────────────────
SPACE_ID = "your-space-id"
ROOT_ID = "your-root-folder-id"
ARCHIVE_ID = "your-archive-folder-id"
