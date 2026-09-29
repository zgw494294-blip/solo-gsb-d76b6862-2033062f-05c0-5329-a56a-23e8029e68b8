# 共享事件故障概率 API

基于 **FastAPI + ROBDD（Reduced Ordered Binary Decision Diagram，约简有序二叉决策图）** 的故障树分析服务。
事件相互独立、同一事件 id 的多次引用共享同一状态；以事件 id 字典序为变量顺序构建 ROBDD，
使用 Python `fractions.Fraction` 做**精确有理数运算**，返回顶事件发生概率与每个事件的
Birnbaum 重要度（`P(top | 强制该事件=1) − P(top | 强制该事件=0)`），结果均为**约分后的分数字符串**。
另提供最小割集接口 `POST /v1/cut-sets`：输入格式相同（仅接受 AND/OR 门），返回顶事件的全部最小割集。
还提供修复方案接口 `POST /v1/repair-plan`：给定各事件当前状态与修复费用，求令顶事件变为 `false`
的最省成本修复方案，`NOT` 门照常求值。
以及联合观测诊断接口 `POST /v1/diagnose`：给定同一时刻对若干事件或门（1～16 个，`AND`/`OR`/`NOT`
均可）的布尔读数，返回全部读数同时成立的证据概率 `P(E)` 与各事件在该证据下的后验概率
`P(事件=1 | E) = P(事件=1 且 E)/P(E)`。
还提供下一步观测推荐接口 `POST /v1/inspect-next`：在已有读数 `E`（0～16 个，可为空）下，
对 1～16 个互异、已声明且未被观测的候选事件/门，分别评估“再观测它一次”对顶事件不确定性的
期望削减（收益），返回各候选的分支概率、顶事件后验与收益，并给出推荐观测对象。
以及最可能成因接口 `POST /v1/likely-cause`：令 `C` 为“顶事件发生且全部读数（0～16 个，可为空）
成立”，在满足 `C` 的完整事件赋值中按独立先验乘积选出最可能的一组成因，返回该赋值、
`condition_probability=P(C)` 与 `assignment_posterior=P(A)/P(C)`。
还提供改版对比接口 `POST /v1/compare-trees`：两版故障树共用一份 `events`，`baseline` 与
`revised` 各提供 `gates` 与 `top`，返回两版顶事件 `B`、`R` 的差异概率
`added_probability=P(¬B∧R)`、`removed_probability=P(B∧¬R)`、`change_probability=P(B⊕R)`
与功能等价标记 `equivalent`。
以及失效数分布接口 `POST /v1/failure-count`：请求格式与 `/v1/analyze` 完全相同，令 `T` 为
顶事件发生、`K` 为请求中**全部已声明事件**里取 `true` 的个数（包括未被顶层引用的事件；同一 id
的重复引用只计一次），返回 `top_probability=P(T)` 与条件计数分布 `P(K=k|T)`（`k=0` 到事件总数）。
还提供首次故障时间接口 `POST /v1/first-failure-time`：`events` 限 1～8 个，`gates`、`top` 校验
同前，另收 `steps`（1～10）与覆盖全部事件 id 的 `transitions`（每项含 `fail`、`recover` 概率）。
`t=0` 各事件按自身 `p` 独立生成状态，此后每步正常事件按 `fail` 故障、故障事件按 `recover` 恢复，
给定上一步状态后各事件独立转移；返回顶事件**首次**在第 `t` 步（`t=0`～`steps`）发生的概率
`first_hit_distribution` 与全程未发生的 `no_hit_probability`。
还提供观测冲突定位接口 `POST /v1/observation-conflicts`：给定 1～16 个对事件或门的布尔读数，
返回全部**最小零概率观测组**——组内读数同时成立的先验概率为 0、但删去组内任一项后概率大于 0；
全部读数可同时成立时返回空组列。
还提供模板实例化展开接口 `POST /v1/expand-model`：给定 1～8 个可复用模板（各含参数、门、
子实例与 `top`）与一个根实例（根参数绑定到事件），递归展开全部子实例，返回门名为
`root/实例路径/门名` 的扁平故障树，可直接提交给 `/v1/analyze`。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

服务监听 **8000** 端口：

- API 地址：`http://localhost:8000`
- 分析接口：`http://localhost:8000/v1/analyze`
- 割集接口：`http://localhost:8000/v1/cut-sets`
- 修复方案接口：`http://localhost:8000/v1/repair-plan`
- 联合观测诊断接口：`http://localhost:8000/v1/diagnose`
- 下一步观测推荐接口：`http://localhost:8000/v1/inspect-next`
- 最可能成因接口：`http://localhost:8000/v1/likely-cause`
- 改版对比接口：`http://localhost:8000/v1/compare-trees`
- 失效数分布接口：`http://localhost:8000/v1/failure-count`
- 首次故障时间接口：`http://localhost:8000/v1/first-failure-time`
- 观测冲突定位接口：`http://localhost:8000/v1/observation-conflicts`
- 模板实例化展开接口：`http://localhost:8000/v1/expand-model`
- 健康检查：`http://localhost:8000/health`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`

依赖（fastapi / uvicorn / pydantic）在镜像构建时自动安装。

## 请求示例

```bash
curl -X POST http://localhost:8000/v1/analyze \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top"
  }'
```

响应：

```json
{
  "top_probability": "34651/1000000",
  "importances": {
    "coolant_loss": "19701/20000",
    "motor_overheat": "9751/10000",
    "pump_fail": "4851/5000"
  }
}
```

- `top_probability`：顶层节点发生概率，约分分数 `n/d`（整数时分母为 `1`，如 `"1/1"`）。
- `importances`：每个事件的重要度，可为负数，同样为约分分数。

## 割集请求示例（POST /v1/cut-sets）

请求体格式与 `/v1/analyze` 完全相同（`events` / `gates` / `top`，校验规则一致，
`p` 仍须合法但不参与割集计算），但**只接受 `AND` / `OR` 门**——出现 `NOT` 门
返回 422 并说明原因：

```bash
curl -X POST http://localhost:8000/v1/cut-sets \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top"
  }'
