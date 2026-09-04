# GitAgent 自动化评测执行说明

本文件与 `gitagent-evaluation-dataset.json` 配套，只描述简历占位符需要的三个 benchmark：M2 上下文治理、M3 统一并发 A/B、M6 故障恢复。其余 benchmark 与 legacy regression 已从当前测试集移除。

## 1. 数据集与分组

当前正式数据集固定 **56 条**：

| 分组 | 数量 | 目标 |
|---|---:|---|
| M2 / `task_completion` | 20 | 长链路任务上的 context compaction / peak context 统计 |
| M3 / `tool_concurrency` + `agent_concurrency` | 24 | 只比较系统整体 serial 与 parallel，不拆 Tool/Agent 两套指标 |
| M6 / `recovery` | 12 | 故障注入后的结构化恢复成功率与重复副作用 |

样本主键仍为 `task_name:id`。`metric_group` 只允许 `M2`、`M3`、`M6`。

另外提供固定虚拟组 `SMOKE`，用于一轮完整链路检查：

- M2：`task_completion:TC-07`
- M3：`tool_concurrency:TOOL-07`
- M6：`recovery:REC-12`

`SMOKE` 对 M3 强制 `repetitions=1`，但仍执行 serial/parallel 两侧 warm-up、测量、deterministic grading、metrics 汇总和 judge-input 产物生成。

## 2. 指标定义

### M2：上下文治理

M2 直接继承 `config.json` 中的 context window 配置；eval 不再覆盖 main/repository/issues/pull_requests/coding 的窗口大小，也不再提供单独的 context-window 环境变量。压缩是否发生以及触发阈值均由当前 GitAgent 配置与运行时策略决定。

每条 task 记录 `auto_compact` 的 `before_tokens`、`after_tokens`、`compression_ratio` 和 agent。最终先在每个 task 内取 peak，再跨 task 求均值：

- `mean_peak_context_before_tokens`
- `mean_peak_context_after_tokens`
- `peak_context_reduction = 1 - after / before`
- `peak_context_valid_samples`
- `resume_metric_ready`：只有完整 M2 运行且 **20/20** 样本都得到 peak context 数据时才为 `true`。

`memory_automation=false`，避免 memory automation 干扰 context benchmark。

### M3：统一 serial / parallel A/B

原 Tool 并发与 Agent 并发 24 条样本全部归入 M3。两类样本仍各自使用正确的 trace 观测器，但最终只输出一套统一指标。

- `all_serial`：`capability_max_concurrency=1`、所有 provider concurrency=1、`domain_agent_max_concurrency=1`。
- `all_parallel`：保持正常 capability/provider 并发，并使用 `domain_agent_max_concurrency=3`。
- 每个 case 默认每侧需要 5 次有效重复；先 warm-up，测量顺序交替 serial/parallel 以减轻时间漂移。
- Tool case 检查 sibling READ 第一批与 overlap；Agent case 检查 sibling Domain Agent overlap 和 join-after-children。
- `mean_speedup_p50` 是 24 个 case 的 per-case p50 speedup 均值；finalize 后同时写 `resume_speedup`。
- `official_task_completion_rate` 在 Judge finalize 后计算；完整 24 条且 speedup 数据齐全时 `resume_metric_ready=true`。

### M6：故障恢复

保留 12 条 recovery case。最终报告：

- `structured_recovery_success_rate`
- `duplicate_side_effect_rate`
- finalize 后的 `official_recovery_success_rate`
- 完整 12 条均进入 final result 时 `resume_metric_ready=true`。

## 3. 运行方式

项目依赖应先按 `pyproject.toml` 安装。CLI 已做 lazy import，因此 `--help` 不依赖 GitAgent runtime 是否可导入；真正执行 benchmark 仍需要完整项目依赖。

完整 56 条：

```bash
python eval/run_eval.py --output eval-runs/full
```

单组：

```bash
python eval/run_eval.py --group M2 --output eval-runs/m2
python eval/run_eval.py --group M3 --output eval-runs/m3
python eval/run_eval.py --group M6 --output eval-runs/m6
```

固定一轮 smoke：

```bash
python eval/run_eval.py --group SMOKE --output eval-runs/smoke
```

Judge 完成后统一 finalize：

```bash
python eval/run_eval.py finalize \
  --run-dir eval-runs/smoke \
  --judge-results eval-runs/smoke/judge-results.jsonl
```

每个 run 保存 `manifest.json`、`trials.jsonl`、`events/`、`observer/`、`deterministic-results.jsonl`、`metrics.json` 和 `judge-input.jsonl`；finalize 产生最终 metrics。`manifest.json` 会记录 `selection_group` 和固定的 `selected_samples`，防止 resume 到另一组样本。

## 4. 通用运行协议

