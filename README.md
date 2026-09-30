# 数字人文文本校勘

这是一个 Python 标准库实现的校勘工作台，使用 SQLite 保存作品、版本、残片、转录、段落、异文、注释、修订层和快照，并通过 `http.server` 暴露 JSON API。

## 启动与测试

```bash
python app.py
python -m unittest discover -s tests -v
```

默认端口 `8114`，地址 <http://127.0.0.1:8114>。首次启动创建一个带缺页残片和不可辨标记的示例。数据库可通过 `COLLATION_DB` 指定，端口可通过 `PORT` 指定。

## 业务规则

- 版本类型限定为 `version`、`fragment`、`transcription`。
- 段落和版本必须属于同一作品，同一版本不能重复对齐同一段落。
- 只有负责人或被单独授权的编辑可以修改对应版本；其他用户只有查看权限。
- `[缺页]`、`[不可辨]`、`[残损]` 等标记会参与校勘稿导出和缺口统计，不匹配的方括号会拒绝保存。
- 每次新增或修改异文都会产生递增修订号和 JSON 快照；提交必须携带 `expected_revision`，旧页面不能覆盖新层。
- 锁定段落由负责人执行，锁定后任何新修订都会被拒绝。

## 可恢复的合校发布

负责人选定**截止修订号**后为作品生成候选合校稿，逐段落由负责人或版本编辑提交合校：

- 每个段落独立处于 `pending`/`done`/`failed`；某段失败（如尚无版本对齐）只标记该段，可在补齐数据后重试，**已完成段落不会重做**；也可调用批量重试只处理失败段落。
- 两名编辑并发提交同一段落时，写事务串行化且按状态条件更新，**先到者写入**；晚到者收到冲突提示，必须携带 `confirm=true` 重新确认，确认只记录确认人，不重新组装。
- 全部段落完成后负责人才能发布；发布时冻结清单（manifest），此后不可更改、长期保留。
- 候选稿遇到下列事件自动**作废**（`void`），需重新发起：负责人换人（`transfer`）、版本编辑授权撤回（`editors/revoke`）、段落锁定状态变化（`lock`/`unlock`）。已发布版本不受这些事件影响。
- 发布前审阅者（`view`/`review` 权限）只能查看候选稿进度与已完成片段，不能提交或发布。

### 合校发布接口

- `POST /api/works/{id}/releases`（负责人，body: `cutoff_revision`、`user_id`）
- `POST /api/releases/items`（提交/重试单段：`release_id`、`passage_id`、`user_id`，可选 `confirm`）
- `POST /api/releases/{id}/items/retry`（批量重试失败段落）
- `POST /api/releases/{id}/publish`（负责人发布并冻结）
- `GET /api/releases/{id}?user_id=...`、`GET /api/works/{id}/releases?user_id=...`
- `POST /api/passages/{id}/unlock`、`POST /api/works/{id}/transfer`、`POST /api/witnesses/{id}/editors/revoke`

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`
- `GET /api/works/{id}/collation?user_id=...`

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿。
