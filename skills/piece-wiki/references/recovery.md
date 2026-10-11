# Wiki 失败恢复与请求管理

写入失败、回执带失败码，或提交结果不确定时读本文；含读回字段说明、请求键与本地留档、索引重建。

所有 `--json` 返回 `success/message/data/error`，先检查 `success` 再读 `data`。

## 失败处理

若失败响应带 `next_command`（bash 转义）和 `next_argv`（其他调用方式），沿用其完整前缀。读取命令只核查状态；`wiki rebuild-index` 会写派生索引但不改正文。按提示恢复一次，仍失败就停止并如实报告，不盲目重放。没有恢复指令时按错误说明处理，不猜命令。

- **部分成功**：`data.committed=true` 且 `data.partial=true` 表示至少一页已写入，但存在失败页或索引未更新。**不自动整批重提、不换键**；先核对逐页回执与 `errors`。已完成页不会再次写入，确认需继续未完成项时才原键原样重试；仅索引问题用 `wiki rebuild-index`。
- **零写入**：`data.committed=false` 且 `errors` 非空时，`error.code` 不是 `PARTIAL_FAILURE`，而是首条错误的真实失败码（`VERSION_CONFLICT` / `CONTENT_CONFLICT` / `SOURCE_CHANGED` 等）。此时什么都没写，按该码处理后可以重新提交；`PARTIAL_FAILURE` 只出现在至少写入一页的批次。
- **提交结果未知**：优先按提示执行 `wiki request KEY --read-back`（键在回执 `data.request_key`；有请求文件时用 `wiki request --input 原请求文件 --read-back` 由 CLI 取键）。尚未查到结果也不等于失败；需要重试时原样重跑同一条命令（有请求文件则 `wiki apply --input 原请求文件 --read-back`）。
- `READ_BACK_INCOMPLETE`：`data.committed=true` 表示已提交，但读回被截断、读取失败或状态已变；**不要重提交**，按 `next_command` 补充读取和比较。
- `VERSION_CONFLICT` / `CONTENT_CONFLICT`：读回当前页面重新准备增量，**不自动覆盖**，不只换请求键或哈希。`SOURCE_CHANGED` / `QUOTE_MISMATCH` / `INVALID_SOURCE`：重读来源和归属，不伪造引文、页码或哈希。
- `REQUEST_CONFLICT`：该键已用于另一份内容。先 `wiki request` 查原请求，原样重试保留原键，不同操作才用新键。
- `IMPACT_CONFLICT`：预览后依赖已变，重新预览并取得确认，不自动扩大删除范围。
- `CONFIRMATION_REQUIRED`：停下取得用户确认，不能自行加 `--yes`。

## 读回字段

- 高层命令默认读回；`wiki apply --read-back` 也可读回。`read_back` 含 `complete/records/total/limit/truncated/semantic_review_required`，页面还报告 `submitted_revision/revision_matches/content_hash_matches`。最多自动读回 20 条受影响记录；超过预算、读取失败、版本或本库来源变化时返回 `READ_BACK_INCOMPLETE`，此时 `data.committed` 仍为 true，**不重提交**，按 `next_command` 补充只读核查。
- 页面回执的 `submitted_fields` 列出显式提交的字段；它不表示值一定变化，也不证明语义正确。旧请求缺少该字段时不猜测。
- 只报告实际完成的结果，列出歧义和未解决问题。

## 请求键与本地留档

- 两个意图命令（`page add` / `page update`）默认不落盘：CLI 自动生成请求键并回传 `data.request_key`，结果不确定时用 `wiki request KEY --read-back` 查询。页面可选 `--summary`、`--body-file 正文.md`（或 `-`）、可重复的 `--alias`、`--status`；`--reason 原因` 可记录修改原因。不给页面正文也合法。
- 需要本地留档时加 `--save-request`（自动存到数据目录 `requests/` 并回传路径）或 `--request-file 路径`；两者互斥。请求文件形如 `{target_id, request:{request_key, reason, pages/evidence...}}`，路径已存在时只有目标与内容都相同才允许复用，相同命令加同一文件可原样重试。
- 请求文件是**本地敏感材料**：删除在线记录不会删除它，按用户要求自行保管或清理。
- **试探参数用 `--help`，验证写入用 `--dry-run`。** 不加 `--dry-run` 就会提交。`--dry-run` 不写在线数据；核对预检结果后去掉 `--dry-run` 原样重跑即可正式提交。
- 多页面批次、单独补证据、外部来源等高级操作用 `wiki apply --input -` 从 stdin 读批次（heredoc 即可，不必写文件），也接受文件路径或上面的包装请求文件；格式见 [apply.md](apply.md)。

## 索引重建

`wiki rebuild-index` 只按现有 MD 重建搜索与页面链接索引，不重新生成正文、不覆盖页面；写索引重建需要写权限。上次写入返回 `partial=true` 或 `index_status` 不是 `current` 时，优先用它恢复：

```text
<PIECE> wiki rebuild-index --json
```

## 本地恢复副本

每次页面更新 / 删除都会把被替换的原文件留作 `.wiki-recovery-<operation_id>.bak`，不会自动清理。两条命令管理它们：

```text
<PIECE> wiki list-recovery --json
<PIECE> wiki list-recovery --status releasable --json
<PIECE> wiki purge-recovery --json                       # 只预览，不删除
<PIECE> wiki purge-recovery --operation-id OP_UUID --yes --json
```

- `list-recovery` 只读；每项 `status` 为 `needed`（对应操作尚未提交，副本是旧内容的唯一保留，**不能清理**）、`releasable`（操作已结算，审计表已有 `before` 快照）或 `orphan`（没有操作记录）。`counts` 汇总各状态数量与总字节数。
- `purge-recovery` 默认只预览；用户确认后才加 `--yes`。默认只清 `releasable`；`orphan` 须显式 `--include-orphans`；`needed` 永不清理。用 `--operation-id` 点名时，任一副本不能清理会整批拒绝并返回 `RECOVERY_REQUIRED`，不会静默跳过。
- 清理只删本地 `.bak` 文件，不改页面正文、不动审计表；删除后无法再从本地恢复被替换的旧内容，审计历史仍可用 `wiki history` 读到 `before` 快照。