```

响应：

```json
{
  "cut_sets": [
    ["coolant_loss"],
    ["motor_overheat"],
    ["pump_fail"]
  ]
}
```

- `cut_sets`：顶事件的全部**最小割集**。组内事件同时发生时，无论其他事件状态如何，
  顶事件都发生；删去组内任一事件则不再保证。
- 共享事件在组内只计一次；包含关系会被吸收，例如 `A OR (A AND B)` 仅返回 `[["a"]]`。
- 排序：组内按事件 id 字典序，组间先按割集长度、再按事件 id 字典序。
- 任一门去重、吸收后仍超过 **5000** 组时返回 422 并说明原因。

含 `NOT` 门的请求示例响应：

```json
{ "detail": "门 'g' 为 NOT：最小割集仅支持 AND/OR 单调结构，含取反的概率分析请使用 /v1/analyze" }
```

## 修复方案请求示例（POST /v1/repair-plan）

在 `events` / `gates` / `top` 之外，另收两个覆盖**全部事件 id**的字段：

- `states`：每个事件的当前状态，布尔值（`true` = 故障发生）；
- `repair_costs`：每个事件的修复费用，取值 `1`～`10^6` 的整数。

只允许把当前为 `true` 的事件修复为 `false`，当前为 `false` 的事件保持原状；
同一事件被多处共享引用时只需修复一次（费用也只计一次），`NOT` 门照常求值。

```bash
curl -X POST http://localhost:8000/v1/repair-plan \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top",
    "states": {
      "motor_overheat": false,
      "coolant_loss": true,
      "pump_fail": false
    },
    "repair_costs": {
      "motor_overheat": 5000,
      "coolant_loss": 800,
      "pump_fail": 1200
    }
  }'
```

顶事件 `motor_overheat OR coolant_loss OR pump_fail` 当前为 `true`，
修复 `coolant_loss` 后即为 `false`：

```json
{
  "possible": true,
  "repair_events": ["coolant_loss"],
  "total_cost": 800
}
```

- 方案选择依次按 **总修复成本最小 → 修复事件数最少 → 按事件 id 字典序排序后的事件序列字典序最小**取唯一最优。
- `repair_events`：需要修复的事件 id，按字典序排序输出。
- 顶事件在当前状态下**原本就为 `false`** 时，返回空方案与零成本：
  `{"possible": true, "repair_events": [], "total_cost": 0}`。
- 在“当前为 `false` 的事件不能改动”的约束下不存在可行修复时，标记不可行：
  `{"possible": false, "repair_events": [], "total_cost": null}`。

含 `NOT` 门的不可行示例：顶事件为 `a OR NOT b`，当前 `a=true、b=false`，
修复 `a` 后仍有 `NOT b = true`，而 `b` 无法从 `false` 改回 `true`：

```json
{ "possible": false, "repair_events": [], "total_cost": null }
```

`states` 或 `repair_costs` 缺项、出现未知事件键、费用不在 `1`～`10^6` 时返回 422，
多项问题会在同一条 `detail` 中一并说明，例如两个字段都漏掉事件 `b` 且多出未知键 `x`：

```json
{ "detail": "states 缺少事件的对应项：'b'；repair_costs 缺少事件的对应项：'b'；repair_costs 出现了不属于任何事件的键：'x'" }
```

## 联合观测诊断示例（POST /v1/diagnose）

在 `events` / `gates` / `top` 之外，另收 `observations`：**1～16 个**已声明**事件或门 id**
到**严格布尔值**的映射，表示同一时刻读到的一组读数。令 E 为“全部读数同时成立”：

- `evidence_probability`：证据概率 `P(E)`，约分分数；
- `posteriors`：按事件 id 给出后验概率 `P(事件=1 | E) = P(事件=1 且 E) / P(E)`，约分分数。

不同事件仍按各自的 `p` 相互独立；同一事件的多处引用共享同一状态；门读数（`AND`/`OR`/`NOT`
均可观测）只是施加在结构上的条件证据，**不会**被当成相互独立的事件，也**不会**修改任何
事件的先验。计算在同一 ROBDD 上把各读数约束合取后做精确加权，不枚举全部事件状态。

仍以 `top = motor_overheat OR (coolant_loss OR pump_fail)` 为例。联合观测
“电机未过热，但顶事件确实发生”：

```bash
curl -X POST http://localhost:8000/v1/diagnose \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top",
    "observations": {
      "motor_overheat": false,
      "top": true
    }
  }'
```

既然电机未过热而顶事件发生，故障必在冷却侧——门读数通过共享结构传导到事件后验：

```json
{
  "evidence_probability": "24651/1000000",
  "posteriors": {
    "coolant_loss": "200/249",
    "motor_overheat": "0/1",
    "pump_fail": "50/249"
  }
}
```

- `evidence_probability = P(motor_overheat=0 且 top=1) = 99/100 · (2/100 + 1/200 − 2/100·1/200) = 24651/1000000`。
- 被直接读数约束的 `motor_overheat` 后验为 `0/1`；`coolant_loss`、`pump_fail` 的后验按
  `P(事件=1 且 E)/P(E)` 精确给出（冷却侧失效条件下的条件概率，分母同为 249）。
- 也可以同时观测门与其输入，例如 `{"cooling_fail": true, "pump_fail": false}` 会推出
  `coolant_loss` 后验为 `1/1`——门读数与事件读数是同一条结构约束，不是两个独立随机事件。
- 与证据无关的事件后验保持先验；观测值必须是 JSON 布尔 `true`/`false`（`1`、`0`、
  `"true"` 等一律拒绝）。
- **`P(E) = 0` 时返回 422** 并说明原因，区分两种情形：
  - 读数在结构上相互矛盾（如同时观测 `a=true` 与 `NOT a=true`，约束合取恒为假）；
  - 结构可满足但按先验没有任何正概率状态支持（如把 `p=0/1` 的事件读成 `true`，
    或把 `p=1/1` 的事件读成 `false`）。

结构矛盾的响应示例：

```json
{ "detail": "观测相互矛盾：给定的事件/门读数不可能在同一时刻同时成立（结构约束的合取恒为假），evidence_probability=0" }
```

## 下一步观测推荐示例（POST /v1/inspect-next）

在 `events` / `gates` / `top` 之外另收两个字段：

- `observations`：已有读数，**0～16** 个（可为空或省略），格式与校验同 `/v1/diagnose`；
- `candidates`：**1～16** 个**互异、已声明且未被观测**的事件或门 id，即考虑下一步观测的对象。

令 `E` 为已有读数的合取。对每个候选 `X`，记分支后验 `q = P(top | E 且 X=true/false)`
（零概率分支的 `q` 为 `null`），用 `G(q) = 2q(1−q)` 衡量顶事件的不确定性，则观测 `X` 的
**收益** 为：

```
gain(X) = G(P(top|E)) − [ P(X=true|E)·G(q_true) + P(X=false|E)·G(q_false) ]
```

即观测前的不确定性减去两分支按概率加权的期望不确定性（信息增益恒 `≥ 0`）。
响应按候选 id 升序给出各分支概率、`q` 与收益（均为约分分数），并按
**收益最大、id 最小**给出 `recommended_id`。

仍以电机冷却系统为例：已观测“电机未过热”（`top` 后验为 `249/10000`），
评估下一步观测 `coolant_loss`、`cooling_fail`、`pump_fail` 中哪个最有价值：

```bash
curl -X POST http://localhost:8000/v1/inspect-next \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top",
    "observations": {"motor_overheat": false},
    "candidates": ["pump_fail", "cooling_fail", "coolant_loss"]
  }'
