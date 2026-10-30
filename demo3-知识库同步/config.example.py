# 配置模板 —— 复制为 config.py 后填入自己的值
#
# 用法：
#   1. cp config.example.py config.py
#   2. 编辑 config.py 填入你自己的路径 / ID
#   3. 运行 sync_to_library.py
#
# ⚠️ config.py 已在 .gitignore 中，不要提交

# ── 本地路径 ────────────────────────────────────────────────────────────
# 你要同步的 markdown 目录（通常是你的工作区 / 知识库目录）
LOCAL_ROOT = r"D:\your-workspace\docs"

# 归档目录：本地 md 与云端同名时，本地旧版先放这里（可留空表示不归档）
LOCAL_ARCHIVE = r"D:\your-workspace\_历史版本"

# ── 云端知识库 ID ──────────────────────────────────────────────────────
# 从你的平台空间设置里拿。示例格式见下（请换成你自己的）
SPACE_ID = "your-space-id"
ROOT_ID = "your-root-folder-id"        # 主目录：同名文档建在这里
ARCHIVE_ID = "your-archive-folder-id"  # 历史版本目录：同名时旧版移到这里
HISTORY_ID = "your-history-folder-id"  # （可选）二级历史

# ── 运行环境 ────────────────────────────────────────────────────────────
# 你的 python 解释器
PYTHON = r"C:\Users\yourname\.workbuddy\binaries\python\versions\3.13.12\python.exe"

# 平台 API 脚本路径（负责建节点/移动节点）
SKILL = r"D:\workbuddy\resources\app.asar.unpacked\resources\plugins\workbuddy-builtin\skills\library"

# ── 敏感扫描（强烈建议保留）─────────────────────────────────────────────
# 上传前先过一遍敏感信息闸门，命中就中断
SENSITIVE_SCAN = True
SCAN_SCRIPT = r"D:\your-workspace\tools\scan_sensitive.py"