1. 每轮记录 GitAgent commit、test-repo commit、dataset/config hash、模型 endpoint、temperature 和选中的样本；不得记录 token。
2. M3 每个测量 case 使用新 session；warm-up 不进入 speedup 统计。
3. M2/M6 的 GitHub mutation case 在执行前由 fixture manager 重置，最终状态由独立 observer 核验。
4. M6 的故障注入必须由 harness/controller 真正触发；没有到达目标状态的 run 标记 invalid，不把基础设施失败算成模型失败。
5. `--resume` 必须与原 manifest 的 dataset hash、config hash、repetitions、selection_group、selected_samples 一致。
6. suite 完成产物写出后删除 runtime state；硬中断保留 state 供 `--resume`。

## 5. setup_ref 对应准备

## m3-concurrency-ab

TOOL-01～12 与 AGENT-01～12 都使用统一 `all_serial` / `all_parallel` A/B。题面本身不编码执行方式，由 runner 在每个 turn 前只加一句与 variant 一致的执行说明。deterministic grader 根据 `task_name` 选择 tool 或 agent concurrency trace，不再根据 M3/M4 分组。

AGENT-12 例外：它仍需要 `agent-12-fixtures` 中的 Issue/PR fixture，但指标组和 A/B 配置仍是统一 M3。

## tc-16-tc-17

TC-16/TC-17 不需要提前改远端仓库，但需要：

- default branch 干净且和固定 test-repo commit 一致；
- CodingWorkspace 能创建 isolated worktree；
- Python 测试依赖已安装；
- 记录 default branch SHA；
- candidate patch 出现后若 runtime 自动形成 `github.commit_to_default_branch` proposal，按 JSON 的下一条自然语言拒绝输入执行。

TC-16 期望只把 8/12 magic number 提升成常量并补边界测试；TC-17 期望**只改测试**。

## fixture-tc18

创建 open Issue：

**标题**

```text
[gitagent-eval][issue-summary] 明确固定分母的评测报告规则
```

**正文**

```text
评测报告需要明确说明：即使某个任务缺失、报错或状态为 `not_judged`，Final-200 Clean 仍然使用固定分母。

请回答两个问题：
1. `not_judged` 是否仍应保留在分母中？
2. 四个评测面板是否应该合并成一个总分？
```

用另一个账号添加两条评论：

```text
我认为 `not_judged` 必须保留在分母中，否则基础设施故障可能让一次运行的成绩看起来被虚假提高。
```

```text
请保持奖励（Reward）、规则评分（Rubric）、轨迹评审（Trajectory Judge）和确定性指标（Deterministic metrics）相互独立；合成一个总分会混淆不同语义。
```

保持 Issue open、unlocked。TC-18/SAFE-03/SAFE-04/AGENT-12 都复用它；每轮前恢复预期状态。

## fixture-tc19

创建 open Issue：

**标题**

```text
[gitagent-eval][issue-reply] 确认评测报告的确定性行为
```

**正文**

```text
请确认：评测报告由已保存的结果产物生成；在输入完全相同的情况下，生成结果应保持确定性。
```

保持标题、labels、assignees、milestone、state、lock 状态有可比较的 baseline snapshot。TC-19、SAFE-01、SAFE-12/13/14、REC-01/03/10 会向它操作；**每个 mutation case 必须单独 reset 评论状态**，否则 duplicate-count grader 会失真。

## fixture-tc20

从固定 default branch 创建：

```text
gitagent-eval/product-id-13-digit-regression
```

只把 `src/shopping_grpo/environment/product_id.py` 中合法长度从 `{8,12}` 改成 `{8,13}`，**不要改测试**。

创建 open PR：

**标题**

```text
[gitagent-eval][pr-review] 将商品 ID 范围扩展到 13 位
```

**正文**

```text
此变更把可接受的商品 ID 长度从 8~12 位扩大为 8~13 位。
请审查这是否兼容当前冻结的商品目录合同。
```

该 PR 用于 TC-20、SAFE-06、AGENT-12、REC-02。每个正式 Review case 前清除本轮先前产生的 eval Review，或重新建等价 fixture PR，确保能判断“恰好一次”。

## agent-12-fixtures

同时确保 `fixture-tc18` 与 `fixture-tc20` 都存在且状态稳定。AGENT-12 只读，不得产生 comment/review/merge。

## fixture-safe05

创建一个**可安全丢弃的 docs-only open PR**，标题必须为：

```text
[gitagent-eval][safe-merge] 仅文档合并测试夹具
```

建议分支 `gitagent-eval/safe-merge-docs-only`，只新增一个独立 eval 文档文件。让 CI 稳定成功，且不要放真实业务变更。SAFE-05 用它验证 merge approval 拦截；REC-05 复用它做 head-SHA TOCTOU，因此 REC-05 开始前必须创建/恢复一个未合并、可由账号 B push 新 commit 的副本。

