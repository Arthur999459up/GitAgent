# 10 上下文治理与读取状态：GitAgent 如何在长会话里控制模型输入与重复读取

前一章讲清楚了 Session、Event History、Pause Snapshot 和恢复流程。本章继续沿着“一个长任务怎样持续运行”这条线，专门解释两个运行时模块：

- **Context Governance**：每次调用模型之前，怎样从历史事实构造当前模型真正能看到的消息；上下文变大以后，又怎样安全压缩。
- **Read State**：Agent 多次读取仓库文件时，怎样记住已经读过哪些范围，减少重叠读取，并在工作区发生变化后让旧状态失效。

这一章沿两条状态链展开：先说明模型输入怎样构造和压缩，再解释文件读取状态怎样记录、失效和恢复。关键设计点会直接说明动机、边界和代价，方便把机制和原因放在同一条运行链里理解。

---

## 1. 先建立全景：本章其实有两条相互配合的链路

先不要急着记阈值、checkpoint、coverage 这些细节。先看一次长会话里，两条链路分别负责什么。

```mermaid
flowchart TD
    subgraph C[模型上下文链路]
        E[Session Events / Agent Messages] --> P[Projector / 当前消息线程]
        P --> X[加入当前 system 与临时 guidance]
        X --> B[计算请求 token]
        B --> K[按压力执行 compaction]
        K --> M[Model Request]
        K --> CE[Compaction Event]
    end

    subgraph R[文件读取链路]
        Q[read_file / read_files 请求] --> L[FileReadLedger.prepare]
        L --> G{哪些范围还没读}
        G -->|全部覆盖| H[返回 already_read + coverage]
        G -->|存在缺口| F[只读取缺口]
        F --> V[校验 Provider 返回]
        V --> U[更新 coverage / EOF]
    end
```

这两条链路关注的对象不同：

| 链路 | 管理的对象 | 直接目标 |
|---|---|---|
| Context Governance | 模型消息、tool call/result、system、临时 guidance | 让下一次模型请求保持可解释、可压缩、可恢复 |
| Read State | 仓库文件读取范围、ref、EOF | 避免重复读取已经覆盖的文件区间 |

它们会在长任务里互相影响：文件读取会产生大量 tool result，tool result 会进入模型历史；历史越来越大后，Context Governance 又会开始压缩这些旧结果。

先记住一句话：

> **Context Governance 管“模型下一轮看到什么”，Read State 管“文件下一次还要不要真的再读”。**

---

# 第一部分：模型上下文是怎样构造出来的

## 2. 第一步：先把持久化事件投影成标准模型消息

### 2.1 先看它怎么做

GitAgent 持久化的是 Session Event。模型接口最终需要的是标准的 `system / user / assistant / tool` 消息序列。

中间由 **Context Projector** 完成转换。

```mermaid
flowchart LR
    E[Event History] --> F[按 agent / run_id 选择事件]
    F --> P[Context Projector]
    P --> C[Canonical Messages]
    C --> T[当前 Agent 消息线程]
```

Projector 做的事情可以拆成四步。

### 第 1 步：选择属于当前消息线程的事件

Main Agent 和 Domain Agent 共用同一份 Session Event History，但它们看到的消息线程不同。

| 消息线程 | 投影依据 |
|---|---|
| Main | 取 Main 可见的用户消息、Main model message、相关 compaction event |
| Domain Agent | 按 `agent + run_id` 只恢复这一条 Domain run 的 model message 与 compaction event |

因此，同一个 Session 里可以同时存在 Main、Repository、Coding 等 Agent 的运行事实，恢复时仍然能够把各自的模型线程拆开。

### 第 2 步：把事件转成统一的 canonical message

所有准备进入模型线程的消息都会经过统一格式校验。系统只接受标准 role 和对应字段，例如 tool result 必须带 `tool_call_id`，assistant 的 tool call 必须有合法 call id、函数名和参数字符串。

