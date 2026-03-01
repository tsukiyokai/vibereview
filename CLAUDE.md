# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

Vibe Review — CANN代码仓（GitCode平台）的自动化检视机器人。通过`ai_reviewer.py`调用`claude -p`配合vibe-review skill审查PR diff，将检视意见发布为GitCode PR评论。

## 常用命令

```bash
# 安装（clone后一次性执行，建立skill软链接）
bash setup.sh

# 审查PR
python3 ai_reviewer.py --pr 1150                    # 指定PR
python3 ai_reviewer.py --pr 1150 --comment           # 审查并发布评论
python3 ai_reviewer.py --pr 1150 --comment --inline   # 含行内评论
python3 ai_reviewer.py --pr 1150 --save               # 保存到log/
python3 ai_reviewer.py --pr 1150 --dry-run             # 只拉取diff不审查

# 审查本地文件（不需要token）
python3 ai_reviewer.py --file src/foo.cpp
python3 ai_reviewer.py --dir src/framework/zero_copy/

# 管理
python3 ai_reviewer.py --clean 1150                  # 清除AI评论
python3 ai_reviewer.py --stats --days 90             # 采纳率统计
```

没有测试套件和lint配置。验证改动的方式是`--dry-run`或对已知PR执行审查确认输出正确。

## 架构

三阶段流水线：Fetch → Review → Publish。

`ai_reviewer.py`是唯一的核心文件（~4500行），包含所有逻辑：

1. API层 — `api_get/api_post`封装GitCode REST API，`fetch_open_prs/fetch_pr_files`拉取PR数据
2. Diff格式化 — `format_diff_for_review`构造unified diff，`_build_diff_position_map`映射行号用于inline评论
3. Claude调用 — `_run_claude`通过`subprocess`调用`claude -p`，解析JSON输出获取review文本和token统计。移除`CLAUDECODE`环境变量以支持嵌套调用
4. 输出路由 — 终端stdout / `write_review_md`写本地markdown / `post_review_comment`发布GitCode评论
5. 追踪统计 — SQLite(`tracking.db`)记录审查历史，`_check_finding_status`判断意见是否被采纳

`review_loop.sh` — 轮询守护脚本，每60秒比对HEAD SHA检测变更，调用ai_reviewer.py。

`skill/vibe-review/` — 审查逻辑的核心。SKILL.md定义审查流程、置信度分级、输出格式模板。`references/`下分层存放编码规范（公司→产品线→项目→个人）。渐进式加载：根据repo类型决定加载哪些规范文件。

## 关键常量

| 常量 | 值 | 作用 |
|------|------|------|
| `MAX_DIFF_CHARS` | 80000 | 单PR diff上限，防止超出context window |
| `MAX_CLAUDE_TURNS` | 40 | 单次审查agentic回合上限 |
| `MAX_COMMENT_CHARS` | 60000 | GitCode评论字符上限，超出自动拆分 |
| `MIN_REVIEW_CHARS` | 500 | 审查结果最短有效长度，低于则重试 |
| `MAX_PARALLEL_REVIEWS` | 2 | 并发审查数，避免API限流 |
| `AI_REVIEW_MARKER` | `## AI Code Review` | PR评论标识符（勿改，向后兼容） |
| `AI_INLINE_MARKER` | `<!-- AI_CODE_REVIEW -->` | 行内评论标识符（勿改，向后兼容） |

## 开发约定

- `ai_reviewer.py`是单文件架构，所有功能在一个文件内。修改时注意函数间依赖关系
- Claude调用时的工具白名单：文件审查用`["Read", "Grep", "Glob", "Skill"]`，PR审查额外允许只读git命令
- 审查输出格式严格遵循skill中定义的模板（变更概述 → 审查发现 → 总结），每个finding必须声明位置、规则、置信度
- `AI_REVIEW_MARKER`和`AI_INLINE_MARKER`用于识别和清理旧评论，修改会导致无法清理已有评论
- 终端输出使用`_c/_bold/_red`等辅助函数做ANSI着色，并行模式下用StringIO缓冲防止交错
- 日志输出路径：`log/cann/{repo}/by_pr/`、`by_file/`、`by_dir/`