```

响应：

```json
{
  "evidence_probability": "99/100",
  "top_posterior": "249/10000",
  "candidates": [
    {
      "id": "coolant_loss",
      "branches": {
        "true":  {"probability": "1/50",  "q": "1/1"},
        "false": {"probability": "49/50", "q": "1/200"}
      },
      "gain": "1940449/50000000"
    },
    {
      "id": "cooling_fail",
      "branches": {
        "true":  {"probability": "249/10000",  "q": "1/1"},
        "false": {"probability": "9751/10000", "q": "0/1"}
      },
      "gain": "2427999/50000000"
    },
    {
      "id": "pump_fail",
      "branches": {
        "true":  {"probability": "1/200",  "q": "1/1"},
        "false": {"probability": "199/200", "q": "1/50"}
      },
      "gain": "477799/50000000"
    }
  ],
  "recommended_id": "cooling_fail"
}
```

- 电机未过热时 `top` 等价于 `cooling_fail`，直接观测该门可完全确定顶事件
  （两分支 `q` 分别为 `1/1` 与 `0/1`，`G` 均为 0），收益最大，故被推荐。
- 候选在 `E` 下取值已被确定（某分支概率为 `0/1`）时，零概率分支的 `q` 为 `null`，
  该候选收益为 `0/1`；例如把 `p=0/1` 的事件列为候选，其 `true` 分支即
  `{"probability": "0/1", "q": null}`。
- 计算复用同一 ROBDD：先合取观测约束得到 `E`，再对每个候选构建 `E∧X` 与
  `E∧X∧top` 的合取并精确加权（假分支由减法精确得到），不枚举事件状态。
- `P(E) = 0`（观测矛盾或先验下无正概率支持）、候选重复/未声明/已被观测、
  或累计非终端节点超过 **50000** 时返回 422 并说明原因。

## 最可能成因示例（POST /v1/likely-cause）

在 `events` / `gates` / `top` 之外另收 `observations`：**0～16** 个（可为空或省略）
已声明事件或门 id 到严格布尔值的映射，格式与校验同 `/v1/diagnose`。令
`C` 为“顶事件发生且全部读数成立”，本接口在满足 `C` 的**完整事件真假赋值** `A` 中，
按各事件独立先验乘积选出 `P(A)` 最大者：

- `assignment`：最优赋值，按事件 id 字典序排列；并列时按事件 id 字典序比较真假序列，
  `false` 优先；
- `condition_probability`：`P(C)`，约分分数；
- `assignment_posterior`：`P(A)/P(C)`，即该最优赋值在条件 `C` 下的后验占比，约分分数。

共享引用共用同一状态、`NOT` 门照常求值；未被结构约束的事件也参与赋值与概率计算
（各自取先验较大的一侧，`p=1/2` 时取 `false`）。最优赋值在同一 ROBDD 上用动态规划
精确求得，不枚举全部事件状态。

仍以电机冷却系统为例，联合观测“电机未过热，但顶事件确实发生”，求最可能的事件组合：

```bash
curl -X POST http://localhost:8000/v1/likely-cause \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top",
    "observations": {
      "motor_overheat": false,
      "top": true
    }
  }'
```

响应：

```json
{
  "assignment": {
    "coolant_loss": true,
    "motor_overheat": false,
    "pump_fail": false
  },
  "condition_probability": "24651/1000000",
  "assignment_posterior": "199/249"
}
```

- 电机未过热而顶事件发生，最可能的成因是**仅冷却液流失**：
  `P(A) = 99/100 · 2/100 · 199/200 = 19701/1000000`，优于“仅泵失效”
  （`4851/1000000`）等其它满足 `C` 的赋值。
- `condition_probability = P(C) = 99/100 · 249/10000 = 24651/1000000`；
  `assignment_posterior = 19701/24651 = 199/249`。
- 省略 `observations` 或传空对象时 `C` 就是“顶事件发生”，上例将同样返回
  `coolant_loss` 单独发生（`condition_probability = 34651/1000000`，
  `assignment_posterior = 19701/34651`）。
- **`P(C) = 0` 时返回 422** 并说明原因，区分两种情形（同 `/v1/diagnose`）：
  顶事件与读数在结构上相互矛盾（合取恒为假），或结构可满足但先验下无正概率支持。
- 观测键/值非法或累计非终端节点超过 **50000** 时同样返回 422。

结构矛盾的响应示例：

```json
{ "detail": "顶事件与观测相互矛盾：顶事件发生且全部读数同时成立在结构上不可能（结构约束的合取恒为假），condition_probability=0" }
```

## 改版对比示例（POST /v1/compare-trees）

请求体共用一份 `events`（格式与校验同 `/v1/analyze`），另收两个版本对象：

- `baseline` / `revised`：各含 `gates`（0～64 个，可省略）与 `top`，分别描述基线版与
  改版故障树；两版**各自沿用 `/v1/analyze` 的全部校验**（元数、未知引用、环等）。
- 两版的门 id **可同名但定义相互独立**：版本内的引用只解析到本版本的门，
  **不能跨版引用**另一版本独有的门 id（违反返回 422 并说明原因）。
- 同一事件在两版**共享状态与先验**：两版顶事件是同一组事件变量上的两个布尔函数。

设两版顶事件为 `B`、`R`，响应给出三个约分分数与一个等价标记：

- `added_probability = P(¬B∧R)`：基线不发生而改版发生（改版新增）的概率；
- `removed_probability = P(B∧¬R)`：基线发生而改版不发生（改版移除）的概率；
- `change_probability = P(B⊕R)`：两版顶事件不一致的概率（恒等于前两者之和）；
- `equivalent`：仅在**全部事件赋值**下 `B` 与 `R` 都相同时为 `true`——
  仅存在于零概率赋值上的差异（差异概率为 `0/1`）仍算**不等价**。

以电机冷却系统为例：改版给冷却侧加泵冗余，把 `cooling_fail` 从“任一失效”（`OR`）
改为“同时失效”（`AND`）。两版门 id 同名（`cooling_fail`、`top`），定义各自独立：

```bash
curl -X POST http://localhost:8000/v1/compare-trees \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "baseline": {
      "gates": [
        {"id": "cooling_fail", "op": "OR",
         "inputs": ["coolant_loss", "pump_fail"]},
        {"id": "top", "op": "OR",
         "inputs": ["motor_overheat", "cooling_fail"]}
      ],
      "top": "top"
    },
    "revised": {
      "gates": [
        {"id": "cooling_fail", "op": "AND",
         "inputs": ["coolant_loss", "pump_fail"]},
        {"id": "top", "op": "OR",
         "inputs": ["motor_overheat", "cooling_fail"]}
      ],
      "top": "top"
    }
  }'