这一层的作用是把“历史事件格式”收束成“模型接口格式”。后面的压缩、恢复和 token 计算都建立在同一种消息结构上。

### 第 3 步：把 tool result 放回对应 tool call 附近

某些 Agent 调用可能经历异步执行或父子 Agent 调度，事件写入顺序和模型最适合看到的调用顺序可能存在差异。

Projector 会根据 `call_id` 把已经完成的 tool result 关联回原来的 assistant tool call，使模型线程重新形成连续的调用关系。

```mermaid
sequenceDiagram
    participant A as Assistant Message
    participant P as Projector
    participant T as Tool Result

    A->>P: tool call, call_id=c1
    T->>P: result, tool_call_id=c1
    P->>P: 按 call_id 关联
    P-->>A: assistant call 后紧跟对应 result
```

### 第 4 步：应用历史中的 compaction event

如果历史里已经发生过上下文压缩，Projector 会按照 compaction event 中保存的 replacement、checkpoint 和保留索引重新得到压缩后的模型视图。

所以恢复出来的消息线程已经包含过去发生过的压缩决定。

## 3. 第二步：在持久消息之外，加入本轮才需要的上下文

### 3.1 先看它怎么做

模型请求并不只包含历史消息。真正发给模型之前，还会加入当前运行环境才能确定的信息。

对于 Main Agent，可以把构造过程理解成：

```mermaid
flowchart TD
    H[Event History 投影出的 Main 历史] --> B[ContextBuilder]
    S[当前 system prompt] --> B
    T[当前可见 tool schemas] --> B
    B --> C[统一做 token 预算与 compaction]
    C --> M[Main Model Request]
```

对于 Domain Agent，`AgentContext` 自己维护当前消息线程。在调用模型之前，它会复制这份线程，并把本轮的 guidance、额外 memory read 等临时信息追加进 system message，然后再进入同一套 compaction 逻辑。

```mermaid
flowchart TD
    D[AgentContext.messages] --> E[_ephemeral_messages]
    G[Agent guidance] --> E
    R[额外 memory reads] --> E
    E --> C[compact_messages]
    C --> M[Domain Model Request]
```

这里需要区分三类信息，而且 Main 与 Domain 的 system 处理方式有一点差别。

| 信息 | 例子 | 当前实现怎样处理 |
|---|---|---|
| Durable thread | user、assistant、tool call、tool result；Domain 线程中的 system | 作为 model message 持久化并可重放 |
| Main current system | Main 当前系统规则 | `ContextBuilder` 构造 Main 请求时放到消息最前面 |
| Ephemeral guidance | Domain 本轮 guidance、额外 memory read | 只参与当前请求构造，不直接写回永久消息内容 |

Domain Agent 的 system message 本身属于它的消息线程。临时 guidance 会在本轮请求里追加到这条 system message 上；如果本轮触发压缩，GitAgent 更新 `AgentContext.messages` 时会把第一条 system 恢复成 Agent 原始的 `system_prompt`，避免临时内容随着压缩永久进入消息线程。

### 3.2 这一步最重要的边界

可以把“模型可见信息”和“Harness 控制状态”分开理解。

例如：

- tool call 是否仍然 open，由 Harness 根据消息协议判断；
- `waiting`、child context、未提交 capability result 属于运行控制状态；
- 文件 coverage 属于读取控制状态；
- 当前 guidance 只有需要模型知道时才进入本轮请求。

这些状态没有必要全部转成聊天文字塞给模型。

# 第二部分：上下文太大以后怎样压缩

## 4. 第三步：所有消息先经过统一 token 压力检查

### 4.1 先看它怎么做

Main 和 Domain Agent 最终都复用同一套 `compact_messages` 策略。

输入包括：

- 当前准备发送的 messages；
- 当前 tool schemas；
- 当前 Agent 的 context window 大小。

系统先计算整份请求占用，再得到：

