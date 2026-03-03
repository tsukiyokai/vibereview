#!/usr/bin/env python3
"""
代码审查工具：支持 GitCode PR 审查 和 本地文件审查，调用 Claude Code vibe-review skill。

  支持跨仓库审查：通过 --repo 参数指定目标仓库（默认 hcomm-dev）。
  --repo 同时决定本地仓库路径（脚本所在 jbs 目录的同级目录）和 GitCode API 目标。

  PR 审查结果保存到 vibereview/log/{owner}/{repo}/by_pr/ 目录。
  本地文件审查结果保存到 vibereview/log/{owner}/{repo}/by_file/ 目录。

用法:
  1. 设置 GitCode 个人访问令牌 (PR 审查需要):
     export GITCODE_TOKEN=your_personal_access_token

  2. 审查最近 N 个 open PR (默认 3，默认仓库 hcomm-dev):
     python3 jbs/vibereview/ai_reviewer.py
     python3 jbs/vibereview/ai_reviewer.py --count 5

  3. 审查指定 PR:
     python3 jbs/vibereview/ai_reviewer.py --pr 1150
     python3 jbs/vibereview/ai_reviewer.py --pr 1150 1144 1143

  4. 跨仓库审查（--repo 指定目标仓库）:
     python3 jbs/vibereview/ai_reviewer.py --repo hcomm --pr 100
     python3 jbs/vibereview/ai_reviewer.py --repo hcomm-dev --count 3
     python3 jbs/vibereview/ai_reviewer.py --repo hcomm --file src/xxx.cpp

  5. 审查指定用户的 open PR:
     python3 jbs/vibereview/ai_reviewer.py --author lilin_137           # 最近 3 个
     python3 jbs/vibereview/ai_reviewer.py --author lilin_137 -n 0      # 全部

  6. 审查并保存到本地:
     python3 jbs/vibereview/ai_reviewer.py --pr 1150 --save

  7. 审查并发布评论到 GitCode PR:
     python3 jbs/vibereview/ai_reviewer.py --pr 1150 --comment

  8. 审查已合并的 PR:
     python3 jbs/vibereview/ai_reviewer.py --state merged --count 3

  9. 审查本地文件:
     python3 jbs/vibereview/ai_reviewer.py --file src/xxx.cpp
     python3 jbs/vibereview/ai_reviewer.py --file src/a.cpp src/b.h --save

 10. 强制重新审查 (忽略已审查过最新提交的判断):
     python3 jbs/vibereview/ai_reviewer.py --pr 1150 --comment --force

 11. 查看审查采纳率统计:
     python3 jbs/vibereview/ai_reviewer.py --stats
     python3 jbs/vibereview/ai_reviewer.py --stats --days 90

 12. 手动追踪审查结果:
     python3 jbs/vibereview/ai_reviewer.py --track
     python3 jbs/vibereview/ai_reviewer.py --track --pr 1150

 13. 导入历史审查数据:
     python3 jbs/vibereview/ai_reviewer.py --import-logs

  选项可组合：--author 筛选用户, --count 限制数量, --state 筛选状态, --dry-run 只拉取不审查。
  --repo 默认 hcomm-dev，对应本地 ~/repo/hcomm-dev/ 和 GitCode cann/hcomm-dev。
  --pr 为精确模式，不受 --count/--author/--state 影响。
  --file 为本地文件模式，不需要 GitCode 令牌。
  默认审查结果输出到终端，--save 保存本地文件，--comment 发布 PR 评论。
"""

import argparse
import concurrent.futures
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

