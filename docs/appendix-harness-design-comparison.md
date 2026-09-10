# 面试速查附录：GitAgent、Claude Code、Codex、Pi、DeepSeek Harness 的设计思路对比

> 这不是竞品功能清单，也不是要证明 GitAgent 比谁“更强”。
>
> 这份附录只服务一个面试目标：**当面试官问“为什么你的 Harness 这样设计，其他 Coding Agent/Harness 又是怎么做的”时，能快速把设计取舍讲清楚。**
>
> 对比基于截至 **2026-09-09** 能看到的公开文档与开源实现。Claude Code、Codex 等产品持续变化，因此这里重点记“设计方向”，不要死背某个版本的功能数量。

---

## 先记住五句话

| 系统 | 最值得记住的设计思路 |
|---|---|
| **GitAgent** | 把模型提出的动作和真实副作用拆开，用显式 Agent Loop、Capability、Execution、CandidatePatch、Approval、Recovery 契约把 GitHub 软件工程任务变成一条可验证、可恢复的流水线。 |
| **Claude Code** | 以成熟 Coding Agent 产品为中心，围绕内置工具、权限、Hooks、Skills、MCP、Subagent 和 Context 管理不断扩展运行时能力。 |
| **Codex** | 很强调“模型能力 + 受约束执行环境”，把 OS sandbox、approval policy、workspace 权限和 agent runtime 结合起来，让 Agent 可以更自主地执行真实编码任务。 |
| **Pi** | 极简核心：默认只给模型少量基础工具，把工作流差异尽量留给 Extensions、Skills、Prompt、Packages 和外部环境；核心不替用户规定唯一 Agent 形态。 |
| **DeepSeek Harness（dsh）** | 核心思想是 **Everything is a Plugin**：工具、上下文、模型适配、子 Agent、权限等能力通过可组合插件挂到 Harness 上，强调组合性和可替换性。 |

面试时不要把这五种方案理解成同一张“功能排行榜”。它们首先是在回答不同的问题。

```text
GitAgent     ：怎样让一次 GitHub Agent 任务按业务约束可靠落地？
Claude Code  ：怎样把 Coding Agent 做成高可用、可扩展的开发产品？
Codex        ：怎样让强模型在真实开发环境里自主工作，同时守住执行边界？
Pi           ：怎样把 Coding Harness 核心压到最小，让用户自己组合工作流？
DSH          ：怎样把 Harness 本身变成高度可组合的插件运行时？
```

---

## 一张表快速对比主要模块