> `context pressure = 当前请求 token / context window token`

当前实现有三档阈值：

| 阶段 | 触发压力 | 主要动作 |
|---|---:|---|
| Light | 50% | 优先缩短较大的旧 tool result |
| Summary | 70% | 把较早的完整消息 span 收成 checkpoint |
| Emergency | 90% | 只保留必须保留的消息，再尽量从最近历史向前补 |

执行顺序固定为：

```mermaid
flowchart LR
    I[原始请求] --> L{>= 50%?}
    L -->|是| LC[Light]
    L -->|否| O[直接使用]
    LC --> S{压缩后仍 >= 70%?}
    S -->|是| SC[Summary]
    S -->|否| O
    SC --> E{压缩后仍 >= 90%?}
    E -->|是| EC[Emergency]
    E -->|否| O
    EC --> O
```

每一级都会重新计算 token。前一级已经把压力降下来时，后面的更激进策略不会继续执行。

## 5. Light：先处理体积大的旧 tool result

### 5.1 先看它怎么做

Light 阶段只扫描 `role=tool` 的消息。

当某条 tool result 的估算体积达到 256 token 以上时，系统会把正文替换成一个短说明，其中保留：

- 这是一条已经执行过的工具结果；
- 原结果大概有多大；
- 对应的 `tool_call_id` 仍然存在。

整个过程会从较早消息向后扫描。一旦总体压力已经降到 50% 以下，就停止继续替换。

```mermaid
flowchart TD
    T[扫描旧 tool result] --> S{结果是否较大}
    S -->|否| N[继续下一条]
    S -->|是| R[替换为短说明]
    R --> P[重新计算请求压力]
    P -->|低于 50%| X[结束 Light]
    P -->|仍较高| N
```

这里保留的是 tool message 的协议外壳。`tool_call_id` 不会因为正文被压缩而消失。

## 6. Summary：按原子 span 把早期历史收成 checkpoint

### 6.1 先理解 atomic span 是什么

Summary 阶段不能随便在任意消息位置切一刀。GitAgent 先把历史拆成 **atomic span**。

普通消息自己就是一个 span。例如一条独立的 user message，可以作为一个完整历史块参与后续边界计算。

带工具调用的 assistant message，会和紧跟其后的匹配 tool result 组成一个 span：

```mermaid
flowchart LR
    A[assistant: tool calls c1,c2] --> T1[tool result c1]
    T1 --> T2[tool result c2]
```

对于 Agent delegation，一组 `agent__...` 调用完成以后，如果后面紧跟一条不再发工具调用的 assistant 消息，这条总结消息也会被纳入同一个 span。

可以把 span 理解成：**模型协议上适合作为一个整体保留或整体折叠的最小历史块。**

### 6.2 Summary 具体怎样选择边界

系统保留当前 system message，然后尝试从最早的完整 span 开始向 checkpoint 中收拢。

```mermaid
flowchart LR
    S[当前 system] --> O
    H1[早期 span 1] --> C[Checkpoint]
    H2[早期 span 2] --> C
    H3[近期 span 3] --> O[最终请求]
    H4[近期 span 4] --> O
    C --> O
```

每尝试一个新的边界，系统都会构造候选请求并重新计算 token。

checkpoint 内容由固定规则从被折叠消息中提取：

- 普通 user / assistant 文本保留为有长度上限的摘要记录；
- assistant tool call 保留工具名和有长度上限的参数描述；
- tool result 保留有长度上限的结果描述；
- 如果早期已经存在 checkpoint，会把它的内容继续纳入新的 checkpoint。

这里的“deterministic”主要体现在：**边界选择和 checkpoint 拼接遵循固定程序，不交给模型临场自由判断。**

## 7. Emergency：先守住最小必要上下文，再尽量保留最近历史

### 7.1 先看它怎么做

如果 Summary 之后压力仍达到 90%，系统进入 Emergency。

