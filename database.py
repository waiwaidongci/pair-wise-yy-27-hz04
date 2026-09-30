from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path


class DomainError(ValueError):
    """Business rule violation."""


WITNESS_KINDS = {"version", "fragment", "transcription"}
SPECIAL_TOKENS = {"[缺页]", "[不可辨]", "[残损]", "[插入]", "[删除]"}


def validate_transcription(text: str) -> str:
    text = text.strip()
    if not text:
        raise DomainError("文本不能为空")
    unclosed = text.count("[") - text.count("]")
    if unclosed:
        raise DomainError("校勘标记括号不匹配")
    return text


class CollationDB:
    """SQLite-backed textual collation service with optimistic revisions."""

    def __init__(self, path: str = "collation.db") -> None:
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            yield
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              name TEXT NOT NULL UNIQUE,
              role TEXT NOT NULL CHECK(role IN ('owner','editor','reviewer'))
            );
            CREATE TABLE IF NOT EXISTS works (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              title TEXT NOT NULL,
              description TEXT NOT NULL DEFAULT '',
              owner_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS work_access (
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              permission TEXT NOT NULL CHECK(permission IN ('view','review')),
              PRIMARY KEY(work_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS witnesses (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              siglum TEXT NOT NULL,
              kind TEXT NOT NULL CHECK(kind IN ('version','fragment','transcription')),
              source_note TEXT NOT NULL DEFAULT '',
              missing_sections TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL,
              UNIQUE(work_id,siglum)
            );
            CREATE TABLE IF NOT EXISTS witness_editors (
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              user_id INTEGER NOT NULL REFERENCES users(id),
              granted_by INTEGER NOT NULL REFERENCES users(id),
              PRIMARY KEY(witness_id,user_id)
            );
            CREATE TABLE IF NOT EXISTS passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL,
              base_text TEXT NOT NULL,
              status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','locked')),
              revision INTEGER NOT NULL DEFAULT 0,
              updated_by INTEGER NOT NULL REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(work_id,label)
            );
            CREATE TABLE IF NOT EXISTS alignments (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id) ON DELETE CASCADE,
              aligned_text TEXT NOT NULL,
              sort_order INTEGER NOT NULL,
              note TEXT NOT NULL DEFAULT '',
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id,witness_id)
            );
            CREATE TABLE IF NOT EXISTS variants (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              witness_id INTEGER NOT NULL REFERENCES witnesses(id),
              base_text TEXT NOT NULL,
              proposed_text TEXT NOT NULL,
              reason TEXT NOT NULL,
              layer INTEGER NOT NULL DEFAULT 1,
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS revisions (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              variant_id INTEGER REFERENCES variants(id) ON DELETE CASCADE,
              revision_no INTEGER NOT NULL,
              layer INTEGER NOT NULL,
              snapshot_json TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              UNIQUE(passage_id, revision_no)
            );
            CREATE TABLE IF NOT EXISTS notes (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
              body TEXT NOT NULL,
              author_id INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS passage_locks (
              passage_id INTEGER PRIMARY KEY REFERENCES passages(id) ON DELETE CASCADE,
              locked_by INTEGER NOT NULL REFERENCES users(id),
              reason TEXT NOT NULL DEFAULT '',
              locked_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS collation_drafts (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id) ON DELETE CASCADE,
              label TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','invalid','published')),
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              published_by INTEGER REFERENCES users(id),
              published_at TEXT
            );
            CREATE TABLE IF NOT EXISTS collation_draft_passages (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              draft_id INTEGER NOT NULL REFERENCES collation_drafts(id) ON DELETE CASCADE,
              passage_id INTEGER NOT NULL REFERENCES passages(id) ON DELETE CASCADE,
              cutoff_revision INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','failed')),
              content_json TEXT,
              fail_reason TEXT NOT NULL DEFAULT '',
              version INTEGER NOT NULL DEFAULT 0,
              confirmed_by INTEGER REFERENCES users(id),
              confirmed_at TEXT,
              updated_by INTEGER REFERENCES users(id),
              updated_at TEXT NOT NULL,
              UNIQUE(draft_id,passage_id)
            );
            """
        )
        self.conn.commit()

    def seed_demo(self) -> None:
        if self.conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
            return
        owner = self.add_user("项目负责人", "owner")
        editor = self.add_user("校勘编辑", "editor")
        work = self.create_work("一则残卷", "演示不同版本的校勘", owner)
        w1 = self.add_witness(work, "甲本", "version", "馆藏胶片", "")
        w2 = self.add_witness(work, "乙本", "fragment", "残片转录", "第二句残损")
        self.grant_witness_editor(w2, editor, owner)
        passage = self.add_passage(work, "第1节", "春水东流，故人南去。", owner)
        self.align_passage(passage, w1, "春水东流，故人南去。", 1, owner)
        self.align_passage(passage, w2, "春水东流，[不可辨][不可辨]。", 2, owner)
        variant = self.create_variant(passage, w2, "春水东流，故人南去。", "综合语义与行款补足", owner, 0)
        self.add_note(variant, "补字仍需参照纸背墨迹。", editor)

    def add_user(self, name: str, role: str) -> int:
        if not name.strip() or role not in {"owner", "editor", "reviewer"}:
            raise DomainError("用户名或角色无效")
        with self.transaction():
            try:
                cur = self.conn.execute("INSERT INTO users(name,role) VALUES(?,?)", (name.strip(), role))
            except sqlite3.IntegrityError as exc:
                raise DomainError("用户名已存在") from exc
        return int(cur.lastrowid)

    def create_work(self, title: str, description: str, owner_id: int) -> int:
        owner = self.conn.execute("SELECT role FROM users WHERE id=?", (owner_id,)).fetchone()
        if not owner or owner["role"] != "owner" or not title.strip():
            raise DomainError("作品标题或负责人无效")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO works(title,description,owner_id,created_at) VALUES(?,?,?,?)",
                (title.strip(), description.strip(), owner_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def grant_work_access(self, work_id: int, user_id: int, permission: str, granted_by: int) -> None:
        if permission not in {"view", "review"}:
            raise DomainError("权限必须为 view 或 review")
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT INTO work_access(work_id,user_id,permission) VALUES(?,?,?) "
                "ON CONFLICT(work_id,user_id) DO UPDATE SET permission=excluded.permission",
                (work_id, user_id, permission),
            )

    def _require_owner(self, work_id: int, user_id: int) -> None:
        row = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if not row:
            raise DomainError("只有项目负责人可以执行此操作")

    def can_view_work(self, work_id: int, user_id: int) -> bool:
        return bool(self.conn.execute(
            "SELECT 1 FROM works WHERE id=? AND owner_id=? "
            "UNION ALL SELECT 1 FROM work_access WHERE work_id=? AND user_id=? "
            "UNION ALL SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id, work_id, user_id, work_id, user_id),
        ).fetchone())

    def can_edit_witness(self, witness_id: int, user_id: int) -> bool:
        row = self.conn.execute(
            "SELECT w.work_id,wa.permission FROM witnesses w LEFT JOIN work_access wa ON wa.work_id=w.work_id AND wa.user_id=? WHERE w.id=?",
            (user_id, witness_id),
        ).fetchone()
        if not row:
            return False
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (row["work_id"], user_id)).fetchone()
        editor = self.conn.execute("SELECT 1 FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id)).fetchone()
        return bool(owner or editor)

    def add_witness(self, work_id: int, siglum: str, kind: str, source_note: str = "", missing_sections: str = "") -> int:
        if not self.conn.execute("SELECT 1 FROM works WHERE id=?", (work_id,)).fetchone():
            raise DomainError("作品不存在")
        if not siglum.strip() or kind not in WITNESS_KINDS:
            raise DomainError("版本标识或类型无效")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO witnesses(work_id,siglum,kind,source_note,missing_sections,created_at) VALUES(?,?,?,?,?,?)",
                    (work_id, siglum.strip(), kind, source_note.strip(), missing_sections.strip(), datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("同一作品中的版本标识不能重复") from exc
        return int(cur.lastrowid)

    def grant_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute(
                "INSERT OR IGNORE INTO witness_editors(witness_id,user_id,granted_by) VALUES(?,?,?)",
                (witness_id, user_id, granted_by),
            )

    def add_passage(self, work_id: int, label: str, base_text: str, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        text = validate_transcription(base_text)
        if not label.strip():
            raise DomainError("段落标签不能为空")
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO passages(work_id,label,base_text,updated_by,updated_at) VALUES(?,?,?,?,?)",
                    (work_id, label.strip(), text, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("段落标签已存在") from exc
        return int(cur.lastrowid)

    def align_passage(self, passage_id: int, witness_id: int, aligned_text: str, sort_order: int, user_id: int) -> int:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if sort_order <= 0:
            raise DomainError("排序号必须大于0")
        text = validate_transcription(aligned_text)
        with self.transaction():
            try:
                cur = self.conn.execute(
                    "INSERT INTO alignments(passage_id,witness_id,aligned_text,sort_order,created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (passage_id, witness_id, text, sort_order, user_id, datetime.now().isoformat()),
                )
            except sqlite3.IntegrityError as exc:
                raise DomainError("该版本已经对齐此段落") from exc
        return int(cur.lastrowid)

    def create_variant(self, passage_id: int, witness_id: int, proposed_text: str, reason: str,
                       user_id: int, expected_revision: int) -> int:
        with self.transaction():
            passage, lock = self._editable_passage(passage_id, witness_id, user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            if not self.conn.execute("SELECT 1 FROM alignments WHERE passage_id=? AND witness_id=?", (passage_id, witness_id)).fetchone():
                raise DomainError("该版本尚未对齐此段落")
            cur = self.conn.execute(
                "INSERT INTO variants(passage_id,witness_id,base_text,proposed_text,reason,created_by,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (passage_id, witness_id, passage["base_text"], text, reason.strip(), user_id, datetime.now().isoformat(), datetime.now().isoformat()),
            )
            variant_id = int(cur.lastrowid)
            revision = self._record_revision(passage_id, variant_id, 1, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), passage_id))
        return variant_id

    def update_variant(self, variant_id: int, proposed_text: str, reason: str, user_id: int,
                       expected_revision: int) -> int:
        with self.transaction():
            variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
            if not variant:
                raise DomainError("异文记录不存在")
            passage, _ = self._editable_passage(variant["passage_id"], variant["witness_id"], user_id, expected_revision)
            text = validate_transcription(proposed_text)
            if len(reason.strip()) < 3:
                raise DomainError("取舍理由至少3个字符")
            layer = int(self.conn.execute("SELECT COALESCE(MAX(layer),0)+1 FROM variants WHERE passage_id=? AND witness_id=?", (variant["passage_id"], variant["witness_id"])).fetchone()[0])
            self.conn.execute(
                "UPDATE variants SET proposed_text=?,reason=?,layer=?,updated_at=? WHERE id=?",
                (text, reason.strip(), layer, datetime.now().isoformat(), variant_id),
            )
            revision = self._record_revision(variant["passage_id"], variant_id, layer, user_id)
            self.conn.execute("UPDATE passages SET revision=?,updated_by=?,updated_at=? WHERE id=?", (revision, user_id, datetime.now().isoformat(), variant["passage_id"]))
        return revision

    def _editable_passage(self, passage_id: int, witness_id: int, user_id: int, expected_revision: int):
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        witness = self.conn.execute("SELECT * FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not passage or not witness or passage["work_id"] != witness["work_id"]:
            raise DomainError("段落与版本不属于同一作品")
        if passage["status"] == "locked" or self.conn.execute("SELECT 1 FROM passage_locks WHERE passage_id=?", (passage_id,)).fetchone():
            raise DomainError("段落已锁定，不能修改")
        if not self.can_edit_witness(witness_id, user_id):
            raise DomainError("无权编辑该版本")
        if passage["revision"] != expected_revision:
            raise DomainError(f"版本冲突：当前修订为 {passage['revision']}，提交基于 {expected_revision}")
        return passage, None

    def _record_revision(self, passage_id: int, variant_id: int, layer: int, user_id: int) -> int:
        revision = int(self.conn.execute("SELECT COALESCE(MAX(revision_no),0)+1 FROM revisions WHERE passage_id=?", (passage_id,)).fetchone()[0])
        snapshot = {
            "passage": dict(self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()),
            "variant": dict(self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()),
            "alignments": [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id WHERE a.passage_id=? ORDER BY a.sort_order",
                (passage_id,),
            ).fetchall()],
        }
        self.conn.execute(
            "INSERT INTO revisions(passage_id,variant_id,revision_no,layer,snapshot_json,author_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (passage_id, variant_id, revision, layer, json.dumps(snapshot, ensure_ascii=False), user_id, datetime.now().isoformat()),
        )
        return revision

    def add_note(self, variant_id: int, body: str, author_id: int) -> int:
        variant = self.conn.execute("SELECT * FROM variants WHERE id=?", (variant_id,)).fetchone()
        if not variant or not self.can_view_work(
            self.conn.execute("SELECT work_id FROM passages WHERE id=?", (variant["passage_id"],)).fetchone()["work_id"], author_id
        ):
            raise DomainError("异文不存在或无权评论")
        if not body.strip():
            raise DomainError("注释不能为空")
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO notes(variant_id,body,author_id,created_at) VALUES(?,?,?,?)",
                (variant_id, body.strip(), author_id, datetime.now().isoformat()),
            )
        return int(cur.lastrowid)

    def lock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )
            self._invalidate_open_drafts(passage["work_id"])

    def unlock_passage(self, passage_id: int, user_id: int, reason: str = "") -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='open',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute("DELETE FROM passage_locks WHERE passage_id=?", (passage_id,))
            self._invalidate_open_drafts(passage["work_id"])

    def _invalidate_open_drafts(self, work_id: int) -> None:
        """作废该作品下所有未发布的候选稿；已发布版本保留不动。"""
        self.conn.execute(
            "UPDATE collation_drafts SET status='invalid' WHERE work_id=? AND status='draft'",
            (work_id,),
        )

    def change_work_owner(self, work_id: int, new_owner_id: int, user_id: int) -> None:
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        self._require_owner(work_id, user_id)
        new_owner = self.conn.execute("SELECT * FROM users WHERE id=?", (new_owner_id,)).fetchone()
        if not new_owner or new_owner["role"] != "owner":
            raise DomainError("新负责人不存在或角色不符")
        with self.transaction():
            self.conn.execute("UPDATE works SET owner_id=? WHERE id=?", (new_owner_id, work_id))
            self._invalidate_open_drafts(work_id)

    def revoke_work_access(self, work_id: int, user_id: int, granted_by: int) -> None:
        self._require_owner(work_id, granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute("DELETE FROM work_access WHERE work_id=? AND user_id=?", (work_id, user_id))
            self._invalidate_open_drafts(work_id)

    def revoke_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        if not self.conn.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
            raise DomainError("用户不存在")
        with self.transaction():
            self.conn.execute("DELETE FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id))
            self._invalidate_open_drafts(witness["work_id"])

    def get_snapshot(self, passage_id: int, revision_no: int, user_id: int) -> dict:
        passage = self.conn.execute("SELECT work_id FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage or not self.can_view_work(passage["work_id"], user_id):
            raise DomainError("无权查看该快照")
        row = self.conn.execute("SELECT * FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, revision_no)).fetchone()
        if not row:
            raise DomainError("快照不存在")
        return {"revision_no": row["revision_no"], "layer": row["layer"], "created_at": row["created_at"], "snapshot": json.loads(row["snapshot_json"])}

    def export_collation(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该校勘项目")
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        witnesses = [dict(r) for r in self.conn.execute("SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (work_id,))]
        passages = []
        gaps = 0
        for passage in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall():
            alignments = []
            for row in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind,w.missing_sections FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage["id"],)
            ).fetchall():
                item = dict(row)
                if "[缺页]" in item["aligned_text"] or "[残损]" in item["aligned_text"]:
                    item["has_gap"] = True
                    gaps += 1
                alignments.append(item)
            variants = []
            for row in self.conn.execute("SELECT * FROM variants WHERE passage_id=? ORDER BY witness_id,layer,id", (passage["id"],)).fetchall():
                variant = dict(row)
                variant["notes"] = [dict(r) for r in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (row["id"],))]
                variants.append(variant)
            passages.append({**dict(passage), "alignments": alignments, "variants": variants})
        return {"work": dict(work), "witnesses": witnesses, "passages": passages, "gap_count": gaps}

    # ---- 合校发布：可恢复的合校稿 ----

    def _can_confirm_draft(self, work_id: int, user_id: int) -> bool:
        owner = self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone()
        if owner:
            return True
        editor = self.conn.execute(
            "SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id "
            "WHERE w.work_id=? AND e.user_id=? LIMIT 1", (work_id, user_id),
        ).fetchone()
        return bool(editor)

    def create_collation_draft(self, work_id: int, label: str, cutoffs: dict | None, user_id: int) -> int:
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        self._require_owner(work_id, user_id)
        label = label.strip() or "合校稿"
        cutoffs = cutoffs or {}
        if not isinstance(cutoffs, dict):
            raise DomainError("截止修订必须是段落号到修订号的映射")
        passages = [dict(r) for r in self.conn.execute("SELECT * FROM passages WHERE work_id=? ORDER BY id", (work_id,))]
        if not passages:
            raise DomainError("作品还没有段落")
        # 归一化截止修订映射，并校验段落归属
        cutoff_map: dict[int, int] = {}
        for raw_pid, raw_rev in cutoffs.items():
            pid = int(raw_pid)
            if not any(p["id"] == pid for p in passages):
                raise DomainError(f"段落 {pid} 不属于该作品")
            rev = int(raw_rev)
            if rev < 0:
                raise DomainError(f"段落 {pid} 的截止修订不能为负")
            cutoff_map[pid] = rev
        now = datetime.now().isoformat()
        with self.transaction():
            cur = self.conn.execute(
                "INSERT INTO collation_drafts(work_id,label,status,created_by,created_at) VALUES(?,?, 'draft',?,?)",
                (work_id, label, user_id, now),
            )
            draft_id = int(cur.lastrowid)
            for p in passages:
                rev = cutoff_map.get(p["id"], int(p["revision"]))
                self.conn.execute(
                    "INSERT INTO collation_draft_passages(draft_id,passage_id,cutoff_revision,status,version,updated_at) "
                    "VALUES(?,?,?, 'pending',0,?)",
                    (draft_id, p["id"], rev, now),
                )
        self._generate_draft(draft_id)
        return draft_id

    def _assemble_passage_at_revision(self, passage_id: int, cutoff_revision: int) -> dict:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        if cutoff_revision > int(passage["revision"]):
            raise DomainError(f"截止修订 {cutoff_revision} 晚于当前修订 {passage['revision']}")
        alignments_sql = (
            "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
            "WHERE a.passage_id=? ORDER BY a.sort_order"
        )
        if cutoff_revision == 0:
            # 尚无修订：取当前段落与对齐，异文为空
            return {
                "passage": dict(passage),
                "alignments": [dict(a) for a in self.conn.execute(alignments_sql, (passage_id,))],
                "variants": [],
            }
        if not self.conn.execute("SELECT 1 FROM revisions WHERE passage_id=? AND revision_no=?", (passage_id, cutoff_revision)).fetchone():
            raise DomainError(f"截止修订 {cutoff_revision} 不存在")
        # 回放截止修订（含）之前的所有修订，还原该时点的段落状态
        variants: dict[int, dict] = {}
        alignments: list[dict] = []
        passage_state: dict | None = None
        for r in self.conn.execute(
            "SELECT * FROM revisions WHERE passage_id=? AND revision_no<=? ORDER BY revision_no",
            (passage_id, cutoff_revision),
        ).fetchall():
            snap = json.loads(r["snapshot_json"])
            variants[int(snap["variant"]["id"])] = snap["variant"]
            alignments = snap["alignments"]
            passage_state = snap["passage"]
        variant_list = []
        for v in variants.values():
            v = dict(v)
            v["notes"] = [dict(n) for n in self.conn.execute(
                "SELECT * FROM notes WHERE variant_id=? ORDER BY id", (v["id"],),
            )]
            variant_list.append(v)
        return {"passage": passage_state, "alignments": alignments, "variants": variant_list}

    def _generate_draft(self, draft_id: int) -> None:
        """逐段生成合校内容；已完成段落不重做，失败段落标记 failed 等待重试。"""
        rows = self.conn.execute(
            "SELECT * FROM collation_draft_passages WHERE draft_id=? AND status!='done' ORDER BY id",
            (draft_id,),
        ).fetchall()
        now = datetime.now().isoformat()
        for row in rows:
            try:
                content = self._assemble_passage_at_revision(row["passage_id"], row["cutoff_revision"])
                content_json = json.dumps(content, ensure_ascii=False)
                with self.transaction():
                    self.conn.execute(
                        "UPDATE collation_draft_passages SET status='done',content_json=?,fail_reason='',updated_at=? WHERE id=?",
                        (content_json, now, row["id"]),
                    )
            except DomainError as exc:
                with self.transaction():
                    self.conn.execute(
                        "UPDATE collation_draft_passages SET status='failed',fail_reason=?,updated_at=? WHERE id=?",
                        (str(exc), now, row["id"]),
                    )

    def retry_draft_passage(self, draft_id: int, passage_id: int, user_id: int) -> dict:
        draft = self.conn.execute("SELECT * FROM collation_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("合校稿不存在")
        if draft["status"] == "published":
            raise DomainError("已发布版本不能修改")
        if draft["status"] == "invalid":
            raise DomainError("合校稿已作废，请重新创建")
        if not self.can_view_work(draft["work_id"], user_id):
            raise DomainError("无权操作该合校稿")
        row = self.conn.execute(
            "SELECT * FROM collation_draft_passages WHERE draft_id=? AND passage_id=?",
            (draft_id, passage_id),
        ).fetchone()
        if not row:
            raise DomainError("段落不在合校稿中")
        if row["status"] == "done":
            raise DomainError("段落已完成，无需重试")
        now = datetime.now().isoformat()
        try:
            content = self._assemble_passage_at_revision(passage_id, row["cutoff_revision"])
        except DomainError as exc:
            with self.transaction():
                self.conn.execute(
                    "UPDATE collation_draft_passages SET status='failed',fail_reason=?,updated_at=? WHERE id=?",
                    (str(exc), now, row["id"]),
                )
            raise
        with self.transaction():
            self.conn.execute(
                "UPDATE collation_draft_passages SET status='done',content_json=?,fail_reason='',updated_at=? WHERE id=?",
                (json.dumps(content, ensure_ascii=False), now, row["id"]),
            )
        return {"ok": True, "status": "done"}

    def update_draft_passage_cutoff(self, draft_id: int, passage_id: int, cutoff_revision: int, user_id: int) -> dict:
        draft = self.conn.execute("SELECT * FROM collation_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("合校稿不存在")
        if draft["status"] == "published":
            raise DomainError("已发布版本不能修改")
        self._require_owner(draft["work_id"], user_id)
        row = self.conn.execute(
            "SELECT * FROM collation_draft_passages WHERE draft_id=? AND passage_id=?",
            (draft_id, passage_id),
        ).fetchone()
        if not row:
            raise DomainError("段落不在合校稿中")
        cutoff_revision = int(cutoff_revision)
        if cutoff_revision < 0:
            raise DomainError("截止修订不能为负")
        now = datetime.now().isoformat()
        with self.transaction():
            self.conn.execute(
                "UPDATE collation_draft_passages SET cutoff_revision=?,status='pending',content_json=NULL,"
                "fail_reason='',version=0,confirmed_by=NULL,confirmed_at=NULL,updated_at=? WHERE id=?",
                (cutoff_revision, now, row["id"]),
            )
        self._generate_draft(draft_id)
        return {"ok": True}

    def confirm_draft_passage(self, draft_id: int, passage_id: int, user_id: int,
                              expected_version: int, content: dict | None = None) -> dict:
        """编辑提交段落：先到者写入，晚到者凭版本冲突重新确认。"""
        draft = self.conn.execute("SELECT * FROM collation_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("合校稿不存在")
        if draft["status"] == "published":
            raise DomainError("已发布版本不能修改")
        if draft["status"] == "invalid":
            raise DomainError("合校稿已作废，不能提交")
        if not self._can_confirm_draft(draft["work_id"], user_id):
            raise DomainError("无权提交该段落")
        row = self.conn.execute(
            "SELECT * FROM collation_draft_passages WHERE draft_id=? AND passage_id=?",
            (draft_id, passage_id),
        ).fetchone()
        if not row:
            raise DomainError("段落不在合校稿中")
        if row["status"] != "done":
            raise DomainError("段落尚未生成，不能确认")
        content_json = None
        if content is not None:
            if not isinstance(content, dict):
                raise DomainError("提交内容必须是对象")
            content_json = json.dumps(content, ensure_ascii=False)
        now = datetime.now().isoformat()
        with self.transaction():
            if content_json is not None:
                cur = self.conn.execute(
                    "UPDATE collation_draft_passages SET version=version+1,content_json=?,confirmed_by=?,"
                    "confirmed_at=?,updated_by=?,updated_at=? WHERE draft_id=? AND passage_id=? AND version=?",
                    (content_json, user_id, now, user_id, now, draft_id, passage_id, expected_version),
                )
            else:
                cur = self.conn.execute(
                    "UPDATE collation_draft_passages SET version=version+1,confirmed_by=?,"
                    "confirmed_at=?,updated_by=?,updated_at=? WHERE draft_id=? AND passage_id=? AND version=?",
                    (user_id, now, user_id, now, draft_id, passage_id, expected_version),
                )
            if cur.rowcount == 0:
                raise DomainError("版本冲突：段落已被其他编辑提交，请重新确认")
        new_row = self.conn.execute(
            "SELECT version FROM collation_draft_passages WHERE draft_id=? AND passage_id=?",
            (draft_id, passage_id),
        ).fetchone()
        return {"ok": True, "version": int(new_row["version"])}

    def publish_draft(self, draft_id: int, user_id: int) -> dict:
        draft = self.conn.execute("SELECT * FROM collation_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("合校稿不存在")
        if draft["status"] != "draft":
            raise DomainError("只有候选稿可以发布")
        self._require_owner(draft["work_id"], user_id)
        pending = int(self.conn.execute(
            "SELECT COUNT(*) FROM collation_draft_passages WHERE draft_id=? AND status!='done'",
            (draft_id,),
        ).fetchone()[0])
        if pending:
            raise DomainError(f"仍有 {pending} 个段落未完成，不能发布")
        now = datetime.now().isoformat()
        with self.transaction():
            self.conn.execute(
                "UPDATE collation_drafts SET status='published',published_by=?,published_at=? WHERE id=?",
                (user_id, now, draft_id),
            )
        return {"ok": True, "status": "published"}

    def get_collation_draft(self, draft_id: int, user_id: int) -> dict:
        draft = self.conn.execute("SELECT * FROM collation_drafts WHERE id=?", (draft_id,)).fetchone()
        if not draft:
            raise DomainError("合校稿不存在")
        if not self.can_view_work(draft["work_id"], user_id):
            raise DomainError("无权查看该合校稿")
        passages = [dict(r) for r in self.conn.execute(
            "SELECT * FROM collation_draft_passages WHERE draft_id=? ORDER BY id", (draft_id,),
        )]
        result = dict(draft)
        result["passages"] = passages
        return result

    def list_collation_drafts(self, work_id: int, user_id: int) -> list[dict]:
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该作品的合校稿")
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM collation_drafts WHERE work_id=? ORDER BY id DESC", (work_id,),
        ).fetchall()]

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
            "collation_drafts": [dict(r) for r in self.conn.execute("SELECT * FROM collation_drafts ORDER BY id")],
            "collation_draft_passages": [dict(r) for r in self.conn.execute("SELECT * FROM collation_draft_passages ORDER BY id")],
        }