| 设计维度 | GitAgent | Claude Code | Codex | Pi | DeepSeek Harness / dsh |
|---|---|---|---|---|---|
| **Agent Loop** | 显式 Loop，结构化 call、child、wait、end 都由 Harness 控制 | 产品化 tool-use loop，工具、subagent、hook 等能力嵌入生命周期 | 完整 coding-agent runtime，围绕 turn/thread、tool execution、approval、sandbox 工作 | 极简 agent core：模型 → tool → result → 继续 | 极简 host + 插件组合，Agent 能力由 preset / plugin 装配 |
| **工具系统** | `Capability` 是稳定契约；统一 schema、权限、provider、execution profile | 内置工具 + MCP；Skill 是按需工作流，不必都变成独立工具 | 内置 coding tools + MCP/app 等能力，并和权限策略结合 | 默认 `read/write/edit/bash` 四个核心工具，更多能力由 extension/package 加 | 工具本身也是插件，可注册到统一 context/service 中 |
| **Context** | Persistent messages + current system + ephemeral knowledge；有 tool-span-aware compaction、读取状态 | 自动 compaction；Skills 按需加载；Subagent 用独立 context 隔离大量中间信息 | 强调增量 context、大小上限、减少 cache miss；支持自动 compaction | `AGENTS.md`/`SYSTEM.md` + session tree + compaction，鼓励用户自己做 context engineering | context 能力也是插件；workspace instructions、session reference 等进入可重放 session history |
| **Multi-Agent** | 固定 Main → Domain → Coding 拓扑；职责与结构化交接明确 | Subagent 独立 context，可限制工具/模型，也有更丰富的并行协作能力 | 支持 subagent/custom agent，并继承或覆盖 runtime 权限 | **核心故意不内置 subagent**；可用 extension/package/tmux 自己实现 | `ctx.subagents` 是独立 capability seam，可接 in-process、ACP、Codex、Claude Code、dsh child 等 provider |
| **并发调度** | Harness 显式维护资源声明、兼容组、prepare/run/ordered commit | 产品内部支持并行 subagent / 工具场景；公开层更偏使用能力而非 GitAgent 这种资源声明模型 | 多 agent/thread 与工具执行由 runtime 管理，安全策略和 thread 身份绑定 | 核心保持简单，并发形态更多交给扩展或外部工具 | 子 Agent、工具等都可通过插件机制组合，支持多 provider 并存 |
| **代码执行边界** | Git worktree 隔离候选代码，但明确 **不是 OS sandbox** | 权限系统控制 Bash/编辑/MCP 等动作，可按工具配置 allow/deny/ask | 这是其明显重点：本地使用 OS-level sandbox，workspace write、网络与审批策略分层 | 默认不做 permission popup；官方思路更偏“需要时自己用 container/extension 做” | 可组合 sandbox backend，例如 bash sandbox 插件，并和 approval 插件分离 |
| **代码候选与验证** | `ChangeRequest → Workspace revision → VerificationReport → CandidatePatch`，验证绑定实际 revision | Agent 直接在工作区修改并执行检查，Hooks/工具可补充验证流程 | Agent 在受控 workspace 中修改、测试；强调真实开发环境执行 | 直接对当前工作目录读写执行，rollback/checkpoint 更依赖 git 或 extension | 由具体 coding preset / tool / workflow 插件组合，不强制只有一种候选模型 |
| **Approval / Side Effect** | 远端 mutation 单独建模；Approval 绑定精确 capability + normalized args + order + state | 权限系统围绕具体工具调用控制执行 | sandbox 决定“技术上能做什么”，approval policy 决定“什么时候必须问用户” | 核心不内置 permission popup，交给运行环境或 extension | approval 是独立 service/plugin，可在 tool pre-execute 阶段 ask/deny |
| **Persistence / Resume** | Event History 保存消息事实，Pause Snapshot 保存控制树；明确支持 waiting/approval 跨进程恢复 | Session 可继续，subagent 也支持 resume 等产品能力 | thread/turn 是稳定运行单元，支持 resume，runtime 状态与 approval 流程围绕 identity 管理 | session 是树结构，可从历史节点 branch；支持 resume 和 compaction | session 采用事件化设计，context、subagent continuation 等可以基于 session store 恢复 |
| **扩展哲学** | 先定义稳定内部契约，再把 Native/GitHub/MCP/RAG/Skill 接入统一层 | Hooks + Skills + MCP + Subagents 是主要扩展面 | runtime/config/MCP/app/sandbox/agent 配置共同扩展 | **极简 core + 极强 extension**，很多别人内置的能力它刻意不内置 | **Everything is a Plugin**，扩展性本身就是核心架构 |
| **最像 GitAgent 的地方** | — | ToolUse、context isolation、skills/MCP、权限 | runtime 边界、approval、workspace、安全执行 | 简洁 tool loop、exact edit、skills/context engineering | provider registry、capability seam、可组合 context/subagent/tool |
| **和 GitAgent 最大区别** | — | 更偏通用 Coding Agent 产品，而 GitAgent 还显式建模 GitHub 领域工作流和远端 mutation | Codex 有真正 OS sandbox；GitAgent 当前主要是 worktree + capability/approval 约束 | Pi 主动减少核心策略，而 GitAgent 主动把执行、恢复、业务安全规则固化进 Harness | DSH 追求插件化通用 Harness；GitAgent 更强调固定业务 pipeline 和确定性契约 |