```

响应：

```json
{
  "added_probability": "0/1",
  "removed_probability": "3069/125000",
  "change_probability": "3069/125000",
  "equivalent": false
}
```

- 改版后顶事件情形是基线的子集（`R ⊆ B`）：`added_probability` 为 `0/1`，
  `removed_probability = P(B) − P(R) = 34651/1000000 − 10099/1000000 = 3069/125000`。
- 计算用**同一变量顺序（事件 id 字典序）与共享 ROBDD** 精确完成，不枚举事件赋值：
  两版在同一唯一表上建图，差异运算 `¬B∧R`、`B∧¬R`、`B⊕R` 也在同一表上求值后精确加权。
- `equivalent` 由 ROBDD 规范化直接判定：两版顶事件根节点相同 ⟺ 全部赋值下取值相同。
  例如 `p=0/1` 的事件只影响零概率赋值时，三个差异概率都是 `0/1`，但 `equivalent`
  仍为 `false`。
- 跨版引用示例响应：`{"detail": "baseline 跨版引用了另一版本的门：'h'；两版门 id 可同名但定义相互独立，不能跨版引用"}`。
- 两版建图及差异运算**累计**非终端节点超过 **50000** 时返回 422 并说明原因。

## 失效数分布请求示例（POST /v1/failure-count）

请求体格式与 `/v1/analyze` **完全相同**（`events` / `gates` / `top`，校验规则一致），
`AND` / `OR` / `NOT` 门均照常求值。令：

- `T`：顶事件发生；
- `K`：请求中**全部已声明事件**里取 `true` 的事件个数——**包括未被顶层引用的事件**；
  同一事件 id 被门多处重复引用时**只计一次**（共享同一状态）。

响应给出：

- `top_probability`：`P(T)`，约分分数 `n/d`；
- `count_distribution`：长度为「事件总数 + 1」的数组，下标 `k` 对应条件概率
  `P(K=k|T)`，覆盖 `k=0` 到事件总数，每项均为约分分数。

仍以电机冷却系统为例，统计顶事件发生时有多少个事件真正失效：

```bash
curl -X POST http://localhost:8000/v1/failure-count \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top"
  }'
```

顶事件 `motor_overheat OR coolant_loss OR pump_fail` 只可能在至少一个事件失效时发生，
故 `P(K=0|T) = 0/1`；恰有一个、两个、三个事件失效的条件概率分别为：

```json
{
  "top_probability": "34651/1000000",
  "count_distribution": ["0/1", "34303/34651", "347/34651", "1/34651"]
}
```

- 例如 `P(K=1 且 T) = (2/100·99/100·199/200) + (1/100·98/100·199/200)
  + (1/200·98/100·99/100) = 34303/1000000`，除以 `P(T)=34651/1000000` 得
  `P(K=1|T) = 34303/34651`。
- **未被顶层引用的事件同样计入 `K`**：例如顶事件只引用了事件 `a`，但请求还声明了
  事件 `b`，则 `b` 是否发生按其先验独立计入总数；数组长度仍为「全部事件数 + 1」。
- 计数在同一 ROBDD 上以生成函数动态规划精确完成（每个节点维护取 1 变量个数的概率多项式），
  **不枚举事件状态**；ROBDD 约简跳过的变量与到达终端后剩余的变量都按各自先验补计。
- **`P(T) = 0` 时返回 422** 并说明原因，区分两种情形：
  - 顶事件在结构上不可能发生（如 `a AND NOT a`，BDD 恒为假）；
  - 结构可满足但按先验没有任何正概率状态支持（如顶事件要求 `p=0/1` 的事件发生）。
- 请求校验与 `/v1/analyze` 完全一致；ROBDD 构建累计非终端节点超过 **50000** 时同样返回 422。

结构上不可能的响应示例：

```json
{ "detail": "顶事件在结构上不可能发生：顶事件的 BDD 恒为假，top_probability=0，无法计算条件分布 P(K=k|T)" }
```

## 首次故障时间请求示例（POST /v1/first-failure-time）

在 `events` / `gates` / `top` 之外另收两个字段：

- `steps`：转移步数，**1～10** 的整数；
- `transitions`：**覆盖全部事件 id** 的映射，每项含两个 `n/d` 概率
  （校验规则与事件的 `p` 相同：`0≤n≤d≤10^6`）：
  - `fail`：每一步中，当前为正常（`false`）的事件发生故障（变 `true`）的概率；
  - `recover`：每一步中，当前为故障（`true`）的事件恢复正常（变 `false`）的概率。

时序模型：

- `t=0`：各事件**相互独立**地按自身先验 `p` 生成初始状态（`true` 的概率为 `p`）；
- 此后每一步，给定上一步的完整状态后，**各事件独立转移**——
  正常事件以 `fail` 故障、以 `1−fail` 保持正常；故障事件以 `recover` 恢复、以 `1−recover` 保持故障；
- 每一步都用当步的**共享事件状态**按 `AND` / `OR` / `NOT` 求一次顶事件。

响应给出：

- `first_hit_distribution`：长度为 `steps + 1` 的数组，**下标 `t` 对应顶事件首次在第 `t` 步发生的
  概率**（即顶事件在 `0,…,t−1` 步均未发生而在第 `t` 步发生），覆盖 `t=0` 到 `t=steps`；
- `no_hit_probability`：直到第 `steps` 步顶事件**始终未发生**的概率。
  两者之和恒为 `1/1`，各项均为约分分数 `n/d`。

下例中顶事件为 `a OR b`：两事件初始都确定正常（`p=0/1`），每步各以 `1/2` 独立故障、一旦故障
不再恢复（`recover=0/1`），共模拟 3 步：

```bash
curl -X POST http://localhost:8000/v1/first-failure-time \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "a", "p": "0/1"},
      {"id": "b", "p": "0/1"}
    ],
    "gates": [
      {"id": "top", "op": "OR", "inputs": ["a", "b"]}
    ],
    "top": "top",
    "steps": 3,
    "transitions": {
      "a": {"fail": "1/2", "recover": "0/1"},
      "b": {"fail": "1/2", "recover": "0/1"}
    }
  }'
