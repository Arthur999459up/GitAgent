# 豆包 Agent Harness 算法实习：最高频 Agent 八股速背

> 面向：豆包大模型 Agent Harness 算法实习生  
> 筛选原则：结合近期字节 AI Agent / 大模型 Agent 面经、JD 点名能力，以及简历中 GitAgent 最可能被追问的内容。  
> 目标：**用最少题目覆盖最高频考点。**

## 一、最高频 Agent / Harness 八股

### Q1. Agent、LLM、Workflow、Harness 分别是什么？

**标准回答**

LLM 负责生成和决策；Agent 是让 LLM 围绕目标持续观察、决策、调用工具的系统。Workflow 的执行路径通常预先定义，而 Agent 可以动态决定下一步；Harness 是承载 Agent 运行的工程底座，负责 Context、Tools、执行、权限、状态、恢复和评测。

> **速记：** LLM 是大脑，Agent 是智能体，Workflow 是固定流程，Harness 是运行底座。

### Q2. Harness 和 Orchestrator 有什么区别？

**标准回答**

术语没有完全统一的行业定义。工程上通常把 **Orchestrator** 理解为路由、调度、依赖和执行顺序控制；**Harness** 范围更大，还包括 Context、Tool Runtime、权限、安全、持久化、恢复和 Eval，因此可以把 Orchestrator 看成 Harness 的核心模块之一。

### Q3. 一个完整的 Agent 系统有哪些核心模块？

**标准回答**

最核心的是：

`Model + Context + Planning / Agent Loop + Tools + Execution`

生产系统通常还需要：

`Memory / RAG + Permission / Sandbox + Persistence / Recovery + Eval / Observability`

模型负责概率决策，Harness 负责把决策可靠地落到真实环境。

### Q4. ReAct 和 Plan-and-Execute 有什么区别？

**标准回答**

ReAct 是“思考一步、执行一步、观察后再决定”，适合环境变化大、需要边做边看的任务。Plan-and-Execute 先生成整体计划再逐步执行，适合结构较清晰的长任务；实际系统常混合使用，即先粗规划，再用 ReAct 动态修正。

### Q5. Tool Calling 的完整流程是什么？Tool 应该怎么设计？

**标准回答**

模型只负责生成结构化 Tool Call；Harness 再完成：

`解析 → Schema 校验 → 权限 / 状态校验 → 执行 → 返回 Observation → 下一轮推理`

Tool 的名称、描述和参数要语义明确、尽量正交；参数尽量 typed / enum，错误要结构化且可恢复。

### Q6. Function Calling、MCP、A2A 有什么区别？

**标准回答**

- **Function Calling**：解决 LLM 如何向宿主表达结构化动作。
- **MCP**：解决 Agent / 宿主如何标准化连接外部 Tools、Resources、Prompts。
- **A2A**：更关注 Agent 与 Agent 之间的发现、任务委托和结果交换。

> **速记：** Function Calling：Model → Harness；MCP：Harness → Tool；A2A：Agent → Agent。

### Q7. Skill 和 Tool 有什么区别？

**标准回答**

Tool 是可执行能力，例如读文件、搜索、运行测试；Skill 是可复用的任务方法、规范或流程知识，告诉 Agent“这类任务应该怎么做”。加载 Skill 不应该自动扩大 Tool 权限。

### Q8. Context、Memory、RAG 有什么区别？

**标准回答**

- **Context**：当前这一次推理真正输入模型的信息。
- **Memory**：跨轮次或跨 Session 持久化的信息。
- **RAG**：从外部知识库按需检索信息的方法。

Memory 和 RAG 的结果最终都需要经过筛选，再注入当前 Context。

### Q9. Context 超过窗口怎么办？上下文压缩应该保留什么？

**标准回答**

优先删除无关信息、裁剪可重取的大型 Tool Output，再对较老且已经闭合的历史做摘要。System / User 约束、当前任务状态、近期关键 Observation、未闭合 Tool Call 必须优先保留。

评价压缩策略时要同时看 **task success、token、latency 和恢复一致性**，不能只看压缩率。

### Q10. Agent 为什么会路径震荡、重复调用工具？怎么解决？

**标准回答**

常见原因是状态不清晰、Tool Error 信息不足、没有记录已尝试动作、缺少停止条件。可以加入结构化状态、Tool Call 去重、No-Progress Detection、最大步数、明确错误反馈和外部 Verifier。

### Q11. 什么时候应该用 Multi-Agent，而不是 Single-Agent？

**标准回答**

当任务可以自然分工、不同角色需要不同 Context / Tools、可以并行，或者需要独立 Reviewer / Verifier 时，多 Agent 有价值。任务简单或高度耦合时，Multi-Agent 会增加通信、Token 和失败面，Single-Agent 往往更合适。