---

# 1. Agent Loop：GitAgent 为什么更“显式”

## GitAgent

GitAgent 把模型输出看成**候选控制信号**。

```text
AgentContext
    ↓
Model
    ↓
StructuredCall / Text
    ↓
Agent Loop 判断
    ├─ Capability Call
    ├─ Child Agent
    ├─ Wait
    └─ End
```

`call_id`、open call、waiting、child return 都属于 Harness 的正式状态。

它的重点是让 Harness 能明确回答下面这些控制问题，模型自由地“想一步做一步”只占其中一部分：

- 现在到底有哪些动作还没闭合？
- 这个 tool result 属于哪个 call？
- 当前为什么暂停？
- 恢复以后应该从哪里继续？

## 对比

**Claude Code / Codex** 也有成熟 tool-use runtime，但作为产品，它们公开给用户的抽象更多是 session、tool、subagent、approval、permission 等，不需要用户理解 GitAgent 这种业务内部状态机。

**Pi** 则故意把核心 loop 保持很小：给模型几个工具，不断把 tool result 放回模型，复杂行为让 extension 再加。

**DSH** 更进一步把很多能力拆成 plugin/service seam；loop 是宿主，能力通过组合挂载。

### 面试一句话

> GitAgent 选择把“可恢复、可审计、能安全接业务副作用”需要的控制状态显式化，因此 Agent Loop 比最小核心更重；Pi 更偏最小核心，DSH 更偏可组合宿主，Claude Code/Codex 则属于更成熟的通用 Coding Agent runtime。

---

# 2. Tools / Capability：真正差异不是有没有 MCP

GitAgent 的关键在于所有工具进入 Agent Loop 前都会先变成统一的 `Capability`，工具数量本身并非设计重点：

```text
不同 Provider
Native / GitHub / MCP / RAG / Skill
              ↓
        Capability Registry
              ↓
 schema + permission + allowed agent
 + execution profile + provider binding
              ↓
           Agent Loop
```

这样 Agent 不需要知道能力来自本地 Python、GitHub API 还是远端 MCP。

### Claude Code

它更像成熟产品的“多扩展面”：

- 内置 coding tools；
- MCP 增加外部工具；
- Skill 按需加载工作流知识；
- Hook 插到 lifecycle；
- Subagent 再提供独立执行上下文。

### Codex

工具和**权限 / sandbox** 结合得更紧。一个动作是否能执行，不只是 schema 是否合法，还取决于当前 workspace、sandbox mode、网络策略和 approval policy。

### Pi

Pi 的反方向特别值得记：默认只有很少的基础工具，并且明确**不把 MCP、subagent、permission popup、plan mode 全塞进 core**。需要时用 extension/package 构建。

### DSH

DSH 把“能力从哪里来”做成插件组合问题。Tool、LLM adapter、subagent provider、context provider 都能通过 service/plugin 注册。

### 面试一句话

> GitAgent 的 Tool System 更像一个“稳定能力中间层”：我关心的是任何外部能力进入 Agent 前都要先统一 schema、权限、agent scope 和执行语义；这和 DSH 的 capability seam 有相似点，但 GitAgent 的约束更围绕自己的 GitHub workflow 固化。

---

# 3. Context：大家都在压 token，但设计目标不完全一样

### GitAgent

GitAgent 把上下文拆成：

```text
Persistent Messages
+ Current System
+ Ephemeral Knowledge
        ↓
Context Builder
        ↓
Budget / Compaction
        ↓
Model Context
```

并且特别关心：

- tool call/result span 不能被压坏；
- 文件读到什么范围由 `FileReadState` 记录；
- 文件改变后读取状态失效；
- Memory / RAG 是临时注入，不自动变成当前事实。

### Claude Code

最值得记的是三种 context engineering 手段：

1. 自动 compaction；
2. Skills 按需加载，避免全部知识常驻；
3. Subagent 用独立 context，把大量搜索和中间结果隔离出去，只把结果返回主线程。

