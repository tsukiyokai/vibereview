# Vibe Review

[CANN](https://gitcode.com/cann)代码仓（GitCode平台）的自动化检视机器人。

CANN代码量大（如HCCL+HCOMM），团队新人多，传统静态分析工具（cppcheck、clang-tidy）能覆盖的问题类型有限。本工具通过Claude Code配合自定义的[vibe-review skill](skill/vibe-review)，在审查PR diff时同时读取上下文代码（不只看diff本身），并将检视意见发布为GitCode PR评论。

维护者：@tsukiyokai <br>
Slack：[#vibereview](https://claude-rfj1883.slack.com/archives/C0AHLUT5E0M)

## 工作流程

```
                         review_loop.sh (poll 60s)
                                  |
                                  v
+-----------+   GitCode API   +----------------+   claude -p   +-------------------+
|           | --------------> |                | ------------> |                   |
| GitCode   |   fetch diff    | ai_reviewer.py | invoke skill  | Claude Code       |
| PR / Push |                 |                | <------------ | vibe-review skill |
|           |                 +----------------+    report     +-------------------+
+-----------+                    |    |    |                        |
                                 |    |    |                   read context
                                 v    v    v                       |
                             terminal log/ GitCode            local repo
                              stdout  save PR comment       ~/repo/cann/*
```

`review_loop.sh`通过对比HEAD SHA判断变更，无变化时仅消耗1次API调用。

## 快速开始

前置条件：Python 3.10+，已安装[Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI。

```bash
# 1. 克隆仓库
git clone https://github.com/tsukiyokai/vibereview.git
cd vibereview

# 2. 安装vibe-review skill（软链接到Claude Code的skills目录）
bash setup.sh

# 3. 设置GitCode个人访问令牌
export GITCODE_TOKEN=your_token

# 4. 审查一个PR（默认仓库hcomm，可通过--repo指定）
python3 ai_reviewer.py --repo hcomm --pr 1150
```

## 用法

### 审查PR

```bash
python3 ai_reviewer.py                                    # 最近3个open PR
python3 ai_reviewer.py --pr 1150 1144                     # 指定PR
python3 ai_reviewer.py --author lilin_137 -n 0            # 某用户的全部open PR
python3 ai_reviewer.py --state merged --count 3           # 已合并的PR
python3 ai_reviewer.py --repo ops-transformer --pr 2071   # 其他仓库
```

### 输出控制

审查结果默认输出到终端，可通过以下标志控制：

```bash
python3 ai_reviewer.py --pr 1150 --save               # 保存到log/
python3 ai_reviewer.py --pr 1150 --comment            # 发布评论到GitCode PR
python3 ai_reviewer.py --pr 1150 --comment --force    # 强制重审（忽略"已审查过"）
```

### 审查本地文件

不需要GitCode token：

```bash
python3 ai_reviewer.py --file src/foo.cpp src/bar.h --save
python3 ai_reviewer.py --dir src/framework/zero_copy/ --save
```

### 持续轮询

```bash
bash review_loop.sh $GITCODE_TOKEN 60 hcomm
```

每60秒检查team成员是否有新push，有则自动触发审查并发布评论。

### 统计与追踪

```bash
python3 ai_reviewer.py --stats --days 90     # 采纳率统计
python3 ai_reviewer.py --track --pr 1150     # 追踪单个PR的检视意见
python3 ai_reviewer.py --import-logs         # 导入历史审查日志到追踪DB
```

选项可组合：`--author`按用户筛选，`--count`/`-n`限制数量，`--state`筛选PR状态，`--dry-run`只拉取不审查。

## 项目结构

```
ai_reviewer.py           # 核心：GitCode API、diff拉取、Claude调用、评论发布
review_loop.sh           # 轮询守护脚本
team.txt                 # 团队成员名单（姓名 工号 GitCode账号）
skill/vibe-review/        # vibe-review skill 源文件
doc/best_practice.md     # 踩坑记录与部署经验
log/                     # 检视产出，按仓库和维度组织：
  └── cann/
      └── <repo>/
          ├── by_pr/     #   pr_1150_review.md, pr_1150_diff.md
          ├── by_file/   #   foo_cpp_review.md
          └── by_dir/    #   module_review.md
```

## 配置项

| 配置 | 说明 |
|------|------|
| `GITCODE_TOKEN` | GitCode个人访问令牌（环境变量或`--token`参数） |
| `--repo` | 目标仓库名，默认`hcomm`。同时决定本地路径`~/repo/cann/<repo>/`和GitCode API目标`cann/<repo>` |
| `team.txt` | 每行一人：`姓名 工号 gitcode账号`。轮询脚本用此文件筛选team成员的PR |
| `MAX_DIFF_CHARS` | 单PR diff最大字符数（80K），防止超出Claude上下文窗口 |
| `MAX_CLAUDE_TURNS` | 单次审查最大agentic回合数（40），平衡深度与成本 |

## 开发历程

> 2026年2-3月，从手动review到全自动检视机器人。

2/12 — 起步：创建vibe-review skill，基于CANN C++编码规范。手动curl下载PR diff，手动调用skill审查。

2/13 — 探索输出形式：确定markdown为标准输出格式。

2/16 — 脚本诞生：编写review_prs.py（ai_reviewer.py前身），通过GitCode API自动拉取open PR diff并调用Claude Code审查。支持指定PR、按作者筛选。

2/17 — 打通评论流程：实现--comment将审查结果发布为GitCode PR评论。旧评论自动清理。添加--state支持已合并PR、--save控制本地保存。审查结果最短长度校验防止空报告。

2/18 — 结构化与扩展：建立log/by_pr、log/by_file目录结构。添加--file本地文件审查、--author批量筛选。撰写best_practice.md踩坑博客。

2/21 — 成本与并行：添加token消耗和耗时统计、变更文件LOC显示（+/-）。实现多PR并行审查。上线inline模式（逐行评论到GitCode代码行）。

2/22 — inline攻坚：多轮修复inline评论定位偏移问题。添加审查进度实时显示、--clean清除AI评论、--dir目录级审查。分析200K context window对审查质量的影响。

2/24 — 团队化：支持team.txt批量审查团队成员PR、自动跳过\[WIP\]标记的PR、短任务优先调度。基于Claude官方定价优化成本监控。审查报告代码块添加语法高亮。

2/25 — 跨仓库与持续轮询：脚本从hcomm-dev/迁移到jbs/独立目录。添加--repo参数支持跨仓库审查。实现基于HEAD SHA的重复检视防护（--force强制重审）。编写review_loop.sh轮询守护脚本。

2/26 — 生产加固：review_loop.sh完善（失败重试、变更检测优化）。评论发布后输出GitCode链接。创建canndev skill覆盖PR全生命周期。

2/27 — 追踪统计：实现--stats采纳率统计、--track检视意见追踪、--import-logs历史数据导入。行号统一为范围格式（199-201）。log目录重构为log/cann/\<repo\>/层级。扩展支持ops-transformer仓库。轮询脚本失败恢复修复。

2/28 — 开源与重构：vibe-review skill重构（渐进式加载、分层规范文件）。项目托管到GitHub，编写README。

3/1 — 品牌重塑：项目从ai_code_review重命名为vibereview，skill从codereview重命名为vibe-review。skill内容纳入仓库版本管理（替换符号链接），添加setup.sh一键安装。

## TODO

> Slack讨论，这里落纸，挑感兴趣的发PR。

### 已完成

- [x] 创建vibe-review skill，基于CANN C++编码规范
- [x] 通过GitCode API自动拉取PR diff
- [x] 调用Claude Code vibe-review skill进行审查
- [x] 审查指定PR（--pr）
- [x] 按作者筛选PR（--author）
- [x] 审查结果发布为GitCode PR评论（--comment）
- [x] 发布前自动清理旧的AI评论
- [x] 审查结果保存到本地markdown（--save）
- [x] 审查结果最短长度校验，防止空报告
- [x] 支持已合并PR审查（--state merged）
- [x] 审查本地文件（--file）
- [x] 审查本地目录（--dir）
- [x] log目录结构：by_pr / by_file / by_dir
- [x] token消耗和耗时统计（成本监控）
- [x] 变更文件LOC显示（+/-）
- [x] 多PR并行审查
- [x] inline模式：逐行评论到GitCode代码行
- [x] 审查进度实时显示
- [x] 清除指定PR的AI评论（--clean）
- [x] team.txt批量审查团队成员PR
- [x] 自动跳过\[WIP\]标记的PR
- [x] 短任务优先调度
- [x] 基于Claude官方定价的成本计算
- [x] 审查报告代码块语法高亮
- [x] 跨仓库审查（--repo）
- [x] 基于HEAD SHA防止重复检视
- [x] 强制重新审查（--force）
- [x] review_loop.sh轮询守护脚本
- [x] 评论发布后输出GitCode链接
- [x] 轮询脚本失败自动恢复
- [x] 采纳率统计（--stats）
- [x] 检视意见追踪（--track）
- [x] 历史审查数据导入（--import-logs）
- [x] 行号范围格式统一（199-201）
- [x] log目录按项目/仓库分层（log/cann/\<repo\>/）
- [x] 支持多个CANN仓库（hcomm、ops-transformer）
- [x] vibe-review skill重构（渐进式加载、分层规范）
- [x] GitHub托管与README
- [x] 项目重命名为vibereview，skill重命名为vibe-review
- [x] skill内容纳入仓库版本管理，添加setup.sh一键安装

### 待做（这几个方面可以同时推进）：

#### A 质量专题

目标是发现更真实、更严重的BUG。

- [x] 从hcomm仓库git历史挖掘HCCL高价值缺陷模式：分析全部428次提交，识别84次缺陷提交，逐条分析根因和修复模式，产出48条审查规则覆盖12个缺陷类别（算法正确性、并发、内存、整数溢出、错误处理、资源生命周期等）+ 6条跨类别系统性风险规则。规则已写入skill的references/standards-project-hccl.md
- [ ] 管道模式和交互模式的效果差异分析

#### B 推广专题

目标是覆盖更多代码仓。

推广到2012中央硬件工程院-计算网络技术部（HCCL和HCOMM仓）的分析：因为hccl代码规模比别的算子组大且存在大量host侧的纯cpp代码（framework、platform），能够产生的检视意见数量更多，理论效果比扫描acl代码更好，且反馈更及时。个人希望是马上就能在hccl小范围（先定50人，我自费给hccl扫代码）部署，需要一些软件&业务高手帮忙分析检视报告，定期（每周）反馈误报+高价值案例+历史dts的分析结果到我这，我统一优化skill。同步地，我最近帮中硬自己跑起来，后面可以保持合作探索。

推广到2012中央研究院-创新算子部（其他CANN仓）的分析：功能上我已支持扫描任何cann仓，而且现在就能开扫，但其他算子组acl代码多，而类dsl的扫描结果有待商榷，个人担心出现大量噪音且发现不了真正的问题，希望等我打磨效果到我满意的程度再推。

#### C 运营专题

目标是建立长效机制不断把对误报/高价值缺陷模式的分析结果先反馈到skill再前馈到检视结果，侧重运作机制而非对模式的分析方法。

- [ ] 采纳率统计算法优化

#### D 成本专题

目标是降低时间和tok成本。

现阶段全力提质量，先不管成本，不过有兴趣的同学也可以先思考看看。

## 参与贡献

所有变更走PR，不直接push `main`。维护者负责merge。

1. 创建分支：`git checkout -b your-feature`
2. 本地改好后测试：`python3 ai_reviewer.py --pr <any_pr> --dry-run`
3. 提PR，写清楚改了什么、为什么改
4. 维护者review后merge

适合上手的贡献：总结误报经验反馈并闭环到skill、按最佳实践优化vibe-review skill的prompt、修复你碰到的bug。

沟通约定：日常问题在Slack交流；决策和TODO变更落到GitHub。

## 延伸阅读

- [doc/best_practice.md](doc/best_practice.md) — AI检视在HCCL的部署经验与踩坑
- [Claude Code skills](https://docs.anthropic.com/en/docs/claude-code) — vibe-review skill的工作原理
- [GitCode API](https://gitcode.com/docs/openapi) — PR拉取和评论发布所用的API

## 许可

仅限内部使用，不得对外分发。