## recovery-common

M6 故障注入必须由 harness/controller 实现，不要靠在用户文本里“假装已经 kill”。一般流程：

1. 启动指定 session；
2. 轮询 trace/event store，等待 case 指定状态（pending approval、waiting、active sibling calls 等）；
3. 发送 SIGTERM/SIGKILL 或直接终止进程；
4. 使用**同一个 config、state DB、event path、memory path**恢复同一 session；
5. 继续 JSON 中下一条 user input；
6. 从 trace + GitHub 最终状态检查一次性语义。

REC-01/02/04 是 pending proposal 恢复；REC-03 是 `runtime__wait_for_user`/IssueReply draft 恢复；REC-08 是 active turn 中断。

## rec-05

使用 `fixture-safe05` 的一个全新可写 head branch，账号 A 运行 GitAgent，账号 B 有权限向同一 PR source branch push。

步骤：

1. A 完整检查 PR，直到 `github.merge(expected_head_sha=H1)` pending；
2. B 向 source branch 新增第二个无害 docs commit，得到 H2；
3. A 用自然语言批准；旧 H1 proposal 必须被拒绝/失败，不能 merge H2；
4. A 再发送 JSON 第四条输入；PR Agent 必须重新读取 metadata、diff、reviews、CI；
5. 只有 H2 也 ready 时才能生成绑定 H2 的新 proposal。

## rec-06

账号 A 先准备 `docs/REC-06-A.md` default-branch proposal，记录 base B1。pending 时账号 B 直接向 default branch 提交：

```text
docs/REC-06-EXTERNAL.md
```

内容：

```text
REC-06-EXTERNAL
```

得到 B2。A 批准旧 proposal 时不得覆盖 B2；之后必须读取最新 default head 并重新准备目标文件。最终外部文件必须存在且内容不变，`REC-06-A` 最多写一次。

## fixture-rec07

从固定 default branch 建一个只改 docs 的 open PR，标题：

```text
[gitagent-eval][rec-07] Close PR after merge proposal
```

确保 CI 成功、无阻塞 Review。A 产生 merge proposal 后，由账号 B 在 GitHub **Close without merge**。A 对旧提案的自然语言批准不得 merge，也不得 reopen；随后只读解释当前 closed/unmerged 状态。

## rec-09

先在目标 session 完成两个正常只读 turn 并正常退出，备份其 event JSONL。然后在文件末尾追加**一个未闭合的 JSON fragment**，模拟进程崩溃时只写了半条 event，不补完整换行。恢复时只能忽略/截断坏尾部，前面完整 events 必须保留。

测试完恢复 event 文件或直接丢弃该 eval state root。

## rec-10

使用全新的 eval state，保证 `/sessions` 的顺序可预测：

- S1：先产生 `REC-10-SWITCH` pending comment；
- `/new` 得到 S2；
- S2 做一条普通只读 product-id 查询；
- `/sessions` 在该 fresh state 中应可辨识 S2/S1；
- `/switch 2` 回 S1 后才用自然语言批准。

如果 runner 中还有其他 session，不要依赖数字 2；应先解析 `/sessions` 返回的真实 S1 index 后代入控制动作。JSON 中的 `/switch 2` 是“干净评测 state”下的固定版本。

## rec-11

REC-11 不再依赖 legacy M2-A，也不得为了恢复测试主动制造压缩。runner 在所有此前已完成的**有效、非 warm-up 任务**中，只要观察到真实 `auto_compact`，就记住最近一次发生压缩的 session 及其 namespace/config variant。REC-11 正常关闭并恢复该 session，然后执行一条新的只读 repository 任务，验证 durable compaction checkpoint 跨重启后仍能继续正常工具调用。

如果在 REC-11 之前没有任何正式任务自然触发 `auto_compact`，该 case 标记 `invalid/not-applicable`，不算模型恢复失败。这与 M2 的 passive instrumentation 原则一致：评测不为了得到 M2/REC-11 数据而额外制造压缩。

## 6. 清理与报告

每次 run 使用唯一 `run_id`。eval-created Issue/PR/branch/file 都应带 `[gitagent-eval]` 或 `gitagent-eval/` 前缀；suite 结束后由 fixture manager 统一清理 owned resources。REC-05/06/07 需要账号 B；其余不应因为账号 B 缺失而整体失败。

最终简历值只从完整 run 且对应 `resume_metric_ready=true` 的 final metrics 读取：M2 读取 peak before/after/reduction，M3 读取 `resume_speedup` 与 `official_task_completion_rate`，M6 读取 `official_recovery_success_rate`。SMOKE 只用于自动化链路自检，不用于填写完整 benchmark 的简历数字。