### Codex

Codex 的开源仓库对 model-visible context 有很明确的工程原则：**增量构建、避免频繁改历史导致 cache miss、任何注入内容都必须 bounded**。这体现的是“context 不只是 token 数，还会影响模型缓存和系统稳定性”。

### Pi

Pi 把 context engineering 很大程度交给用户：`AGENTS.md`、`SYSTEM.md`、skills、session tree、compaction，再通过 extension 自己改策略。

### DSH

DSH 把 workspace instruction、session reference、时间等 context source 都做成 request-context plugin，并让注入内容进入 session history，因此能够 replay 和 compact。

### 面试一句话

> GitAgent 的 Context 设计更关注协议完整性和“读过什么”的业务状态；Claude Code 很强调按需加载和 subagent context isolation；Codex 还明确考虑 context 变化对 cache 的影响；Pi/DSH 则分别体现“用户可编程”和“插件可组合”的 context engineering。

---

# 4. Multi-Agent：GitAgent 选择固定拓扑，而不是通用 Agent Swarm

## GitAgent

```text
Main
  ↓ route
Repository / Issue / PR
  ↓ when coding is needed
Coding
```

这个设计故意比较固定。

Main 不负责所有细节；Domain Agent 持有 GitHub 业务语义；Coding Agent 只负责本地分析或产生候选代码。

好处是：

- delegation 边界清楚；
- child context 隔离；
- 业务状态不会全塞给一个 universal agent；
- 哪一层能生成什么产物比较稳定。

## 对比

**Claude Code** 的 subagent 更通用：每个 subagent 可以有独立 prompt、tool、permission、model/context，用于探索、规划或专门任务。

**Codex** 也支持 subagent/custom agents，并把 child 的 sandbox/approval 与当前 runtime policy 联系起来。

**Pi** 最有意思：它认为“subagent 有很多正确做法”，所以 core 不强行内置一种；你可以用 extension、package、tmux 或多个 Pi 进程实现。

**DSH** 把 subagent 做成 provider seam，同一个 Harness 甚至可以同时挂 in-process child、Codex child、Claude Code child 或另一个 dsh child。

### 面试一句话

> GitAgent 的 Multi-Agent 目标是用固定角色减少单个 Agent 同时承担路由、GitHub 业务和 coding 带来的上下文与权限耦合，并未扩展成通用 swarm；这种设计可控，但灵活性低于 Claude Code/DSH 的通用 subagent 体系。

---

# 5. Execution / Sandbox：这里 GitAgent 和 Codex 差异最大

这一点面试一定要主动说清楚。

## GitAgent

GitAgent 有两个不同层面的隔离：

```text
逻辑执行约束
Capability permission
+ ResourceClaims
+ prepare/run/commit
+ approval

代码候选隔离
Detached Git Worktree
+ source SHA
+ workspace revision
```

但：

> **Git worktree 不是 OS sandbox。**

它隔离 Git 候选工作区，不意味着 shell 无法访问工作区以外文件，也不代表网络天然关闭。

## Codex

Codex 把这一层做得更底层：本地 runtime 有 OS-enforced sandbox，workspace write、网络访问和 approval policy 分开管理。

因此二者不是“都有 workspace，所以一样”。

```text
GitAgent worktree
主要解决：候选代码版本隔离、验证绑定、patch snapshot

Codex sandbox
主要解决：进程到底能访问/修改哪些 OS 资源
```

## Claude Code

Claude Code 主要通过 permission policy 控制 Bash、编辑、MCP 等动作，再结合用户环境运行。

## Pi

Pi 很明确地不在 core 里默认提供 permission popup；如果需要强隔离，倾向让用户使用容器或 extension。

## DSH

DSH 则把 sandbox backend 和 approval 当成可组合能力，因此不同 preset 可以选择不同执行边界。

### 面试一句话