这一步的策略发生明显变化。系统先计算哪些消息必须留下：

1. 当前最近一条 user message；
2. 所有仍然存在未闭合 tool call 的 span。

这些消息组成最低保留集合。

随后系统从**最近的 span**开始向前尝试补回历史，只要加入以后仍能放进 context window，就继续保留。

```mermaid
flowchart TD
    R[最近 user + open tool spans] --> B[最低保留集合]
    H[其余历史 spans] --> P[从最近向前尝试加入]
    P --> F{加入后还能放入窗口吗}
    F -->|能| K[保留该 span]
    F -->|不能| O[留在 omitted 历史]
    B --> Q[候选请求]
    K --> Q
    O --> C[尝试生成 checkpoint]
    C --> Q
```

如果被省略的历史还能压成 checkpoint 并放进窗口，系统就加上 checkpoint；如果 checkpoint 本身也放不下，就只保留选中的必要消息和近期 span。

如果连“当前 user + 活跃工具协议”这一最低集合都已经超过窗口，系统直接抛出 `ContextWindowExceeded`，停止继续构造无效请求。

## 8. tool call/result 协议是压缩过程里的硬边界

### 8.1 先看协议检查怎样工作

在压缩前和压缩后，GitAgent 都会校验工具消息关系。

一个合法片段通常长这样：

```mermaid
sequenceDiagram
    participant M as Model
    participant H as Harness
    participant T as Tool

    M->>H: assistant tool_call c1
    H->>T: execute c1
    T-->>H: tool_result c1
    H-->>M: result c1
```

检查规则主要包括：

- tool result 必须找到对应的 assistant tool call；
- 一组 tool calls 尚未全部收到结果时，不能插入无关普通消息打断；
- 前一组调用仍未闭合时，不能开始下一组 assistant tool calls；
- Emergency 必须把 open tool span 保留在最低集合中。

### 8.2 再看为什么这一层不能只按消息条数截断

假设历史顺序是“assistant 发出 `c1` → tool 返回 `c1` → assistant 给出下一步结论”。

如果按“只保留最近 N 条”截断，很容易只剩 tool result，或者只剩 assistant call。模型接口看到的调用链会失去对应关系。

# 第三部分：压缩结果怎样跨重启继续生效

## 9. Compaction 保存的是“怎样得到新视图”的持久化增量

### 9.1 先看它怎么做

一次 compaction 发生后，GitAgent 会形成 `MessageCompactionPlan`。计划里可能包含三类信息：

| 字段 | 表达的含义 |
|---|---|
| `tool_replacements` | 哪些 tool result 被 Light 替换成短说明 |
| `checkpoint` | 被折叠历史形成的 checkpoint 内容 |
| `retain_message_indexes` | 原历史里哪些消息继续完整保留 |

随后 Application / SessionManager 把这个计划写成 `compaction_checkpoint` 事件。

```mermaid
flowchart LR
    M[当前完整消息视图] --> C[compact_messages]
    C --> P[MessageCompactionPlan]
    P --> E[compaction_checkpoint event]
    E --> L[Event History]
```

以后恢复消息时，Projector 读到这条事件，就在当时已有的投影视图上应用同一份增量：

```mermaid
flowchart LR
    H[历史 model messages] --> P[Projector]
    E[compaction_checkpoint] --> P
    P --> R[替换 tool result]
    R --> K[按索引保留消息]
    K --> C[加入 checkpoint]
    C --> V[恢复后的模型视图]
```

这意味着 compaction 本身也属于 Session 历史演化的一部分。

### 9.2 Main 与 Domain Agent 都会持久化压缩

Main 的 `ContextBuilder` 在构造请求时发现 compaction 后，会直接记录 compaction event。

Domain Agent 通过 `AgentContext.model_messages()` 触发同一套压缩，再由 Application 提供的 compaction sink 把计划写入事件历史，并记录压缩前后的 token 使用量。

