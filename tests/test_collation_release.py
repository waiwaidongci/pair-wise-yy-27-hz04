import os, sys, tempfile, unittest, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class CollationReleaseTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = CollationDB(self.path)
        self.owner = self.db.add_user("负责人", "owner")
        self.editor = self.db.add_user("编辑甲", "editor")
        self.editor2 = self.db.add_user("编辑乙", "editor")
        self.reviewer = self.db.add_user("审阅", "reviewer")
        self.outsider = self.db.add_user("外部", "reviewer")
        self.new_owner = self.db.add_user("新负责人", "owner")
        self.work = self.db.create_work("残卷", "合校", self.owner)
        self.w1 = self.db.add_witness(self.work, "甲本", "version")
        self.w2 = self.db.add_witness(self.work, "乙本", "fragment")
        self.db.grant_witness_editor(self.w2, self.editor, self.owner)
        self.db.grant_witness_editor(self.w2, self.editor2, self.owner)
        self.db.grant_work_access(self.work, self.reviewer, "view", self.owner)
        self.p1 = self.db.add_passage(self.work, "第一节", "春水东流。", self.owner)
        self.p2 = self.db.add_passage(self.work, "第二节", "故人南去。", self.owner)
        self.db.align_passage(self.p1, self.w1, "春水东流。", 1, self.owner)
        self.db.align_passage(self.p1, self.w2, "春水东流，[缺页]。", 2, self.editor)
        self.db.align_passage(self.p2, self.w1, "故人南去。", 1, self.owner)
        self.db.align_passage(self.p2, self.w2, "故人[不可辨]去。", 2, self.editor)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _by_passage(self, draft):
        return {p["passage_id"]: p for p in draft["passages"]}

    def test_create_draft_assembles_at_cutoff_revision(self):
        v = self.db.create_variant(self.p1, self.w2, "春水东流，故人南去。", "按义补足", self.editor, 0)
        self.db.update_variant(v, "春水东流，[不可辨]人南去。", "墨迹再校", self.editor, 1)
        draft = self.db.create_collation_draft(self.work, "初校", {self.p1: 1}, self.owner)
        d = self.db.get_collation_draft(draft, self.owner)
        by = self._by_passage(d)
        self.assertEqual("done", by[self.p1]["status"])
        content = json.loads(by[self.p1]["content_json"])
        self.assertEqual(1, content["variants"][0]["layer"])
        self.assertEqual("春水东流，故人南去。", content["variants"][0]["proposed_text"])
        self.assertEqual("done", by[self.p2]["status"])
        self.assertEqual([], json.loads(by[self.p2]["content_json"])["variants"])

    def test_recoverable_retry_and_done_passage_not_redone(self):
        draft = self.db.create_collation_draft(self.work, "初校", {self.p1: 999}, self.owner)
        d = self.db.get_collation_draft(draft, self.owner)
        by = self._by_passage(d)
        self.assertEqual("failed", by[self.p1]["status"])
        self.assertTrue(by[self.p1]["fail_reason"])
        self.assertEqual("done", by[self.p2]["status"])
        # 编辑先确认 p2
        self.db.confirm_draft_passage(draft, self.p2, self.editor, 0)
        # 负责人修正 p1 截止修订后重新生成：已完成的 p2 不应重做
        self.db.update_draft_passage_cutoff(draft, self.p1, 0, self.owner)
        d = self.db.get_collation_draft(draft, self.owner)
        by = self._by_passage(d)
        self.assertEqual("done", by[self.p1]["status"])
        self.assertEqual(1, by[self.p2]["version"])
        self.assertEqual(self.editor, by[self.p2]["confirmed_by"])
        # 已完成段落重试被拒绝
        with self.assertRaisesRegex(DomainError, "已完成"):
            self.db.retry_draft_passage(draft, self.p2, self.owner)

    def test_concurrent_confirm_first_wins_latecomer_reconfirms(self):
        draft = self.db.create_collation_draft(self.work, "初校", {}, self.owner)
        r1 = self.db.confirm_draft_passage(draft, self.p1, self.editor, 0)
        self.assertEqual(1, r1["version"])
        # 晚到者持旧版本提交 -> 冲突，需重新确认
        with self.assertRaisesRegex(DomainError, "版本冲突"):
            self.db.confirm_draft_passage(draft, self.p1, self.editor2, 0)
        r2 = self.db.confirm_draft_passage(draft, self.p1, self.editor2, 1)
        self.assertEqual(2, r2["version"])
        d = self.db.get_collation_draft(draft, self.owner)
        p1 = self._by_passage(d)[self.p1]
        self.assertEqual(2, p1["version"])
        self.assertEqual(self.editor2, p1["confirmed_by"])

    def test_invalidation_on_lock_owner_change_and_revoke(self):
        d1 = self.db.create_collation_draft(self.work, "稿一", {}, self.owner)
        self.db.lock_passage(self.p1, self.owner, "定稿")
        self.assertEqual("invalid", self.db.get_collation_draft(d1, self.owner)["status"])
        self.db.unlock_passage(self.p1, self.owner)

        d2 = self.db.create_collation_draft(self.work, "稿二", {}, self.owner)
        self.db.change_work_owner(self.work, self.new_owner, self.owner)
        self.assertEqual("invalid", self.db.get_collation_draft(d2, self.new_owner)["status"])

        d3 = self.db.create_collation_draft(self.work, "稿三", {}, self.new_owner)
        self.db.revoke_witness_editor(self.w2, self.editor, self.new_owner)
        self.assertEqual("invalid", self.db.get_collation_draft(d3, self.new_owner)["status"])

        d4 = self.db.create_collation_draft(self.work, "稿四", {}, self.new_owner)
        self.db.revoke_work_access(self.work, self.reviewer, self.new_owner)
        self.assertEqual("invalid", self.db.get_collation_draft(d4, self.new_owner)["status"])

    def test_invalid_draft_cannot_be_confirmed_or_published(self):
        draft = self.db.create_collation_draft(self.work, "稿", {}, self.owner)
        self.db.lock_passage(self.p1, self.owner, "锁")
        with self.assertRaisesRegex(DomainError, "作废"):
            self.db.confirm_draft_passage(draft, self.p1, self.editor, 0)
        with self.assertRaisesRegex(DomainError, "只有候选稿"):
            self.db.publish_draft(draft, self.owner)

    def test_publish_requires_all_passages_done(self):
        draft = self.db.create_collation_draft(self.work, "稿", {self.p1: 999}, self.owner)
        with self.assertRaisesRegex(DomainError, "未完成"):
            self.db.publish_draft(draft, self.owner)

    def test_reviewer_view_only_and_published_retained_immutable(self):
        draft = self.db.create_collation_draft(self.work, "定稿", {}, self.owner)
        # 审阅者可查看候选稿
        self.assertEqual("draft", self.db.get_collation_draft(draft, self.reviewer)["status"])
        # 审阅者不能提交
        with self.assertRaisesRegex(DomainError, "无权"):
            self.db.confirm_draft_passage(draft, self.p1, self.reviewer, 0)
        # 外部人员不能查看
        with self.assertRaisesRegex(DomainError, "无权"):
            self.db.get_collation_draft(draft, self.outsider)
        # 非负责人不能发布
        with self.assertRaisesRegex(DomainError, "负责人"):
            self.db.publish_draft(draft, self.editor)
        # 负责人发布
        self.db.publish_draft(draft, self.owner)
        self.assertEqual("published", self.db.get_collation_draft(draft, self.reviewer)["status"])
        # 已发布版本不可修改
        with self.assertRaisesRegex(DomainError, "已发布版本不能修改"):
            self.db.confirm_draft_passage(draft, self.p1, self.editor, 0)
        # 已发布版本保留：锁定不会作废已发布版本
        self.db.lock_passage(self.p1, self.owner, "定稿")
        self.assertEqual("published", self.db.get_collation_draft(draft, self.owner)["status"])
        drafts = self.db.list_collation_drafts(self.work, self.owner)
        self.assertTrue(any(d["id"] == draft and d["status"] == "published" for d in drafts))


if __name__ == "__main__":
    unittest.main()