```

响应：

```json
{
  "first_hit_distribution": ["0/1", "3/4", "3/16", "3/64"],
  "no_hit_probability": "1/64"
}
```

- `t=0` 两事件都确定正常，顶事件不可能发生：`0/1`。
- 第 1 步至少一个事件故障的概率为 `1 − 1/2·1/2 = 3/4`；能不触发地走到第 2 步的状态只有
  “两事件都正常”，其概率每步乘以 `1/4`，故首次发生在第 `t` 步（`t≥1`）的概率为
  `(1/4)^(t-1) · 3/4`：`3/4`、`3/16`、`3/64`。
- 3 步内始终只有“两事件都正常”一种未触发状态，其概率为 `(1/4)^3 = 1/64`，即
  `no_hit_probability`；校验：`3/4 + 3/16 + 3/64 + 1/64 = 1`。
- 动态规划**只保留未触发状态**的概率质量：每步顶事件发生的状态被当场吸收（计入当步
  `first_hit` 后移出活跃分布），其后续演化不再参与计算；事件数不超过 8，活跃状态至多
  `2^8 = 256` 个，全程整数公分母精确运算，输出时统一约分。
- 同一事件被门多处引用时共享同一份状态；未被顶层引用的事件也照常按自身 `fail` / `recover`
  演化（只是不影响顶事件取值）。
- `fail`、`recover` 允许取 `0/1` 与 `1/1`：例如 `fail=1/1`、`recover=0/1` 表示正常事件下一步
  必然故障且不可恢复。

`transitions` 缺项、出现未知事件键（多余项）、或某项的 `fail` / `recover` 不是合法 `n/d`
概率时返回 422，多项问题在同一条 `detail` 中一并说明：

```json
{ "detail": "transitions 缺少事件的对应项：'pump_fail'" }
```

## 观测冲突定位示例（POST /v1/observation-conflicts）

在 `events` / `gates` / `top` 之外，另收 `observations`：**1～16 个**已声明**事件或门 id**
到**严格布尔值**的映射（格式与校验同 `/v1/diagnose`），表示同一时刻读到的一组读数。
本接口找出读数之间全部**最小零概率观测组**：

- 组内读数**同时成立的先验概率为 0**——要么结构上不可能同时成立（如 `a=true` 与
  `NOT a=true`），要么结构可满足但每个满足赋值都把某个 `p=0/1` / `p=1/1` 的事件取在
  零概率一侧（如把 `p=0/1` 的事件读成 `true`）；
- **最小**：删去组内任一项后，剩余读数同时成立的概率大于 0。

全部读数可同时成立（联合概率大于 0）时返回空组列。门读数按共享事件状态求值，`NOT`
照常生效；同一事件的多处引用共享同一状态。响应的 `conflicts` 每组只列观测 id：
组内按 id 字典序排序，组间先按组长度、再按 id 序列字典序排序。

仍以电机冷却系统为例，增设冷却状态指示灯 `cooling_ok = NOT cooling_fail`：指示灯显示
冷却正常、电机也未过热，但泵实际已失效——指示灯读数与泵读数矛盾：

```bash
curl -X POST http://localhost:8000/v1/observation-conflicts \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "gates": [
      {"id": "cooling_fail", "op": "OR",
       "inputs": ["coolant_loss", "pump_fail"]},
      {"id": "cooling_ok", "op": "NOT", "inputs": ["cooling_fail"]},
      {"id": "top", "op": "OR",
       "inputs": ["motor_overheat", "cooling_fail"]}
    ],
    "top": "top",
    "observations": {
      "cooling_ok": true,
      "motor_overheat": false,
      "pump_fail": true
    }
  }'
```

响应：

```json
{
  "conflicts": [
    ["cooling_ok", "pump_fail"]
  ]
}
```

- `cooling_ok=true` 等价于 `cooling_fail=false`，即 `coolant_loss=false` 且
  `pump_fail=false`，与 `pump_fail=true` 的读数矛盾；删去任一项后剩余读数都可成立，
  故 `["cooling_ok", "pump_fail"]` 是最小零概率组。`motor_overheat=false` 与其余读数
  都不冲突，不出现在任何组中。
- 全部读数可同时成立时返回空组列：`{"conflicts": []}`。
- `p=0/1` 的事件被读成 `true`（或 `p=1/1` 被读成 `false`）时，该读数单独成组，
  例如 `{"conflicts": [["a"]]}`。
- 计算复用决策图作精确判定，**不枚举事件赋值**：把 `p∈{0,1}` 事件的强制文字
  （`p=1/1` 取 `x`、`p=0/1` 取 `¬x`）折叠进各观测约束后，零概率等价于约束合取在
  ROBDD 上恒为假；再按观测子集枚举合取（子集合取复用同一唯一表），利用"零概率
  关于组的包含关系单调"跳过已知含冲突的超集。
- 结果超过 **5000** 组、或累计非终端节点超过 **50000** 时返回 422 并说明原因。

## 模板实例化展开示例（POST /v1/expand-model）

`events` 的格式与校验同 `/v1/analyze`。另收：

- `templates`：**1～8** 项「模板名 → 模板定义」。每个模板含：
  - `params`：参数名列表（可空）；
  - `gates`：**非空**门列表，格式同 `/v1/analyze` 的门，但 `inputs` 引用的是**本地参数、
    本模板内的门或子实例 id**（取该子实例的顶层输出），不能直接写事件 id；
  - `instances`：子实例列表（可空），每项含 `id`、`template`（被调模板名）与 `bindings`；
  - `top`：本模板顶层节点，引用本地参数、门或子实例 id。
- `root`：根实例，含 `template` 与 `bindings`。根模板参数通过 `bindings` 绑定到**事件 id**；
  子实例的 `bindings` 只能把被调模板参数绑定到**父模板的参数**。所有 `bindings` 都必须
  **恰好覆盖**被调模板的全部参数（缺项或多项均返回 422）。

命名规则：模板名、参数名、门名、实例名一律为 **ASCII 字母开头、后接字母/数字/下划线**；
同一模板内参数名、门名、实例名**两两互异**（跨种类也不能重名）。门的算子与元数校验同
`/v1/analyze`（`AND`/`OR` 为 2～8 输入，`NOT` 恰有 1 输入）。

展开后每个实例的门被赋予全局唯一名 **`root[/子实例 id …]/门名`**；参数按各层绑定解析为
事件 id，子实例输出解析为该子实例展开后的 `top`。多个实例（含同一模板的多个实例）绑定到
同一事件时**共享该事件的状态与 id**。响应原样返回 `events`，并给出扁平的 `gates` 与 `top`，
其格式与 `/v1/analyze` 请求体完全一致，可直接提交分析。

下例定义二选一组件 `Voter`（`OR`）与系统模板 `System`（`AND` 两个 `Voter` 实例）：

```bash
curl -X POST http://localhost:8000/v1/expand-model \
  -H "Content-Type: application/json" \
  -d '{
    "events": [
      {"id": "motor_overheat", "p": "1/100"},
      {"id": "coolant_loss",  "p": "2/100"},
      {"id": "pump_fail",     "p": "1/200"}
    ],
    "templates": {
      "Voter": {
        "params": ["a", "b"],
        "gates": [
          {"id": "any", "op": "OR", "inputs": ["a", "b"]}
        ],
        "instances": [],
        "top": "any"
      },
      "System": {
        "params": ["m", "c", "p"],
        "gates": [
          {"id": "all", "op": "AND", "inputs": ["m", "cooling", "hydraulic"]}
        ],
        "instances": [
          {"id": "cooling", "template": "Voter",
           "bindings": {"a": "c", "b": "p"}},
          {"id": "hydraulic", "template": "Voter",
           "bindings": {"a": "m", "b": "p"}}
        ],
        "top": "all"
      }
    },
    "root": {
      "template": "System",
      "bindings": {"m": "motor_overheat", "c": "coolant_loss", "p": "pump_fail"}
    }
  }'