两条路径最终都使用同一种 compaction event 语义。

# 第四部分：文件读取状态是怎样工作的

## 10. 先区分两个读取机制：通用 Read Cache 与 FileReadLedger

`AgentContext` 里同时存在两个容易混淆的结构。

| 机制 | 适用范围 | 保存内容 | 命中后的行为 |
|---|---|---|---|
| `read_cache` | 一般 READ capability；排除 repository file read 与 memory read | capability 的完整原始结果 | 相同 capability + 参数可以直接复用结果 |
| `FileReadLedger` | `repository.read_file` / `repository.read_files` | 读取 coverage、EOF、repository/path/ref 身份 | 已覆盖范围不再访问 Provider；返回 `already_read + coverage` |

这两个机制的目标都包含“减少重复读取”，实现方式差异很大。

尤其要记住：**FileReadLedger 保存读取覆盖事实，不保存文件正文。**

---

## 11. FileReadLedger 的完整流程：Prepare → Execute → Complete

### 11.1 先看它怎么做

一次仓库文件读取进入 Harness 后，会先经过 `FileReadLedger.prepare()`。

```mermaid
flowchart TD
    Q[Agent 请求读取文件] --> P[Prepare]
    P --> I[确定 repository / path / ref]
    I --> C[查询已有 coverage]
    C --> G{请求范围是否已覆盖}
    G -->|全部覆盖| A[actual_arguments = None]
    G -->|部分或未覆盖| D[只生成第一个未覆盖缺口]
    D --> X[执行 repository read capability]
    X --> V[校验返回结果]
    V --> U[Complete: 更新 coverage / EOF]
    A --> O[返回 already_read + coverage]
    U --> O2[返回本次新读取结果]
```

这个过程分三层理解最清楚。

### Prepare：先算“真正还需要读什么”

每个文件读取请求先规范成：

- `path`；
- `start_line`；
- `limit`；
- 用户是否显式传了 `start_line`。

Ledger 用 `(repository, path, ref)` 作为一个文件读取状态的身份键。

随后根据已有 coverage 计算缺口。

### Execute：只把缺口交给底层 Provider

如果整段请求已经覆盖，`actual_arguments` 会变成空，底层 capability 不再执行。

如果只覆盖了一部分，Harness 会把底层请求改成当前范围内的第一个未覆盖区间。

### Complete：先校验结果，再记账

底层返回以后，Ledger 不会直接相信结果。它会检查：

- 返回对象数量是否和实际请求数量一致；
- 返回 path 是否和请求 path 一致；
- `start_line / end_line / content / truncated` 结构是否合法。

校验通过后，才把新的范围加入 coverage，并根据 `truncated` 更新 EOF。

## 12. Coverage 怎样表示“这个文件到底读到哪里了”

### 12.1 先看区间合并

`FileCoverage` 保存多个已覆盖行区间，读取时会把相邻或重叠范围合并。

例如先后读取：

- 1–100；
- 80–200；
- 250–300。

最终 coverage 会合并成 `[1, 200]` 和 `[250, 300]` 两段，也就是：

| 已读范围 | 状态 |
|---|---|
| 1–200 | 已覆盖 |
| 201–249 | 缺口 |
| 250–300 | 已覆盖 |

如果接下来请求 150–260，Ledger 会发现前半段已经覆盖，当前第一个缺口从 201 开始，因此只需要继续获取缺失部分。

### 12.2 显式 start_line 与省略 start_line 的行为不同

这是当前实现里很容易漏掉的一点。

#### 情况 A：用户显式指定 start_line

例如请求 150–260。Ledger 只在这个指定范围里寻找第一个缺口。

#### 情况 B：用户省略 start_line

这类请求通常表示“继续给我下一段”。Ledger 会从第 1 行开始寻找第一个尚未覆盖的位置，再读取一段新的内容。

