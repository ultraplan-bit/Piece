---
name: piece-graph
description: 使用 Piece CLI 维护知识图谱：实体、有证据的语义关系，查询实体邻域、关系与来源反查，以及核查结构和失效来源。适用于建图、实体与语义关系维护、图谱查询与知识核查；不编译或修改 Wiki 页面，不把页面内链当作语义关系。
---

# Piece 知识图谱维护与查询

用 Piece CLI 维护图谱实体、有证据的语义关系，查询实体邻域、关系与来源反查，并核查失效来源。图谱实体、关系、证据是独立数据，不要求存在 Wiki 页面；图谱基本操作不调用模型或嵌入接口。

## 先核对实例

执行一次 `<PIECE> status --json`，核对配置目录、端口、`cli_version` 与就绪时的 `library_id`，沿用导出头部的前缀。服务未运行（`SERVICE_UNAVAILABLE`）就提示用户启动；命令不存在时要求更新并重新导出 Skill。

## 先选路线

| 用户意图 | 路线 |
| --- | --- |
| 查实体 / 关系 / 邻域 | `graph search`（实体）或 `graph relation-search`（关系）→ `graph get` → `graph graph`，不写入 |
| 建图、补主体或语义关系 | 按下文最小命令写入 |
| 只给已有实体／关系补证据 | `references/apply.md#仅补证据`；不改摘要凑更新 |
| 核查失效与争议 | `graph lint` → 读候选与来源 → 报告，不自动修复 |
| 索引缺失或查询异常 | `graph rebuild-index`（见 `references/recovery.md`） |
| 删除 | 先预览影响，用户确认后才执行（见 `references/safety.md`） |

## 最小命令

一次写入就是一条命令，不写任何临时文件：

```text
<PIECE> status --json
<PIECE> graph search "关键词" --limit 5 --json
<PIECE> chunk get CHUNK_ID --json
<PIECE> graph object add --kind concept --title "实体" --summary "摘要" --evidence CHUNK_ID:2-3 --json
<PIECE> graph relation add --source SOURCE_UUID --predicate depends_on --target TARGET_UUID --description "关系说明" --basis explicit --evidence CHUNK_ID:grep=关键句 --json
```

只在用户要求保存时写入；删除先 `--dry-run` 预览影响，用户确认后才加 `--yes`。失败回执带 `next_command` 时照它执行一次。

## 按需读参考

- 建关系、补证据、选谓词 / basis / stance、判断是否合并，以及只读查询命令：`references/semantics.md`
- 失败码（`VERSION_CONFLICT`、`READ_BACK_INCOMPLETE`、提交结果未知等）与恢复动作、读回字段、请求键与留档、重建索引：`references/recovery.md`
- 删除预览与确认、备份范围、安全红线：`references/safety.md`
- 一次提交多条记录、字段级契约、仅补证据批次：`references/apply.md`