> GitAgent 当前最重要的边界是：worktree 是版本与候选隔离，不是安全 sandbox。如果要向 Codex 那种更通用的 autonomous coding runtime 演进，OS-level sandbox 会是独立的一层，而不是继续往 Git worktree 上堆规则。

---

# 6. Approval：GitAgent 更强调“业务 Mutation 授权”

GitAgent 有一个比较有特色的拆分：

```text
CandidatePatch
     ↓
Domain Agent
     ↓
Mutation Plan
     ↓
Approval
     ↓
Protected Preflight
     ↓
GitHub Mutation
```

用户批准的是一份精确调用计划，宽泛的“可以修改”不足以成为运行时授权。

这特别适合 GitHub 业务，因为：

- push 哪个 branch；
- 基于哪个 expected head；
- 创建哪一个 PR；
- merge 哪个 PR；

这些都是远端持久副作用。

### Codex

Codex 更强调通用执行风险：

```text
Sandbox = 技术上允许做到哪里
Approval = 哪些动作需要用户确认
```

这是非常漂亮的两层分离。

### Claude Code

更偏具体工具权限和用户策略：哪些 tool 可以直接执行，哪些 ask，哪些 deny。

### Pi

Pi core 不规定统一 approval UX，留给 extension/容器/用户自己的运行环境。

### DSH

approval 是独立 service，可以在 tool pre-execute 阶段返回 ask/deny，因此和 plugin composition 融合。

### 面试一句话

> Codex 的 approval 主要是通用 runtime risk control；GitAgent 又多了一层 GitHub domain mutation plan，因为我的项目特别关心“批准的到底是哪一个远端业务动作”，而不是只问“这个 shell command 能不能跑”。

---

# 7. Persistence / Recovery：GitAgent 为什么拆 Event History 和 Pause Snapshot

GitAgent 没有把“恢复”简单理解成重新把聊天记录发给模型。

```text
Event History
负责：发生过什么、消息是什么

Pause Snapshot
负责：控制树现在停在哪里
```

因此 approval waiting、child waiting 等状态可以显式恢复。

**Pi** 的 session tree 很适合理解另一种思路：历史天然支持 branch，可以跳回旧节点继续，但它不是 GitAgent 这种业务控制树快照模型。

**Claude Code / Codex** 作为完整产品同样支持 session/thread resume；Codex 尤其把 thread/turn identity 和 runtime、approval flow 联系起来。

**DSH** 的 session/event 模型与 GitAgent 在“事件可 replay”这件事上有相似方向，并且 continuable subagent 也可以依赖 session store。

### 面试一句话

> GitAgent 做 recovery 时区分“消息历史”和“控制状态”，因为仅重放聊天记录不能可靠推出一个正在等待 approval/child 的运行树；这是为确定性恢复增加的复杂度。

---

# 8. 最后不要说“谁更好”，要说取舍

如果面试官问：

> 你看过 Claude Code / Codex / Pi / DSH，那你的 GitAgent 和它们比有什么特点？

可以这样回答：

> 我不会把 GitAgent 和这些成熟 Harness 做功能数量比较，因为定位不同。GitAgent 是我为了研究 GitHub 软件工程 Agent Harness 做的项目，所以我把很多通用产品可能隐藏在 runtime 里的机制显式建模了：Agent call identity、Capability 契约、资源并发、workspace revision、CandidatePatch、Mutation Plan、Approval 和 Pause Snapshot。
>
> 对比下来，我觉得几种方案代表了不同取舍。Pi 让我看到“核心可以非常小，把策略留给 extension”；DSH 更进一步强调 everything-is-a-plugin；Claude Code 展示了成熟 Coding Agent 如何通过 Skills、Hooks、MCP、Subagent 做产品化扩展；Codex 对 sandbox 和 approval 的分层尤其值得参考。GitAgent 自己选择的是更强约束、更固定的 GitHub workflow，所以可控和可解释，但通用性和动态组合能力没有它们强。

这个回答比说“我也实现了 MCP、Multi-Agent，所以和 Claude Code 很像”好得多。

