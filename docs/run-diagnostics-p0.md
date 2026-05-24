# 运行诊断与数据可靠性 1.0（Phase 0）

本文档定义 #1391 的 **Phase 0（P0）**：在不引入新页面、不改变全局分析策略与 fallback 核心语义前提下，收敛契约边界并限定本轮运行时修复范围。

## 目标

- 给后续实现提供统一术语：`trace_id`、关键链路记录、诊断摘要、脱敏排障信息。
- 明确第一阶段范围，避免把需求扩成“完整可观测平台”。
- 固化 fail-open、安全与 retention 基线，降低回归风险。

## 当前文档范围（本轮）

- 本文件为 Phase 0 合同与验收边界文档，当前 PR 为 docs + runtime fix，本轮同步补齐 `baostock_fetcher.py`、`pytdx_fetcher.py`、`tushare_fetcher.py` 的 A 股裸码归属边界，并配套由 `tests/test_a_share_fetcher_code_conversion.py` 做回归验证。
- 若无新 Token/模型提供商/接口映射要求，本轮不扩展 Tushare `600/601/603/688` 外规则（如 `605xxx`）；该问题继续由后续专门 PR 跟进。

## 非目标

- 不做 OpenTelemetry / APM / Grafana 风格监控系统。
- 不在首版展示 p95、全量 Provider 调用明细、完整运维面板。
- 不改变现有数据源优先级、分析策略、通知策略。
- 不变更 LLM provider 列表、Base URL、`llm_call` 运行时参数、`REPORT_*` 配置语义与迁移路径；本轮改动限定在 A 股代码归属解析与诊断字段边界。

## 术语与契约（P0 草案）

### 1) `trace_id`

- 含义：一次分析运行链路的统一关联 ID。
- 要求：
  - 每次分析任务仅有一个 `trace_id`。
  - 可由入口生成，或由已有任务 ID 映射（例如 Web 任务）。
  - 出现在日志/结构化诊断中用于排障关联。

### 2) `RunDiagnosticSummary`

- 含义：给用户看的简短运行诊断摘要。
- 建议字段（首版保持最小）：
  - `trace_id`
  - `status`：`ok` / `degraded` / `failed`
  - `data_status`：关键数据路径是否降级
  - `notify_status`：通知结果摘要
  - `error_hint`：脱敏后的简要原因
- 说明：这是用户可感知能力，不等于内部全量事件日志。

### 3) 关键链路记录（最小集合）

首版只要求记录以下关键节点结果（成功/失败/降级 + 简短原因）：

- `realtime_quote`
- `daily_data`
- `llm_call`
- `report_persist`
- `notification_dispatch`

> 说明：`news`、`fundamental`、`capital_flow` 等放到后续扩展，不作为首版阻断项。

## 安全与稳定性边界（P0 必须遵守）

### Fail-open

- 诊断记录失败不应阻断主分析流程。
- 即使诊断写入失败，也必须继续产出分析结果（除非主流程本身失败）。

### 脱敏

- 复制排障信息中禁止包含密钥、token、完整 webhook URL、用户账号标识。
- 错误文案输出以摘要为主，避免泄露第三方返回的敏感原文。

### Retention

- 诊断数据保留周期应可配置或可统一清理。
- 默认策略优先保守（例如仅保留必要时间窗），避免无限增长。

### 兼容性

- 新字段应优先追加，不破坏现有 API / Web / Desktop 读取路径。
- 旧历史记录缺少新字段时应可安全回退。

### 验证与沟通边界（本轮）

- PR 触及后端数据源代码，不再标注为“docs-only”；本轮为 docs + runtime fix。
- PR 合并前需以实际状态说明回归测试执行结果：`python -m pytest tests/test_a_share_fetcher_code_conversion.py` 与 `./scripts/ci_gate.sh`（至少需给出两者的成功/未执行原因及截图/日志）。
- 回滚方式为回退对应 `data_provider/baostock_fetcher.py`、`data_provider/pytdx_fetcher.py`、`data_provider/tushare_fetcher.py` 的本轮变更；如需最小风险处置，可直接还原该轮提交。
- 本轮未改动 LLM provider 列表、Base URL、`llm_call` 迁移路径和运行时配置语义，避免扩展到模型路由兼容面。
- 本轮需先确认无未解决 merge conflict；若存在冲突，需先清理后再以最终 head 重新确认 diff 与验证记录。

## Phase 0 交付清单

- [x] 明确目标/非目标，防止范围失控。
- [x] 定义 `trace_id` 与 `RunDiagnosticSummary` 最小契约。
- [x] 明确首版关键链路覆盖范围。
- [x] 固化 fail-open、脱敏、retention、兼容性基线。

## 后续阶段（仅说明，不在 P0 实现）

- Phase 1：`trace_id` 贯通与关键链路最小记录落地。
- Phase 2：生成并持久化 `RunDiagnosticSummary`，支持复制脱敏排障信息。
- Phase 3：Web 侧最小展示（默认折叠），并补齐文档和回滚说明。