因此，多次调用同一个“默认从头读”的文件读取能力时，Ledger 会逐步把读取窗口向后推进，而不会反复从第 1 行开始。

## 13. EOF 怎样让 Ledger 知道文件已经结束

### 13.1 先看它怎么做

Provider 返回文件片段时会同时告诉 Harness 结果是否被截断。

当 `truncated=false`，Ledger 会把本次 `end_line` 记录为 `eof_line`。

例如请求 1–500，文件实际只有 230 行。Provider 返回到第 230 行并标记 `truncated=false`，Ledger 随即记录 `eof_line=230`。

以后再请求 200–1000 时，Ledger 会把可计算范围限制到 EOF，230 之后不会再制造新的读取请求。

## 14. 批量读取怎样处理“一部分已经读过”的情况

### 14.1 先看它怎么做

`repository.read_files` 可以一次提交多个文件请求。Prepare 会逐个计算 coverage。

假设一次批量请求包含：

| 请求 | 当前状态 | 实际动作 |
|---|---|---|
| `a.py` 1–200 | 已覆盖 | 不访问 Provider |
| `b.py` 1–200 | 未读取 | 真实读取 |
| `c.py` 100–300 | 100–180 已覆盖 | 只请求当前第一个缺口 |

最终传给 Provider 的批量参数只包含仍需真实读取的项。

Complete 阶段再把新结果和“已经覆盖”的项分别整理回当前调用的结果视图。

对完全覆盖的项，返回的是类似 `already_read + coverage` 的状态说明。Ledger 不会从内部拼出旧文件正文，因为它根本没有保存正文副本。

# 第五部分：读取状态什么时候失效，什么时候恢复

## 15. 当前实现采用“写成功后整体清空”的保守失效策略

### 15.1 先看它怎么做

只要一个 capability 属于 `WRITE` 或 `DESTRUCTIVE`，并且执行结果为 success，`AgentContext` 就会清空：

- 通用 `read_cache`；
- `FileReadLedger`。

这一步发生在 capability commit 流程中。

```mermaid
flowchart LR
    R[已有 read state] --> W[WRITE / DESTRUCTIVE capability]
    W --> S{执行成功}
    S -->|是| C[清空 read_cache + FileReadLedger]
    C --> N[下一次读取重新建立事实]
```

这里采用的是**整体清空**。当前实现没有只按单个 path 精细删除 coverage。

另外，`native.bash` 可能绕过结构化写工具直接改变 worktree。Harness 会比较执行前后的 worktree state；一旦发现变化，同样会：

- 记录 workspace mutation；
- 清空 `read_cache`；
- 清空 `FileReadLedger`。

### 15.2 要和 workspace revision 分开理解

读取状态失效与 workspace revision 都和修改有关，但触发语义不完全相同。

结构化 native write/destructive capability 成功后，读取状态会直接清空。

workspace revision 的推进还会进一步查看这次原始结果是否表明 `changed=true`，用于标记代码工作区真的产生了新的 revision。

因此复习时可以这样区分：

> **读取状态关心“旧读结果还敢不敢继续复用”，workspace revision 关心“工作区事实是否形成了新的代码版本”。**

## 16. repository、path、ref 共同限定一份 FileCoverage

### 16.1 先看它怎么做

FileReadLedger 内部使用三元组作为读取身份：

> `(repository, path, ref)`

因此：

- 同一个 path 在不同 repository 下拥有不同 coverage；
- 同一个 path 在不同 `ref` 下拥有不同 coverage；
- ref 相同且 repository/path 相同时，才能共享这一份行覆盖状态。

这对远端仓库读取尤其重要。例如同一个 `config.py`，Main branch 和某个 PR head 对应的 ref 不同，Ledger 会维护两份独立 coverage。

## 17. Pause Snapshot 会保存读取状态，恢复后继续使用

### 17.1 先看它怎么做

当 Agent 因等待用户、审批或其他控制点而保存 Context 时，Application 会把两类读取状态一起序列化进 pause context：