```

响应（门按“本模板门在前、子实例门按 `instances` 顺序在后”的前序排列）：

```json
{
  "events": [
    {"id": "motor_overheat", "p": "1/100"},
    {"id": "coolant_loss",  "p": "2/100"},
    {"id": "pump_fail",     "p": "1/200"}
  ],
  "gates": [
    {"id": "root/all", "op": "AND",
     "inputs": ["motor_overheat", "root/cooling/any", "root/hydraulic/any"]},
    {"id": "root/cooling/any", "op": "OR",
     "inputs": ["coolant_loss", "pump_fail"]},
    {"id": "root/hydraulic/any", "op": "OR",
     "inputs": ["motor_overheat", "pump_fail"]}
  ],
  "top": "root/all"
}
```

该响应可原样作为 `/v1/analyze` 的请求体（以 shell 管道串联两个接口）：

```bash
curl -s -X POST http://localhost:8000/v1/expand-model \
  -H "Content-Type: application/json" -d @model.json \
| curl -s -X POST http://localhost:8000/v1/analyze \
  -H "Content-Type: application/json" -d @-
```

- 模板支持任意深度嵌套（如模板 `A` 含模板 `B` 的实例、`B` 再含 `C` 的实例），门路径保留
  完整实例链：`root/实例1/实例2/门名`；被调模板未使用的参数与未被 `top` 引用的门均允许存在。
- **模板间不允许递归**（直接自递归或多模板相互递归，即使未被 `root` 触达）；模板**内部**
  的门引用关系也不允许成环。
- **展开后的门总数超过 64**（与 `/v1/analyze` 的门数上限一致）时返回 422；门数上限在
  **模板调用图**上记忆化限幅判定——同一模板每次实例化发射的门数相同，按
  `本模板门数 + Σ(子实例数 × 被调模板门数)` 在至多 8 个模板节点上累加并尽早截断，
  **不遍历实际实例树**。因此密集嵌套（如 8 层、每层 8 个子实例，完全展开将产生
  1+8+…+8⁷ = 2,396,745 个门）也会瞬时返回 422；恰好 64 个门仍可正常展开。
  未被 `root` 触达的模板不参与门数统计，但其静态校验（命名、元数、环、递归等）照常执行。
- 除上述模板规则外，事件与门的其余校验（概率、ASCII id、门元数、引用、环等）沿用
  `/v1/analyze`；展开后的门路径若与事件 id 冲突同样返回 422。

绑定缺项的响应示例（`Voter` 需要 `a`、`b` 两个参数，只绑定了一个）：

```json
{ "detail": "root 调用模板 'Voter' 的 bindings 缺少参数绑定：'b'" }
```

### 密集嵌套超限与边界输入

门数只取决于模板调用关系，与请求体大小无关：下例用 8 个模板 `L0`～`L7` 构成 8 层同构嵌套，
`L0` 是 1 个门的叶子，`L1`～`L7` 各自含 8 个指向下一层的子实例，完全展开将产生
1 + 8 + 8² + … + 8⁷ = **2,396,745 个门**。请求体仅约 4 KB，但服务端不会去遍历整棵实例树，
而是在模板调用图上立即判定超限并返回 422。可用下面的脚本生成请求体并调用：

```bash
python3 - <<'PY' | curl -s -X POST \
  http://localhost:8000/v1/expand-model -H "Content-Type: application/json" -d @-
import json

depth, fan = 8, 8                       # 8 层、每层 8 个子实例、每模板 1 个门
templates = {}
for i in range(depth):
    if i == 0:
        tpl = {"params": ["p"],
               "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
               "instances": [], "top": "g"}
    else:
        tpl = {"params": ["p"],
               "gates": [{"id": "g", "op": "NOT", "inputs": ["p"]}],
               "instances": [
                   {"id": f"c{j}", "template": f"L{i-1}",
                    "bindings": {"p": "p"}} for j in range(fan)],
               "top": "g"}
    templates[f"L{i}"] = tpl

print(json.dumps({
    "events": [{"id": "e1", "p": "1/2"}],
    "templates": templates,
    "root": {"template": f"L{depth-1}", "bindings": {"p": "e1"}},
}))
PY
```

响应（422，瞬时返回；`detail` 说明是模板调用图上的门数统计，而非实例树展开结果）：

```json
{ "detail": "展开后门数超过 64 个上限：仅根实例触达的模板按调用关系实例化后的门数即已超过 64（根模板 'L7' 的展开门数至少为 65）；判定基于模板调用图统计，未遍历具体实例树" }
```

更小的手工示例：根模板 1 个门 + 8 个各含 8 个门的叶子实例，共 1 + 8×8 = **65** 个门，
返回 422；把叶子实例改为 7 个则共 57 个门，正常展开。

```bash
curl -s -X POST http://localhost:8000/v1/expand-model \
  -H "Content-Type: application/json" \
  -d '{
    "events": [{"id": "e1", "p": "1/2"}],
    "templates": {
      "Leaf": {
        "params": ["p"],
        "gates": [
          {"id": "g0", "op": "NOT", "inputs": ["p"]},
          {"id": "g1", "op": "NOT", "inputs": ["p"]},
          {"id": "g2", "op": "NOT", "inputs": ["p"]},
          {"id": "g3", "op": "NOT", "inputs": ["p"]},
          {"id": "g4", "op": "NOT", "inputs": ["p"]},
          {"id": "g5", "op": "NOT", "inputs": ["p"]},
          {"id": "g6", "op": "NOT", "inputs": ["p"]},
          {"id": "g7", "op": "NOT", "inputs": ["p"]}
        ],
        "instances": [],
        "top": "g0"
      },
      "Box": {
        "params": ["p"],
        "gates": [{"id": "h", "op": "NOT", "inputs": ["p"]}],
        "instances": [
          {"id": "k0", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k1", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k2", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k3", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k4", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k5", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k6", "template": "Leaf", "bindings": {"p": "p"}},
          {"id": "k7", "template": "Leaf", "bindings": {"p": "p"}}
        ],
        "top": "h"
      }
    },
    "root": {"template": "Box", "bindings": {"p": "e1"}}
  }'
