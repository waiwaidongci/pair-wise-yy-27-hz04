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

## 合校发布

负责人选定每个段落的截止修订号后生成合校稿：系统按截止修订回放该段落的对齐、异文与注释，逐段组装成稿。

- 可恢复：任一段落组装失败会标记为 `failed` 并记录原因，其余段落不受影响；修正截止修订后可重试，已完成（`done`）段落不会重做。
- 先到者写入：两名编辑提交同一段落时携带 `expected_version`，先提交者写入成功，晚到者收到版本冲突后需重新确认。
- 候选稿作废：负责人换人、编辑授权或审阅授权被撤回、段落锁定状态变化时，该作品下所有未发布候选稿立即作废；已发布版本保留且不受影响。
- 权限：发布前审阅者只能查看合校稿，不能提交；已发布版本只读、不可修改，并保留在版本列表中。

## 主要接口

- `POST /api/users`、`POST /api/works`
- `POST /api/works/{id}/witnesses`、`POST /api/witnesses/{id}/editors`
- `POST /api/works/{id}/passages`、`POST /api/works/{id}/access`
- `POST /api/alignments`
- `POST /api/variants`、`POST /api/variants/{id}/revisions`
- `GET /api/passages/{id}/snapshots/{revision}?user_id=...`
- `POST /api/passages/{id}/lock`、`POST /api/passages/{id}/unlock`
- `POST /api/works/{id}/owner`
- `POST /api/works/{id}/access/revoke`、`POST /api/witnesses/{id}/editors/revoke`
- `GET /api/works/{id}/collation?user_id=...`
- `POST /api/works/{id}/collation-drafts`（body 含 `cutoffs`：段落号→截止修订号）
- `GET /api/works/{id}/collation-drafts?user_id=...`
- `GET /api/collation-drafts/{id}?user_id=...`
- `POST /api/collation-drafts/{id}/publish`
- `POST /api/collation-drafts/{d}/passages/{p}/retry`
- `POST /api/collation-drafts/{d}/passages/{p}/cutoff`
- `POST /api/collation-drafts/{d}/passages/{p}/confirm`（body 含 `expected_version`）

导出接口把版本对齐、异文、注释、残损缺口和锁定状态组合成可复核的校勘稿。