- `read_cache`；
- `file_reads`。

恢复 Context 时：

- `read_cache` 直接还原成字典；
- `file_reads` 通过 `FileReadLedger.from_plain()` 重建 coverage 和 EOF。

```mermaid
flowchart LR
    A[运行中的 AgentContext] --> S[Pause Snapshot]
    A -->|serialize| R1[read_cache]
    A -->|serialize| R2[file coverage + EOF]
    R1 --> S
    R2 --> S
    S --> X[恢复 AgentContext]
    X --> C[继续原来的读取判断]
```

因此，一个 Agent 在暂停前已经读过 `a.py` 1–200，恢复后仍然知道这段范围已经覆盖。

# 第六部分：为什么上下文治理和读取状态要分开维护

前面已经分别走完模型消息和文件读取的实现，现在再看为什么这两套状态不能合并成一个“大缓存”。

模型历史记录的是“过去模型已经看到过什么”。即使某段旧文件内容后来过时，它仍然是当时真实发生过的 tool result，不能因为文件被修改就从历史中抹掉。Context Governance 处理的是如何在保持 tool call/result 协议的前提下压缩这些历史，并把压缩决定持久化成可重放的 compaction event。

FileReadLedger 处理的是另一个问题：**下一次还要不要真的访问 Provider。** 它只记录 repository、path、ref 对应的 coverage 和 EOF，不保存正文。文件内容一旦可能被 WRITE / DESTRUCTIVE 或 Bash 改变，旧 coverage 就不再能证明当前文件已经被观察过，因此读取状态需要失效；但历史消息仍然保留为过去的事实。

这也解释了为什么 read cache、FileReadLedger 和 workspace revision 不能混为一个概念。read cache 复用一般 READ 的完整结果；FileReadLedger 只管理仓库文件的行覆盖；workspace revision 则表示当前候选是否真的形成了新的代码版本。成功写操作会保守地清理读取状态，而 revision 还会根据真实变化进一步判断是否推进。

等待恢复时，两套状态也从不同来源回来：消息和 compaction 从 Event History 重放，read cache 与 FileReadLedger 随暂停控制树恢复。这样恢复后的 Agent 既知道模型之前看过什么，也知道哪些文件范围在当前可恢复状态下仍然可以视为已读取。

理解本章时要特别注意几个边界：Light 主要缩短大型 tool result，而不是删除整个调用；Summary 只能在 atomic span 边界折叠；Emergency 必须优先保留最近用户消息和 open tool span；FileReadLedger 不是正文缓存，完全命中 coverage 时返回的是“已覆盖”状态而不是拼回旧正文；Bash 只要改变 worktree，同样会让读取状态失效。

---

## 18. 代码定位：复习时应该去哪里核对

| 想核对的问题 | 主要位置 |
|---|---|
| canonical message 格式与 tool call 结构 | `gitagent/harness/context/messages.py` |
| 50% / 70% / 90% 压力阈值 | `gitagent/harness/context/budget.py` |
| Main 请求构造与统一 compaction | `gitagent/harness/context/builder.py` |
| atomic span、Light、Summary、Emergency 具体策略 | `gitagent/harness/context/builder.py` |
| Main / Domain 消息投影与 compaction 重放 | `gitagent/harness/context/projector.py` |
| Domain Agent 临时 guidance、消息线程、read cache | `gitagent/harness/context/state.py` |
| coverage、gap、EOF、批量文件读取 | `gitagent/harness/file_reads.py` |
| compaction event 的持久化格式 | `gitagent/infra/persistence/sessions.py` |
| Pause Snapshot 中读取状态的序列化与恢复 | `gitagent/application/service.py` |

下一章进入跨会话知识层：[GitAgent 怎样从已完成交互中提取长期记忆，并处理作用域、冲突、过期和遗忘](11-long-term-memory.md)。
