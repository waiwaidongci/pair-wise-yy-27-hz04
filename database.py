from __future__ import annotations

import json
import sqlite3
import threading
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
        # 单连接配合线程锁：BEGIN IMMEDIATE 之外再串行化进程内的写事务，
        # 保证两名编辑并发提交同一段落时严格先到者写入
        self._xlock = threading.RLock()
        self.conn.execute("PRAGMA foreign_keys=ON")
        if path != ":memory:":
            self.conn.execute("PRAGMA journal_mode=WAL")
        self._schema()

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self):
        with self._xlock:
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
            CREATE TABLE IF NOT EXISTS releases (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              work_id INTEGER NOT NULL REFERENCES works(id),
              seq INTEGER NOT NULL,
              cutoff_revision INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'candidate' CHECK(status IN ('candidate','published','void')),
              created_by INTEGER NOT NULL REFERENCES users(id),
              created_at TEXT NOT NULL,
              published_at TEXT,
              voided_at TEXT,
              void_reason TEXT NOT NULL DEFAULT '',
              manifest_json TEXT NOT NULL DEFAULT '',
              UNIQUE(work_id,seq)
            );
            CREATE TABLE IF NOT EXISTS release_items (
              release_id INTEGER NOT NULL REFERENCES releases(id),
              passage_id INTEGER NOT NULL REFERENCES passages(id),
              position INTEGER NOT NULL,
              status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','done','failed')),
              attempts INTEGER NOT NULL DEFAULT 0,
              revision_no INTEGER NOT NULL DEFAULT 0,
              content_json TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              submitted_by INTEGER REFERENCES users(id),
              reconfirmed_by INTEGER REFERENCES users(id),
              updated_at TEXT NOT NULL,
              PRIMARY KEY(release_id,passage_id)
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
        if passage["status"] == "locked":
            raise DomainError("段落已处于锁定状态")
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='locked',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute(
                "INSERT OR REPLACE INTO passage_locks(passage_id,locked_by,reason,locked_at) VALUES(?,?,?,?)",
                (passage_id, user_id, reason.strip(), datetime.now().isoformat()),
            )
        self._void_candidates(passage["work_id"], "段落锁定状态发生变化")

    def unlock_passage(self, passage_id: int, user_id: int) -> None:
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        self._require_owner(passage["work_id"], user_id)
        if passage["status"] != "locked":
            raise DomainError("段落未处于锁定状态")
        with self.transaction():
            self.conn.execute("UPDATE passages SET status='open',updated_by=?,updated_at=? WHERE id=?", (user_id, datetime.now().isoformat(), passage_id))
            self.conn.execute("DELETE FROM passage_locks WHERE passage_id=?", (passage_id,))
        self._void_candidates(passage["work_id"], "段落锁定状态发生变化")

    def transfer_work(self, work_id: int, new_owner_id: int, user_id: int) -> None:
        self._require_owner(work_id, user_id)
        new_owner = self.conn.execute("SELECT 1 FROM users WHERE id=? AND role='owner'", (new_owner_id,)).fetchone()
        if not new_owner:
            raise DomainError("新负责人不存在或不是负责人角色")
        with self.transaction():
            self.conn.execute("UPDATE works SET owner_id=? WHERE id=?", (new_owner_id, work_id))
        self._void_candidates(work_id, "项目负责人已更换")

    def revoke_witness_editor(self, witness_id: int, user_id: int, granted_by: int) -> None:
        witness = self.conn.execute("SELECT work_id FROM witnesses WHERE id=?", (witness_id,)).fetchone()
        if not witness:
            raise DomainError("版本不存在")
        self._require_owner(witness["work_id"], granted_by)
        with self.transaction():
            cur = self.conn.execute("DELETE FROM witness_editors WHERE witness_id=? AND user_id=?", (witness_id, user_id))
        if cur.rowcount:
            self._void_candidates(witness["work_id"], "版本编辑授权已撤回")

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

    def snapshot(self) -> dict:
        return {
            "users": [dict(r) for r in self.conn.execute("SELECT id,name,role FROM users ORDER BY id")],
            "works": [dict(r) for r in self.conn.execute("SELECT * FROM works ORDER BY id")],
            "witnesses": [dict(r) for r in self.conn.execute("SELECT * FROM witnesses ORDER BY id")],
            "passages": [dict(r) for r in self.conn.execute("SELECT * FROM passages ORDER BY id")],
        }

    # ------------------------------------------------------------------
    # 可恢复的合校发布
    # ------------------------------------------------------------------

    def _void_candidates(self, work_id: int, reason: str) -> None:
        with self.transaction():
            self.conn.execute(
                "UPDATE releases SET status='void',voided_at=?,void_reason=? WHERE work_id=? AND status='candidate'",
                (datetime.now().isoformat(), reason, work_id),
            )

    def create_release(self, work_id: int, cutoff_revision: int, user_id: int) -> int:
        self._require_owner(work_id, user_id)
        work = self.conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
        if not work:
            raise DomainError("作品不存在")
        if not isinstance(cutoff_revision, int) or cutoff_revision < 0:
            raise DomainError("截止修订号必须是不小于0的整数")
        max_rev = int(self.conn.execute(
            "SELECT COALESCE(MAX(revision_no),0) FROM revisions r JOIN passages p ON p.id=r.passage_id WHERE p.work_id=?",
            (work_id,),
        ).fetchone()[0])
        if cutoff_revision > max_rev:
            raise DomainError(f"截止修订号不能超过当前最大修订号 {max_rev}")
        with self.transaction():
            active = self.conn.execute("SELECT 1 FROM releases WHERE work_id=? AND status='candidate'", (work_id,)).fetchone()
            if active:
                raise DomainError("该作品已有未发布的候选稿，请先发布或作废")
            seq = int(self.conn.execute("SELECT COALESCE(MAX(seq),0)+1 FROM releases WHERE work_id=?", (work_id,)).fetchone()[0])
            now = datetime.now().isoformat()
            cur = self.conn.execute(
                "INSERT INTO releases(work_id,seq,cutoff_revision,created_by,created_at) VALUES(?,?,?,?,?)",
                (work_id, seq, cutoff_revision, user_id, now),
            )
            release_id = int(cur.lastrowid)
            rows = self.conn.execute("SELECT id FROM passages WHERE work_id=? ORDER BY id", (work_id,)).fetchall()
            if not rows:
                raise DomainError("作品还没有段落，无法生成合校稿")
            for position, row in enumerate(rows, 1):
                self.conn.execute(
                    "INSERT INTO release_items(release_id,passage_id,position,updated_at) VALUES(?,?,?,?)",
                    (release_id, row["id"], position, now),
                )
        return release_id

    def _release_row(self, release_id: int):
        release = self.conn.execute("SELECT * FROM releases WHERE id=?", (release_id,)).fetchone()
        if not release:
            raise DomainError("合校稿不存在")
        return release

    def _can_contribute(self, work_id: int, user_id: int) -> bool:
        if self.conn.execute("SELECT 1 FROM works WHERE id=? AND owner_id=?", (work_id, user_id)).fetchone():
            return True
        return bool(self.conn.execute(
            "SELECT 1 FROM witnesses w JOIN witness_editors e ON e.witness_id=w.id WHERE w.work_id=? AND e.user_id=? LIMIT 1",
            (work_id, user_id),
        ).fetchone())

    def _assemble_item(self, passage_id: int, cutoff_revision: int) -> dict:
        """按截止修订号重建一个段落的合校片段；无对齐或缺口标记非法时失败。

        异文旧层在活表中会被覆盖，因此截止点的异文状态从各修订快照中
        按 variant 取截止前最近一条；注释为追加型数据，直接取活表。
        """
        passage = self.conn.execute("SELECT * FROM passages WHERE id=?", (passage_id,)).fetchone()
        if not passage:
            raise DomainError("段落不存在")
        chosen = self.conn.execute(
            "SELECT * FROM revisions WHERE passage_id=? AND revision_no<=? ORDER BY revision_no DESC LIMIT 1",
            (passage_id, cutoff_revision),
        ).fetchone()
        revision_no = chosen["revision_no"] if chosen else 0
        if chosen:
            snapshot = json.loads(chosen["snapshot_json"])
            base_text = snapshot["passage"]["base_text"]
            alignments = snapshot["alignments"]
        else:
            base_text = passage["base_text"]
            alignments = [dict(r) for r in self.conn.execute(
                "SELECT a.*,w.siglum,w.kind FROM alignments a JOIN witnesses w ON w.id=a.witness_id "
                "WHERE a.passage_id=? ORDER BY a.sort_order", (passage_id,)
            ).fetchall()]
        if not alignments:
            raise DomainError("该段落尚无任何版本对齐，无法合校")
        align_out, gaps = [], 0
        for a in alignments:
            text = a["aligned_text"]
            has_gap = ("[缺页]" in text or "[残损]" in text)
            if has_gap:
                gaps += 1
            align_out.append({
                "witness_id": a["witness_id"], "siglum": a["siglum"], "kind": a["kind"],
                "aligned_text": text, "sort_order": a["sort_order"], "has_gap": has_gap,
            })
        # 截止点异文：每个 variant 取截止前最近一次快照状态
        variant_states: dict = {}
        if chosen:
            for rev in self.conn.execute(
                "SELECT variant_id,snapshot_json FROM revisions WHERE passage_id=? AND revision_no<=? "
                "AND variant_id IS NOT NULL ORDER BY revision_no",
                (passage_id, cutoff_revision),
            ).fetchall():
                snap = json.loads(rev["snapshot_json"])
                if snap.get("variant"):
                    variant_states[rev["variant_id"]] = snap["variant"]
        else:
            for v in self.conn.execute("SELECT * FROM variants WHERE passage_id=?", (passage_id,)).fetchall():
                variant_states[v["id"]] = dict(v)
        variants = []
        for v in sorted(variant_states.values(), key=lambda x: (x["witness_id"], x["layer"], x["id"])):
            variant = {k: v[k] for k in ("id", "witness_id", "base_text", "proposed_text", "reason", "layer")}
            variant["notes"] = [
                {"id": n["id"], "body": n["body"], "author_id": n["author_id"], "created_at": n["created_at"]}
                for n in self.conn.execute("SELECT * FROM notes WHERE variant_id=? ORDER BY id", (v["id"],)).fetchall()
            ]
            variants.append(variant)
        return {
            "passage_id": passage_id,
            "label": passage["label"],
            "base_text": base_text,
            "revision_no": revision_no,
            "alignments": align_out,
            "variants": variants,
            "gap_count": gaps,
        }

    def submit_release_item(self, release_id: int, passage_id: int, user_id: int, confirm: bool = False) -> dict:
        release = self._release_row(release_id)
        if release["status"] == "void":
            raise DomainError("候选稿已作废，请重新发起合校")
        if release["status"] == "published":
            raise DomainError("合校稿已发布，段落内容不可更改")
        if not self._can_contribute(release["work_id"], user_id):
            raise DomainError("无权提交合校段落（需要负责人或版本编辑身份）")
        with self.transaction():
            item = self.conn.execute(
                "SELECT * FROM release_items WHERE release_id=? AND passage_id=?", (release_id, passage_id)
            ).fetchone()
            if not item:
                raise DomainError("该段落不属于此合校稿")
            if item["status"] == "done":
                if not confirm:
                    raise DomainError(
                        f"该段落已由编辑 {item['submitted_by']} 先完成；如确认沿用其结果，请重新提交并携带 confirm=true"
                    )
                self.conn.execute(
                    "UPDATE release_items SET reconfirmed_by=?,updated_at=? WHERE release_id=? AND passage_id=?",
                    (user_id, datetime.now().isoformat(), release_id, passage_id),
                )
                return {"release_id": release_id, "passage_id": passage_id,
                        "status": "done", "reconfirmed": True,
                        "submitted_by": item["submitted_by"]}
            # pending / failed 都允许尝试；done 不会走到这里，因此已完成段落绝不重做
            cur = self.conn.execute(
                "UPDATE release_items SET attempts=attempts+1,updated_at=? "
                "WHERE release_id=? AND passage_id=? AND status!='done'",
                (datetime.now().isoformat(), release_id, passage_id),
            )
            if cur.rowcount == 0:
                raise DomainError("该段落刚被其他编辑完成，请重新确认")
            try:
                content = self._assemble_item(passage_id, int(release["cutoff_revision"]))
            except DomainError as exc:
                self.conn.execute(
                    "UPDATE release_items SET status='failed',error=?,submitted_by=?,updated_at=? "
                    "WHERE release_id=? AND passage_id=?",
                    (str(exc), user_id, datetime.now().isoformat(), release_id, passage_id),
                )
                return {"release_id": release_id, "passage_id": passage_id,
                        "status": "failed", "error": str(exc), "attempts": item["attempts"] + 1}
            self.conn.execute(
                "UPDATE release_items SET status='done',revision_no=?,content_json=?,error='',"
                "submitted_by=?,reconfirmed_by=NULL,updated_at=? WHERE release_id=? AND passage_id=?",
                (content["revision_no"], json.dumps(content, ensure_ascii=False), user_id,
                 datetime.now().isoformat(), release_id, passage_id),
            )
        return {"release_id": release_id, "passage_id": passage_id, "status": "done"}

    def retry_failed_items(self, release_id: int, user_id: int) -> dict:
        release = self._release_row(release_id)
        if release["status"] != "candidate":
            raise DomainError("只有候选稿可以重试失败段落")
        if not self._can_contribute(release["work_id"], user_id):
            raise DomainError("无权提交合校段落")
        results = []
        failed = self.conn.execute(
            "SELECT passage_id FROM release_items WHERE release_id=? AND status='failed' ORDER BY position",
            (release_id,),
        ).fetchall()
        for row in failed:
            results.append(self.submit_release_item(release_id, row["passage_id"], user_id))
        return {"release_id": release_id, "retried": results}

    def publish_release(self, release_id: int, user_id: int) -> dict:
        release = self._release_row(release_id)
        self._require_owner(release["work_id"], user_id)
        if release["status"] == "void":
            raise DomainError("候选稿已作废，不能发布")
        if release["status"] == "published":
            raise DomainError("合校稿已发布")
        with self.transaction():
            pending = self.conn.execute(
                "SELECT COUNT(*) FROM release_items WHERE release_id=? AND status!='done'", (release_id,)
            ).fetchone()[0]
            if pending:
                raise DomainError(f"还有 {pending} 个段落未完成（含失败），不能发布")
            manifest = self._build_manifest(release_id)
            now = datetime.now().isoformat()
            self.conn.execute(
                "UPDATE releases SET status='published',published_at=?,manifest_json=? WHERE id=?",
                (now, json.dumps(manifest, ensure_ascii=False), release_id),
            )
        return {"release_id": release_id, "status": "published", "published_at": now}

    def _build_manifest(self, release_id: int) -> dict:
        release = self._release_row(release_id)
        work = dict(self.conn.execute("SELECT * FROM works WHERE id=?", (release["work_id"],)).fetchone())
        witnesses = [dict(r) for r in self.conn.execute(
            "SELECT * FROM witnesses WHERE work_id=? ORDER BY id", (release["work_id"],))]
        items, gaps = [], 0
        for row in self.conn.execute(
            "SELECT * FROM release_items WHERE release_id=? ORDER BY position", (release_id,)
        ).fetchall():
            content = json.loads(row["content_json"])
            gaps += content.get("gap_count", 0)
            items.append(content)
        return {"cutoff_revision": release["cutoff_revision"], "work": work,
                "witnesses": witnesses, "passages": items, "gap_count": gaps}

    def get_release(self, release_id: int, user_id: int) -> dict:
        release = self._release_row(release_id)
        if not self.can_view_work(release["work_id"], user_id):
            raise DomainError("无权查看该合校稿")
        items = []
        for row in self.conn.execute(
            "SELECT passage_id,position,status,attempts,revision_no,error,submitted_by,reconfirmed_by,updated_at "
            "FROM release_items WHERE release_id=? ORDER BY position", (release_id,)
        ).fetchall():
            item = dict(row)
            if row["status"] == "done":
                item["content"] = json.loads(
                    self.conn.execute("SELECT content_json FROM release_items WHERE release_id=? AND passage_id=?",
                                      (release_id, row["passage_id"])).fetchone()["content_json"]
                )
            items.append(item)
        result = {k: release[k] for k in ("id", "work_id", "seq", "cutoff_revision", "status",
                                          "created_by", "created_at", "published_at", "voided_at", "void_reason")}
        result["items"] = items
        counts = {"pending": 0, "done": 0, "failed": 0}
        for item in items:
            counts[item["status"]] += 1
        result["counts"] = counts
        if release["status"] == "published":
            result["manifest"] = json.loads(release["manifest_json"])
        return result

    def list_releases(self, work_id: int, user_id: int) -> dict:
        if not self.can_view_work(work_id, user_id):
            raise DomainError("无权查看该合校项目")
        rows = self.conn.execute(
            "SELECT id,seq,cutoff_revision,status,created_at,published_at,void_reason FROM releases "
            "WHERE work_id=? ORDER BY seq", (work_id,)
        ).fetchall()
        return {"work_id": work_id, "releases": [dict(r) for r in rows]}
