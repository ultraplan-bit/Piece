# 图谱失败恢复与请求管理

写入失败、回执带失败码，或提交结果不确定时读本文；含读回字段说明、请求键与本地留档、索引重建。

所有 `--json` 返回 `success/message/data/error`，先检查 `success` 再读 `data`。

## 失败处理

若失败响应带 `next_command`（bash 转义）和 `next_argv`（其他调用方式），它们是完整前缀的**只读**恢复命令。按它恢复一次；仍失败就停止并如实报告，不盲目重放。没有恢复指令时按错误说明处理，不猜命令。

- **提交结果未知**：优先按提示执行 `graph request KEY --read-back`（键在回执 `data.request_key`；有请求文件时用 `graph request --input 原请求文件 --read-back` 由 CLI 取键）。尚未查到结果也不等于失败；需要重试时原样重跑同一条命令（有请求文件则 `graph apply --input 原请求文件 --read-back`），不换键、不重新组织批次。
- `READ_BACK_INCOMPLETE`：`data.committed=true` 表示已提交，但读回被截断、读取失败或状态已变；**不要重提交**，按 `next_command` 补充读取和比较。
- `VERSION_CONFLICT`：读回当前版本重新准备增量，**不自动覆盖**，不只换请求键。
- `KNOWLEDGE_CONFLICT`：同端点、谓词、条件的关系已存在且内容不同。`data.existing` 给出它的 `id/revision/display/description/basis`，`next_command` 是 `graph relation update EXISTING_UUID --expected-revision N` 前缀；要改就补上待改字段执行，要保留就不再写，不要换 `--qualifier` 绕过去新建。
- `REQUEST_CONFLICT`：该键已用于另一份内容。先 `graph request` 查原请求，原样重试保留原键，不同操作才用新键。
- `IMPACT_CONFLICT`：预览后依赖已变，重新预览并取得确认，不自动删除新关系。
- `SOURCE_CHANGED` / `QUOTE_MISMATCH` / `INVALID_SOURCE`：重读来源和归属，不伪造引文、页码或哈希。
- `CONFIRMATION_REQUIRED`：停下取得用户确认，不能自行加 `--yes`。

## 读回字段

- 高层命令默认读回；`graph apply --read-back` 也可读回。`read_back` 含 `complete/records/total/limit/truncated/semantic_review_required`，实体和关系还报告 `submitted_revision/revision_matches`。最多自动读回 20 条受影响记录；超过预算、读取失败、版本或本库来源变化时返回 `READ_BACK_INCOMPLETE`，此时 `data.committed` 仍为 true，**不重提交**，按 `next_command` 补充只读核查。
- 实体／关系回执的 `submitted_fields` 列出显式提交的字段；它不表示值一定变化，也不证明语义正确。旧请求缺少该字段时不猜测。关系读回的 `record.display` 显示当前标题与谓词；核对它和实际 `source_id/predicate/target_id`，不要只看 revision 增加。
- 只报告实际完成的结果，列出歧义和未解决问题。

## 请求键与本地留档

- 四个意图命令（`object add` / `object update` / `relation add` / `relation update`）默认不落盘：CLI 自动生成请求键并回传 `data.request_key`，结果不确定时用 `graph request KEY --read-back` 查询。实体可选 `--summary`、可重复的 `--alias`、`--status`；新增关系必须提供 `--description` 和 `--basis explicit|synthesis|inference|user_statement`；`--reason 原因` 可记录修改原因。
- 需要本地留档时加 `--save-request`（自动存到数据目录 `requests/` 并回传路径）或 `--request-file 路径`；两者互斥。请求文件形如 `{target_id, request:{request_key, reason, objects/relations/evidence...}}`，路径已存在时只有目标与内容都相同才允许复用，相同命令加同一文件可原样重试。
- 请求文件是**本地敏感材料**：删除在线记录不会删除它，按用户要求自行保管或清理。
- **试探参数用 `--help`，验证写入用 `--dry-run`。** 不加 `--dry-run` 就会提交。只有标题、没有摘要的实体也合法。`--dry-run` 不写在线数据；核对预检结果后去掉 `--dry-run` 原样重跑即可正式提交。
- 多实体批次、单独补证据、外部来源等高级操作用 `graph apply --input -` 从 stdin 读批次（heredoc 即可，不必写文件），也接受文件路径或上面的包装请求文件；格式见 [apply.md](apply.md)。

## 图谱索引重建

`graph rebuild-index` 只重建图谱实体的派生全文索引（FTS），不修改实体、关系、证据或历史，也不调用模型；写索引重建需要写权限。检索结果异常或索引缺失时用它恢复：

```text
<PIECE> graph rebuild-index --json
```