### Q12. 多 Agent / 多 Tool 并发最难的问题是什么？

**标准回答**

难点不是“怎么开线程”，而是 **并发执行不能破坏共享状态的一致性**。常见做法是资源声明 / 锁、独立 Workspace、版本号或乐观并发控制，并把“物理并行执行”和“逻辑状态提交顺序”分开。

### Q13. RAG 的完整流程是什么？为什么常用 Hybrid Retrieval + Rerank？

**标准回答**

离线流程：

`Chunk → Embedding → Index`

在线流程：

`Query → Retrieve → Rerank → Context Assembly → LLM`

BM25 擅长精确关键词，Dense Retrieval 擅长语义匹配，Hybrid 可以提高召回；Reranker 再对少量候选做更精确排序。

### Q14. Agent Evaluation 应该看哪些指标？

**标准回答**

至少看四类：

1. **Outcome**：任务是否成功。
2. **Trajectory**：Tool 和步骤是否合理。
3. **Safety**：是否越权或违反 must-not。
4. **Efficiency / Reliability**：Token、成本、延迟、错误和恢复。

能程序判断的事实优先使用 deterministic grader，LLM Judge 只补语义评价。

### Q15. Agent 安全为什么不能只靠 Prompt？

**标准回答**

Prompt 只是 soft constraint，真正的安全必须由 Runtime 强制执行。核心手段包括最小权限、Tool Schema / 参数校验、Sandbox、敏感操作审批、审计，以及对有副作用操作使用幂等键或状态复核，避免盲目 Retry。

### Q16. 如果让你优化一个 Coding Agent / Agent Harness，你会怎么做？

**标准回答**

先建立指标和失败归因，区分问题来自 Model、Context、Tool、Retrieval、Runtime 还是 Permission；再针对性优化 Tool Schema、Context、并行、Model Routing、Verifier 和 Recovery。最后固定模型和任务集做 A/B，比较 success、cost 和 latency，而不是一上来就换更强模型。

## 二、结合 GitAgent：最可能被追问的 3 题

### Q17. GitAgent 为什么做 Multi-Agent，而不是一个 Agent 调所有工具？

**标准回答**

主要不是为了“Agent 更多更强”，而是为了 **职责隔离、Context 隔离和最小权限**。Main Agent 负责路由，领域 Agent 处理 Repository / Issue / PR，Coding Agent 单独处理代码修改，避免代码探索产生的大量上下文污染主 Agent。

### Q18. GitAgent 的 Context Compaction 为什么这样设计？怎么证明有效？

**标准回答**

项目按 Context 压力逐级处理：先压缩可重取 Tool Output，再生成确定性历史检查点，最后才做紧急裁剪，并始终保持 Tool Call / Result 成对完整。

简历结果是 20 个长任务中峰值 Context Token 从 `74,976` 降至 `41,100`，降低 `45.17%`。回答时要强调：不仅看 Token，还要保证任务成功率和恢复一致性。

### Q19. GitAgent 的并发、安全和恢复怎么保证？

**标准回答**

并发上用逻辑资源声明，只让无冲突调用并行执行，并按原逻辑顺序提交状态，因此 24 项任务相对串行达到 `1.66×` 加速。安全上采用默认拒绝、参数级审批、独立 Git Worktree 和真实测试验证；恢复时持久化调用、审批和状态，对结果不确定的远端写操作禁止自动重试。

## 三、面试前最后 3 分钟

1. **Harness 是 Agent 的运行底座，Orchestrator 更偏调度。**
2. **ReAct 边观察边决策，Plan-and-Execute 先规划再执行。**
3. **模型只生成 Tool Call，真正执行和权限检查在 Harness。**
4. **Function Calling：Model → Harness；MCP：Harness → Tool；A2A：Agent → Agent。**
5. **Context 是当前输入，Memory 是长期状态，RAG 是按需检索。**
6. **Context 压缩优先删可重取信息，不能破坏关键状态和 Tool Call / Result。**
7. **Multi-Agent 的核心价值是 specialization、context isolation、parallelism 和 verification。**
8. **并发难点是共享状态一致性，而不是线程池。**
9. **Agent Eval 要看结果、轨迹、安全、效率和真实外部状态。**
10. **Prompt 不是安全边界，权限、Sandbox、Approval、Verifier 必须 Runtime enforce。**

## 四、筛选依据

近期字节 Agent 面经中重复出现的主题主要包括：Harness 与 Orchestrator、Agent 核心架构、ReAct / Plan、Context 压缩、Memory、MCP / Tool / Skill、Multi-Agent、RAG、Coding Agent、Harness Eval 和 Sandbox / Safety。

本文只保留与本 JD 和 GitAgent 简历最相关的最高频部分。