---

# 9. 高频追问速答

| 面试官追问 | 建议回答主线 |
|---|---|
| **为什么不做 Pi 那么简单？** | GitAgent 要支持 approval、远端 mutation、crash recovery 和确定性 eval，所以需要更多显式控制状态；这是项目目标不同，不是越复杂越好。 |
| **为什么不直接学 Claude Code 做一个 universal agent？** | GitAgent 故意让 Domain Agent 持有 Issue/PR/Repository 业务语义，减少 universal agent 的权限和 context 耦合。 |
| **GitAgent 有 sandbox 吗？** | 当前没有 Codex 那种 OS-level sandbox；Coding Workspace 是 Git worktree 隔离，安全边界主要来自 capability/permission/approval。 |
| **你和 DSH 的 capability 有什么不同？** | 都有统一能力 seam 的思想；DSH 更通用、更插件化，GitAgent 的 Capability/Execution 契约更围绕自己的固定 pipeline 和资源语义。 |
| **为什么 Skill 不直接变成 Tool？** | Skill 主要给模型按需补充工作方法和说明，不自动获得新权限；Tool/Capability 是运行时真正能执行的动作。 |
| **为什么 Multi-Agent 不做动态 DAG？** | 当前 GitHub 任务角色非常明确，固定 topology 更易控制、评测和恢复；动态 DAG 会增加 routing、权限、context 和 recovery 状态空间。 |
| **为什么 Approval 不直接用 allow/deny？** | 本地工具权限适合 allow/deny，但远端 mutation 还要绑定具体参数、顺序和当前 GitHub 状态，否则“同意修改”过于宽泛。 |
| **最想借鉴 Codex 什么？** | OS-level sandbox 与 approval policy 的分层，把“技术能力边界”和“用户授权边界”进一步解耦。 |
| **最想借鉴 Pi 什么？** | 对 core complexity 的克制：不是所有好功能都应该进入 Harness 核心。 |
| **最想借鉴 DSH 什么？** | 插件组合与 provider seam，让不同模型、subagent、context source 可以更低耦合替换。 |
| **最想借鉴 Claude Code 什么？** | Skills / Hooks / Subagents / MCP 围绕真实开发工作流形成的成熟扩展生态，以及用 context isolation 控制长任务成本。 |

---

# 10. 复习时只需要记住这张坐标图

```text
                    更强的固定运行时约束
                           ↑
                           │
               GitAgent   │   Codex
                           │
业务工作流专用 ←───────────┼───────────→ 通用 Coding Harness
                           │
                Claude Code│  DSH
                           │
                     Pi    │
                           ↓
                    更强调外部组合 / 扩展
```

这张图不是严格排名，只是帮助建立设计直觉：

- **GitAgent**：固定领域 workflow + 显式契约；
- **Codex**：通用 coding runtime + 强执行边界；
- **Claude Code**：通用 coding product + 丰富扩展面；
- **Pi**：极简 core + 用户自定义；
- **DSH**：插件化 host + 组合式 Harness。

面试真正要展示的是：**你知道自己的设计为什么成立，也知道它没有解决什么，更知道另一种 Harness 为什么会做出不同选择。**

---

## 公开资料入口

以下只用于复习设计方向，具体功能以各项目最新官方文档为准。

- Claude Code：`https://code.claude.com/docs/en/how-claude-code-works`
- Claude Code Tools：`https://code.claude.com/docs/en/tools-reference`
- Claude Code Subagents：`https://code.claude.com/docs/en/subagents`
- OpenAI Codex：`https://github.com/openai/codex`
- Codex sandbox / approvals：`https://developers.openai.com/codex/agent-approvals-security`
- Pi Coding Agent：`https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent`
- Pi：`https://pi.dev/`
- DeepSeek Harness：`https://github.com/deepseek-ai/deepseek-harness`
- DeepSeek Harness Subagent：`https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/subsystems/subagent.md`
