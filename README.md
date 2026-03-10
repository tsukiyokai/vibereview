# Vibe Review

[CANN](https://gitcode.com/cann)仓自动检视机器人。

CANN代码量大（如HCCL+HCOMM），团队新人多，传统静态分析工具（如cppcheck、clang-tidy）能覆盖的问题类型有限。本工具通过Claude Code管道模式配合自定义的[vibe-review skill](https://github.com/tsukiyokai/vibe-review-skill)，在审查PR diff时同时按需读取上下文代码（不只看diff本身），并将检视意见发布为GitCode PR评论。

维护者：@tsukiyokai <br>
Slack:[#vibereview](https://claude-rfj1883.slack.com/archives/C0AHLUT5E0M)

## 效果

### 截图

![demo](./assets/demo.gif)

### 案例

累计输出400+检视意见，部分如下所示（开源代码）：

![findings](./assets/screenshot_findings.png)

### 统计

#### 准确率

采样最近100条检视意见后人工分析，准确率约80%。

#### 响应速度

| 推理指标      | CodeReview指标       |
| ------------- | -------------------- |
| TTFT          | 首条检视意见提出时间 |
| TPS           | 每日检视意见数量     |
| Total Latency | Time To Merge        |

- 某committer是hcomm仓最活跃的人类reviewer，他在最新170个PR（50个已合并+120个open）的所有实质性检视意见（排除bot、AI review、PR作者自评、以及lgtm/approve/compile等纯命令）中贡献了全部人类检视意见的约25%（远超第二名），日均14条检视意见，2月27日一天就提了35条（可能是集中review了一批PR）。另外，所有人类reviewer整体的中位响应时间为1.9天。
- 作为对比：3月4日，AI审查了名单里的64个PR，提出193条检视意见。单PR从开始审查到评论发出的中位数6m14s，均值6m19s，57%的PR在5-10分钟内完成，25%在3-5分钟，最快1分钟（小PR），最慢15分钟（大PR），端到端时延（开发者push到收到评论）还要加上轮询间隔平均感知延迟\~30s（60s轮询周期）和Step1获取PR列表\~5s。所以典型场景是开发者push代码后约7分钟收到AI检视评论。冷启动时（积压多个PR）最长一轮耗时51分钟。

### 用户评价

- 新员工：这个AI挺厉害的，扫出来的两个（多线程问题）是对的。
- 新员工：框架这边的AI读代码感觉更严格点，然后读的范围更大。
- 模块设计师：感觉你这个检视很强了。
- 迭代经理：大家写代码关注下这个AI检视，我发现这个检视工具可以发现绝大多数的问题，可以提高检视的效率。不仅只看这笔代码，还能举一反三看其他地方有没有改到。

## 执行流程

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
git clone https://github.com/tsukiyokai/vibe-review-bot.git
cd vibe-review-bot

# 2. 安装vibe-review skill
npx @tsukiyokai/vibe-review --global

# 3. 设置GitCode个人访问令牌
export GITCODE_TOKEN=your_token

# 4. 审查一个PR（默认仓库hcomm，可通过--repo指定）
python3 ai_reviewer.py --repo hcomm --pr 1150
```

## 用法

### 审查PR

```bash
python3 ai_reviewer.py                                     # 最近3个open PR
python3 ai_reviewer.py --pr 1150 1144                      # 指定PR
python3 ai_reviewer.py --author lilin_137 -n 0             # 某用户的全部open PR
python3 ai_reviewer.py --state merged --count 3            # 已合并的PR
python3 ai_reviewer.py --repo ops-transformer --pr 2071    # 其他CANN仓库
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
python3 ai_reviewer.py --stats --days 90    # 采纳率统计
python3 ai_reviewer.py --track --pr 1150    # 追踪单个PR的检视意见
python3 ai_reviewer.py --import-logs        # 导入历史审查日志到追踪DB
```

选项可组合：`--author`按用户筛选，`--count`/`-n`限制数量，`--state`筛选PR状态，`--dry-run`只拉取不审查。

## 项目结构

```
ai_reviewer.py            # 核心：GitCode API、diff拉取、Claude调用、评论发布
review_loop.sh            # 轮询守护脚本
teams/                    # 团队成员名单（按仓库命名，如hcomm.txt）
doc/best_practice.md      # 踩坑记录与部署经验
log/                      # 检视产出，按仓库组织：
  └── cann/
      └── <repo>/
          ├── pr_1150/    #   ad4019.md, ad4019_diff.md (按commit hash保存)
          ├── by_file/    #   foo_cpp_review.md
          └── by_dir/     #   module_review.md
```

## 配置项

| 配置               | 说明                                                                                 |
| ------------------ | ------------------------------------------------------------------------------------ |
| `GITCODE_TOKEN`    | GitCode个人访问令牌（环境变量或`--token`参数）                                       |
| `--repo`           | 目标仓库名，同时决定本地路径`~/repo/cann/<repo>/`和GitCode API目标`cann/<repo>`      |
| `teams/*.txt`      | 团队成员名单，按仓库命名（如`hcomm.txt`），不纳入git托管，需自行创建。格式见下方说明 |
| `MAX_DIFF_CHARS`   | 单PR diff最大字符数(80K)，防止超出Claude上下文窗口                                   |
| `MAX_CLAUDE_TURNS` | 单次审查最大agentic回合数(40)，平衡深度与成本                                        |

`teams/*.txt`格式：每行一人，首行为标题行，空行和`#`开头的行会被忽略。

```
姓名      gitcode
张三      zhangsan
李四      lisi123
```

轮询脚本`review_loop.sh`和`--team`参数都依赖此文件来筛选团队成员的PR。该文件已加入`.gitignore`，克隆仓库后需自行创建。

## milestone

> 2026年2-3月，从手动review到全自动检视机器人。

2/12 — 起步：创建vibe-review skill，基于CANN C++编码规范。手动curl下载PR diff，手动调用skill审查。

2/13 — 探索输出形式：确定markdown为标准输出格式。

2/16 — 脚本诞生：编写review_prs.py（ai_reviewer.py前身），通过GitCode API自动拉取open PR diff并调用Claude Code审查。支持指定PR、按作者筛选。

2/17 — 打通评论流程：实现--comment将审查结果发布为GitCode PR评论。旧评论自动清理。添加--state支持已合并PR、--save控制本地保存。审查结果最短长度校验防止空报告。

2/18 — 结构化与扩展：建立log/by_pr、log/by_file目录结构。添加--file本地文件审查、--author批量筛选。撰写best_practice.md踩坑博客。

2/21 — 成本与并行：添加token消耗和耗时统计、变更文件LOC显示(+/-)。实现多PR并行审查。上线inline模式（逐行评论到GitCode代码行）。

2/22 — inline攻坚：多轮修复inline评论定位偏移问题。添加审查进度实时显示、--clean清除AI评论、--dir目录级审查。分析200K context window对审查质量的影响。

2/24 — 团队化：支持team.txt批量审查团队成员PR、自动跳过\[WIP\]标记的PR、短任务优先调度。基于Claude官方定价优化成本监控。审查报告代码块添加语法高亮。

2/25 — 跨仓库与持续轮询：脚本从hcomm-dev/迁移到jbs/独立目录。添加--repo参数支持跨仓库审查。实现基于HEAD SHA的重复检视防护（--force强制重审）。编写review_loop.sh轮询守护脚本。

2/26 — 生产加固：review_loop.sh完善（失败重试、变更检测优化）。评论发布后输出GitCode链接。创建canndev skill覆盖PR全生命周期。

2/27 — 追踪统计：实现--stats采纳率统计、--track检视意见追踪、--import-logs历史数据导入。行号统一为范围格式(199-201)。log目录重构为log/cann/\<repo\>/层级。扩展支持ops-transformer仓库。轮询脚本失败恢复修复。

2/28 — 开源与重构：vibe-review skill重构（渐进式加载、分层规范文件）。项目托管到GitHub，编写README。

3/1 — 改名：项目从ai_code_review重命名为vibe-review,skill从codereview重命名为vibe-review.skill内容纳入仓库版本管理（替换符号链接），添加setup.sh一键安装。

3/3 — npm发包：vibe-review skill提取为独立项目[vibe-review-skill](https://github.com/tsukiyokai/vibe-review-skill)，发布到npm（[@tsukiyokai/vibe-review](https://www.npmjs.com/package/@tsukiyokai/vibe-review)）。用户通过`npx @tsukiyokai/vibe-review --global`一键安装。vibe-review-bot仓库不再包含skill源码，改为依赖npm包。

![daily](./assets/daily_cost.png)

## todos

- [x]创建vibe-review skill，基于CANN C++编码规范
- [x]通过GitCode API自动拉取PR diff
- [x]调用Claude Code vibe-review skill进行审查
- [x]审查指定PR(--pr)
- [x]按作者筛选PR(--author)
- [x]审查结果发布为GitCode PR评论(--comment)
- [x]发布前自动清理旧的AI评论
- [x]审查结果保存到本地markdown(--save)
- [x]审查结果最短长度校验，防止空报告
- [x]支持已合并PR审查(--state merged)
- [x]审查本地文件(--file)
- [x]审查本地目录(--dir)
- [x] log目录结构：by_pr / by_file / by_dir
- [x] token消耗和耗时统计（成本监控）
- [x]变更文件LOC显示(+/-)
- [x]多PR并行审查
- [x] inline模式：逐行评论到GitCode代码行
- [x]审查进度实时显示
- [x]清除指定PR的AI评论(--clean)
- [x] team.txt批量审查团队成员PR
- [x]自动跳过\[WIP\]标记的PR
- [x]短任务优先调度
- [x]基于Claude官方定价的成本计算
- [x]审查报告代码块语法高亮
- [x]跨仓库审查(--repo)
- [x]基于HEAD SHA防止重复检视
- [x]强制重新审查(--force)
- [x] review_loop.sh轮询守护脚本
- [x]评论发布后输出GitCode链接
- [x]轮询脚本失败自动恢复
- [x]采纳率统计(--stats)
- [x]检视意见追踪(--track)
- [x]历史审查数据导入(--import-logs)
- [x]行号范围格式统一(199-201)
- [x] log目录按项目/仓库分层(log/cann/\<repo\>/)
- [x]支持多个CANN仓库(hcomm、ops-transformer)
- [x] vibe-review skill重构（渐进式加载、分层规范）
- [x] GitHub托管与README
- [x]项目重命名为vibe-review,skill重命名为vibe-review
- [x] skill内容纳入仓库版本管理，添加setup.sh一键安装
- [x] vibe-review skill提取为独立[npm包](https://www.npmjs.com/package/@tsukiyokai/vibe-review)（[GitHub](https://github.com/tsukiyokai/vibe-review-skill)）
- [ ]支持Gitee V5 API
- [ ] webhook打通（跑个HTTP server来接收GitCode的webhook请求，部署复杂度UP）。
- [x]从hcomm仓库git历史挖掘HCCL高价值缺陷模式：分析全部428次提交，识别84次缺陷提交，逐条分析根因和修复模式，产出48条审查规则覆盖12个缺陷类别（算法正确性、并发、内存、整数溢出、错误处理、资源生命周期等）+ 6条跨类别系统性风险规则。规则已写入skill的references/standards-project-hccl.md -- 260302
- [x]用上述方法完成ops-transformer代码仓分析，输出references/standards-project-ops-transformer.md -- 260303
- [ ] cc管道模式和交互模式的效果差异分析
- [ ]与CMC合作形成一套检视意见反馈skill的方法论
- [ ]采纳率算法优化（存储上使用了Python标准库的sqlite3模块，主要用于PR审查的跟踪数据库(TRACKING_DB)；算法上因为diff追踪算法还没完全实现出来所以下图数据不算数）

  ![screenshot_stats](./assets/screenshot_stats.png)

- [ ]切内部模型

## roadmap

### 一、基础功能

目标：开发更多有趣的功能

详见[TODO](#todo)。

### 二、效果

目标：揭示真正的漏洞，减少噪音

<img src="./assets/meme_wtfs_per_minute.png" alt="meme_wtfs_per_minute"  />

#### 揭示问题

AI代码审查的价值上限取决于规则的质量。通用规范（编码红线、Google C++ Style）能覆盖通用缺陷，但最高价值的发现来自项目特异性的缺陷模式——那些在特定代码库中反复出现、被同一批开发者反复犯的错误。这些模式不写在任何规范里，只沉淀在git历史中。

repo-dig是一套从仓库完整git历史中系统化提炼缺陷模式的方法论。核心思路：每一次bugfix提交都是一个"这里曾经出过问题"的信号，大量信号聚合后会涌现出高频模式，这些模式比任何通用规范都更贴近项目的真实风险。

七阶段流程：

```
git log全量提取 → 关键词筛选缺陷提交 → 逐条分析diff根因
→ Revert专项(逃逸到主干的严重缺陷) → 热点文件与结构性风险
→ 模式归纳分类 → 输出审查规则 → 打磨验证(抽样反查代码)
```

关键约束：全量分析不采样（低频高危缺陷只有全量才能捕获）；每条规则必须有commit hash证据链（可`git show`验证）；类别从数据中自然涌现，不预设框架。

已在CANN生态6个仓库上完成：

| 仓库            | 提交数 | 缺陷数 | 审查规则 |
| --------------- | ------ | ------ | -------- |
| ops-transformer | 1323   | 243    | 46条     |
| ops-nn          | 1474   | 380    | 39条     |
| hcomm-dev       | 488    | 162    | 40条     |
| hccl            | 153    | —      | 48条     |
| hccl-dev        | 133    | 10     | 9条      |
| ops-nn-dev      | 2571   | 612    | 进行中   |

三个跨仓库共性模式：

1. 计算参数不一致。Host侧tiling与kernel侧独立计算workspace大小、buffer对齐、struct定义，物理分离缺乏编译期约束，导致两侧公式不匹配。ops-transformer中GQA的gSize缩放因子遗漏横跨6条独立commit反复出现。
2. 边界条件处理不完整。除零（shape维度或中间计算结果为0）、空tensor四层联动缺失（aclnn/infershape/tiling/kernel任一层遗漏即崩溃）、整数溢出截断(int64→uint32)、null检查时序错误（先解引用后判空）。
3. 并发安全缺乏系统性设计。全局/static变量无锁访问、TOCTOU竞态、资源释放顺序错误导致UAF、内存屏障缺失。hcomm-dev中15/18个热点文件存在并发问题。

仓库特异性模式同样有价值：算子库的tiling参数爆炸（layout×模式×量化×稀疏的组合空间指数增长）、kernel指令位宽限制（DataCopy uint16_t上限65535）、硬件流水线同步（MTE2/MTE3/Vector/Scalar数据依赖）；通信库的API/ABI兼容性债务（extern "C"块用namespace、公共头文件含C++默认参数）、资源生命周期跨组件管理（设备内存/IPC handle/transport link/notify）。

这些规则已整合到vibe-review skill的分仓标准文件中（`standards-project-hccl.md`、`standards-project-ops-transformer.md`），在审查对应仓库的PR时自动加载。效果：审查的发现从"命名不规范""缺少注释"这类低价值项，转向"tiling与kernel的workspace计算公式不一致""FinalizeChannels超时逻辑不可达导致死循环"这类真正的功能缺陷。

仓库挖掘小结：

repo-dig全程由Claude Code自主执行，人工仅提供方法论说明和偶尔审查产物，实际投入约3-5小时。AI侧：78个会话，API费用$211，约1周wall clock跑完。

产出规模：7份单仓标准文档共8，851行，1份跨仓综合分析458行，逐条缺陷分析记录共16，549行（每条含commit hash、根因类别、涉及文件、缺陷描述、修复模式、可审查性评级、审查规则建议）。阶段7交叉验证共抽样验证约120个commit hash，修正了约10处描述不准确。

纯人工工作量估算（假设一位熟悉CANN代码库的资深C++工程师全职投入）：

| 阶段     | 工作内容                                               | 估算             |
| -------- | ------------------------------------------------------ | ---------------- |
| 阶段1    | 9237条提交分类，关键词筛选后人工二次过滤排除误匹配     | 7天              |
| 阶段2    | 2279条缺陷提交逐条git show、读diff、理解根因、撰写分析 | 83天             |
| 阶段3-4  | 7个仓库的Revert专项 + 热点文件分析                     | 9天              |
| 阶段5-6  | 7个仓库的模式归纳分类 + 输出标准文档                   | 21天             |
| 阶段7    | 7个仓库的交叉验证（抽样反查代码）                      | 7天              |
| 跨仓综合 | 8个共性模式提取 + Top-28规则排序 + dev vs main对比     | 3天              |
| 合计     |                                                        | 130天（6.5人月） |

阶段2是绝对瓶颈，占总工作量的64%。按每条缺陷diff平均15分钟估算（简单的配置遗漏5分钟，复杂的并发/tiling问题30-60分钟）。如果不具备CANN领域知识，还需额外的上下文理解时间，总工作量可能翻倍到12-13人月。

三条经验：

1. 全量分析不可替代。低频高危缺陷（如hcomm-dev中memory fence缺失导致的数据竞争，全仓只出现2次）只有全量扫描才能捕获。采样分析会系统性遗漏这类"罕见但致命"的模式，而这恰恰是最高价值的审查规则来源。
2. 让类别从数据中涌现。不预设缺陷分类框架，而是在逐条分析后自底向上归纳——不同仓库涌现出5-18个不等的缺陷类别，通信库和算子库的类别结构差异显著。预设框架会把数据往已有类别上套，压制真正有特异性的模式。
3. 证据链是信任基础。每条规则绑定commit hash，可随时`git show`验证。阶段7的交叉验证发现约8%的描述存在不同程度的偏差（部分一致或需修正），说明即使是AI生成的分析也必须经过人工抽检。不可验证的结论无价值。

#### 减少噪音

当前vibe-review是纯LLM系统——所有检测逻辑编码在skill prompt里（48条HCCL规则 + 46条ops-transformer规则 + 部门红线28条），由Claude做模式识别和上下文推理。准确率80%意味着每5条有1条误报。

业界如何驯服AI代码检视中的误报？从Google、Meta、Semgrep、Snyk、腾讯等公司的实践中涌现出五条设计原则：

1. 衡量开发者行为，而非技术正确性。Google的"有效误报"和Meta的"行动率"都以开发者实际做了什么来重新定义问题。一个没人修复的"正确"警告，对工程质量的贡献是零。
2. 部署上下文主导检测质量。Meta相同的分析器仅仅通过改变警告何时何地出现，修复率从0%跳升到70%。diff时报告、仅报告新问题、按严重性分层——这些不是优化，是先决条件。
3. 创建直接问责闭环。Google对超过噪声阈值的分析器自动下线、将自动归档bug路由给分析器作者，是迄今记录的最有效治理机制。没有问责就没有持续改进。
4. 渐进式部署不可协商。每个成功的系统都把新规则当作高风险变更处理，需要金丝雀式部署。Google AutoCommenter的分阶段推广、Semgrep的monitor→comment→block晋升、GitHub的A/B测试——没有一个系统是直接全量发布规则变更的。
5. 混合方法优于纯ML。最有效的系统将确定性分析与ML过滤结合，而不是依赖其中任一方。Snyk的符号+神经引擎、腾讯的LLM4PFA、Semgrep的Assistant+规则都体现了这一模式。

第5点是最具工程可操作性的方向。以下记录对混合方法的探索。

##### 混合方法：确定性分析 + LLM

三个候选工具：

- [weggli](https://github.com/weggli-rs/weggli) — Google Project Zero开发的C/C++语义搜索工具，基于tree-sitter AST匹配。模式语法接近实际C代码（学习曲线低），贪心匹配低漏报，极快。但项目已停更(~2022),tree-sitter在进化而weggli的parser可能逐渐落后。
- [semgrep](https://github.com/semgrep/semgrep) — 通用SAST，YAML规则 + 元变量，支持30+语言，社区规则丰富，SARIF/JSON输出。C++支持中等。Community Edition功能受限，高级功能移到商业平台。
- [opengrep](https://github.com/opengrep/opengrep) — semgrep的开源社区分支（2025年1月），LGPL-2.1许可。恢复了CE移除的功能（跨函数污点分析、结果指纹识别），性能平均3.15倍快于semgrep，完全兼容semgrep YAML规则格式。

结论：opengrep更适合长期投入（开源、兼容semgrep生态、性能好、维护活跃）。weggli适合临时探索性搜索但不适合作为生产管道核心。

现有规则中约30-35%可以被确定性工具覆盖——禁用函数(memcpy→memcpy_s)、命名违规、C风格头文件、C风格类型转换、裸new/delete、typedef应改using等。这些规则的共同特征：不需要跨文件上下文，纯模式匹配即可判定，在置信度体系里都是"确定"级别。不能确定性化的是ALG系列（变量遮蔽、参数赋值遗漏）、CON系列（内存屏障顺序）、CALC系列（缓冲单位混淆）等需要语义理解的规则。

设想的混合架构：

```
PR diff
  |
  +-- Stage 1: opengrep扫描（秒级）
  |     输出: SARIF/JSON结构化findings
  |     覆盖: 禁用函数、命名、格式等机械规则
  |
  +-- Stage 2: Claude Code + vibe-review skill
        输入: diff + Stage 1的findings摘要
        职责: 验证Stage 1 findings + 检测语义问题 + 生成统一报告
```

Stage 1的findings不直接发布，先过LLM过滤（利用LLM消除上下文相关的误报，如RAII管理的资源不需要手动释放检查）。用采纳率数据验证每条静态规则的精度，当某条规则采纳率>95%时晋升为直接发布（渐进模式）。

##### 腾讯LLM4PFA：路径可行性分析

以上混合架构解决的是"确定性规则的前置"。对于LLM自身产出的findings，如何进一步降低误报？

[LLM4PFA](https://arxiv.org/html/2506.10322v1)(Du et al., 2025)提出了一个精妙的思路：不要让LLM直接判断"这是不是bug"，而是让它回答一个更精确的问题——"从source到sink的路径约束是否可满足？" 把模糊的语义判断转化为可验证的逻辑问题。

三阶段管道：

```
静态分析器报告(source -> sink trace)
  |
  v
Stage 1: 可行性约束提取
  识别路径上影响sink可达性的关键条件分支
  规则: 出口条件必须满足 + 跳转条件的否定必须成立
  |
  v
Stage 2: LLM Agent符号范围推理
  逐函数分析变量在约束表达式中的值域
  Agent自主决定是否需要深入分析被调函数（最多5轮迭代）
  Memory模块缓存已分析函数，避免重复推理
  |
  v
Stage 3: SMT约束求解(Z3)
  LLM生成Z3 Python脚本（模板 -> 约束转换 -> 合并，带纠错循环最多3轮）
  不可满足 -> 路径不可行 -> 误报
  可满足 -> 路径可行 -> 真实bug
```

核心设计决策是逐函数分解而非整体分析。ablation数据：逐函数vs批量约束求解，FPR_R改善+136.7%~+173.1%。原因：把跨过程分析分解为函数粒度的符号推理，放大了LLM对聚焦代码片段的理解能力。

在Linux Kernel(18M+ LOC)、OpenSSL、Libav上的评估：过滤72%-96%误报，检出42/45真实bug(93% recall)。

[腾讯的工业验证](https://arxiv.org/html/2601.18844v1)(Du, Feng, Zou et al., 2026)在BkCheck（腾讯自研SAT，部署于微信支付和腾讯游戏）的433条警报数据集上对比了多种方法：

| 方法                    | Accuracy  | FPR_R     | 每条耗时 | 每条费用     |
| ----------------------- | --------- | --------- | -------- | ------------ |
| LLM4PFA                 | 0.93-0.94 | 0.94-0.98 | 2-110s   | $0.001-$0.12 |
| LLM4SA（喂大量上下文）  | 0.86-0.92 | 0.84-0.96 | —        | —            |
| Few-shot                | 0.69-0.72 | —         | —        | —            |
| CoT                     | 0.40-0.49 | —         | —        | —            |
| 传统ML（最好的LineVul） | 0.76      | —         | —        | —            |

三个反直觉的发现：CoT反而比基础prompt差（LLM已内化推理能力，强制CoT格式引入干扰）；纯prompt方法的天花板很低（few-shot最好也只有0.72）；传统ML方法全部惨败（缺乏领域训练数据）。

LLM4PFA仍然会失败的场景：长函数（失败案例平均函数长度比整体多95.6行）、复杂级联约束（失败案例平均21+条件语句）、罕见语法结构（深层指针操作、用户自定义嵌套数据结构）。

对vibe-review的启示：LLM4PFA解决的是静态分析器误报过滤（输入是已知的source-sink路径，做验证），vibe-review解决的是代码审查（做发现）。不能直接移植，但可以借鉴两个核心思想：

1. 对"确定"级别的findings，用确定性工具(opengrep)直接检出，不经LLM——见上文混合架构。
2. 对"较确定"/"待确认"级别的findings，在LLM生成finding后追加约束反证——要求LLM列出"这个问题不成立需要满足什么条件"，然后验证这些条件是否成立。这可以在现有prompt的自检清单中实现，不需要额外工具链。

### 三、推广与运营

目标：覆盖更多代码，持续改进

建立长效机制不断改进检视效果和提高自动化程度。把对误报/高价值缺陷模式的分析结果先反馈到skill再前馈到检视结果，侧重运作机制而非对模式的分析方法。

进展：脱敏

### 四、成本

目标：缩短时间，降低价格

当前效果虽好但成本实在有点太高了，考虑到项目还处于早期阶段，打算全力提升质量，先不管成本了，不过有兴趣的同学也可以先思考看看。

本人基于最近92次审查数据分析得到按PR规模分层的检视成本如下：

| 规模             | 样本 | 平均行数 | 平均耗时 | Output | Cache Write | Cache Read | 费用(USD) | 费用(RMB) |
| ---------------- | ---- | -------- | -------- | ------ | ----------- | ---------- | --------- | --------- |
| Tiny (<50)       | 21   | 22       | 3m38s    | 10,552 | 30,999      | 266,190    | $0.58     | ¥4.2      |
| Small (50-200)   | 24   | 131      | 5m35s    | 16,882 | 41,104      | 404,837    | $0.88     | ¥6.4      |
| Medium (200-500) | 15   | 317      | 8m13s    | 23,673 | 66,872      | 694,209    | $1.35     | ¥9.8      |
| Large (500-1k)   | 14   | 707      | 7m28s    | 21,841 | 89,544      | 691,530    | $1.46     | ¥10.6     |
| XL (>1000)       | 18   | 1,195    | 8m19s    | 22,416 | 71,485      | 1,199,938  | $1.62     | ¥11.7     |

一些规律：成本增速递减，diff从22行到1195行增长60倍，但费用只从¥4.2涨到¥11.7（2.8倍），固定开销（system prompt、skill加载、多轮协调）占了大头；Output有天花板，不论diff多大，Output稳定在1-2.3万tokens，真正随diff膨胀的是Cache Read（从27万涨到120万）；耗时在Medium后趋于平台，Tiny 3.5分钟，Medium/Large/XL都在7-8分钟；Input几乎可忽略，全部被cache命中，实际Input只有几百tokens。

降成本思路：

1. 大PR比小PR"划算"：Tiny ¥0.19/行，XL ¥0.01/行，单位行成本差20倍。与其频繁扫小PR，不如优先扫大的，ROI更高。
2. 主战场在固定开销：每次调用不管diff多大，system prompt + skill加载 + 多轮协调就要吃掉约¥4的底。精简SKILL.md和references能直接砍固定成本；如果能复用会话（一个session审多个PR而非每个PR独立启动claude进程），固定开销可以被摊薄。
3. Output是最贵的token:Output单价是Cache Read的50倍(sonnet \$15/M vs \$0.30/M)。如果能让模型输出更精炼（只报严重问题、精简修复建议），Output从2万降到1万就能省约30%。
4. Tiny PR预过滤：22行的PR花¥4.2扫，很可能什么都扫不出来。纯改名、纯删除、纯注释修改等trivial变更可以直接跳过。

说到前两点，小PR曾是软件工程的公认最佳实践：

1. Google Engineering Practices明确要求"small CLs"，认为小变更更容易review、更快合入、回滚风险更低。文档见google.github.io/eng-practices/review/developer/small-cls.html
2. Microsoft Research Czerwonka et al. 发现变更越大，review中发现缺陷的概率反而越低
4. 一个互联网meme：

     <img src="./assets/meme_review_lines.jpg" style="zoom: 67%;" />

这里就引入了一个有趣的矛盾：人工review小PR效果好（因为注意力集中），但AI review小PR成本不划算（因为固定开销摊不薄）。这其实说明AI和人的review特性不同，人的瓶颈是注意力，大PR会疲劳遗漏；AI的瓶颈是启动成本，小PR浪费算力。

核心问题是：谁适配谁？

- 小PR不只是一个review策略，它反映的是人如何思考、如何拆解问题、如何管理风险、如何协作。这些是人的认知规律决定的——人脑的context有限，小批量是对抗复杂性的基本手段。这属于人的领域。
- AI的固定开销——system prompt加载多少token、session如何管理、cache怎么命中——这些是工具的实现细节。这是属于工具的领域。

如果因为工具贵就改变人的工作方式，等于让工具的局限性侵入人的决策领域，人去迁就工具。反过来，优化工具的固定开销，是把问题留在工具的领域内解决，让工具去适配人。

边界应该是：人决定怎么写代码、怎么拆PR、怎么协作，AI作为工具去适配这些决策，而不是反过来。工具的成本结构不应该成为人改变工程实践的理由。

所以不应该因为AI review成本高就鼓励大PR，而是应该反过来优化AI的固定开销，让它适配小PR的最佳实践。前面说的"一个session审多个PR"就是这个思路。

一条检视意见的具体成本如下：

| 指标             | 值                  |
| ---------------- | ------------------- |
| 审查次数         | 132次（PR级）       |
| 总检视意见数     | 640条               |
| 总费用           | $169.72 / ¥1,230.47 |
| 平均每条意见费用 | $0.27 / ¥1.92       |
| 平均每次审查费用 | $1.29 / ¥9.32       |
| 平均每次审查产出 | 4.8条意见           |

分模型来看：

|                       | Opus  | Sonnet |
| --------------------- | ----- | ------ |
| 单次审查费用          | $1.33 | $1.20  |
| 每次产出findings      | 4.9条 | 3.7条  |
| 每条finding输出tokens | 4,079 | 5,764  |
| 每条finding费用       | $0.27 | $0.32  |
| 回合数                | 25.7  | 16.0   |

Sonnet单次审查确实更便宜($1.20 vs $1.33)，但每条finding反而更贵，原因有二：一是产出更少——Sonnet每次审查只找到3.7条意见，Opus找到4.9条，每次审查的固定开销（读取diff、系统prompt的缓存读取量两者相近，约60万tokens）被更少的意见分摊；二是更啰嗦——Sonnet每条finding花5,764 output tokens，比Opus的4，079多了41%。简单说，Sonnet的"找bug能力"弱于Opus，同时表达更冗长，导致虽然API单价低，但摊到每条有效检视意见上反而更贵。用更强的模型做code review不只是质量更好，单位成本也更优。

## cons

1. AI摘取了大量的“低垂果实”（例如空指针检查、常规越界、忘记释放锁），把真正深层次的错误的发现工作留给了人类。
2. 人类过滤20%噪音的精力，远小于人工去找出那80%真缺陷的精力。
3. 检视一段在语法层面上充满坏味道的代码往往会引起人类reviewer的警觉，后者也许可以发现其背后的bug。如今一段垃圾代码都能在ai的建议下修改得像模像样，可能会让人类reviewer放松这方面的警惕。

## 参与贡献

所有变更走PR，不直接push `main`。维护者负责merge。

1. 创建分支：`git checkout -b your-feature`
2. 本地改好后测试：`python3 ai_reviewer.py --pr <any_pr> --dry-run`
3. 提PR，写清楚改了什么、为什么改
4. 维护者review后merge

适合上手的贡献：总结自己本组的误报和高价值检视意见以及DTS缺陷模式，反馈并闭环到skill、修复你碰到的bug。

沟通约定：通过issue异步交流，拒绝微信办公。

<img src="./assets/meme_dame.jpg" alt="img" style="zoom: 50%;" />

## 延伸阅读

- [doc/best_practice.md]() — AI检视在HCCL的部署经验与踩坑
- [Claude Code skills](https://docs.anthropic.com/en/docs/claude-code) — vibe-review skill的工作原理
- [GitCode API](https://gitcode.com/docs/openapi) — PR拉取和评论发布所用的API
- [A Survey of Code Review Benchmarks and Evaluation Practices in Pre-LLM and LLM Era](https://arxiv.org/abs/2602.13377) (2025) — 分析99篇论文，梳理出5个领域、18个细粒度任务的分类体系，目前最完整的代码审查研究入口点
- [LLM4PFA: Path Feasibility Analysis via LLM Agent](https://arxiv.org/abs/2506.10322) (Du et al., 2025) — 逐函数符号范围推理 + Z3约束求解，过滤72-96%静态分析误报，93% recall
- [An Empirical Study of LLM-based Path Feasibility Analysis on Industrial Static Analysis Alarms](https://arxiv.org/abs/2601.18844) (Du, Feng, Zou et al., 2026) — 腾讯BkCheck 433条告警上的工业验证，LLM4PFA达0.93-0.94准确率，CoT反而降低性能