```

边界输入（恰好 **64** 个门）必须**正常展开**：把上例的叶子改为两个各含 8 门的模板、
各实例化 2 次（32 门），中间模板另含 7 个门（共 23 门），根模板含 18 个自身门并实例化
2 个中间模板：18 + 2×(7 + 8 + 8) = **64**，返回 200 且 `gates` 恰有 64 项；
把根模板的自身门增加到 19 个（共 65 门）则返回 422：

```json
{ "detail": "展开后门数超过 64 个上限：仅根实例触达的模板按调用关系实例化后的门数即已超过 64（根模板 'Root' 的展开门数至少为 65）；判定基于模板调用图统计，未遍历具体实例树" }
```

## 输入格式

| 字段 | 规则 |
| --- | --- |
| `events` | 1～32 个，元素含 `id`（唯一 ASCII 标识）与 `p`（`n/d` 字符串，整数 `0≤n≤d≤10^6`） |
| `gates` | 0～64 个，元素含 `id`、`op`、`inputs`；`op` 为 `AND` / `OR` / `NOT`（`/v1/cut-sets` 仅接受 `AND` / `OR`） |
| 门输入元数 | `AND`、`OR` 恰有 2～8 个输入；`NOT` 恰有 1 个输入 |
| `inputs` | 每个引用必须指向已定义的事件或门；同一 id 可被多处引用（共享状态） |
| `top` | 顶层节点，引用某个事件或门 |
| 变量顺序 | 事件 id 按字典序排序后构建有序 BDD |
| `states`（仅 `/v1/repair-plan`） | 必填，键必须恰好覆盖全部事件 id，值为布尔值；`true` 事件可修复为 `false`，`false` 事件不可改动 |
| `repair_costs`（仅 `/v1/repair-plan`） | 必填，键必须恰好覆盖全部事件 id，值为 `1`～`10^6` 的整数修复费用 |
| `observations`（仅 `/v1/diagnose`） | 必填，**1～16** 个键值对；键必须是已声明的事件或门 id，值为严格布尔值；表示同一时刻的读数 |
| `observations`（仅 `/v1/inspect-next`） | 可选，**0～16** 个键值对（可为空或省略），格式同 `/v1/diagnose`；表示已有读数 |
| `candidates`（仅 `/v1/inspect-next`） | 必填，**1～16** 个互异、已声明且未被观测的事件或门 id |
| `observations`（仅 `/v1/likely-cause`） | 可选，**0～16** 个键值对（可为空或省略），格式同 `/v1/diagnose`；与顶事件发生一起构成条件 `C` |
| `baseline` / `revised`（仅 `/v1/compare-trees`） | 必填，各含 `gates`（0～64 个，可省略）与 `top`；两版门 id 可同名但定义独立，不能跨版引用 |
| 请求体（仅 `/v1/failure-count`） | 与 `/v1/analyze` 完全相同：仅 `events` / `gates` / `top`，校验规则一致 |
| `events`（仅 `/v1/first-failure-time`） | **1～8** 个，`p` 规则同上；`gates`（0～64）与 `top` 沿用 `/v1/analyze` 全部校验 |
| `steps`（仅 `/v1/first-failure-time`） | 必填，**1～10** 的整数转移步数 |
| `transitions`（仅 `/v1/first-failure-time`） | 必填，键必须**恰好覆盖全部事件 id**；每项含 `fail`、`recover` 两个 `n/d` 概率（`0≤n≤d≤10^6`），分别为每步的故障概率与恢复概率 |
| `observations`（仅 `/v1/observation-conflicts`） | 必填，**1～16** 个键值对；键必须是已声明的事件或门 id，值为严格布尔值；返回这组读数之间的全部最小零概率观测组 |
| `templates`（仅 `/v1/expand-model`） | 必填，**1～8** 项「模板名 → 模板定义」；模板含 `params`（可空）、非空 `gates`、`instances`（可空）与 `top`；门输入与 `top` 只能引用本地参数、本地门或子实例 id |
| 模板名称（仅 `/v1/expand-model`） | 模板名、参数名、门名、实例名均须 ASCII 字母开头、后接字母/数字/下划线；同一模板内参数/门/实例名两两互异 |
| `bindings`（仅 `/v1/expand-model`） | root 把根模板参数绑定到事件 id；子实例把被调模板参数绑定到父模板参数；键必须恰好覆盖被调模板全部参数 |
| `root`（仅 `/v1/expand-model`） | 必填，含 `template`（已定义模板名）与 `bindings`；展开后门名为 `root[/实例 id …]/门名`，门总数不超过 **64** |

概率在节点上按 `P(node) = (1-p_v)·P(low) + p_v·P(high)` 自底向上精确传播；
事件重要度通过“根到节点路径概率质量”单遍传播精确求得。
`/v1/diagnose` 则在各观测约束合取得到的 ROBDD 根上做两遍加权传播，同时求 `P(E)` 与每个事件的
`P(事件=1 且 E)`（ROBDD 路径跳过的变量按自由变量补计其先验），两者相除即得后验。
`/v1/inspect-next` 先合取观测约束得到 `E`，再对每个候选 `X` 构建 `E∧X`、`E∧X∧top` 的合取，
对唯一表做一次自底向上加权即可同时读出全部根的概率；假分支概率由
`P(E∧¬X) = P(E) − P(E∧X)` 等减法精确得到，无需构建取反合取。
`/v1/likely-cause` 把顶事件与各观测约束合取为 `C` 后在同一 ROBDD 上做动态规划：
自底向上求每个节点子图取 1 的最大先验乘积（路径跳过的自由变量按 `max(p, 1−p)` 补计），
再自顶向下在并列时优先低分支（`false`）走出最优赋值，全程不枚举事件状态。
`/v1/compare-trees` 把两版故障树建到**同一变量顺序、同一 ROBDD**（同一唯一表）上：
同一事件在两版对应同一变量节点（共享状态与先验），同名门各自独立求值；
差异运算 `¬B∧R`、`B∧¬R`、`B⊕R` 也在该表上求值，一次自底向上加权即同时读出三个
差异概率；`equivalent` 由规范化的根节点是否相同直接判定（覆盖全部赋值，含零概率赋值）。
`/v1/failure-count` 在顶事件 ROBDD 上为每个节点自底向上维护取 1 变量个数的**生成函数**
（整系数概率多项式，公分母为各先验分母之积，最后统一约分）：高分支乘先验并把计数加一、
低分支乘补概率不加计数，边上被约简跳过的变量与到达终端后剩余的变量按各自先验以
`((1-p)+p·x)` 因子补入，根多项式的第 `k` 项即 `P(K=k 且 T)`，除以 `P(T)` 得条件分布，
全程不枚举事件状态。
`/v1/first-failure-time` 先在顶事件 ROBDD 上枚举全部 `2^n`（`n≤8`，至多 256 个）完整事件
状态，标出顶事件为真的状态集合；再在该状态空间上做概率动态规划：`t=0` 按各事件先验之积
生成状态质量，每一步按事件序逐个施加二态转移核（给定上步状态后各事件独立），把顶事件为真
的状态质量当场吸收进当步 `first_hit` 并移出活跃分布——动态规划始终只在未触发状态上进行。
全部质量以公分母下的整数传播（转移分母逐事件以最小公倍数并入，仅整数乘加），最终统一约分
为 `n/d`，无浮点误差。
`/v1/observation-conflicts` 把各观测读数化为同一 ROBDD 上的约束节点（读 `false` 取反），
再把 `p∈{0,1}` 事件的强制文字（`p=1/1` 取 `x`、`p=0/1` 取 `¬x`）折叠进每个约束：完整赋值
概率大于 0 当且仅当满足全部强制文字，故"组内读数同时成立的先验概率为 0"等价于折叠后约束
的合取恒为假（终端 0），结构矛盾与 `p=0/1`、`1/1` 造成的零概率被统一识别。随后按观测子集
（至多 16 个观测）自底向上枚举合取：完全合取非零时直接返回空组列；否则利用零概率关于组的
包含关系单调，跳过删单元素后已知零概率的超集，其余子集各经一次 `and_` 在共享唯一表上求值，
为终端 0 即一个最小组，全程不枚举事件赋值。

## 错误处理（HTTP 422）

以下情况均返回 `422 Unprocessable Entity`，响应体形如 `{"detail": "原因说明"}`：

- 引用了未知 id（门输入或 `top` 未定义）；
- 门引用关系存在环；
- 非法概率（不是 `n/d`、分母为 0、`n>d`、`d>10^6`、非十进制整数等）；
- 事件/门 id 重复、门 id 与事件 id 冲突、非 ASCII 或空 id；
- 门算子非法或输入个数不合法；事件数 / 门数超限；
- `/v1/cut-sets` 请求中出现 `NOT` 门；
- `/v1/cut-sets` 任一门的最小割集去重、吸收后仍超过 **5000** 组；
- `/v1/repair-plan` 的 `states` / `repair_costs` 缺少事件对应项、出现未知事件键，
  或修复费用不是 **1～10^6** 的整数（类型错误如字符串、浮点数同样拒绝）；
- `/v1/diagnose` 的 `observations` 为空或超过 **16** 个、键不是已声明的事件或门 id、
  值不是严格布尔值（`1`、`0`、字符串等同样拒绝）；
- `/v1/diagnose` 的 `evidence_probability = P(E) = 0`：观测结构矛盾（合取恒假），
  或结构可满足但先验下无正概率支持（如读 `p=0` 的事件为 `true`），响应说明具体原因；
- `/v1/inspect-next` 的 `observations` 超过 **16** 个或键/值非法、`candidates` 为空、
  超过 **16** 个、存在重复、引用未声明 id 或候选已被观测（多项问题一并说明）；
- `/v1/inspect-next` 的 `P(E) = 0`（同 `/v1/diagnose` 的两种情形）；
- `/v1/likely-cause` 的 `observations` 超过 **16** 个或键/值非法（同 `/v1/diagnose`）；
- `/v1/likely-cause` 的 `condition_probability = P(C) = 0`：顶事件与读数在结构上
  相互矛盾（合取恒假），或结构可满足但先验下无正概率支持，响应说明具体原因；
- `/v1/compare-trees` 的任一版本引用另一版本独有的门 id（跨版引用）、引用未知 id、
  存在环或门定义非法（校验规则与 `/v1/analyze` 一致）；
- `/v1/failure-count` 的 `top_probability = P(T) = 0`：顶事件在结构上不可能发生
  （BDD 恒为假），或结构可满足但先验下无正概率支持（如要求 `p=0` 的事件发生），
  响应说明具体原因；
- `/v1/first-failure-time` 的 `steps` 不是 **1～10** 的整数（类型错误如字符串、布尔、
  浮点同样拒绝）、`transitions` 缺少事件对应项或出现未知事件键（多项一并说明）、
  某项的 `fail` / `recover` 不是合法 `n/d` 概率（规则同事件 `p`），或事件数超出 **8** 个；
- `/v1/observation-conflicts` 的 `observations` 为空或超过 **16** 个、键不是已声明的
  事件或门 id、值不是严格布尔值（`1`、`0`、字符串等同样拒绝）；
- `/v1/observation-conflicts` 的最小零概率观测组超过 **5000** 组；
- `/v1/expand-model` 的模板名/参数名/门名/实例名不符合命名规则、模板内参数/门/实例
  重名（含跨种类重名）、门元数非法、root 或子实例调用未定义模板、`bindings` 缺项或
  绑定了不属于被调模板的参数、root 参数绑定到未知事件、子实例参数绑定到非父参数、
  门输入或 `top` 引用未知本地节点、模板内门成环、模板间递归（含未被 root 触达的模板）、
  或展开后门总数超过 **64**；门数在模板调用图上记忆化限幅判定，不遍历实例树，
  故密集嵌套（如 8 层 × 每层 8 个同构子实例）也会瞬时返回 422，恰好 64 门仍可展开；
- ROBDD 构建过程中累计创建的非终端节点超过 **50000** 个
  （`/v1/analyze`、`/v1/repair-plan`、`/v1/diagnose`、`/v1/inspect-next`、
  `/v1/likely-cause`、`/v1/failure-count`、`/v1/first-failure-time` 与
  `/v1/observation-conflicts`；其中
  `/v1/diagnose`、`/v1/inspect-next`、`/v1/likely-cause` 还含合取观测约束与候选分支
  新增的节点，`/v1/failure-count` 的计数动态规划与 `/v1/first-failure-time` 的状态空间
  动态规划均不新增 BDD 节点，限额仅针对故障树建图；
  `/v1/observation-conflicts` 还含强制文字折叠与观测子集合取枚举新增的节点；
  `/v1/compare-trees` 为两版建图及差异运算的累计）。

环示例响应：

```json
{ "detail": "门的引用关系存在环（涉及门 'g1'）" }
```

## 本地开发（不使用 Docker）

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

运行测试（含 40 个随机电路对穷举真值表的精确对照）：

```bash
pip install pytest
python -m pytest -q
```

## 项目结构

```
app/
  __init__.py
  bdd.py       # ROBDD：唯一表、ITE 算子、精确概率/重要度、联合证据加权与失效数计数分布
  conflicts.py # 最小零概率观测组：强制文字折叠 + 观测子集合取枚举（5000 组上限）
  cutsets.py   # 最小割集：位掩码割集族、去重吸收、5000 组上限
  repair.py    # 修复方案：ROBDD 上的受限赋值动态规划（成本/修复数/字典序）
  likely.py    # 最可能成因：ROBDD 上的最大先验乘积赋值动态规划（false 优先）
  first_hit.py # 首次故障时间：未触发状态上的整数公分母马尔可夫动态规划
  main.py      # FastAPI 应用、请求模型与校验
Dockerfile
docker-compose.yml
requirements.txt
tests/test_api.py
tests/test_cutsets.py
tests/test_repair.py
tests/test_diagnose.py
tests/test_inspect_next.py
tests/test_likely_cause.py
tests/test_compare_trees.py
tests/test_failure_count.py
tests/test_first_failure_time.py
tests/test_observation_conflicts.py
tests/test_expand_model.py
```