# ======================== 配置 ========================
GITCODE_API_BASE = "https://api.gitcode.com/api/v5"
OWNER = "cann"
# REPO 和 REPO_URL 改为运行时从 --repo 参数确定，见 RepoConfig
SCRIPT_DIR = Path(__file__).resolve().parent
REPOS_ROOT = SCRIPT_DIR.parent.parent  # jbs 的父目录，即 ~/repo/
# 单个 PR diff 最大字符数（防止超出 Claude 上下文窗口）
MAX_DIFF_CHARS = 80000
# vibe-review skill 路径
SKILL_MD_PATH = Path.home() / ".claude" / "skills" / "vibe-review" / "SKILL.md"
# 单条 PR 评论最大字符数（GitCode 限制）
MAX_COMMENT_CHARS = 60000
# claude -p 最大 agentic 回合数（工具调用 + 文本输出）
# 质量优先：充足的回合数确保 Claude 有空间进行深度分析和工具验证
MAX_CLAUDE_TURNS = 40
# 美元兑人民币汇率（用于费用显示，近似值）
USD_TO_CNY = 7.25
# 模型价格表（$/MTok，来源：platform.claude.com/docs/en/about-claude/pricing）
# cache_write 为 5 分钟缓存写入价格（1.25× 输入价）
# cache_read 为缓存命中价格（0.1× 输入价）
MODEL_PRICING: dict[str, dict[str, float]] = {
    "claude-opus-4-6":   {"input": 5,  "output": 25, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-5":   {"input": 5,  "output": 25, "cache_write": 6.25, "cache_read": 0.50},
    "claude-opus-4-1":   {"input": 15, "output": 75, "cache_write": 18.75, "cache_read": 1.50},
    "claude-sonnet-4-6": {"input": 3,  "output": 15, "cache_write": 3.75, "cache_read": 0.30},
    "claude-sonnet-4-5": {"input": 3,  "output": 15, "cache_write": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5":  {"input": 1,  "output": 5,  "cache_write": 1.25, "cache_read": 0.10},
}
# AI 评论标识（用于识别和清理旧评论）
AI_REVIEW_MARKER = "## AI Code Review"
# 行内评论标识（附加在每条行内评论 body 末尾，用于识别和清理）
AI_INLINE_MARKER = "<!-- AI_CODE_REVIEW -->"
# 并发审查上限，避免 API 限流
MAX_PARALLEL_REVIEWS = 2
# 目录审查文件数上限
MAX_DIR_FILES = 20
# 审查结果最短有效长度（低于此值视为无效输出，触发重试）
MIN_REVIEW_CHARS = 500
# 小组人员名单（姓名 工号 gitcode 账号，每行一人，首行为标题）
TEAM_FILE = SCRIPT_DIR / "team.txt"
# 审查结果日志目录
LOG_DIR = SCRIPT_DIR / "log"
# 审查追踪数据库（存活性检测 + 采纳率统计）
TRACKING_DB = LOG_DIR / "review_tracking.db"

# 文件审查工具：允许 Claude 读取本地文件和搜索代码
FILE_REVIEW_TOOLS = ["Read", "Grep", "Glob", "Skill"]
# PR 审查工具：在文件审查工具基础上允许只读 git 命令
# Claude 可能用 git -C <path> 在非 cwd 仓库执行，需同时覆盖直接和 -C 两种形式
PR_REVIEW_TOOLS = [
    "Read", "Grep", "Glob", "Skill",
    "Bash(git show *)", "Bash(git log *)", "Bash(git diff *)", "Bash(git blame *)",
    "Bash(git -C * show *)", "Bash(git -C * log *)", "Bash(git -C * diff *)", "Bash(git -C * blame *)",
]


@dataclass
class RepoConfig:
    """目标仓库配置（从 --repo 参数派生）。"""
    name: str       # "hcomm-dev"
    owner: str      # "cann"
    path: Path      # ~/repo/cann/hcomm-dev

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def url(self) -> str:
        return f"https://gitcode.com/{self.owner}/{self.name}"

    @property
    def api_prefix(self) -> str:
        return f"/repos/{self.owner}/{self.name}"

    @property
    def pr_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name / "by_pr"

    @property
    def file_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name / "by_file"

    @property
    def dir_log_dir(self) -> Path:
        return SCRIPT_DIR / "log" / self.owner / self.name / "by_dir"


def _migrate_legacy_logs(repo: RepoConfig) -> None:
    """一次性迁移旧的扁平 log 目录到按仓库分层的结构。"""
    for subdir in ("by_pr", "by_file", "by_dir"):
        old = LOG_DIR / subdir
        new = SCRIPT_DIR / "log" / repo.owner / repo.name / subdir
        if old.exists() and not new.exists():
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)


# ======================== 终端颜色 ========================
def _supports_color() -> bool:
    """检测终端是否支持 ANSI 颜色（遵循 no-color.org 标准）。"""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(sys.stdout, "isatty") and sys.stdout.isatty()


_USE_COLOR = _supports_color()


def _c(code: str, text: str) -> str:
    """应用 ANSI 颜色代码。"""
    return f"\033[{code}m{text}\033[0m" if _USE_COLOR else str(text)


def _dim(t: str) -> str:    return _c("2", t)
def _bold(t: str) -> str:   return _c("1", t)
def _red(t: str) -> str:    return _c("31", t)
def _green(t: str) -> str:  return _c("32", t)
def _yellow(t: str) -> str: return _c("33", t)
def _blue(t: str) -> str:   return _c("34", t)
def _cyan(t: str) -> str:   return _c("36", t)


def _sev(severity: str) -> str:
    """为严重级别添加颜色。"""
    if "严重" in severity:
        return _red(severity)
    if "一般" in severity:
        return _yellow(severity)
    if "建议" in severity:
        return _blue(severity)
    return severity


def _file_link(path) -> str:
    """用 OSC 8 生成终端可点击的文件超链接（WezTerm/iTerm2/等支持）。"""
    p = str(path)
    if _USE_COLOR:
        return f"\033]8;;file://{p}\033\\{p}\033]8;;\033\\"
    return p

def _ok(msg: str) -> str:   return f"{_green('✓')} {msg}"
def _fail(msg: str) -> str:  return f"{_red('✗')} {msg}"
def _warn(msg: str) -> str:  return f"{_yellow('⚠')} {msg}"
def _skip(msg: str) -> str:  return f"{_dim('○')} {msg}"

def _now() -> str:           return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
def _fmt_secs(s: float) -> str: return f"{s:.1f}s" if s < 60 else f"{int(s)//60}m {int(s)%60}s"


# ======================== 审查统计 ========================
@dataclass
class ReviewStats:
    """单次审查的 token 消耗和耗时统计。"""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    cost_usd: float = 0.0       # Claude Code 报告的费用
    calc_cost_usd: float = 0.0  # 基于官方价格表独立计算的费用
    permission_denials: list[str] = field(default_factory=list)  # 被拒绝的工具调用描述
    duration_ms: int = 0
    num_turns: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def best_cost(self) -> float:
        """优先使用 Claude Code 报告的费用，兜底使用独立计算值。"""
        return self.cost_usd if self.cost_usd > 0 else self.calc_cost_usd

    def fmt(self) -> str:
        """格式化为一行摘要。"""
        parts = []
        if self.input_tokens or self.output_tokens:
            parts.append(f"输入 {self.input_tokens:,} / 输出 {self.output_tokens:,} tokens")
        if self.cache_creation_tokens:
            parts.append(f"缓存写入 {self.cache_creation_tokens:,}")
        if self.cache_read_tokens:
            parts.append(f"缓存读取 {self.cache_read_tokens:,}")
        cost = self.best_cost
        if cost > 0:
            parts.append(f"${cost:.4f} / ¥{cost * USD_TO_CNY:.4f}")
        if self.num_turns > 0:
            parts.append(f"{self.num_turns} 回合")
        if self.duration_ms > 0:
            parts.append(f"{_fmt_secs(self.duration_ms / 1000)} (API)")
        return " | ".join(parts) if parts else "无统计数据"


class _DirectOutput:
    """直接输出到 stdout 的流适配器（顺序模式使用），接口兼容 StringIO。"""

    def write(self, s: str) -> int:
        sys.stdout.write(s)
        sys.stdout.flush()
        return len(s)

    def getvalue(self) -> str:
        return ""


@dataclass
class InlineFinding:
    """单个行内审查发现。"""
    id: int
    severity: str       # "严重" / "一般" / "建议"
    title: str
    file: str
    line: int
    body: str


def _extract_findings_for_inline(
    review_text: str, files: list[dict], buf: io.StringIO,
    file_position_maps: dict[str, dict[int, tuple[int, bool]]] | None = None,
) -> list[InlineFinding]:
    """从审查报告中提取发现并定位到 diff 中的精确位置。

    纯文本解析 + diff 搜索，无需额外 API 调用（零成本、无超时风险）。
    定位策略：
    1. 从 '位置:' 行提取明确的行号（如 file.cc:395），在 diff 中验证
    2. 从代码片段在 diff 的所有可见行中搜索匹配
    3. 从函数名/标识符在 diff 的所有可见行中搜索
    3.5. 从位置描述中提取标识符搜索
    """
    # 构建文件名 → raw diff 映射
    file_diffs: dict[str, str] = {}
    for f in files:
        fname = get_filename(f)
        raw_diff = get_file_diff(f)
        if raw_diff:
            file_diffs[fname] = raw_diff

    # 按 ### #N [...] 分割审查发现
    finding_pattern = r'### #(\d+)\s+\[([^\]]+)\]\s+(.*?)(?=\n---\s*$|\n### #\d|\Z)'
    matches = list(re.finditer(finding_pattern, review_text, re.DOTALL | re.MULTILINE))

    if not matches:
        buf.write(f"  {_warn('未能从审查报告中解析到发现')}\n")
        return []

    buf.write(f"  解析审查报告：发现 {_bold(str(len(matches)))} 个问题\n")

    findings: list[InlineFinding] = []
    for m in matches:
        fid = int(m.group(1))
        severity = m.group(2).strip()
        content = m.group(3)

        # 提取 title（第一行，去掉尾部 "— 描述"）
        title = content.split("\n")[0].strip()
        title = re.sub(r"\s*—\s*.*$", "", title)
        if len(title) > 80:
            title = title[:77] + "..."

        # 提取文件路径（优先 backtick 格式，兼容无 backtick 格式）
        # 同时匹配中文全角冒号 `：` 和 ASCII 半角冒号 `:`（SKILL.md 模板用全角，prompt 示例用半角）
        loc_match = re.search(r"位置[：:]\s*`([^`]+)`", content)
        if not loc_match:
            # 兼容无 backtick 格式："- 位置：file.cc:123"
            loc_match = re.search(r"位置[：:]\s*(\S+)", content)
        if not loc_match:
            buf.write(f"  {_skip(f'#{fid}: 未找到位置信息')}\n")
            continue
        location = loc_match.group(1)

        # 解析 file:line 格式（如 "file.cc:395, 427, 457" 或 "file.cc:31-33"）
        file_path = location
        explicit_lines: list[int] = []
        line_match = re.match(r"^(.+?):(\d[\d,\s\-]*)$", location)
        if line_match:
            file_path = line_match.group(1)
            # 解析逗号分隔的行号/范围，取每组的起始行号
            # "66-70, 94-98" → [66, 94]; "395, 427" → [395, 427]
            for part in re.split(r',\s*', line_match.group(2).strip()):
                nums = re.findall(r'\d+', part)
                if nums:
                    explicit_lines.append(int(nums[0]))

        # 匹配到 diff 中的实际文件名
        matched_file = _match_diff_filename(file_path, file_diffs)
        if not matched_file:
            buf.write(f"  {_skip(f'#{fid}: 文件不在 diff 中：{file_path}')}\n")
            continue

        raw_diff = file_diffs[matched_file]

        # 构建行内评论 body
        body = _build_inline_body(content)

        # 定位策略
        target_lines: list[int] = []

        strategy = ""
        if explicit_lines:
            # 策略 1：使用「位置」: 中的明确行号，在 diff 中验证
            if file_position_maps and matched_file in file_position_maps:
                pos_map = file_position_maps[matched_file]
            else:
                pos_map = _build_diff_position_map(raw_diff)
            # 第一轮：精确匹配（行号必须在 diff 中）
            for ln in explicit_lines:
                if ln in pos_map:
                    target_lines.append(ln)
                    strategy = f"策略 1-精确（行 {ln}）"
                    break
            # 第二轮：少量行号时允许偏移匹配，多位置发现不偏移（避免匹配无关行）
            if not target_lines and len(explicit_lines) <= 3:
                for ln in explicit_lines:
                    adjusted = _find_nearest_diff_line(ln, pos_map)
                    if adjusted is not None:
                        target_lines.append(adjusted)
                        strategy = f"策略 1-偏移（行{ln}→{adjusted})"
                        break

        if not target_lines:
            # 策略 2：从代码片段搜索（兼容多种格式，搜索所有可见行）
            code_lines = _extract_code_snippet(content)
            for code_line in code_lines:
                if len(code_line) < 15:
                    continue
                # 处理 ... 截断：取 ... 之前的部分
                search_str = re.split(r"\.\.\.", code_line)[0].rstrip('" ;,')
                if len(search_str) < 15:
                    search_str = code_line  # ... 在开头或太短，用全行
                found = _search_in_diff_all_lines(search_str, raw_diff)
                if found is not None:
                    target_lines.append(found)
                    strategy = f"策略 2-代码片段（行 {found}）"
                    break
                # 回退：用前 40 字符搜索
                if len(search_str) > 40:
                    found = _search_in_diff_all_lines(search_str[:40], raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 2-代码片段前 40（行 {found}）"
                        break

        if not target_lines:
            # 策略 3：从函数名搜索（多种格式）
            func_match = re.search(r"`(\w+(?:::\w+)*)\s*\(\)`", content)
            if not func_match:
                # 不带反引号但带 () 的函数名
                func_match = re.search(r"(?<!\w)(\w+(?:::\w+)*)\s*\(\)(?!\w)", content)
            if not func_match:
                # 位置行 "— `FuncName` 函数" 格式
                func_match = re.search(r"—\s*`(\w+(?:::\w+)*)`", content)
            if func_match:
                func_name = func_match.group(1)
                found = _search_in_diff_all_lines(func_name, raw_diff)
                if found is not None:
                    target_lines.append(found)
                    strategy = f"策略 3-函数名 '{func_name}'（行 {found}）"
                elif "::" in func_name:
                    # 回退：仅搜索方法名部分
                    method = func_name.split("::")[-1]
                    found = _search_in_diff_all_lines(method, raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 3-方法名 '{method}'（行 {found}）"

        if not target_lines:
            # 策略 3.5：从位置行描述中提取标识符搜索
            loc_desc = re.search(r"位置[：:].*?—\s*(.*?)$", content, re.MULTILINE)
            if loc_desc:
                identifiers = re.findall(r"`(\w+(?:::\w+)*)`", loc_desc.group(1))
                for ident in identifiers:
                    found = _search_in_diff_all_lines(ident, raw_diff)
                    if found is not None:
                        target_lines.append(found)
                        strategy = f"策略 3.5-标识符 '{ident}'（行 {found}）"
                        break
                    if "::" in ident:
                        method = ident.split("::")[-1]
                        found = _search_in_diff_all_lines(method, raw_diff)
                        if found is not None:
                            target_lines.append(found)
                            strategy = f"策略 3.5-方法名 '{method}'（行 {found}）"
                            break

        if not target_lines:
            buf.write(f"  {_skip(f'#{fid}: 无法在 diff 中定位（位置：{location}）')}\n")
            continue

        buf.write(f"  {_green('→')} #{fid} [{_sev(severity)}] {matched_file}:{target_lines[0]} {_dim(f'({strategy})')}\n")
        for ln in target_lines:
            findings.append(InlineFinding(
                id=fid, severity=severity, title=title,
                file=matched_file, line=ln, body=body,
            ))

    buf.write(f"  定位完成：{_green(str(len(findings)))} 条发现已定位\n")
    return findings


def _match_diff_filename(file_path: str, file_diffs: dict[str, str]) -> str | None:
    """将审查报告中的文件路径匹配到 diff 中的实际文件名。"""
    # 精确匹配
    if file_path in file_diffs:
        return file_path
    # 后缀匹配（审查报告可能使用简短路径）
    for fname in file_diffs:
        if fname.endswith(file_path) or file_path.endswith(fname):
            return fname
    # 文件名匹配
    basename = Path(file_path).name
    for fname in file_diffs:
        if Path(fname).name == basename:
            return fname
    return None



def _search_in_diff_all_lines(
    search_str: str, raw_diff: str, prefer_added: bool = True,
) -> int | None:
    """在 diff 的所有可见行（'+' 行和上下文行）中搜索字符串。

    优先返回 '+' 行的匹配，其次返回上下文行的匹配。
    """
    new_line = 0
    in_hunk = False
    first_added_match = None
    first_context_match = None

    for line in raw_diff.split("\n"):
        if not line and in_hunk:
            continue  # 跳过尾部空行（split 产物）
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            in_hunk = True
            continue

        if not in_hunk:
            continue

        if line.startswith("+"):
            new_line += 1
            if search_str in line and first_added_match is None:
                first_added_match = new_line
        elif line.startswith("-"):
            pass
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file" 等元数据行
        else:
            new_line += 1
            if search_str in line and first_context_match is None:
                first_context_match = new_line

    if prefer_added and first_added_match is not None:
        return first_added_match
    if first_context_match is not None:
        return first_context_match
    if first_added_match is not None:
        return first_added_match
    return None


def _extract_code_snippet(content: str) -> list[str]:
    """从审查发现内容中提取代码片段行，兼容多种格式。"""
    # 模式 0: "问题代码(...):" 后接围栏代码块 ```...```
    m = re.search(r"问题代码[^:：\n]*[：:]\s*\n```\w*\n(.*?)```", content, re.DOTALL)
    if m:
        return [l for l in m.group(1).split("\n") if l.strip()]

    # 模式 1: "问题代码(...):"\n\n 后接 4+ 空格缩进代码块
    m = re.search(r"问题代码[^:：\n]*[：:]\s*\n\n?((?:    .+\n?)+)", content)
    if m:
        return [l.strip() for l in m.group(1).split("\n") if l.strip()]

    # 模式 2: "问题描述(...):"\n 后续段落中的缩进代码块
    m = re.search(r"问题描述[^:：\n]*[：:]\s*\n(.*?)((?:\n    .+)+)", content, re.DOTALL)
    if m:
        return [l.strip() for l in m.group(2).split("\n") if l.strip()]

    # 模式 3: "以下代码..." 后的缩进代码块
    m = re.search(r"以下代码[^：:\n]*[：:]?\s*\n\n?((?:    .+\n?)+)", content)
    if m:
        return [l.strip() for l in m.group(1).split("\n") if l.strip()]

    # 模式 3.5: 通用围栏代码块回退 — 第一个 ```...``` 块
    # 安全检查：如果代码块前面出现了修复建议关键词，说明是修复代码，跳过
    m = re.search(r"```\w*\n(.*?)```", content, re.DOTALL)
    if m:
        before = content[:m.start()]
        if not re.search(r"修复建议|建议修改|建议改为|建议修复|Suggested\s+fix", before, re.IGNORECASE):
            return [l for l in m.group(1).split("\n") if l.strip()]

    # 模式 4: 通用回退 — 第一个连续 4 空格缩进块（至少 1 行）
    blocks = re.findall(r"(?:^    .+$\n?)+", content, re.MULTILINE)
    if blocks:
        return [l.strip() for l in blocks[0].split("\n") if l.strip()]

    return []


def _find_nearest_diff_line(
    target: int, pos_map: dict[int, tuple[int, bool]], max_offset: int = 5,
) -> int | None:
    """验证行号是否在 diff 中，优先匹配 '+' 行，其次上下文行。"""
    # 精确匹配 — 无论 '+' 行还是上下文行都接受
    if target in pos_map:
        return target

    # 附近搜索第一轮：优先 '+' 行
    for offset in range(1, max_offset + 1):
        for candidate in [target + offset, target - offset]:
            if candidate in pos_map:
                _, is_added = pos_map[candidate]
                if is_added:
                    return candidate

    # 附近搜索第二轮：接受上下文行
    for offset in range(1, max_offset + 1):
        for candidate in [target + offset, target - offset]:
            if candidate in pos_map:
                return candidate

    return None


def _build_inline_body(section_text: str) -> str:
    """从发现内容构建精简的行内评论 body（适合行内评论，≤500 字）。"""
    lines = section_text.split("\n")
    body_parts: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()
        # 围栏代码块内不做元数据过滤（避免代码中恰好含 "- 位置:" 等模式被误删）
        if stripped.startswith("```"):
            in_code_block = not in_code_block
        # 跳过元数据行（同时匹配中文全角冒号 `：` 和 ASCII 半角冒号 `:`）
        if not in_code_block and (
                re.match(r"- 位置[：:]", stripped) or re.match(r"- 规则[：:]", stripped) or
                re.match(r"- 置信度[：:]", stripped)):
            continue
        # 跳过标题行（第一行）
        if not body_parts and not stripped:
            continue
        if stripped:
            body_parts.append(line)
        elif body_parts:
            body_parts.append("")  # 保留段落间空行

    body = "\n".join(body_parts).strip()
    # 截断（行内评论保留足够空间展示代码片段和修复建议）
    if len(body) > 2000:
        body = body[:1997] + "..."
    return body


def _parse_json_output(raw: str) -> tuple[str, ReviewStats]:
    """解析 claude -p --output-format json 的输出，提取文本和统计信息。"""
    stats = ReviewStats()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return raw, stats  # JSON 解析失败，当作纯文本返回

    # 提取文本结果
    text = data.get("result", "")
    if "result" not in data and "type" not in data:
        # 兜底：JSON 中无 result 字段且非 Claude Code 结构化输出，尝试整体当文本
        text = raw

    # 提取权限拒绝信息（由调用者负责输出）
    for d in data.get("permission_denials", []):
        tool = d.get("tool_name", "?")
        cmd = d.get("tool_input", {}).get("command", "")
        desc = f"权限拒绝 {tool}: {cmd[:120]}" if cmd else f"权限拒绝 {tool}"
        stats.permission_denials.append(desc)

    # 提取费用和耗时
    stats.cost_usd = data.get("cost_usd", 0) or data.get("total_cost_usd", 0)
    stats.duration_ms = data.get("duration_ms", 0) or data.get("duration_api_ms", 0)
    stats.num_turns = data.get("num_turns", 0)

    # 提取 token 用量 —— 优先使用 modelUsage（会话级汇总，比 usage 更完整）
    # usage 只是最后一轮的快照，modelUsage 是跨所有轮次、所有模型的累计
    model_usage = data.get("modelUsage", {})
    if isinstance(model_usage, dict) and model_usage:
        for model_name, mu in model_usage.items():
            stats.input_tokens += mu.get("inputTokens", 0)
            stats.output_tokens += mu.get("outputTokens", 0)
            stats.cache_read_tokens += mu.get("cacheReadInputTokens", 0)
            stats.cache_creation_tokens += mu.get("cacheCreationInputTokens", 0)
            # 基于官方价格表独立计算费用
            prices = MODEL_PRICING.get(model_name)
            if prices:
                stats.calc_cost_usd += (
                    mu.get("inputTokens", 0) * prices["input"]
                    + mu.get("outputTokens", 0) * prices["output"]
                    + mu.get("cacheCreationInputTokens", 0) * prices["cache_write"]
                    + mu.get("cacheReadInputTokens", 0) * prices["cache_read"]
                ) / 1_000_000
    else:
        # 兜底：使用 usage（可能只是单轮数据）
        usage = data.get("usage", {})
        if isinstance(usage, dict):
            stats.input_tokens = usage.get("input_tokens", 0)
            stats.output_tokens = usage.get("output_tokens", 0)
            stats.cache_read_tokens = usage.get("cache_read_input_tokens", 0)
            stats.cache_creation_tokens = usage.get("cache_creation_input_tokens", 0)

    return text, stats


# ======================== GitCode API ========================
def _api_request(
    method: str, path: str, token: str,
    params: dict | None = None, body: dict | None = None,
) -> dict | list | None:
    """GitCode REST API 统一请求封装。

    返回 JSON 响应或 None（出错时）。DELETE 方法成功时返回空 dict。
    """
    if params is None:
        params = {}
    params["access_token"] = token
    url = f"{GITCODE_API_BASE}{path}?{urlencode(params)}"

    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")

    req = Request(url, data=data, method=method, headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        print(f"  {_fail(f'API {method} 失败：{path}')}")
        print(f"  {_dim(f'HTTP {e.code}: {resp_body[:300]}')}")
        return None
    except URLError as e:
        print(f"  {_fail(f'网络错误：{e.reason}')}")
        return None


def api_get(path: str, token: str, params: dict = None) -> dict | list | None:
    """调用 GitCode REST API（GET），返回 JSON 响应。"""
    return _api_request("GET", path, token, params=params)


def api_post(path: str, token: str, body: dict) -> dict | list | None:
    """调用 GitCode REST API（POST JSON），返回 JSON 响应。"""
    return _api_request("POST", path, token, body=body)


def api_post_form(path: str, token: str, fields: dict) -> dict | list | None:
    """调用 GitCode REST API（POST form-encoded），返回 JSON 响应。

    GitCode 部分 API（如行内评论）仅在 form-encoded 格式下正确处理
    path/position/commit_id 等字段，JSON 格式会被静默忽略。
    """
    fields["access_token"] = token
    url = f"{GITCODE_API_BASE}{path}"
    data = urlencode(fields).encode("utf-8")
    headers = {"Content-Type": "application/x-www-form-urlencoded",
               "Accept": "application/json"}
    req = Request(url, data=data, method="POST", headers=headers)
    try:
        with urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else {}
    except HTTPError as e:
        resp_body = e.read().decode("utf-8", errors="replace")
        print(f"  {_fail(f'API POST 失败：{path}')}")
        print(f"  {_dim(f'HTTP {e.code}: {resp_body[:300]}')}")
        return None
    except URLError as e:
        print(f"  {_fail(f'网络错误：{e.reason}')}")
        return None


def api_delete(path: str, token: str) -> bool:
    """调用 GitCode REST API（DELETE），返回是否成功。"""
    return _api_request("DELETE", path, token) is not None


def fetch_open_prs(repo: RepoConfig, token: str, count: int = 3, state: str = "open") -> list:
    """获取指定数量的 PR 列表。count=0 表示获取全部。"""
    if count == 0:
        # 获取全部：翻页遍历
        all_prs = []
        page = 1
        per_page = 50
        max_pages = 50
        while page <= max_pages:
            data = api_get(
                f"{repo.api_prefix}/pulls",
                token,
                {"state": state, "per_page": per_page, "page": page, "sort": "created", "direction": "desc"},
            )
            if not data:
                break
            all_prs.extend(data)
            if len(data) < per_page:
                break
            page += 1
        return all_prs

    data = api_get(
        f"{repo.api_prefix}/pulls",
        token,
        {"state": state, "per_page": count, "page": 1, "sort": "created", "direction": "desc"},
    )
    if data is None:
        return []
    return data[:count]


def fetch_pr_by_number(repo: RepoConfig, token: str, pr_number: int) -> dict | None:
    """根据 PR 编号获取单个 PR 详情。"""
    return api_get(f"{repo.api_prefix}/pulls/{pr_number}", token)


def fetch_prs_by_authors(repo: RepoConfig, token: str, authors: list, count: int = 3, state: str = "open") -> list:
    """获取指定用户的 PR，翻页遍历直到满足数量要求。count=0 表示获取全部。

    GitCode API 不支持按 author 过滤，因此客户端侧翻页 + 过滤。
    """
    authors_lower = {a.lower() for a in authors}
    matched = []
    page = 1
    per_page = 20  # 每页拉取量（平衡请求次数和过滤效率）
    max_pages = 50  # 安全上限，防止无限翻页

    while (count == 0 or len(matched) < count) and page <= max_pages:
        data = api_get(
            f"{repo.api_prefix}/pulls",
            token,
            {"state": state, "per_page": per_page, "page": page, "sort": "created", "direction": "desc"},
        )
        if not data:
            break

        for pr in data:
            login = pr.get("user", {}).get("login", "")
            if login.lower() in authors_lower:
                matched.append(pr)
                if count > 0 and len(matched) >= count:
                    break

        # 最后一页不足 per_page 条，说明已经没有更多数据
        if len(data) < per_page:
            break
        page += 1

    return matched


def load_team_members(filepath: Path = TEAM_FILE) -> tuple[list[str], dict[str, str]]:
    """从 team.txt 读取小组成员的 gitcode 账号列表。

    文件格式：每行 '姓名 工号 gitcode 账号'，首行为标题行。
    返回 (账号列表, {账号："姓名 工号"} 映射)。账号去重保序。
    """
    if not filepath.exists():
        print(f"  {_fail(f'人员名单不存在：{filepath}')}")
        sys.exit(1)

    accounts = []
    info_map: dict[str, str] = {}
    for i, line in enumerate(filepath.read_text(encoding="utf-8").splitlines()):
        if i == 0:
            continue  # 跳过标题行
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 3:
            account = parts[-1]  # 最后一列是 gitcode 账号
            info_map[account] = f"{parts[0]} {parts[1]}"
            accounts.append(account)
        elif len(parts) == 1:
            accounts.append(parts[0])   # 只有账号的简略格式
    if not accounts:
        print(f"  {_fail(f'人员名单为空：{filepath}')}")
        sys.exit(1)
    unique = list(dict.fromkeys(accounts))  # 去重保序
    return unique, info_map


def collect_prs(repo: RepoConfig, token: str, args: argparse.Namespace) -> list:
    """根据命令行参数收集待审查的 PR 列表。

    优先级：--pr > --team > --author > 默认(最近 open)
    """
    if args.pr:
        # 精确模式：逐个获取指定 PR
        prs = []
        for num in args.pr:
            print(f"  获取 PR #{num}")
            pr = fetch_pr_by_number(repo, token, num)
            if pr:
                prs.append(pr)
            else:
                print(f"  {_warn(f'PR #{num} 获取失败，跳过。')}")
        return prs

    if args.team:
        # 小组模式：从 team file 读取全部成员，获取每人的 PR
        members, info_map = load_team_members(args.team)
        display = [f"{m}({info_map[m]})" if m in info_map else m for m in members]
        print(f"  小组成员 ({len(members)} 人): {', '.join(display)}")
        return fetch_prs_by_authors(repo, token, members, args.count, args.state)

    if args.author:
        # 用户过滤模式
        print(f"  筛选用户：{', '.join(args.author)}")
        return fetch_prs_by_authors(repo, token, args.author, args.count, args.state)

    # 默认模式：最近 N 个 PR
    return fetch_open_prs(repo, token, args.count, args.state)


def fetch_pr_files(repo: RepoConfig, token: str, pr_number: int) -> list:
    """获取 PR 的变更文件列表（含 patch diff）。

    GitCode API 返回格式:
      [{
        "sha": "...", "filename": "path/to/file.cc",
        "additions": 5, "deletions": 3,
        "patch": {
          "diff": "--- a/...\n+++ b/...\n@@...",
          "old_path": "...", "new_path": "...",
          "new_file": false, "renamed_file": false, "deleted_file": false,
          "added_lines": 5, "removed_lines": 3
        }
      }, ...]
    """
    data = api_get(f"{repo.api_prefix}/pulls/{pr_number}/files", token)
    if data is None:
        return []
    if isinstance(data, list):
        return data
    # 兼容可能的嵌套格式
    return data.get("files", data.get("data", []))


# ======================== Diff 格式化 ========================
def get_file_diff(file_entry: dict) -> str:
    """从文件条目中提取 diff 文本，兼容不同 API 返回格式。"""
    patch = file_entry.get("patch", "")
    if isinstance(patch, dict):
        # GitCode 格式：patch 是嵌套对象，diff 在 patch.diff 字段
        return patch.get("diff", "")
    # 兼容 GitHub 格式：patch 直接是字符串
    return patch


def get_file_status(file_entry: dict) -> str:
    """从文件条目中推导文件变更状态。"""
    # 优先使用顶层 status 字段（GitHub 兼容）
    status = file_entry.get("status", "")
    if status:
        return status
    # GitCode 格式：从 patch 对象的布尔标志推导
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        if patch.get("new_file"):
            return "added"
        if patch.get("deleted_file"):
            return "removed"
        if patch.get("renamed_file"):
            return "renamed"
    return "modified"


def get_filename(file_entry: dict) -> str:
    """获取文件名，优先使用 patch.new_path。"""
    patch = file_entry.get("patch", {})
    if isinstance(patch, dict):
        return patch.get("new_path", "") or file_entry.get("filename", "unknown")
    return file_entry.get("filename", "unknown")


def format_diff_for_review(repo: RepoConfig, pr: dict, files: list) -> str:
    """将 PR 元信息和文件 diff 格式化为审查用文本。"""
    pr_number = pr.get("number", "?")
    pr_title = pr.get("title", "无标题")
    author = pr.get("user", {}).get("login", "unknown")
    head_ref = pr.get("head", {}).get("ref", "?")
    base_ref = pr.get("base", {}).get("ref", "?")
    body = (pr.get("body") or "").strip()

    lines = [
        f"# PR #{pr_number}: {pr_title}",
        f"",
        f"- 作者：{author}",
        f"- 分支：{head_ref} -> {base_ref}",
        f"- 链接：{repo.url}/merge_requests/{pr_number}",
    ]
    if body:
        lines.append(f"- 描述：{body[:500]}")
    lines.append("")

    # 文件列表概览
    cpp_files = [f for f in files if is_cpp_file(get_filename(f))]
    non_cpp_files = [f for f in files if not is_cpp_file(get_filename(f))]

    lines.append(f"## 变更文件 ({len(files)} 个, 其中 C/C++ 文件 {len(cpp_files)} 个)")
    lines.append("")
    for f in files:
        fname = get_filename(f)
        status = get_file_status(f)
        adds = f.get("additions", 0)
        dels = f.get("deletions", 0)
        marker = " *" if is_cpp_file(fname) else ""
        lines.append(f"- [{status}] {fname} (+{adds}, -{dels}){marker}")
    lines.append("")

    # 只输出 C/C++ 文件的 diff（代码审查重点）
    review_files = cpp_files if cpp_files else files
    lines.append("## Diff 内容")
    lines.append("")

    total_chars = 0
    for f in review_files:
        fname = get_filename(f)
        diff_text = get_file_diff(f)
        if not diff_text:
            continue

        # 防止超长 diff
        if total_chars + len(diff_text) > MAX_DIFF_CHARS:
            lines.append(f"### {fname}")
            lines.append("(diff 过长，已截断)")
            lines.append("")
            break

        lines.append(f"### {fname}")
        lines.append("```diff")
        lines.append(diff_text)
        lines.append("```")
        lines.append("")
        total_chars += len(diff_text)

    # 如果有非 C++ 文件被跳过，注明
    if cpp_files and non_cpp_files:
        skipped = ", ".join(get_filename(f) for f in non_cpp_files[:10])
        lines.append(f"> 注：以下非 C/C++ 文件未纳入审查：{skipped}")
        lines.append("")

    return "\n".join(lines)


def is_cpp_file(filename: str) -> bool:
    """判断是否为 C/C++ 源文件或头文件。"""
    exts = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    return Path(filename).suffix.lower() in exts


# ======================== Diff Position 计算 ========================
def _build_diff_position_map(raw_diff: str) -> dict[int, tuple[int, bool]]:
    """解析单个文件的 raw diff，建立行号→position 映射。

    返回：{new_line_number: (position, is_added)}
    - position: GitCode API 的 diff 相对行号（从 1 开始）
    - is_added: True 表示 '+' 行（新增），False 表示上下文行

    算法说明：
    - 首个 @@ 行不计入 position（position 从其后第一行开始为 1）
    - 后续 @@ 行本身计入 position（占 1 个 position）
    - '-' 行（删除行）计入 position 但不增加 new_line
    - '+' 行（新增行）计入 position 且增加 new_line
    - 上下文行（无 +/-）计入 position 且增加 new_line
    """
    mapping: dict[int, tuple[int, bool]] = {}
    position = 0
    new_line = 0
    first_hunk = True

    for line in raw_diff.split("\n"):
        if not line and not first_hunk:
            continue  # 跳过尾部空行（split 产物）
        if line.startswith("@@"):
            # 解析 @@ -old_start,old_count +new_start,new_count @@
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            if first_hunk:
                first_hunk = False
            else:
                position += 1
            continue

        if first_hunk:
            # 跳过 diff header（--- a/... / +++ b/... 等）
            continue

        position += 1
        if line.startswith("+"):
            new_line += 1
            mapping[new_line] = (position, True)
        elif line.startswith("-"):
            pass  # 删除行不增加 new_line
        elif line.startswith("\\"):
            pass  # "\ No newline at end of file" 等元数据行，计 position 但不增加 new_line
        else:
            new_line += 1
            mapping[new_line] = (position, False)

    return mapping


def _build_diff_line_content(raw_diff: str) -> dict[int, str]:
    """解析 diff，构建 {新文件行号：行内容} 映射（用于行号校验）。"""
    content_map: dict[int, str] = {}
    new_line = 0
    in_hunk = False

    for line in raw_diff.split("\n"):
        if line.startswith("@@"):
            match = re.search(r"\+(\d+)", line)
            if match:
                new_line = int(match.group(1)) - 1
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("+"):
            new_line += 1
            content_map[new_line] = line[1:]  # 去掉 '+' 前缀
        elif line.startswith("-") or line.startswith("\\"):
            pass
        else:
            new_line += 1
            content_map[new_line] = line[1:] if line.startswith(" ") else line

    return content_map


def _verify_and_correct_line(
    finding: "InlineFinding", content_map: dict[int, str], max_offset: int = 5,
) -> int:
    """校验 finding 的行号是否精确指向问题代码，偏差时自动修正。

    优先用 title 中的标识符定位最相关的行（title 描述了具体问题，比 body 中的
    多行代码块更精确），然后用 body 代码行作为后备。
    """
    # ---- 第 1 层：从 title 提取标识符（最能指向问题行的关键词）----
    title_ids: list[str] = []
    # 反引号包裹的标识符
    for m in re.finditer(r"`([^`]{3,})`", finding.title):
        title_ids.append(m.group(1))
    # ALL_CAPS 标识符（HCCL_ERROR, HCCL_INFO, SPRINTF 等）
    for m in re.finditer(r"\b([A-Z][A-Z0-9_]{4,})\b", finding.title):
        ident = m.group(1)
        if ident not in title_ids:
            title_ids.append(ident)
    # PascalCase 函数名（GetEndpointNum, AddrPositionToEndpointLoc 等）
    for m in re.finditer(r"\b([A-Z][a-z]+(?:[A-Z][a-z0-9]*)+)\b", finding.title):
        ident = m.group(1)
        if ident not in title_ids:
            title_ids.append(ident)

    # ---- 第 2 层：从 body 提取代码行关键词 ----
    body_kws: list[str] = []
    # 围栏代码块内的代码行
    for fence_m in re.finditer(r"```\w*\n(.*?)```", finding.body, re.DOTALL):
        for line in fence_m.group(1).split("\n"):
            code = line.strip()
            if len(code) >= 10 and not code.startswith("//"):
                body_kws.append(code)
    # 4 空格缩进代码行（兼容旧格式）
    if not body_kws:
        for m in re.finditer(r"^    (.+)$", finding.body, re.MULTILINE):
            code = m.group(1).strip()
            if len(code) >= 10 and not code.startswith("//"):
                body_kws.append(code)

    if not title_ids and not body_kws:
        return finding.line

    def _search(keywords: list[str]) -> int | None:
        """在 finding.line ±max_offset 内搜索匹配 keywords 的行。"""
        cur = content_map.get(finding.line, "")
        if any(kw in cur for kw in keywords):
            return finding.line
        for off in range(1, max_offset + 1):
            # 优先向后搜索（问题代码常在代码块的后几行）
            for cand in [finding.line + off, finding.line - off]:
                c = content_map.get(cand, "")
                if any(kw in c for kw in keywords):
                    return cand
        return None

    # 优先用 title 标识符定位（更精确地指向问题行本身）
    if title_ids:
        result = _search(title_ids)
        if result is not None:
            return result

    # 后备：用 body 代码行关键词
    if body_kws:
        result = _search(body_kws)
        if result is not None:
            return result

    return finding.line


# ======================== Claude Code 审查 ========================
_SPINNER_CHARS = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _spinner_thread(stop_event: threading.Event):
    """后台线程：显示旋转动画和已耗时。"""
    start = time.monotonic()
    idx = 0
    while not stop_event.is_set():
        elapsed = time.monotonic() - start
        ch = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
        sys.stderr.write(f"\r  审查中 {ch} {_fmt_secs(elapsed)} ")
        sys.stderr.flush()
        idx += 1
        stop_event.wait(0.5)


def _clean_review_output(text: str) -> str | None:
    """清理审查输出中的非审查内容（如 Claude 的中间推理文字、Explanatory 风格 Insight 块）。

    1. 剥离 Explanatory 输出风格产生的 ★ Insight 块（settings.local.json outputStyle 污染）
    2. 审查正文以 '## ' 开头的 markdown 标题起始，之前的内容视为内部推理，予以删除。
    清理后不足 MIN_REVIEW_CHARS 字符则视为无效，返回 None。
    """
    # 剥离 ★ Insight 块（Explanatory 输出风格产生的教育性内容）
    # 格式：`★ Insight ───...───`\n内容\n`───...───`
    text = re.sub(
        r"`★ Insight[^`]*`\s*\n.*?\n`─+`\s*\n?",
        "", text, flags=re.DOTALL,
    )
    # 兜底：剥离残余的 Insight 标记行
    text = re.sub(r"^`[★─][^`]*`\s*$", "", text, flags=re.MULTILINE)

    match = re.search(r"^## ", text, re.MULTILINE)
    if match and match.start() > 0:
        text = text[match.start():]
    text = text.strip()
    if len(text) < MIN_REVIEW_CHARS:
        return None
    return text


def _run_claude(prompt: str, cwd: Path, max_retries: int = 2, allowed_tools: list = None,
                show_progress: bool = False, timeout: int = 900,
                max_turns: int = MAX_CLAUDE_TURNS,
                log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 执行审查。空结果时自动重试。

    返回 (清理后的审查文本或 None, 统计信息)。
    使用 --output-format json 获取 token 用量和费用信息。

    重试策略：空结果或结果过短时重试（保持相同工具配置）。
    回合耗尽或权限拒绝属于确定性失败，直接放弃，不降级为无工具模式。

    cwd: 子进程工作目录（被审查仓库的根目录）。
    allowed_tools: 授权 Claude 自主使用的工具列表（如 ["Read", "Grep", "Glob"]）。
    show_progress: 是否显示实时进度 spinner（仅顺序模式下启用，并行模式下关闭）。
    timeout: 子进程超时秒数（默认 900s，大 PR 应按 diff 大小动态调整）。
    log: 日志输出函数。顺序模式用 print，并行模式传入写 buffer 的函数。
    """
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    env["CLAUDE_CODE_OUTPUT_STYLE"] = ""
    stats = ReviewStats()
    for attempt in range(1, max_retries + 1):
        cmd = ["claude", "-p", "--output-format", "json", "--model", "claude-opus-4-6"]
        if allowed_tools:
            for tool in allowed_tools:
                cmd.extend(["--allowedTools", tool])
            cmd.extend(["--max-turns", str(max_turns)])

        actual_prompt = prompt

        try:
            if show_progress:
                proc = subprocess.Popen(
                    cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True,
                    cwd=str(cwd), env=env,
                )
                proc.stdin.write(actual_prompt)
                proc.stdin.close()

                stop_event = threading.Event()
                spinner = threading.Thread(
                    target=_spinner_thread, args=(stop_event,), daemon=True)
                spinner.start()

                try:
                    stdout, stderr_out = proc.communicate(timeout=timeout)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    raise
                finally:
                    stop_event.set()
                    spinner.join(timeout=2)
                    sys.stderr.write("\r" + " " * 40 + "\r")
                    sys.stderr.flush()

                class _Result:
                    pass
                result = _Result()
                result.stdout = stdout
                result.stderr = stderr_out
                result.returncode = proc.returncode
            else:
                result = subprocess.run(
                    cmd,
                    input=actual_prompt,
                    capture_output=True,
                    text=True,
                    cwd=str(cwd),
                    timeout=timeout,
                    env=env,
                )

            stderr = result.stderr.strip()
            if result.returncode != 0:
                log(f"  {_warn(f'claude 返回码 {result.returncode}')}")
                if stderr:
                    log(f"  {_dim(f'stderr: {stderr[:500]}')}")

            output = result.stdout.strip()
            if not output:
                log(f"  {_warn(f'第 {attempt} 次审查未返回结果 (returncode={result.returncode})')}")
                if stderr:
                    log(f"  {_dim(f'stderr: {stderr[:500]}')}")
                if attempt == max_retries:
                    _diagnose_empty_output(prompt, cwd, allowed_tools, env, log)
            else:
                text, stats = _parse_json_output(output)
                for denial in stats.permission_denials:
                    log(f"  {_warn(denial)}")
                cleaned = _clean_review_output(text)
                if cleaned is not None:
                    return cleaned, stats
                log(f"  {_warn(f'第 {attempt} 次审查结果过短 ({len(text)} 字符)，视为无效')}")
                log(f"  {_dim(f'前 200 字符: {text[:200]}')}")

                # 回合耗尽或权限拒绝是确定性失败，重试不会改善，直接放弃
                turns_exhausted = stats.num_turns >= max_turns - 2
                has_denials = len(stats.permission_denials) > 0
                if turns_exhausted:
                    log(f"  {_fail(f'回合耗尽 ({stats.num_turns}/{max_turns})，放弃本次审查')}")
                    break
                elif has_denials:
                    log(f"  {_fail(f'工具权限拒绝 ({len(stats.permission_denials)} 次)，放弃本次审查')}")
                    break

            if attempt < max_retries:
                log(f"  {_yellow(f'重试中 ({attempt + 1}/{max_retries})')}")

        except FileNotFoundError:
            log(f"  {_fail('未找到 claude 命令，请确认 Claude Code CLI 已安装并在 PATH 中')}")
            sys.exit(1)
        except subprocess.TimeoutExpired:
            log(f"  {_warn(f'第 {attempt} 次审查超时（超过 {_fmt_secs(timeout)}）')}")
            if attempt < max_retries:
                log(f"  {_yellow(f'重试中 ({attempt + 1}/{max_retries})')}")

    log(f"  {_fail(f'{max_retries} 次尝试均未获得审查结果')}")
    return None, stats


def _diagnose_empty_output(prompt: str, cwd: Path, allowed_tools: list, env: dict, log=print):
    """当审查返回空结果时，用 JSON 格式做一次诊断性调用，分析失败原因。"""
    _diag = lambda s: log(f"  {_dim(f'[诊断] {s}')}")
    _diag("尝试用 JSON 格式获取诊断信息")
    diag_cmd = ["claude", "-p", "--output-format", "json", "--max-turns", "3"]
    diag_prompt = "请回复'连通性测试成功'。"
    try:
        diag_result = subprocess.run(
            diag_cmd,
            input=diag_prompt,
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=60,
            env=env,
        )
        if diag_result.stdout.strip():
            _diag(f"基本连通性正常，问题可能是工具调用耗尽了 {MAX_CLAUDE_TURNS} 个 turns")
            _diag(f"建议：增大 MAX_CLAUDE_TURNS（当前={MAX_CLAUDE_TURNS}）或减少工具数量")
            if allowed_tools:
                _diag(f"当前启用的工具：{', '.join(allowed_tools)}")
        else:
            _diag(f"基本连通性也失败 (returncode={diag_result.returncode})")
            if diag_result.stderr.strip():
                _diag(f"stderr: {diag_result.stderr.strip()[:500]}")
    except Exception as e:
        _diag(f"诊断调用异常：{e}")


def run_claude_review(diff_text: str, pr: dict, cwd: Path, max_retries: int = 2,
                      show_progress: bool = False,
                      log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 执行 PR 代码审查。

    启用工具能力，Claude 可主动读取文件和搜索代码以获取上下文。
    对于 PR 审查，指引 Claude 用 git show 读取 PR 分支的文件内容。
    """
    head_sha = pr.get("head", {}).get("sha", "")
    head_ref = pr.get("head", {}).get("ref", "")
    base_ref = pr.get("base", {}).get("ref", "")

    # 根据 diff 大小动态调整超时和 turns（需在 prompt 构造前计算，以便写入工具预算）
    # 超时：基准 600s + 每 50 字符 diff 加 1s，上限 1800s（30 分钟）
    review_timeout = min(600 + len(diff_text) // 50, 1800)
    # turns：基准 35 + 每 200 行 diff 加 5 turns，上限 80
    # 基准 35 = 约 20 次工具调用 + 15 次文本输出余量，防止小 PR 回合耗尽
    diff_lines = diff_text.count("\n")
    review_turns = min(35 + (diff_lines // 200) * 5, 80)
    # 工具调用预算 = 总回合的 60%，剩余留给文本输出
    max_tool_calls = review_turns * 3 // 5

    # 构建工具使用和输出要求指引（编码规则全部在 skill 中，此处只管流程）
    context_guide = f"""\

## 工具使用要求

**质量优先：发现一个真实的严重问题，比节省工具调用更有价值。** 以下场景必须使用工具验证，不要仅凭 diff 猜测：

- **指针操作**：读取被调函数实现，确认是否可能返回 null；检查函数参数是值传递还是引用传递
- **算术运算**：读取变量声明，确认类型（uint32_t? int64_t?）和值域范围
- **结构体/类成员变更**：grep 被删除/新增/重命名的成员名，检查所有引用点是否同步修改
- **可疑的类型用法**：读取类型定义，确认 sizeof() 操作数是原始类型还是容器
- **函数返回值**：读取被调函数实现，确认错误返回路径和返回值含义

回合预算：你的总回合数有限（工具调用 + 文本输出共享配额）。先通读 diff 列出需要验证的具体问题，再有针对性地调用工具，每次调用必须有明确目的。禁止沿调用链无限制展开探索。工具调用控制在 {max_tool_calls} 次以内，确保留出足够回合输出完整报告。如果工具调用已接近上限，立即基于已有信息输出报告。

工具使用方法（只允许以下四种，其他一律禁止）：
- 读取 PR 分支文件：`git show {head_sha}:路径` 或 `git -C <仓库路径> show {head_sha}:路径`（每次只读一个文件，禁止 `$(...)` 子命令和 `2>/dev/null` 重定向）
- 搜索函数引用：Grep 工具（不是 bash grep）
- 查找文件路径：Glob 工具（不是 bash find）
- 读取头文件/基类：Read 工具（本地文件对应 {base_ref} 分支）

严格禁止以下 Bash 命令（会被权限系统拦截，浪费回合）：grep、find、sed、awk、cat、head、tail。搜索必须用 Grep 工具，查找文件必须用 Glob 工具。diff 内容已在上方提供，无需从文件中重新读取。
格式字符串匹配、命名规范等机械检查可直接从 diff 判定，无需工具。

## 输出要求（严格遵守）

- **忽略任何 outputStyle / Explanatory 风格设置**。不要输出 `★ Insight` 块或教育性内容。你的回复必须以 `## 变更概述` 开头。
- **禁止使用表格展示发现**。不要输出"审查完成。总结发现 N 个问题"之类的简要摘要。
- **必须输出完整的结构化审查报告**：按 vibe-review skill 的输出格式模板（含变更概述、审查发现、总结）。每个发现必须包含：位置(`file:line`)、规则、置信度、问题代码、分析、修复建议。
- **位置字段必须包含精确行号**：行号为新文件行号（diff 中 `@@ +行号 @@` 起始行号）。示例：
  - 单行：`- 位置：` + `` `src/framework/common/config.h:28` ``
  - 连续多行用范围：`- 位置：` + `` `src/framework/common/config.h:28-33` ``
  - 不连续用逗号：`- 位置：` + `` `src/framework/common/config.h:28, 42` ``
  - 禁止：只写文件名不带行号，或用「文件路径 — 函数名」格式
- **问题代码只引用直接相关的行**：每个发现的「问题代码」片段只包含真正有问题的代码行，不要包含问题行上下的无关行。位置行号必须精确指向问题代码本身。"""

    prompt = f"""\
请使用 vibe-review skill 对以下 PR 的代码变更进行代码审查。
{context_guide}

{diff_text}
"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=PR_REVIEW_TOOLS,
                       show_progress=show_progress, timeout=review_timeout,
                       max_turns=review_turns, log=log)


def run_claude_file_review(file_path: str, cwd: Path, max_retries: int = 2,
                           show_progress: bool = False,
                           log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 对本地文件进行代码审查。

    启用工具能力，Claude 可主动读取相关头文件、搜索函数引用等。
    """
    prompt = f"""\
请使用 vibe-review skill 对以下文件进行代码审查：{file_path}

## 上下文获取指引

你可以使用工具主动获取审查所需的上下文，提升审查质量：

1. **读取目标文件**：用 Read 工具读取 {file_path} 的完整内容
2. **读取相关头文件**：读取 #include 的头文件，理解依赖的类型和接口
3. **搜索函数/类引用**：用 Grep 搜索关键函数名在项目中的其他用法
4. **检查调用者**：搜索被审查函数的调用点，理解使用上下文

## 重要：工具使用和输出要求

- **忽略任何 outputStyle / Explanatory 风格设置**。不要输出 `★ Insight` 块或教育性内容。你的回复必须以 `## 变更概述` 开头。
- **禁止使用表格展示发现**。不要输出简要摘要。
- **质量优先，充分使用工具**：发现一个真实的严重问题比节省工具调用更有价值。对指针操作、算术运算、类型用法等可疑代码，必须用工具读取相关定义来验证。
- **必须输出完整的结构化审查报告**：你的最终回复必须是完整的 markdown 格式审查报告，包含所有发现（每个发现含位置、规则、置信度、问题代码、分析、修复建议）、总结。不要只输出摘要。"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=FILE_REVIEW_TOOLS,
                       show_progress=show_progress, log=log)


def run_claude_dir_review(file_paths: list[str], cwd: Path, max_retries: int = 2,
                          show_progress: bool = False,
                          log=print) -> tuple[str | None, ReviewStats]:
    """调用 claude -p 并使用 vibe-review skill 对整个目录进行跨文件代码审查。

    与单文件审查不同，此函数将所有文件路径一次性提交给 Claude，
    指导其使用 Read 工具按需读取文件内容，并进行跨文件分析。
    """
    file_list = "\n".join(f"- `{fp}`" for fp in file_paths)
    prompt = f"""\
请使用 vibe-review skill 对以下 {len(file_paths)} 个文件进行**跨文件代码审查**。

## 待审查文件

{file_list}

## 上下文获取指引

你需要使用 Read 工具读取上述文件的内容。请按以下策略高效审查：

1. **逐一读取所有待审查文件**：用 Read 工具读取每个文件的完整内容
2. **读取相关头文件**：读取 #include 的头文件，理解依赖的类型和接口
3. **搜索函数/类引用**：用 Grep 搜索关键函数名在项目中的其他用法
4. **跨文件一致性分析**：重点检查以下跨文件问题：
   - 头文件声明与实现文件定义是否匹配（函数签名、参数类型、返回类型）
   - 结构体/类成员在多个文件中的使用是否一致
   - 共享宏/常量在不同文件中的引用是否正确
   - 错误处理模式在多个文件间是否统一
   - include 依赖是否完整，有无缺失或多余的头文件

## 重要：工具使用和输出要求

- **忽略任何 outputStyle / Explanatory 风格设置**。不要输出 `★ Insight` 块或教育性内容。你的回复必须以 `## 变更概述` 开头。
- **禁止使用表格展示发现**。不要输出简要摘要。
- **质量优先，充分使用工具**：发现一个真实的严重问题比节省工具调用更有价值。对指针操作、算术运算、类型用法等可疑代码，必须用工具读取相关定义来验证。
- **必须输出完整的结构化审查报告**：你的最终回复必须是完整的 markdown 格式审查报告，包含所有发现（每个发现含位置、规则、置信度、问题代码、分析、修复建议）、总结。不要只输出摘要。"""
    return _run_claude(prompt, cwd, max_retries, allowed_tools=FILE_REVIEW_TOOLS,
                       show_progress=show_progress, log=log)


# ======================== 输出 ========================
def write_review_md(repo: RepoConfig, pr: dict, review_text: str, output_dir: Path, head_sha: str = "") -> Path:
    """将审查结果写入 markdown 文件。"""
    pr_number = pr.get("number", 0)
    pr_title = pr.get("title", "无标题")
    author = pr.get("user", {}).get("login", "unknown")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    output_file = output_dir / f"pr_{pr_number}_review.md"

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    content = (
        f"# Code Review: PR #{pr_number}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file


def write_file_review_md(file_path: str, review_text: str, output_dir: Path) -> Path:
    """将本地文件审查结果写入 markdown 文件。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = Path(file_path).name.replace(".", "_")
    output_file = output_dir / f"{safe_name}_review.md"

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    content = (
        f"# Code Review: {file_path}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 文件 | `{file_path}` |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |{summary_row}\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file


def write_dir_review_md(dir_path: str, file_paths: list[str],
                        review_text: str, output_dir: Path) -> Path:
    """将目录审查结果写入 markdown 文件。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    safe_name = Path(dir_path).name or "root"
    output_file = output_dir / f"{safe_name}_review.md"

    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    file_list = "\n".join(f"  - `{fp}`" for fp in file_paths)
    content = (
        f"# Code Review: {dir_path}/\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 目录 | `{dir_path}` |\n"
        f"| 文件数 | {len(file_paths)} |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |{summary_row}\n\n"
        f"<details>\n<summary>审查文件列表</summary>\n\n{file_list}\n</details>\n\n"
        f"---\n\n"
        f"{review_text}\n"
    )

    output_file.write_text(content, encoding="utf-8")
    return output_file


# ======================== 发布到 GitCode PR ========================
def _split_comment(text: str, max_chars: int = MAX_COMMENT_CHARS) -> list:
    """将过长的评论拆分为多条，优先在 '---' 分隔线处拆分。"""
    if len(text) <= max_chars:
        return [text]

    parts = []
    remaining = text

    while remaining:
        if len(remaining) <= max_chars:
            parts.append(remaining)
            break

        # 在 max_chars 范围内寻找最后一个 '---' 分隔线
        search_range = remaining[:max_chars]
        split_pos = search_range.rfind("\n---\n")

        if split_pos == -1 or split_pos < max_chars // 4:
            # 找不到合适的分隔线，在最后一个换行处拆分
            split_pos = search_range.rfind("\n")
            if split_pos == -1 or split_pos < max_chars // 4:
                split_pos = max_chars

        chunk = remaining[:split_pos].rstrip()
        remaining = remaining[split_pos:].lstrip("\n-").lstrip()
        parts.append(chunk)

    # 为多条评论添加序号
    if len(parts) > 1:
        parts = [f"**[{i + 1}/{len(parts)}]**\n\n{p}" for i, p in enumerate(parts)]

    return parts


def _fetch_all_pr_comments(repo: RepoConfig, token: str, pr_number: int) -> list:
    """翻页获取 PR 的所有评论（总结 + 行内）。"""
    all_comments = []
    page = 1
    per_page = 100
    while True:
        data = api_get(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"per_page": per_page, "page": page},
        )
        if not data or not isinstance(data, list):
            break
        all_comments.extend(data)
        if len(data) < per_page:
            break
        page += 1
    return all_comments


def _is_already_reviewed(repo: RepoConfig, token: str, pr_number: int, head_sha: str) -> bool:
    """检查 PR 是否已基于最新提交审查过。

    扫描评论区的 AI 审查评论，查找隐藏标记 <!-- REVIEWED_SHA:xxx -->，
    若其中的 SHA 与当前 head_sha 一致，说明已审查过，返回 True。
    """
    comments = _fetch_all_pr_comments(repo, token, pr_number)
    for comment in comments:
        body = comment.get("body", "")
        is_ai_comment = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
        )
        if not is_ai_comment:
            continue
        match = re.search(r"<!-- REVIEWED_SHA:(\w+) -->", body)
        if match and match.group(1) == head_sha:
            return True
    return False


def delete_old_review_comments(repo: RepoConfig, token: str, pr_number: int) -> int:
    """删除 PR 中已有的 AI 评论（含总结评论和行内评论，并行删除）。返回删除数量。"""
    comments = _fetch_all_pr_comments(repo, token, pr_number)

    to_delete = []
    for comment in comments:
        body = comment.get("body", "")
        is_ai_comment = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
            or AI_INLINE_MARKER in body
        )
        if is_ai_comment:
            comment_id = comment.get("id")
            if comment_id:
                to_delete.append(comment_id)

    if not to_delete:
        return 0

    deleted = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = {
            pool.submit(api_delete, f"{repo.api_prefix}/pulls/comments/{cid}", token): cid
            for cid in to_delete
        }
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                deleted += 1

    return deleted


def _extract_issue_summary(review_text: str) -> str:
    """从审查正文中提取问题计数摘要（如 '严重 3 / 一般 7 / 建议 4'）。"""
    match = re.search(r"(严重\s*\d+\s*/\s*一般\s*\d+\s*/\s*建议\s*\d+)", review_text)
    return match.group(1) if match else ""


# ======================== 审查追踪 ========================

def _init_tracking_db() -> sqlite3.Connection:
    """初始化追踪数据库，返回连接。表不存在时自动创建。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(TRACKING_DB), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pr_number INTEGER NOT NULL,
            repo TEXT NOT NULL,
            pr_title TEXT,
            pr_author TEXT,
            head_sha TEXT NOT NULL,
            review_timestamp TEXT NOT NULL,
            review_round INTEGER DEFAULT 1,
            finding_count INTEGER,
            severity_summary TEXT,
            cost_usd REAL,
            duration_ms INTEGER,
            UNIQUE(pr_number, repo, head_sha)
        );
        CREATE TABLE IF NOT EXISTS findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            review_id INTEGER NOT NULL REFERENCES reviews(id),
            finding_index INTEGER NOT NULL,
            severity TEXT NOT NULL,
            title TEXT NOT NULL,
            file_path TEXT,
            line_numbers TEXT,
            rule TEXT,
            confidence TEXT,
            code_snippet TEXT,
            body TEXT,
            outcome TEXT,
            outcome_method TEXT,
            outcome_detail TEXT,
            outcome_sha TEXT,
            outcome_timestamp TEXT,
            UNIQUE(review_id, finding_index)
        );
    """)
    # 兼容旧数据库：添加 fix_snippet 列（如果不存在）
    try:
        conn.execute("ALTER TABLE findings ADD COLUMN fix_snippet TEXT")
    except sqlite3.OperationalError:
        pass  # 列已存在
    conn.commit()
    return conn


def _normalize_whitespace(s: str) -> str:
    """空白归一化：去除首尾空白、连续空白压缩为单空格。"""
    return re.sub(r"\s+", " ", s.strip())


def _extract_fix_snippet(finding_text: str) -> str | None:
    """从 finding 文本中提取"修复建议"部分的代码片段。

    匹配常见的修复建议格式：围栏代码块或缩进代码块，
    跟在"修复建议"/"建议修改"/"建议改为"等标题之后。
    """
    # 模式 1: "修复建议(...):" 后接围栏代码块
    m = re.search(
        r"(?:修复建议|建议修改|建议改为|建议修复|修改建议|Suggested fix)[^:：\n]*[：:]\s*\n```\w*\n(.*?)```",
        finding_text, re.DOTALL | re.IGNORECASE,
    )
    if m:
        lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
        if lines:
            return "\n".join(lines)

    # 模式 2: "修复建议(...):" 后接 4 空格缩进代码块
    m = re.search(
        r"(?:修复建议|建议修改|建议改为|建议修复|修改建议|Suggested fix)[^:：\n]*[：:]\s*\n\n?((?:    .+\n?)+)",
        finding_text, re.IGNORECASE,
    )
    if m:
        lines = [l.strip() for l in m.group(1).split("\n") if l.strip()]
        if lines:
            return "\n".join(lines)

    return None


def _extract_snippet_for_tracking(finding_text: str) -> str | None:
    """从 finding 文本提取问题代码核心行（用于存活性检测）。

    取问题代码块中所有有辨识度的行（用 \\n 连接），
    排除注释行、空行、省略号。跨多行 finding 保留多行以提高判定准确性。
    """
    lines = _extract_code_snippet(finding_text)
    if not lines:
        return None
    # 过滤掉省略号行、纯注释行
    filtered = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("...", "…", "// ..."):
            continue
        if stripped.startswith("//") and len(stripped) < 10:
            continue
        # 排除冲突标记
        if stripped.startswith(("<<<<<<", "======", ">>>>>>")):
            continue
        # 排除 git diff 注解行（如 "\ No newline at end of file"）
        if stripped.startswith("\\") and "newline" in stripped.lower():
            continue
        # 排除低辨识度的 include guard / 通用单行关键字
        if stripped in ("#endif", "#else", "return;", "break;", "default:",
                        "continue;", "} else {", "};", "});"):
            continue
        if re.match(r"^#(?:ifndef|define)\s+\w+_H_?$", stripped):
            continue
        if len(stripped) >= 8:
            filtered.append(stripped)
    if not filtered:
        return None
    # 保留所有有效行，用换行连接
    return "\n".join(filtered)


def _extract_all_findings(review_text: str) -> list[dict]:
    """从审查报告中提取所有结构化 findings。

    返回 list[dict]，每个 dict 含：
    index, severity, title, file_path, line_numbers, rule, confidence,
    code_snippet, body
    """
    finding_pattern = r"### #(\d+)\s+\[([^\]]+)\]\s+(.*?)(?=\n---\s*$|\n### #\d|\Z)"
    matches = list(re.finditer(finding_pattern, review_text, re.DOTALL | re.MULTILINE))
    results = []
    for m in matches:
        idx = int(m.group(1))
        severity = m.group(2).strip()
        content = m.group(3).strip()
        title_line = content.split("\n")[0].strip()
        # 提取位置
        file_path = None
        line_numbers = None
        loc_m = re.search(r"位置[：:]\s*`([^`]+)`", content)
        if loc_m:
            loc_str = loc_m.group(1)
            # file.cc:123, 456
            fp_m = re.match(r"([^:]+):(.+)", loc_str)
            if fp_m:
                file_path = fp_m.group(1).strip()
                line_numbers = fp_m.group(2).strip()
            else:
                file_path = loc_str.strip()
        # 提取规则
        rule = None
        rule_m = re.search(r"规则[：:]\s*(.+?)(?:\n|$)", content)
        if rule_m:
            rule = rule_m.group(1).strip()
        # 提取置信度：只取核心标签（确定/较确定/待确认）
        confidence = None
        conf_m = re.search(r"置信度[：:]\s*\*{0,2}(确定|较确定|待确认)\*{0,2}", content)
        if conf_m:
            confidence = conf_m.group(1)
        # 提取代码片段
        snippet = _extract_snippet_for_tracking(content)
        results.append({
            "index": idx,
            "severity": severity,
            "title": title_line,
            "file_path": file_path,
            "line_numbers": line_numbers,
            "rule": rule,
            "confidence": confidence,
            "code_snippet": snippet,
            "body": content,
        })
    return results


def _save_review(
    conn: sqlite3.Connection, repo_name: str, pr_number: int, pr_title: str,
    pr_author: str, head_sha: str, stats: "ReviewStats", duration_ms: int,
    severity_summary: str, finding_count: int,
) -> int | None:
    """写入一条 review 记录，返回 review_id。若已存在返回 None。"""
    # 计算 review_round
    row = conn.execute(
        "SELECT COUNT(*) FROM reviews WHERE pr_number=? AND repo=?",
        (pr_number, repo_name),
    ).fetchone()
    review_round = (row[0] if row else 0) + 1
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        cur = conn.execute(
            """INSERT INTO reviews
               (pr_number, repo, pr_title, pr_author, head_sha, review_timestamp,
                review_round, finding_count, severity_summary, cost_usd, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (pr_number, repo_name, pr_title, pr_author, head_sha, now,
             review_round, finding_count, severity_summary,
             stats.best_cost if stats else 0, duration_ms),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # UNIQUE(pr_number, repo, head_sha) 冲突 — 同一 SHA 已审查过
        return None


def _save_findings(conn: sqlite3.Connection, review_id: int, findings: list[dict]) -> int:
    """批量写入 findings，返回写入数量。"""
    saved = 0
    for f in findings:
        body = f.get("body", "")[:5000]
        fix_snippet = _extract_fix_snippet(body) if body else None
        try:
            conn.execute(
                """INSERT INTO findings
                   (review_id, finding_index, severity, title, file_path, line_numbers,
                    rule, confidence, code_snippet, body, fix_snippet)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (review_id, f["index"], f["severity"], f["title"],
                 f.get("file_path"), f.get("line_numbers"), f.get("rule"),
                 f.get("confidence"), f.get("code_snippet"),
                 body, fix_snippet),
            )
            saved += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return saved


def _check_snippet_alive(repo_path: Path, sha: str, file_path: str, code_snippet: str) -> bool | None:
    """检查代码片段在指定 SHA 的文件中是否仍存在。

    支持多行 snippet（用 \\n 分隔）。逐行检测：
    - 全部消失 → False（已修复）
    - 全部存在 → True（未修复）
    - 部分消失 → False（视为已处理，至少动了）

    Returns:
        True  — 片段仍存在
        False — 片段消失（文件不存在或片段未找到）
        None  — 无法判断（snippet 无效或 git 命令失败）
    """
    if not code_snippet or not file_path or not sha:
        return None
    # 拆分多行 snippet
    snippet_lines = [s for s in code_snippet.split("\n") if s.strip()]
    if not snippet_lines:
        return None
    # 过滤太短的行（缺乏辨识度）
    valid_lines = [s for s in snippet_lines if len(_normalize_whitespace(s)) >= 8]
    if not valid_lines:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            if "does not exist" in result.stderr or "not exist" in result.stderr:
                return False
            return None
        file_lines = {_normalize_whitespace(line) for line in result.stdout.splitlines() if line.strip()}
        alive_count = sum(1 for s in valid_lines if _normalize_whitespace(s) in file_lines)
        survival_rate = alive_count / len(valid_lines)
        if survival_rate > 0.5:
            return True   # 多数行仍存在
        return False      # 多数行已消失 → 已处理
    except (subprocess.TimeoutExpired, OSError):
        return None


def _check_fix_snippet_present(repo_path: Path, sha: str, file_path: str, fix_snippet: str) -> bool | None:
    """检查修复代码片段是否出现在指定 SHA 的文件中。

    Returns:
        True  — 修复代码存在
        False — 修复代码未出现
        None  — 无法判断
    """
    if not fix_snippet or not file_path or not sha:
        return None
    snippet_lines = [s for s in fix_snippet.split("\n") if s.strip()]
    if not snippet_lines:
        return None
    valid_lines = [s for s in snippet_lines if len(_normalize_whitespace(s)) >= 8]
    if not valid_lines:
        return None
    try:
        result = subprocess.run(
            ["git", "show", f"{sha}:{file_path}"],
            capture_output=True, text=True, timeout=10,
            cwd=str(repo_path),
        )
        if result.returncode != 0:
            return None
        file_lines = {_normalize_whitespace(line) for line in result.stdout.splitlines() if line.strip()}
        # 至少半数修复行出现即视为修复代码存在
        match_count = sum(1 for s in valid_lines if _normalize_whitespace(s) in file_lines)
        return match_count >= max(1, len(valid_lines) // 2)
    except (subprocess.TimeoutExpired, OSError):
        return None


def _check_finding_status(
    repo_path: Path, new_sha: str, file_path: str,
    code_snippet: str, fix_snippet: str | None = None,
) -> str | None:
    """判定 finding 状态（三级判定）。

    Returns:
        'addressed'    — 问题代码消失 且 修复代码出现（或无 fix_snippet 可比较）
        'coincidental' — 问题代码消失 但 修复代码未出现（巧合变更）
        'alive'        — 问题代码仍在
        None           — 无法判断
    """
    alive = _check_snippet_alive(repo_path, new_sha, file_path, code_snippet)
    if alive is None:
        return None
    if alive is True:
        return "alive"
    # 问题代码已消失，进一步判断是否真正采纳
    if fix_snippet:
        fix_present = _check_fix_snippet_present(repo_path, new_sha, file_path, fix_snippet)
        if fix_present is True:
            return "addressed"
        if fix_present is False:
            return "coincidental"
        # fix_present is None → 无法判断修复代码，保守标为 addressed
    return "addressed"


def _track_outcomes(
    conn: sqlite3.Connection, repo_path: Path, repo_name: str,
    pr_number: int, new_sha: str, log=print,
) -> int:
    """对该 PR 的 pending findings 做存活性检测，返回更新数。"""
    rows = conn.execute(
        """SELECT f.id, f.code_snippet, f.file_path, f.fix_snippet
           FROM findings f
           JOIN reviews r ON f.review_id = r.id
           WHERE r.pr_number = ? AND r.repo = ? AND f.outcome IS NULL
                 AND f.code_snippet IS NOT NULL""",
        (pr_number, repo_name),
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fid, snippet, fpath, fix_snip in rows:
        status = _check_finding_status(repo_path, new_sha, fpath, snippet, fix_snip)
        if status == "addressed":
            conn.execute(
                """UPDATE findings SET outcome='addressed', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (new_sha, now, fid),
            )
            updated += 1
        elif status == "coincidental":
            conn.execute(
                """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                   outcome_detail='coincidental_change',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (new_sha, now, fid),
            )
            updated += 1
        # status is None → 保持 NULL，留给下一次追踪或最终判定
        # status == 'alive' → 保持 pending，等最终判定
    conn.commit()
    if updated:
        log(f"  追踪结果：{updated}/{len(rows)} 个旧发现已更新状态")
    return updated


# 开发者回复关键词分类
_REPLY_POSITIVE = re.compile(
    r"已修复|已改|已修改|fixed|done|好的|改了|修改了|已处理|已解决|感谢指出",
    re.IGNORECASE,
)
_REPLY_NEGATIVE = re.compile(
    r"不是问题|误报|false.?positive|无需修改|by.?design|设计如此|不需要改|不用改",
    re.IGNORECASE,
)
_REPLY_DEFERRED = re.compile(
    r"下个版本|后续处理|TODO|单独提.?PR|后续.?PR|后面再|稍后|postpone|later",
    re.IGNORECASE,
)


def _classify_reply(text: str) -> str | None:
    """对开发者回复做关键词分类。返回 positive/negative/deferred/None。"""
    if _REPLY_POSITIVE.search(text):
        return "positive"
    if _REPLY_NEGATIVE.search(text):
        return "negative"
    if _REPLY_DEFERRED.search(text):
        return "deferred"
    return None


def _harvest_replies(
    conn: sqlite3.Connection, repo: "RepoConfig", token: str,
    pr_number: int, repo_name: str, log=print,
) -> int:
    """扫描 PR 评论中的开发者回复，关联到 AI findings。返回采集数。"""
    if not token:
        return 0
    # 查看是否有该 PR 的 findings
    review_rows = conn.execute(
        "SELECT id FROM reviews WHERE pr_number=? AND repo=?",
        (pr_number, repo_name),
    ).fetchall()
    if not review_rows:
        return 0

    comments = _fetch_all_pr_comments(repo, token, pr_number)
    if not comments:
        return 0

    # 分离 AI 评论和非 AI 评论
    ai_comments = []
    human_comments = []
    for c in comments:
        body = c.get("body", "")
        is_ai = (
            body.startswith(AI_REVIEW_MARKER)
            or (body.startswith("**[") and AI_REVIEW_MARKER in body[:200])
            or AI_INLINE_MARKER in body
        )
        if is_ai:
            ai_comments.append(c)
        else:
            human_comments.append(c)

    if not human_comments:
        return 0

    # 收集 AI 评论所在的 discussion_id，只采集同线程的回复
    ai_discussion_ids = {c.get("discussion_id") for c in ai_comments if c.get("discussion_id")}
    # AI 行内评论可能嵌入了 finding 编号: <!-- AI_FINDING:3 -->
    ai_finding_map: dict[str, int] = {}  # discussion_id → finding_index
    for ac in ai_comments:
        did = ac.get("discussion_id", "")
        body = ac.get("body", "")
        m = re.search(r"<!-- AI_FINDING:(\d+) -->", body)
        if m and did:
            ai_finding_map[did] = int(m.group(1))

    harvested = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for hc in human_comments:
        # 只处理和 AI 评论在同一 discussion 线程里的回复
        hc_did = hc.get("discussion_id", "")
        if hc_did not in ai_discussion_ids:
            continue

        reply_body = hc.get("body", "")
        classification = _classify_reply(reply_body)
        if not classification:
            continue

        latest_review = conn.execute(
            """SELECT id FROM reviews WHERE pr_number=? AND repo=?
               ORDER BY review_timestamp DESC LIMIT 1""",
            (pr_number, repo_name),
        ).fetchone()
        if not latest_review:
            continue

        review_id = latest_review[0]
        detail = f"[{classification}] {reply_body[:200]}"

        # 优先通过 AI_FINDING 标记精确关联到具体 finding
        target_idx = ai_finding_map.get(hc_did)
        # 根据分类决定 outcome 更新（仅在 outcome IS NULL 时设置，不覆盖 snippet_search 结果）
        outcome_val = None
        if classification == "positive":
            outcome_val = "addressed"
        elif classification == "negative":
            outcome_val = "persisted"

        if target_idx is not None:
            row = conn.execute(
                """SELECT id, outcome FROM findings
                   WHERE review_id=? AND finding_index=? AND outcome_detail IS NULL""",
                (review_id, target_idx),
            ).fetchone()
            if row:
                fid, existing_outcome = row
                if outcome_val and existing_outcome is None:
                    conn.execute(
                        """UPDATE findings SET outcome=?, outcome_method='developer_reply',
                           outcome_detail=?, outcome_timestamp=? WHERE id=?""",
                        (outcome_val, detail, now, fid),
                    )
                else:
                    conn.execute(
                        "UPDATE findings SET outcome_detail=?, outcome_timestamp=? WHERE id=?",
                        (detail, now, fid),
                    )
                harvested += 1
                continue

        # 回退：总结评论下的回复，标记为 PR 级别反馈
        row = conn.execute(
            """SELECT id, outcome FROM findings
               WHERE review_id=? AND outcome_detail IS NULL
               ORDER BY finding_index LIMIT 1""",
            (review_id,),
        ).fetchone()
        if row:
            fid, existing_outcome = row
            detail = f"[{classification}:summary] {reply_body[:200]}"
            if outcome_val and existing_outcome is None:
                conn.execute(
                    """UPDATE findings SET outcome=?, outcome_method='developer_reply',
                       outcome_detail=?, outcome_timestamp=? WHERE id=?""",
                    (outcome_val, detail, now, fid),
                )
            else:
                conn.execute(
                    "UPDATE findings SET outcome_detail=?, outcome_timestamp=? WHERE id=?",
                    (detail, now, fid),
                )
            harvested += 1

    conn.commit()
    if harvested:
        log(f"  采集到 {harvested} 条开发者回复")
    return harvested


def _finalize_outcomes(
    conn: sqlite3.Connection, repo_path: Path, repo_name: str,
    pr_number: int, final_sha: str, log=print,
) -> int:
    """对已合并 PR 做最终结果判定：仍 pending 的 findings → persisted。"""
    rows = conn.execute(
        """SELECT f.id, f.code_snippet, f.file_path, f.fix_snippet
           FROM findings f
           JOIN reviews r ON f.review_id = r.id
           WHERE r.pr_number = ? AND r.repo = ? AND f.outcome IS NULL""",
        (pr_number, repo_name),
    ).fetchall()
    if not rows:
        return 0

    updated = 0
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for fid, snippet, fpath, fix_snip in rows:
        if snippet and fpath:
            status = _check_finding_status(repo_path, final_sha, fpath, snippet, fix_snip)
            if status == "addressed":
                conn.execute(
                    """UPDATE findings SET outcome='addressed', outcome_method='snippet_search',
                       outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                    (final_sha, now, fid),
                )
                updated += 1
                continue
            if status == "coincidental":
                conn.execute(
                    """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                       outcome_detail='coincidental_change',
                       outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                    (final_sha, now, fid),
                )
                updated += 1
                continue
        # snippet 仍存在 或 无法判断 → persisted（最终判定）
        if snippet:
            conn.execute(
                """UPDATE findings SET outcome='persisted', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (final_sha, now, fid),
            )
        else:
            conn.execute(
                """UPDATE findings SET outcome='indeterminate', outcome_method='snippet_search',
                   outcome_sha=?, outcome_timestamp=? WHERE id=?""",
                (final_sha, now, fid),
            )
        updated += 1
    conn.commit()
    if updated:
        log(f"  最终判定：{updated} 个 findings 已分类")
    return updated


def _main_stats(args: argparse.Namespace, repo_name: str | None) -> None:
    """输出采纳率统计报告。repo_name=None 时显示各仓库分别统计 + 汇总。"""
    if not TRACKING_DB.exists():
        print(f"  {_warn('追踪数据库不存在，请先运行审查或 --import-logs')}")
        return

    conn = sqlite3.connect(str(TRACKING_DB))
    days = getattr(args, "days", 30)

    from datetime import timedelta
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    start_str = start_date.strftime("%Y-%m-%d")
    end_str = end_date.strftime("%Y-%m-%d")

    detail = getattr(args, "detail", False)

    if repo_name:
        _print_stats_for_repo(conn, repo_name, start_str, end_str)
        if detail:
            _print_findings_detail(conn, repo_name, start_str)
    else:
        repos = [r[0] for r in conn.execute(
            "SELECT DISTINCT repo FROM reviews WHERE review_timestamp >= ? ORDER BY repo",
            (start_str,),
        ).fetchall()]
        if not repos:
            print(f"  {_warn(f'在 {start_str} ~ {end_str} 期间没有找到审查数据')}")
            conn.close()
            return
        for rn in repos:
            _print_stats_for_repo(conn, rn, start_str, end_str)
            if detail:
                _print_findings_detail(conn, rn, start_str)
        if len(repos) > 1:
            print(f"  {'━' * 50}")
            _print_stats_for_repo(conn, None, start_str, end_str, title="汇总")

    conn.close()


def _print_stats_for_repo(
    conn: "sqlite3.Connection", repo_name: str | None,
    start_str: str, end_str: str, title: str | None = None,
) -> None:
    """输出单个仓库（或全部仓库汇总）的统计。repo_name=None 表示不做 repo 过滤。"""
    where = "WHERE r.review_timestamp >= ?"
    params: list = [start_str]
    if repo_name:
        where += " AND r.repo = ?"
        params.append(repo_name)

    row = conn.execute(
        f"SELECT COUNT(DISTINCT r.id), COUNT(DISTINCT r.pr_number), SUM(r.review_round) "
        f"FROM reviews r {where}", params,
    ).fetchone()
    total_prs, total_rounds = row[1] or 0, row[2] or 0

    row = conn.execute(
        f"SELECT COUNT(*) FROM findings f JOIN reviews r ON f.review_id=r.id {where}",
        params,
    ).fetchone()
    total_findings = row[0] or 0

    if total_findings == 0:
        label = title or repo_name or "全部"
        print(f"  {_warn(f'{label}: 在 {start_str} ~ {end_str} 期间没有找到审查数据')}")
        return

    dist = conn.execute(
        f"""SELECT
            SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome='persisted' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome='indeterminate' THEN 1 ELSE 0 END),
            SUM(CASE WHEN f.outcome IS NULL THEN 1 ELSE 0 END)
           FROM findings f JOIN reviews r ON f.review_id=r.id {where}""",
        params,
    ).fetchone()
    addressed, persisted = dist[0] or 0, dist[1] or 0
    indeterminate, untracked = dist[2] or 0, dist[3] or 0

    _pct = lambda n: f"{n/total_findings*100:.1f}%" if total_findings else "0%"
    heading = title or repo_name or "全部"

    print()
    print(f"  {_bold(heading)} {_dim(f'({start_str} ~ {end_str})')}")
    print(f"  {'─' * 50}")
    print(f"  审查 PR: {_bold(str(total_prs))}  |  发现: {_bold(str(total_findings))}  |  轮次: {_bold(str(total_rounds))}")
    print()

    # 结果分布
    print(f"  结果分布")
    print(f"    {_green('已采纳')}    {addressed:>4}  {_dim(_pct(addressed)):>6}")
    print(f"    {_red('未采纳')}    {persisted:>4}  {_dim(_pct(persisted)):>6}")
    print(f"    {_yellow('不确定')}    {indeterminate:>4}  {_dim(_pct(indeterminate)):>6}")
    print(f"    {_dim('未追踪')}    {untracked:>4}  {_dim(_pct(untracked)):>6}")

    # 按严重级别 / 置信度（只显示有已结案数据的行）
    _sev_color = {"严重": _red, "一般": _yellow, "建议": _cyan}
    _conf_color = {"确定": _green, "较确定": _blue, "待确认": _dim}
    for group_label, col_name, items, color_map in [
        ("按严重级别", "severity", [("严重", 2), ("一般", 2), ("建议", 2)], _sev_color),
        ("按置信度",   "confidence", [("确定", 4), ("较确定", 2), ("待确认", 2)], _conf_color),
    ]:
        group_rows = []
        for val, pad_width in items:
            row = conn.execute(
                f"""SELECT
                    SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END),
                    COUNT(*)
                   FROM findings f JOIN reviews r ON f.review_id=r.id
                   {where} AND f.{col_name}=?""",
                params + [val],
            ).fetchone()
            addr, total = row[0] or 0, row[1] or 0
            if total > 0:
                rate = f"{addr/total*100:.0f}%"
                padding = " " * pad_width
                color_fn = color_map.get(val, lambda x: x)
                group_rows.append(f"    {color_fn(val)}{padding}{rate:>5}  {_dim(f'({addr}/{total})')}")
        if group_rows:
            print()
            print(f"  {group_label}")
            for line in group_rows:
                print(line)

    # 按规则 — 采纳率最低/最高 Top 5
    for label, order, show_suggestion in [
        ("采纳率最低 Top 5 (候选降权)", "ASC", True),
        ("采纳率最高 Top 5 (高价值)", "DESC", False),
    ]:
        rows = conn.execute(
            f"""SELECT f.rule,
                COUNT(*) as total,
                SUM(CASE WHEN f.outcome='addressed' THEN 1 ELSE 0 END) as addr
               FROM findings f JOIN reviews r ON f.review_id=r.id
               {where} AND f.rule IS NOT NULL
               GROUP BY f.rule HAVING total >= 3
               ORDER BY CAST(addr AS REAL)/total {order} LIMIT 5""",
            params,
        ).fetchall()
        if not rows:
            continue
        print()
        print(f"  {label}")
        for rule, total, addr in rows:
            rate = f"{addr/total*100:.0f}%" if total > 0 else "  -"
            tag = _red("  ← 降权") if show_suggestion and total > 0 and addr / total < 0.3 else ""
            print(f"    {_dim(rule or '?'):<20} {total:>3}  {rate:>5}{tag}")

    print()


_SEV_ICON = {"严重": _red("●"), "一般": _yellow("●"), "建议": _cyan("○")}
_OUTCOME_LABEL = {
    "addressed": _green("已采纳"),
    "persisted": _red("未采纳"),
    "indeterminate": _dim("不确定"),
}


def _normalize_location_lines(text: str) -> str:
    """统一审查文本中 '位置：`file:lines`' 的行号格式。"""
    def _repl(m):
        path, nums = m.group(1), m.group(2)
        return f"位置：`{path}:{_compact_line_numbers(nums)}`"
    return re.sub(r"位置[：:]\s*`([^:``]+):([^`]+)`", _repl, text)


def _compact_line_numbers(raw: str) -> str:
    """'119, 124' → '119,124'；'119, 120, 121' → '119-121'；已是范围格式则原样返回。"""
    if "-" in raw and "," not in raw:
        return raw.strip()
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    nums = []
    for p in parts:
        if "-" in p:
            return raw.replace(" ", "")
        try:
            nums.append(int(p))
        except ValueError:
            return raw.replace(" ", "")
    if not nums:
        return raw.strip()
    nums.sort()
    # 检查是否连续
    if len(nums) > 1 and nums[-1] - nums[0] == len(nums) - 1:
        return f"{nums[0]}-{nums[-1]}"
    return ",".join(str(n) for n in nums)


def _print_findings_detail(
    conn: "sqlite3.Connection", repo_name: str, start_str: str,
) -> None:
    """按 PR 分组列出每条 finding 的明细。"""
    rows = conn.execute(
        """SELECT r.pr_number, r.pr_title, f.finding_index, f.severity,
                  f.title, f.file_path, f.line_numbers, f.outcome, f.outcome_detail,
                  f.outcome_sha
           FROM findings f JOIN reviews r ON f.review_id=r.id
           WHERE r.review_timestamp >= ? AND r.repo = ?
           ORDER BY r.pr_number, f.finding_index""",
        (start_str, repo_name),
    ).fetchall()
    if not rows:
        return

    print(f"  检视意见明细")
    print(f"  {'─' * 50}")

    cur_pr = None
    for pr_num, pr_title, idx, sev, title, fpath, lines, outcome, detail, outcome_sha in rows:
        if pr_num != cur_pr:
            cur_pr = pr_num
            print(f"  PR #{pr_num} {_dim(pr_title or '')}")

        icon = _SEV_ICON.get(sev, "·")
        outcome_str = _OUTCOME_LABEL.get(outcome, _dim("待定")) if outcome else _dim("待定")

        # 位置信息
        loc = ""
        if fpath:
            short = fpath.rsplit("/", 1)[-1] if "/" in fpath else fpath
            loc = f"{short}"
            if lines:
                loc += f":{_compact_line_numbers(lines)}"

        # 位置 + 状态放一行紧凑显示
        status_parts = [outcome_str]
        if outcome_sha:
            status_parts.append(_dim(outcome_sha[:8]))
        if loc:
            status_parts.append(_dim(loc))
        print(f"    {icon} #{idx} [{sev}] {title}")
        print(f"      {' · '.join(status_parts)}")
        if detail:
            # 提取分类标签和回复摘要分开显示
            # detail 格式: "[positive] 已修复..." 或 "[deferred] ..."
            import re as _re
            m = _re.match(r'\[(\w+)\]\s*(.*)', detail, _re.DOTALL)
            if m:
                reply_tag = m.group(1)
                reply_text = m.group(2).split("\n")[0][:60].strip()
                tag_label = {"positive": _green("采纳"), "negative": _red("拒绝"),
                             "deferred": _yellow("延后")}.get(reply_tag, reply_tag)
                print(f"      开发者: {tag_label} {_dim(reply_text) if reply_text else ''}")

    print()


def _main_track(repo: "RepoConfig", args: argparse.Namespace, token: str) -> None:
    """手动触发结果追踪：对已合并 PR 做最终分类。"""
    conn = _init_tracking_db()
    repo_name = repo.full_name

    # 获取需要追踪的 PR（有 pending findings 的）
    if getattr(args, "pr", None):
        pr_numbers = args.pr
    else:
        rows = conn.execute(
            """SELECT DISTINCT r.pr_number FROM findings f
               JOIN reviews r ON f.review_id=r.id
               WHERE r.repo=? AND f.outcome IS NULL""",
            (repo_name,),
        ).fetchall()
        pr_numbers = [r[0] for r in rows]

    if not pr_numbers:
        print(f"  {_dim('没有需要追踪的 pending findings')}")
        conn.close()
        return

    print(f"追踪 {len(pr_numbers)} 个 PR 的审查结果")
    total_updated = 0
    for pr_num in pr_numbers:
        # 获取 PR 状态
        try:
            pr_data = api_get(f"{repo.api_prefix}/pulls/{pr_num}", token)
            state = pr_data.get("state", "unknown") if pr_data else "unknown"
            head_sha = pr_data.get("head", {}).get("sha", "") if pr_data else ""
        except Exception:
            print(f"  PR #{pr_num}: 获取状态失败，跳过")
            continue

        print(f"  PR #{pr_num}: 状态={state}")
        if state == "merged":
            # 先采集开发者回复，让 positive/negative 参与判定
            _harvest_replies(conn, repo, token, pr_num, repo_name)
            # 用 head_sha 做最终判定
            if head_sha:
                # 尝试 fetch
                subprocess.run(
                    ["git", "fetch", "origin", head_sha],
                    capture_output=True, timeout=30, cwd=str(repo.path),
                )
            n = _finalize_outcomes(conn, repo.path, repo_name, pr_num, head_sha)
            total_updated += n
        elif state == "closed":
            # closed 未合并 → indeterminate
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            n = conn.execute(
                """UPDATE findings SET outcome='indeterminate',
                   outcome_method='pr_closed', outcome_timestamp=?
                   WHERE outcome IS NULL AND review_id IN
                   (SELECT id FROM reviews WHERE pr_number=? AND repo=?)""",
                (now, pr_num, repo_name),
            ).rowcount
            conn.commit()
            total_updated += n
            if n:
                print(f"    {n} 个 findings 标记为 indeterminate (PR closed)")
        elif state == "open" and head_sha:
            # 先采集开发者回复
            _harvest_replies(conn, repo, token, pr_num, repo_name)
            # open PR：做中间追踪
            subprocess.run(
                ["git", "fetch", "origin", head_sha],
                capture_output=True, timeout=30, cwd=str(repo.path),
            )
            n = _track_outcomes(conn, repo.path, repo_name, pr_num, head_sha)
            total_updated += n

    print(f"\n追踪完成：共更新 {total_updated} 个 findings")
    conn.close()


def _main_import_logs(repo: "RepoConfig", args: argparse.Namespace) -> None:
    """从 log/by_pr/pr_*_review.md 导入历史审查数据。"""
    conn = _init_tracking_db()
    repo_name = repo.full_name
    log_dir = repo.pr_log_dir

    if not log_dir.exists():
        print(f"  {_warn(f'日志目录不存在：{log_dir}')}")
        conn.close()
        return

    review_files = sorted(log_dir.glob("pr_*_review.md"))
    if not review_files:
        print(f"  {_warn('未找到审查日志文件')}")
        conn.close()
        return

    print(f"导入 {len(review_files)} 个历史审查报告")
    imported_reviews = 0
    imported_findings = 0
    skipped = 0

    for fpath in review_files:
        # 从文件名提取 PR 编号
        m = re.search(r"pr_(\d+)_review\.md", fpath.name)
        if not m:
            continue
        pr_number = int(m.group(1))

        content = fpath.read_text(encoding="utf-8")

        # 解析元数据表
        pr_title = ""
        pr_author = ""
        head_sha = ""
        review_timestamp = ""
        severity_summary = ""

        title_m = re.search(r"\|\s*标题\s*\|\s*(.+?)\s*\|", content)
        if title_m:
            pr_title = title_m.group(1).strip()
        author_m = re.search(r"\|\s*作者\s*\|\s*(.+?)\s*\|", content)
        if author_m:
            pr_author = author_m.group(1).strip()
        sha_m = re.search(r"\|\s*基线提交\s*\|\s*(\w+)\s*\|", content)
        if sha_m:
            head_sha = sha_m.group(1).strip()
        time_m = re.search(r"\|\s*审查时间\s*\|\s*(.+?)\s*\|", content)
        if time_m:
            review_timestamp = time_m.group(1).strip()
        summary_m = re.search(r"(严重\s*\d+\s*/\s*一般\s*\d+(?:\s*/\s*建议\s*\d+)?)", content)
        if summary_m:
            severity_summary = summary_m.group(1).strip()

        if not head_sha:
            head_sha = f"imported_{pr_number}"
        if not review_timestamp:
            # 从文件修改时间获取
            mtime = fpath.stat().st_mtime
            review_timestamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")

        # 解析 findings
        findings = _extract_all_findings(content)

        # 写入 review
        review_round = conn.execute(
            "SELECT COUNT(*) FROM reviews WHERE pr_number=? AND repo=?",
            (pr_number, repo_name),
        ).fetchone()[0] + 1

        try:
            cur = conn.execute(
                """INSERT INTO reviews
                   (pr_number, repo, pr_title, pr_author, head_sha, review_timestamp,
                    review_round, finding_count, severity_summary, cost_usd, duration_ms)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)""",
                (pr_number, repo_name, pr_title, pr_author, head_sha, review_timestamp,
                 review_round, len(findings), severity_summary),
            )
            review_id = cur.lastrowid
            imported_reviews += 1
        except sqlite3.IntegrityError:
            skipped += 1
            continue

        # 写入 findings
        n = _save_findings(conn, review_id, findings)
        imported_findings += n
        snippet_count = sum(1 for f in findings if f.get("code_snippet"))
        print(f"  PR #{pr_number}: {n} 个 findings (snippet: {snippet_count}/{n})")

    conn.close()
    print(f"\n导入完成：{imported_reviews} 个审查, {imported_findings} 个 findings"
          f"{f', 跳过 {skipped} 个重复' if skipped else ''}")


def _resolve_comment_url(resp: dict, repo: RepoConfig, token: str, pr_number: int) -> str | None:
    """从 POST 响应中构建评论永久链接。

    POST 返回的 id 是 discussion_id（hex 字符串），numeric note id
    需要从嵌套 notes 或回查 GET 接口获取。
    """
    did = str(resp["id"])
    base = f"{repo.url}/merge_requests/{pr_number}?ref=&did={did}"

    # 尝试从 POST 响应的嵌套 notes 中获取 numeric id
    notes = resp.get("notes")
    if isinstance(notes, list) and notes:
        nid = notes[0].get("id")
        if isinstance(nid, int):
            return f"{base}#tid-{nid}"

    # 回退：GET 最近的评论，按 discussion_id 匹配
    try:
        comments = api_get(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"page": 1, "per_page": 5, "sort": "created", "direction": "desc"},
        )
        if isinstance(comments, list):
            for c in comments:
                if str(c.get("discussion_id", "")) == did:
                    return f"{base}#tid-{c['id']}"
    except Exception:
        pass

    return base


def post_review_comment(repo: RepoConfig, token: str, pr_number: int, pr_title: str, author: str,
                        review_text: str, skip_delete: bool = False,
                        head_sha: str = "") -> bool:
    """将审查结果发布为 PR 评论。先删除旧的 AI 评论，再发布新评论。返回是否成功。"""
    # 删除旧评论（inline 模式已提前删除，skip_delete=True 避免重复）
    if not skip_delete:
        deleted = delete_old_review_comments(repo, token, pr_number)
        if deleted > 0:
            print(f"  {_dim(f'已删除 {deleted} 条旧的 AI 审查评论')}")

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    header = (
        f"{AI_REVIEW_MARKER}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
    )
    sha_tag = f"\n<!-- REVIEWED_SHA:{head_sha} -->" if head_sha else ""
    footer = (
        "\n\n---\n\n"
        "<sub>此评论由 AI 自动生成，仅供参考，请结合实际情况判断。</sub>"
        f"{sha_tag}"
    )

    review_text = _normalize_location_lines(review_text)
    full_text = header + review_text + footer
    parts = _split_comment(full_text)

    success = True
    for i, part in enumerate(parts):
        resp = api_post(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": part},
        )
        if resp is None:
            print(f"  {_fail(f'第 {i + 1}/{len(parts)} 条评论发布失败')}")
            success = False
            break
        else:
            print(f"  {_ok(f'评论 {i + 1}/{len(parts)} 发布成功')}")
            if isinstance(resp, dict) and resp.get("id"):
                url = _resolve_comment_url(resp, repo, token, pr_number)
                print(f"  {_dim(url)}")

    return success


def _post_review_comment_quiet(
    repo: RepoConfig, token: str, pr_number: int, pr_title: str, author: str,
    review_text: str, buf: io.StringIO, head_sha: str = "",
) -> bool:
    """发布完整审查评论（不删除旧评论，日志输出到 buf）。供 inline 模式使用。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    summary = _extract_issue_summary(review_text)
    summary_row = f"\n| 发现 | {summary} |" if summary else ""

    header = (
        f"{AI_REVIEW_MARKER}\n\n"
        f"| 属性 | 值 |\n"
        f"|------|------|\n"
        f"| 标题 | {pr_title} |\n"
        f"| 作者 | {author} |\n"
        f"| 链接 | [{repo.url}/merge_requests/{pr_number}]({repo.url}/merge_requests/{pr_number}) |\n"
        f"| 审查时间 | {now} |\n"
        f"| 审查工具 | Claude Code (`vibe-review` skill) |\n"
        f"| 基线提交 | {head_sha[:12]} |{summary_row}\n\n"
        f"---\n\n"
    )
    sha_tag = f"\n<!-- REVIEWED_SHA:{head_sha} -->" if head_sha else ""
    footer = (
        "\n\n---\n\n"
        "<sub>此评论由 AI 自动生成，仅供参考，请结合实际情况判断。</sub>"
        f"{sha_tag}"
    )

    review_text = _normalize_location_lines(review_text)
    full_text = header + review_text + footer
    parts = _split_comment(full_text)

    success = True
    for i, part in enumerate(parts):
        resp = api_post(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": part},
        )
        if resp is None:
            buf.write(f"  {_fail(f'第 {i + 1}/{len(parts)} 条评论发布失败')}\n")
            success = False
            break
        else:
            buf.write(f"  {_ok(f'评论 {i + 1}/{len(parts)} 发布成功')}\n")
            if isinstance(resp, dict) and resp.get("id"):
                url = _resolve_comment_url(resp, repo, token, pr_number)
                buf.write(f"  {_dim(url)}\n")

    return success


def _post_inline_comments(
    repo: RepoConfig, token: str, pr_number: int, commit_id: str,
    findings: list[InlineFinding],
    files: list[dict], buf,
    file_position_maps: dict[str, dict[int, tuple[int, bool]]] | None = None,
) -> tuple[int, list[InlineFinding]]:
    """发布行内评论（并行），返回 (成功数, 未能发布的 findings 列表)。

    GitCode API 要求使用 form-encoded 格式（非 JSON）来设置行内评论的
    path/position/commit_id 字段。position 参数为源码行号（非 diff 相对位置）。
    """
    # 构建 diff 中存在的文件名集合和行号映射（用于校验 finding 是否在 diff 内）
    if file_position_maps is None:
        file_position_maps = {}
        for f in files:
            fname = get_filename(f)
            raw_diff = get_file_diff(f)
            if raw_diff:
                file_position_maps[fname] = _build_diff_position_map(raw_diff)

    # 第一步：筛选可发布的 findings
    to_post: list[InlineFinding] = []
    unmapped: list[InlineFinding] = []

    for finding in findings:
        pos_map = file_position_maps.get(finding.file)
        if pos_map is None:
            buf.write(f"  {_skip(f'文件不在 diff 中：{finding.file}:{finding.line}')}\n")
            unmapped.append(finding)
            continue

        pos_info = pos_map.get(finding.line)
        if pos_info is None:
            buf.write(f"  {_skip(f'行号不在 diff 中：{finding.file}:{finding.line}')}\n")
            unmapped.append(finding)
            continue

        _position, is_added = pos_info
        if not is_added and finding.severity == "建议":
            buf.write(f"  {_skip(f'#{finding.id} [{finding.severity}] {finding.file}:{finding.line} (非新增行，跳过)')}\n")
            unmapped.append(finding)
            continue

        to_post.append(finding)

    if not to_post:
        return 0, unmapped

    # 第 1.5 步：校验并修正行号（用代码片段在 diff 中验证）
    file_content_maps: dict[str, dict[int, str]] = {}
    for f in files:
        fname = get_filename(f)
        raw_diff = get_file_diff(f)
        if raw_diff:
            file_content_maps[fname] = _build_diff_line_content(raw_diff)

    for finding in to_post:
        cm = file_content_maps.get(finding.file)
        if cm is None:
            continue
        corrected = _verify_and_correct_line(finding, cm)
        if corrected != finding.line:
            buf.write(f"  {_dim(f'#{finding.id} 行号修正：{finding.line}→{corrected}')}\n")
            finding.line = corrected

    # 第二步：并行发布行内评论（form-encoded，position=源码行号）
    def _post_one(finding: InlineFinding) -> tuple[InlineFinding, bool]:
        comment_body = (
            f"**[{finding.severity}]** {finding.title}\n\n"
            f"{finding.body}\n\n"
            f"{AI_INLINE_MARKER}"
        )
        resp = api_post_form(
            f"{repo.api_prefix}/pulls/{pr_number}/comments",
            token,
            {"body": comment_body, "commit_id": commit_id,
             "path": finding.file, "position": finding.line},
        )
        return finding, resp is not None

    posted_count = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
        futures = [pool.submit(_post_one, f) for f in to_post]
        for future in concurrent.futures.as_completed(futures):
            finding, ok = future.result()
            if ok:
                posted_count += 1
            else:
                buf.write(f"  {_fail(f'API 发布失败：{finding.file}:{finding.line}')}\n")
                unmapped.append(finding)

    return posted_count, unmapped


@dataclass
class PRResult:
    """单个 PR 审查结果。"""
    pr_number: int
    pr_title: str
    output_file: Path | None
    posted: bool
    stats: ReviewStats
    log: str  # 该 PR 处理过程的日志文本
    success: bool = True  # 审查是否成功产出结果
    skipped: bool = False  # 已审查过，本次跳过


def _review_single_pr(
    repo: RepoConfig, pr: dict, index: int, total: int, args, token: str,
    save_local: bool, output_dir: Path, show_progress: bool = False,
    direct_output: bool = False,
) -> PRResult | None:
    """审查单个 PR，返回结果。

    direct_output=True 时直接输出到 stdout（顺序模式），
    否则缓冲到 StringIO（并行模式，防止输出交叉）。
    """
    buf = _DirectOutput() if direct_output else io.StringIO()
    log = print if direct_output else (lambda s: buf.write(s + "\n"))
    pr_number = pr["number"]
    pr_title = pr["title"]
    pr_start = time.monotonic()

    # 提前获取 head_sha，用于跳过判断和后续传递
    head_sha = pr.get("head", {}).get("sha", "")

    # 检查是否已基于最新提交审查过（--force 跳过此检查）
    if not getattr(args, "force", False) and head_sha and token:
        if _is_already_reviewed(repo, token, pr_number, head_sha):
            buf.write(f"  PR #{pr_number}: 跳过 (已审查 {head_sha[:12]})\n")
            return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue(), skipped=True)

    buf.write(f"{_bold(f'[Step 2.{index + 1}]')} {_dim(_now())} PR #{pr_number}: {pr_title}\n")

    # 获取变更文件
    buf.write(f"  {_dim(_now())} 获取变更文件\n")
    t0 = time.monotonic()
    files = fetch_pr_files(repo, token, pr_number)
    buf.write(f"  {_dim(f'耗时：{_fmt_secs(time.monotonic() - t0)}')}\n")

    if not files:
        buf.write(f"  {_warn('无变更文件或获取失败，跳过。')}\n")
        return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue(), success=False)

    cpp_count = sum(1 for f in files if is_cpp_file(get_filename(f)))
    total_adds = sum(f.get("additions", 0) for f in files)
    total_dels = sum(f.get("deletions", 0) for f in files)
    buf.write(f"  共 {len(files)} 个变更文件 (C/C++: {cpp_count}, {_green(f'+{total_adds}')}, {_red(f'-{total_dels}')})\n")

    # 格式化 diff
    diff_text = format_diff_for_review(repo, pr, files)

    if args.dry_run:
        # dry-run 模式：仅保存 diff 不审查
        diff_file = output_dir / f"pr_{pr_number}_diff.md"
        diff_file.write_text(diff_text, encoding="utf-8")
        buf.write(f"  {_dim(f'[dry-run] Diff 已保存：{_file_link(diff_file)}')}\n")
        return PRResult(pr_number, pr_title, None, False, ReviewStats(), buf.getvalue())

    # 拉取 PR 分支 commit（供 Claude 用 git show 读取文件，也供存活性检测使用）
    if head_sha:
        fetch_result = subprocess.run(
            ["git", "fetch", "origin", head_sha],
            capture_output=True, text=True, timeout=60,
            cwd=str(repo.path),
        )
        if fetch_result.returncode == 0:
            buf.write(f"  {_ok(f'已拉取 PR commit: {head_sha[:12]}')}\n")
        else:
            buf.write(f"  {_warn('拉取 PR commit 失败，Claude 将使用本地文件作为上下文')}\n")

    # [追踪] 对旧发现做存活性检测（必须在 git fetch 之后，确保 new_sha 已在本地）
    if head_sha:
        try:
            _tracking_conn = _init_tracking_db()
            _track_outcomes(_tracking_conn, repo.path, repo.full_name, pr_number, head_sha, log=log)
            _tracking_conn.close()
        except Exception:
            pass  # 追踪失败不影响主流程

    # 调用 Claude Code 审查（第一步：完整审查，prompt 不变，保证质量）
    use_inline = getattr(args, "inline", False)
    buf.write(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行代码审查\n")
    if use_inline:
        buf.write(f"  模式：行内评论 (--inline, 两步法)\n")
    t0 = time.monotonic()
    review_text, stats = run_claude_review(diff_text, pr, repo.path, show_progress=show_progress, log=log)
    review_secs = time.monotonic() - t0
    buf.write(f"  {_dim(f'审查耗时：{_fmt_secs(review_secs)}')}\n")
    buf.write(f"  {_dim(f'Token 消耗：{stats.fmt()}')}\n")

    if review_text is None:
        buf.write(f"  {_warn(f'跳过 PR #{pr_number}（审查无结果）')}\n")
        return PRResult(pr_number, pr_title, None, False, stats, buf.getvalue(), success=False)

    # [追踪] 保存审查结果和 findings 到数据库
    if len(review_text) >= MIN_REVIEW_CHARS:
        try:
            _tracking_conn = _init_tracking_db()
            pr_author = pr.get("user", {}).get("login", "unknown")
            severity_summary = _extract_issue_summary(review_text)
            all_findings = _extract_all_findings(review_text)
            duration_ms = int(review_secs * 1000)
            review_id = _save_review(
                _tracking_conn, repo.full_name, pr_number, pr_title, pr_author,
                head_sha, stats, duration_ms, severity_summary, len(all_findings),
            )
            if review_id and all_findings:
                n_saved = _save_findings(_tracking_conn, review_id, all_findings)
                snippet_count = sum(1 for f in all_findings if f.get("code_snippet"))
                log(f"  [追踪] 已记录 {n_saved} 个 findings (snippet: {snippet_count}/{n_saved})")
            _tracking_conn.close()
        except Exception:
            pass  # 追踪失败不影响主流程

    # 发布到 PR 评论
    posted = False
    if args.comment:
        # [追踪] 删除旧评论前，先采集开发者回复
        try:
            _tracking_conn = _init_tracking_db()
            _harvest_replies(_tracking_conn, repo, token, pr_number, repo.full_name, log=log)
            _tracking_conn.close()
        except Exception:
            pass

        buf.write(f"  {_dim(_now())} 发布审查结果到 PR #{pr_number} 评论区\n")
        t0 = time.monotonic()
        author = pr.get("user", {}).get("login", "unknown")

        if use_inline:
            # 行内评论模式：删除旧评论 → 发完整总结 → 发 inline
            # 顺序很重要：先总结再 inline，避免 inline 被误删
            deleted = delete_old_review_comments(repo, token, pr_number)
            if deleted > 0:
                buf.write(f"  {_dim(f'已删除 {deleted} 条旧的 AI 审查评论')}\n")

            # 发布完整审查结果作为总结评论（已删除旧评论，跳过重复删除）
            posted = _post_review_comment_quiet(
                repo, token, pr_number, pr_title, author, review_text, buf,
                head_sha=head_sha)
            if posted:
                buf.write(f"  {_ok('总结评论发布成功')}\n")
            else:
                buf.write(f"  {_fail('总结评论发布失败')}\n")

            # 预构建 position maps，供提取和发布共用
            fp_maps: dict[str, dict[int, tuple[int, bool]]] = {}
            for f in files:
                fname = get_filename(f)
                raw_diff = get_file_diff(f)
                if raw_diff:
                    fp_maps[fname] = _build_diff_position_map(raw_diff)
            findings = _extract_findings_for_inline(
                review_text, files, buf, file_position_maps=fp_maps)
            if findings:
                posted_count, unmapped = _post_inline_comments(
                    repo, token, pr_number, head_sha, findings, files, buf,
                    file_position_maps=fp_maps)
                inline_msg = f"行内评论：{_green(str(posted_count))} 条已发布"
                if unmapped:
                    inline_msg += f", {_yellow(str(len(unmapped)))} 条未能定位"
                buf.write(f"  {inline_msg}\n")
            else:
                if not posted:
                    buf.write(f"  {_warn('未能提取行内评论数据，回退到常规评论')}\n")
                    posted = post_review_comment(repo, token, pr_number, pr_title, author, review_text,
                                                head_sha=head_sha)
        else:
            # 常规评论（现有逻辑）
            posted = post_review_comment(repo, token, pr_number, pr_title, author, review_text,
                                         head_sha=head_sha)

        buf.write(f"  {_dim(f'发布耗时：{_fmt_secs(time.monotonic() - t0)}')}\n")

    # 保存本地文件
    output_file = None
    if save_local:
        output_file = write_review_md(repo, pr, review_text, output_dir, head_sha=head_sha)
        buf.write(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}\n")
    elif not posted:
        # 不保存且未发布，输出到终端防止结果丢失
        buf.write(f"\n{review_text}\n\n")

    pr_secs = time.monotonic() - pr_start
    buf.write(f"  {_dim(f'PR #{pr_number} 总耗时：{_fmt_secs(pr_secs)}')}\n")
    return PRResult(pr_number, pr_title, output_file, posted, stats, buf.getvalue())


# ======================== 主流程 ========================
def main() -> None:
    parser = argparse.ArgumentParser(
        description="代码审查工具：支持 GitCode PR 审查和本地文件审查（Claude Code）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              %(prog)s                                    # 审查最近 3 个 open PR
              %(prog)s --count 5                          # 审查最近 5 个 open PR
              %(prog)s --pr 1150                          # 审查指定 PR
              %(prog)s --pr 1150 1144 1143                # 审查多个指定 PR
              %(prog)s --pr 1150 1144 1143 -j1            # 强制顺序审查
              %(prog)s --author lilin_137                 # 审查某用户的 open PR（默认最近 3 个）
              %(prog)s --author lilin_137 -n 0            # 审查某用户的全部 open PR
              %(prog)s --author user1 user2 -n 5          # 审查多个用户的 open PR（最多 5 个）
              %(prog)s --team team.txt                    # 审查小组全员的 open PR
              %(prog)s --team team.txt --count 0          # 审查小组全员的所有 open PR
              %(prog)s --team team.txt --state merged -n 5  # 审查小组最近 5 个已合并 PR
              %(prog)s --state merged --count 3           # 审查最近 3 个已合并 PR
              %(prog)s --pr 1150 --save                   # 审查并保存本地文件
              %(prog)s --pr 1150 --comment                # 审查并发布评论到 PR
              %(prog)s --pr 1150 --comment --inline       # 审查并逐行评论到代码
              %(prog)s --pr 1150 --comment --save         # 发布评论 + 保存本地
              %(prog)s --pr 1150 --comment --force        # 强制重新审查（忽略跳过逻辑）
              %(prog)s --pr 1150 --dry-run                # 只拉取 diff 不审查
              %(prog)s --file src/xxx.cpp                 # 审查本地文件
              %(prog)s --file src/a.cpp src/b.h --save    # 审查多个本地文件并保存
              %(prog)s --file src/platform/resource/      # 审查目录下所有 C/C++ 文件
              %(prog)s --repo hcomm --pr 100              # 审查 hcomm 仓库的 PR
              %(prog)s --repo hcomm --file src/x.cpp      # 审查 hcomm 仓库的本地文件
              %(prog)s --clean 1150                       # 清除指定 PR 的 AI 审查评论
              %(prog)s --clean 1150 1144                  # 清除多个 PR 的 AI 审查评论
              %(prog)s --stats                            # 查看审查采纳率统计（默认 30 天）
              %(prog)s --stats --days 90                  # 查看 90 天统计
              %(prog)s --track                            # 追踪所有 pending PR 的结果
              %(prog)s --track --pr 1150                  # 追踪指定 PR 的结果
              %(prog)s --import-logs                      # 导入历史审查日志
        """),
    )
    parser.add_argument("--pr", type=int, nargs="+", metavar="NUM",
                        help="指定 PR 编号（可多个，如 --pr 1150 1144）")
    parser.add_argument("--file", type=str, nargs="+", metavar="PATH",
                        help="审查本地文件或目录（可多个，目录递归扫描 C/C++ 文件，无需 GitCode 令牌）")
    parser.add_argument("--dir", type=str, nargs="+", metavar="DIR",
                        help="审查整个目录（递归扫描 C/C++ 文件，生成合并报告，支持跨文件分析，无需 GitCode 令牌）")
    parser.add_argument("--team", type=Path, metavar="FILE",
                        help="审查小组全员的 PR，需指定人员名单文件路径（如 --team team.txt）")
    parser.add_argument("--author", type=str, nargs="+", metavar="USER",
                        help="按用户名筛选 open PR（可多个，如 --author user1 user2）")
    parser.add_argument("-n", "--count", type=int, default=2,
                        help="审查的 PR 数量上限（默认 2，0 表示全部，--pr 模式下忽略）")
    parser.add_argument("--state", type=str, default="open",
                        choices=["open", "merged", "closed", "all"],
                        help="PR 状态筛选（默认 open，--pr 模式下忽略）")
    parser.add_argument("--token", type=str, default=None,
                        help="GitCode 访问令牌（也可用 GITCODE_TOKEN 环境变量）")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅获取 PR 信息和 diff，不调用 Claude 审查")
    parser.add_argument("--comment", action="store_true",
                        help="将审查结果发布为 PR 评论")
    parser.add_argument("--inline", action="store_true",
                        help="将审查发现评论到代码具体行（需配合 --comment）")
    parser.add_argument("--save", action="store_true",
                        help="保存审查结果到本地文件（默认仅输出到终端）")
    parser.add_argument("-j", "--jobs", type=int, default=0, metavar="N",
                        help="并行审查的最大 PR 数（默认 0 即自动：1 个 PR 顺序，多个 PR 并行，上限 3）")
    parser.add_argument("--force", action="store_true",
                        help="强制审查，忽略已审查过最新提交的判断")
    parser.add_argument("--repo", type=str, default="hcomm", dest="target_repo",
                        help="目标仓库，支持 owner/name（如 myorg/myrepo）或仅 name（默认 owner=cann）")
    parser.add_argument("--clean", type=int, nargs="+", metavar="NUM",
                        help="清除指定 PR 的所有 AI 审查评论（可多个，如 --clean 1150 1144）")
    parser.add_argument("--stats", action="store_true",
                        help="输出 AI 审查采纳率统计报告")
    parser.add_argument("--detail", action="store_true",
                        help="配合 --stats 列出每条检视意见明细")
    parser.add_argument("--track", action="store_true",
                        help="手动触发结果追踪（对已合并 PR 做最终分类）")
    parser.add_argument("--import-logs", action="store_true", dest="import_logs",
                        help="从 log/by_pr/ 导入历史审查数据到追踪数据库")
    parser.add_argument("--days", type=int, default=30,
                        help="--stats 的统计天数范围（默认 30）")
    parser.add_argument("--highlight", type=str, default="",
                        help="高亮显示的 PR 编号（逗号分隔，如 --highlight 385,538），用于标记触发审查的变更 PR")
    args = parser.parse_args()

    # 解析 owner/name，支持 --repo cann/hcomm 或 --repo hcomm（默认 owner=cann）
    if "/" in args.target_repo:
        repo_owner, repo_name = args.target_repo.split("/", 1)
    else:
        repo_owner, repo_name = OWNER, args.target_repo

    # --stats 不依赖本地仓库目录，提前处理
    if args.stats:
        # 用户显式传了 --repo 则只看该仓库，否则看全部
        if any(a in sys.argv for a in ("--repo",)):
            explicit_repo = f"{repo_owner}/{repo_name}"
        else:
            explicit_repo = None
        _main_stats(args, explicit_repo)
        return

    # 初始化仓库配置
    repo_path = REPOS_ROOT / repo_owner / repo_name
    if not repo_path.is_dir():
        print(f"  {_fail(f'本地仓库目录不存在：{repo_path}')}")
        sys.exit(1)
    repo = RepoConfig(name=repo_name, owner=repo_owner, path=repo_path)
    _migrate_legacy_logs(repo)

    if args.clean:
        # --clean 模式：只需要 token，不需要其他参数校验
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('未提供 GitCode 访问令牌')}")
            print("  请通过以下任一方式提供:")
            print("    1. 环境变量：export GITCODE_TOKEN=your_token")
            print("    2. 命令行：  python3 ai_reviewer.py --token your_token")
            sys.exit(1)
        _main_clean(repo, args.clean, token)
        return

    if args.import_logs:
        _main_import_logs(repo, args)
        return

    if args.track:
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('--track 需要 GitCode 访问令牌')}")
            sys.exit(1)
        _main_track(repo, args, token)
        return

    # 互斥校验：--file、--dir、--pr 三者互斥
    mode_flags = sum(bool(x) for x in [args.file, args.dir, args.pr])
    if mode_flags > 1:
        print(f"  {_fail('--file、--dir 和 --pr 不能同时使用')}")
        sys.exit(1)

    if args.inline and not args.comment:
        print(f"  {_fail('--inline 需要配合 --comment 使用')}")
        print("  用法：python3 ai_reviewer.py --pr <number> --comment --inline")
        sys.exit(1)

    # --file / --dir 模式不需要 GitCode 令牌
    token = None
    if not args.file and not args.dir:
        token = args.token or os.environ.get("GITCODE_TOKEN")
        if not token:
            print(f"  {_fail('未提供 GitCode 访问令牌')}")
            print("  请通过以下任一方式提供:")
            print("    1. 环境变量：export GITCODE_TOKEN=your_token")
            print("    2. 命令行：  python3 ai_reviewer.py --token your_token")
            print(f"  获取令牌：{_dim('https://gitcode.com -> 设置 -> 安全设置 -> 私人令牌')}")
            sys.exit(1)

    # 校验 vibe-review skill
    if not args.dry_run and not SKILL_MD_PATH.exists():
        print(f"  {_fail('vibe-review skill 未安装')}")
        print(f"  缺失文件：{_dim(str(SKILL_MD_PATH))}")
        print("  请先安装 vibe-review skill 到 ~/.claude/skills/vibe-review/SKILL.md")
        sys.exit(1)

    save_local = args.save

    if args.dir:
        _main_dir_review(repo, args, save_local)
    elif args.file:
        _main_file_review(repo, args, save_local)
    else:
        _main_pr_review(repo, args, token, save_local)


def _print_results_summary(
    total_secs: float, stats_list: list[ReviewStats],
    item_lines: list[str], parallel_workers: int = 0,
    succeeded: int = 0, failed: int = 0, skipped: int = 0,
) -> None:
    """打印审查结果汇总统计（PR 审查和文件审查共用）。"""
    total_cost = sum(s.best_cost for s in stats_list)

    print(_dim("─" * 60))
    # 构建一行紧凑摘要
    status_parts = []
    if succeeded > 0:
        status_parts.append(_green(f'审查 {succeeded}'))
    if skipped > 0:
        status_parts.append(_dim(f'跳过 {skipped}'))
    if failed > 0:
        status_parts.append(_red(f'失败 {failed}'))
    if status_parts:
        status = " / ".join(status_parts)
    else:
        status = _bold(_green("审查完成!"))
    summary_parts = [f"总耗时：{_bold(_fmt_secs(total_secs))}"]
    if parallel_workers > 1:
        summary_parts.append(f"并行：{parallel_workers}")
    # 多项时展示费用合计和 token 合计（单项已在 Step 中展示过）
    if len(stats_list) > 1 and total_cost > 0:
        summary_parts.append(f"费用合计：{_cyan(f'${total_cost:.4f}')} / {_cyan(f'¥{total_cost * USD_TO_CNY:.4f}')}")
    print(f"  {status} {' | '.join(summary_parts)}")
    if len(stats_list) > 1:
        total_input = sum(s.input_tokens for s in stats_list)
        total_output = sum(s.output_tokens for s in stats_list)
        total_cache_write = sum(s.cache_creation_tokens for s in stats_list)
        total_cache_read = sum(s.cache_read_tokens for s in stats_list)
        if total_input or total_output:
            tok_parts = [f"输入 {total_input:,}", f"输出 {total_output:,}"]
            if total_cache_write:
                tok_parts.append(f"缓存写入 {total_cache_write:,}")
            if total_cache_read:
                tok_parts.append(f"缓存读取 {total_cache_read:,}")
            sep = " / "
            print(f"  {_dim(f'Token 合计：{sep.join(tok_parts)}')}")
    for line in item_lines:
        print(f"  {line}")
    print(_dim("─" * 60))


def _main_clean(repo: RepoConfig, pr_numbers: list[int], token: str):
    """清除指定 PR 的所有 AI 审查评论。"""
    total_deleted = 0
    for pr_number in pr_numbers:
        print(f"PR #{pr_number}: 清除 AI 审查评论")
        deleted = delete_old_review_comments(repo, token, pr_number)
        total_deleted += deleted
        if deleted > 0:
            print(f"  {_ok(f'已删除 {deleted} 条评论')}")
        else:
            print(f"  {_dim('无 AI 审查评论')}")
    if len(pr_numbers) > 1:
        print(f"\n{_ok(f'共删除 {total_deleted} 条 AI 审查评论')}")


def _main_dir_review(repo: RepoConfig, args: argparse.Namespace, save_local: bool) -> None:
    """目录审查主流程：递归扫描 C/C++ 文件，生成一份合并审查报告。"""
    output_dir = repo.dir_log_dir
    if save_local:
        output_dir.mkdir(parents=True, exist_ok=True)

    repo_root = repo.path
    CPP_EXTS = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}

    all_file_paths = []
    dir_labels = []  # 用于显示的目录路径

    for d in args.dir:
        p = Path(d)
        if not p.is_absolute():
            p = repo_root / p
        if not p.exists():
            print(f"  {_warn(f'目录不存在：{d}，跳过')}")
            continue
        if not p.is_dir():
            print(f"  {_warn(f'不是目录：{d}，请使用 --file 审查单个文件')}")
            continue

        found = sorted(
            fp for fp in p.rglob("*") if fp.is_file() and fp.suffix.lower() in CPP_EXTS
        )
        if not found:
            print(f"  {_warn(f'目录中无 C/C++ 文件：{d}，跳过')}")
            continue

        print(f"  扫描目录 {d}: 发现 {len(found)} 个 C/C++ 文件")
        for fp in found:
            try:
                rel = fp.resolve().relative_to(repo_root.resolve())
            except ValueError:
                rel = fp
            all_file_paths.append(str(rel))
        dir_labels.append(d)

    if not all_file_paths:
        print("  无有效文件，退出。")
        sys.exit(0)

    if len(all_file_paths) > MAX_DIR_FILES:
        print(f"  {_fail(f'文件数 ({len(all_file_paths)}) 超过上限 ({MAX_DIR_FILES})')}")
        print("  请缩小目录范围，或使用 --file 逐文件审查。")
        sys.exit(1)

    dir_display = ", ".join(dir_labels)

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('目录代码审查工具')} (跨文件合并审查)")
    print(f"  目录：{dir_display}")
    print(f"  文件数：{len(all_file_paths)}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()

    print(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行跨文件代码审查 ...")
    t0 = time.monotonic()
    review_text, stats = run_claude_dir_review(all_file_paths, repo.path, show_progress=True)
    wall_secs = time.monotonic() - t0
    print(f"  {_dim(f'审查耗时：{_fmt_secs(wall_secs)}')}")
    print(f"  {_dim(f'Token 消耗：{stats.fmt()}')}")

    if review_text is None:
        print(f"  {_warn('审查无结果')}")
        sys.exit(1)

    output_file = None
    if save_local:
        output_file = write_dir_review_md(dir_display, all_file_paths, review_text, output_dir)
        print(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}")
    else:
        print(f"\n{review_text}\n")

    total_secs = time.monotonic() - total_start
    item_lines = [f"目录：{dir_display} ({len(all_file_paths)} 个文件)"]
    if output_file:
        item_lines.append(f"报告：{_file_link(output_file)}")
    _print_results_summary(total_secs, [stats], item_lines)


def _main_file_review(repo: RepoConfig, args: argparse.Namespace, save_local: bool) -> None:
    """本地文件审查主流程。"""
    output_dir = repo.file_log_dir
    if save_local:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 收集待审查文件（支持文件和目录）
    repo_root = repo.path
    CPP_EXTS = {".h", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"}
    file_paths = []
    for f in args.file:
        p = Path(f)
        if not p.is_absolute():
            p = repo_root / p
        if not p.exists():
            print(f"  {_warn(f'路径不存在：{f}，跳过')}")
            continue
        if p.is_dir():
            # 递归扫描目录下所有 C/C++ 文件
            found = sorted(
                fp for fp in p.rglob("*") if fp.is_file() and fp.suffix.lower() in CPP_EXTS
            )
            if not found:
                print(f"  {_warn(f'目录中无 C/C++ 文件：{f}，跳过')}")
                continue
            print(f"  扫描目录 {f}: 发现 {len(found)} 个 C/C++ 文件")
            for fp in found:
                try:
                    rel = fp.resolve().relative_to(repo_root.resolve())
                except ValueError:
                    rel = fp
                file_paths.append(str(rel))
        else:
            if not is_cpp_file(str(p)):
                print(f"  {_warn(f'非 C/C++ 文件：{f}，跳过')}")
                continue
            try:
                rel = p.resolve().relative_to(repo_root.resolve())
            except ValueError:
                rel = p
            file_paths.append(str(rel))

    if not file_paths:
        print("  无有效文件，退出。")
        sys.exit(0)

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('本地文件代码审查工具')}")
    print(f"  文件：{', '.join(file_paths)}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()
    results = []

    for i, file_path in enumerate(file_paths):
        file_start = time.monotonic()
        print(f"{_bold(f'[{i + 1}/{len(file_paths)}]')} {_dim(_now())} {file_path}")

        print(f"  {_dim(_now())} 调用 Claude Code (vibe-review skill) 进行代码审查")
        t0 = time.monotonic()
        review_text, stats = run_claude_file_review(file_path, repo.path, show_progress=True)
        wall_secs = time.monotonic() - t0
        print(f"  {_dim(f'审查耗时：{_fmt_secs(wall_secs)}')}")
        print(f"  {_dim(f'Token 消耗：{stats.fmt()}')}")

        if review_text is None:
            print(f"  {_warn(f'跳过 {file_path}（审查无结果）')}")
            print()
            continue

        output_file = None
        if save_local:
            output_file = write_file_review_md(file_path, review_text, output_dir)
            print(f"  {_ok(f'审查结果已保存：{_file_link(output_file)}')}")
        else:
            print(f"\n{review_text}\n")

        file_secs = time.monotonic() - file_start
        print(f"  {_dim(f'{file_path} 总耗时：{_fmt_secs(file_secs)}')}")
        print()
        results.append((file_path, output_file, stats))

    total_secs = time.monotonic() - total_start
    if results:
        item_lines = []
        for fp, path, st in results:
            detail = f"文件：{_file_link(path)}" if path else _green("完成")
            item_lines.append(f"{fp}: {detail}")
        _print_results_summary(
            total_secs, [s for *_, s in results], item_lines)


def _main_pr_review(repo: RepoConfig, args: argparse.Namespace, token: str, save_local: bool) -> None:
    """PR 审查主流程。"""
    output_dir = repo.pr_log_dir
    if save_local or args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    # 描述当前模式
    if args.pr:
        mode_desc = f"指定 PR: {', '.join(f'#{n}' for n in args.pr)}"
    elif args.team:
        count_desc = "全部" if args.count == 0 else f"最多 {args.count} 个"
        mode_desc = f"小组全员 ({count_desc}, {args.state})"
    elif args.author:
        count_desc = "全部" if args.count == 0 else f"最多 {args.count} 个"
        mode_desc = f"用户：{', '.join(args.author)} ({count_desc}, {args.state})"
    else:
        mode_desc = f"全部 {args.state} PR" if args.count == 0 else f"最近 {args.count} 个 {args.state} PR"

    output_modes = []
    if save_local:
        output_modes.append(f"本地文件 ({output_dir})")
    if args.comment:
        output_modes.append("PR 评论")
    if not output_modes:
        output_modes.append("终端")

    print(_dim("─" * 60))
    print(f"  {_bold('GitCode PR 代码审查工具')}")
    print(f"  仓库：{_cyan(repo.full_name)}")
    print(f"  模式：{mode_desc}")
    print(f"  输出：{' + '.join(output_modes)}")
    print(_dim("─" * 60))
    print()

    total_start = time.monotonic()

    # Step 1: 收集 PR 列表
    print(f"{_bold('[Step 1]')} {_dim(_now())} 获取 PR")
    t0 = time.monotonic()
    prs = collect_prs(repo, token, args)
    print(f"  {_dim(f'耗时：{_fmt_secs(time.monotonic() - t0)}')}")

    # 批量模式下跳过标题含 [WIP] 的 PR（--pr 精确模式不过滤）
    if not args.pr:
        filtered = []
        for pr in prs:
            if "wip" in pr.get("title", "").lower():
                pr_num = pr["number"]
                print(f"  {_skip(f'PR #{pr_num} 标题含 WIP，跳过')}")
            else:
                filtered.append(pr)
        prs = filtered

    if not prs:
        print(f"  {_warn('未找到匹配的 PR，退出。')}")
        sys.exit(0)

    # 按变更规模升序排列（短任务优先，减少总等待时间）
    # GitCode PR 详情接口不含 additions/deletions，需从文件列表接口获取
    def _pr_size(pr: dict) -> int:
        return pr.get("_additions", 0) + pr.get("_deletions", 0)

    if len(prs) > 1:
        print(f"  获取变更统计（用于排序）...")
        for pr in prs:
            files = fetch_pr_files(repo, token, pr["number"])
            pr["_additions"] = sum(f.get("additions", 0) for f in files)
            pr["_deletions"] = sum(f.get("deletions", 0) for f in files)
            pr["_changed_files"] = len(files)
        prs.sort(key=_pr_size)

    # 加载姓名映射（team file 优先，回退到 GitCode API 的 name 字段）
    _team_name: dict[str, str] = {}
    team_file = args.team if args.team else TEAM_FILE
    if team_file.exists():
        _, _team_info = load_team_members(team_file)
        for acct, info in _team_info.items():
            _team_name[acct] = info.split()[0]  # 只取姓名，不要工号

    highlight_prs = set()
    if getattr(args, "highlight", ""):
        highlight_prs = {int(x) for x in args.highlight.split(",") if x.strip().isdigit()}

    print(f"  共 {_bold(str(len(prs)))} 个 PR (按变更规模升序):")
    for pr in prs:
        user = pr.get("user", {})
        login = user.get("login", "?")
        api_name = user.get("name", "")
        team_name = _team_name.get(login, "")
        name_parts = [login]
        if api_name and api_name != login:
            name_parts.append(api_name)
        if team_name and team_name not in name_parts:
            name_parts.append(team_name)
        author = "/".join(name_parts)
        state = pr.get("state", "?")
        adds = pr.get("_additions", "?")
        dels = pr.get("_deletions", "?")
        n_files = pr.get("_changed_files", "")
        size_parts = []
        if n_files:
            size_parts.append(f"{n_files} 文件")
        size_parts.append(f"{_green(f'+{adds}')}/{_red(f'-{dels}')}")
        marker = _yellow("▶ ") if pr["number"] in highlight_prs else "  "
        print(f"  {marker}#{pr['number']} {_dim(f'[{state}]')} ({_cyan(author)}) {pr['title']} {_dim('(' + ', '.join(size_parts) + ')')}")
    print()

    # Step 2: 审查 PR（顺序或并行）
    # 解析并行度
    if args.jobs == 0:
        # 自动：1 个 PR 顺序，多个 PR 自动并行
        max_workers = 1 if len(prs) == 1 else min(len(prs), MAX_PARALLEL_REVIEWS)
    else:
        max_workers = min(args.jobs, len(prs))

    results = []
    if max_workers <= 1:
        # 顺序模式：直接输出到 stdout，每步实时显示
        for i, pr in enumerate(prs):
            result = _review_single_pr(repo, pr, i, len(prs), args, token, save_local, output_dir,
                                       show_progress=True, direct_output=True)
            if result is not None:
                results.append(result)
    else:
        # 并行模式
        print(f"{_bold('[Step 2]')} {_dim(_now())} 并行审查 ({max_workers} 个同时)\n")
        print_lock = threading.Lock()
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_review_single_pr, repo, pr, i, len(prs), args, token, save_local, output_dir): pr
                for i, pr in enumerate(prs)
            }
            for future in concurrent.futures.as_completed(futures):
                result = future.result()
                if result is not None:
                    with print_lock:
                        print(result.log, end="")
                    results.append(result)

    # Step 3: 汇总
    total_secs = time.monotonic() - total_start
    if results:
        item_lines = []
        for r in results:
            if r.skipped:
                pass  # 跳过的 PR 不在最终汇总中显示
            elif r.success:
                parts: list[str] = []
                if r.output_file:
                    parts.append(f"文件：{_file_link(r.output_file)}")
                if r.posted:
                    parts.append(_green("已发布到 PR"))
                detail = " | ".join(parts) if parts else _green("完成")
                item_lines.append(_ok(f"PR #{r.pr_number} ({r.pr_title}): {detail}"))
            else:
                item_lines.append(_fail(f"PR #{r.pr_number} ({r.pr_title}): {_red('审查失败')}"))

        succeeded = sum(1 for r in results if r.success and not r.skipped)
        skipped = sum(1 for r in results if r.skipped)
        failed = sum(1 for r in results if not r.success)
        _print_results_summary(
            total_secs, [r.stats for r in results], item_lines,
            parallel_workers=max_workers, succeeded=succeeded, failed=failed, skipped=skipped)
        if failed > 0:
            sys.exit(1)


if __name__ == "__main__":
    main()
