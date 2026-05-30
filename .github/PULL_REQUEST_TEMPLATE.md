<!--
For Chinese contributors: 请直接用中文填写。
For English contributors: please fill in English. All fields marked (EN) accept English.
-->

## PR Type

- [ ] fix
- [ ] feat
- [ ] refactor
- [ ] docs
- [ ] chore
- [ ] test

## Background And Problem

请描述当前问题、影响范围与触发场景。  
*(EN) Describe the problem, its impact, and what triggers it.*

## Scope Of Change

请列出本 PR 修改的模块和文件范围。  
*(EN) List the modules and files changed in this PR.*

## Acceptance Criteria

### PR 元信息与验收边界

以下内容仅适用于 **docs/governance-only** PR，非 docs/governance PR 可跳过：

- [ ] **仅说明边界，不新增 runtime 行为**：说明性 PR 不承担 `SCHEDULE_TIME` 或其他运行时能力交付。
- [ ] **docs-only 自检清单**：边界说明与实际行为一致、双语文档同步、`docs/CHANGELOG.md` 同步（如涉及用户可见文档）；
- [ ] **Issue 链接语义正确**：文档/治理说明类 PR 请使用 `Refs #<issue_number>`（或无 issue 说明），并将 PR 类型限定为 `docs`，避免误关闭 Issue。


## Issue Link

必须填写以下之一 / Fill in one of:
- `Fixes #<issue_number>`
- `Refs #<issue_number>`
- 无 Issue 时说明原因与验收标准 / If no issue, explain the motivation and acceptance criteria

> 文档/治理类、仅做边界澄清且不含运行时交付的 PR，请选择 `Refs #<issue_number>`（或不填）并在 PR 类型中勾选 `docs`，避免误触发 Issue 自动关闭；不要使用 `Fixes`/`Closes`（二者都会触发 Issue 自动关闭）用于此类 PR。
> For docs/governance-only changes that only clarify boundaries and do not include runtime delivery, use `Refs #<issue_number>` (or leave it blank) and select PR type `docs`; do not use `Fixes` or `Closes` (both auto-close issues) for this PR type.

## Verification Commands And Results

请填写你实际执行过的命令和关键结果（不要只写"已测试"）。  
*(EN) Paste the commands you actually ran and their key output (don't just write "tested"):*

```bash
# example
./scripts/ci_gate.sh
python -m pytest -m "not network"
```

关键输出/结论 / Key output & conclusion:

## Compatibility And Risk

请说明兼容性影响、潜在风险（如无请写 `None`）。  
*(EN) Describe compatibility impact and potential risks (write `None` if not applicable).*

- 若本 PR 修改第三方模型 / API 的兼容语义、请求参数、路由前缀或 provider fallback，请提供**官方来源链接或公告**，并说明这是长期约束、当前运行时约束还是临时兼容处理。  
  *(EN) If this PR changes third-party model/API compatibility, request parameters, routing prefixes, or provider fallback behavior, include an **official source link or announcement** and clarify whether the rule is permanent, runtime-specific, or a temporary compatibility workaround.)*
- 若本 PR 依赖特定运行时 / 锁定依赖窗口（例如 LiteLLM 版本范围、OpenAI-compatible 路由、YAML alias 行为），请写明当前验证过的兼容范围与覆盖路径。  
  *(EN) If this PR depends on a specific runtime or pinned dependency window (for example a LiteLLM version range, OpenAI-compatible routing, or YAML alias behavior), state the compatibility window you verified and which code paths were covered.)*
- 若本 PR 触及运行时配置保存、清理、迁移或回填逻辑，请明确说明旧配置是否会被自动改写、清空、迁移或保持不变，以及用户如何恢复原行为。  
  *(EN) If this PR touches runtime config save/cleanup/migration/backfill logic, explicitly describe whether existing config is rewritten, cleared, migrated, or left intact, and how users can restore the previous behavior.)*

## Rollback Plan

请至少写一句可执行的回滚方案（必填）。  
*(EN) Provide at least one actionable rollback step (required).*

- 如果是兼容性修复，默认应写出**最小回滚方式**（例如 `revert this PR`），并说明是否需要额外回滚配置或数据迁移。  
  *(EN) For compatibility fixes, include the **minimal rollback path** (for example `revert this PR`) and whether any additional config or data rollback is required.)*

## EXTRACT_PROMPT Change (if applicable)

若本 PR 修改了 `src/services/image_stock_extractor.py` 中的 `EXTRACT_PROMPT`，请在此处粘贴完整变更后的 prompt。  
*If this PR changes `EXTRACT_PROMPT` in `src/services/image_stock_extractor.py`, paste the full updated prompt here:*

<details>
<summary>展开 / Expand: Full EXTRACT_PROMPT</summary>

```
(paste full prompt here)
```

</details>

## Checklist

- [ ] 本 PR 有明确动机和业务价值 / This PR has a clear motivation and value
- [ ] 已提供可复现的验证命令与结果 / Reproducible verification commands and results are included
- [ ] 已评估兼容性与风险 / Compatibility and risk have been assessed
- [ ] 已提供回滚方案 / A rollback plan is provided
- [ ] 若涉及用户可见变更，已同步更新相关文档与 `docs/CHANGELOG.md`；`README.md` 仅在首页级信息变化时更新，细节优先写入 `docs/*.md` / If user-visible changes are included, relevant docs and `docs/CHANGELOG.md` are updated; `README.md` is updated only for homepage-level changes, with details kept in `docs/*.md`
