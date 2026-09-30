import os, sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import CollationDB, DomainError


class ReleaseFlowTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        self.db = CollationDB(self.path)
        self.owner = self.db.add_user("负责人", "owner")
        self.owner2 = self.db.add_user("接任负责人", "owner")
        self.ed1 = self.db.add_user("编辑甲", "editor")
        self.ed2 = self.db.add_user("编辑乙", "editor")
        self.reviewer = self.db.add_user("审阅", "reviewer")
        self.work = self.db.create_work("残卷", "合校发布测试", self.owner)
        self.w1 = self.db.add_witness(self.work, "甲本", "version")
        self.w2 = self.db.add_witness(self.work, "乙本", "fragment")
        self.db.grant_witness_editor(self.w1, self.ed1, self.owner)
        self.db.grant_witness_editor(self.w2, self.ed2, self.owner)
        self.db.grant_work_access(self.work, self.reviewer, "view", self.owner)
        self.p1 = self.db.add_passage(self.work, "第一节", "春水东流，故人南去。", self.owner)
        self.p2 = self.db.add_passage(self.work, "第二节", "秋风西起。", self.owner)
        self.db.align_passage(self.p1, self.w1, "春水东流，故人南去。", 1, self.ed1)
        self.db.align_passage(self.p1, self.w2, "春水东流，[缺页]", 2, self.ed2)
        self.db.align_passage(self.p2, self.w1, "秋风西起。", 1, self.ed1)
        self.v1 = self.db.create_variant(self.p1, self.w2, "春水东流，故人南去。", "按语义补足缺页", self.ed2, 0)
        self.rev1 = self.db.update_variant(self.v1, "春水东流，[不可辨]人南去。", "墨迹受损改存疑", self.ed2, 1)

    def tearDown(self):
        self.db.close(); os.unlink(self.path)

    def _make_release(self, cutoff=2):
        return self.db.create_release(self.work, cutoff, self.owner)

    def test_full_publish_freezes_manifest_and_reviewer_readonly(self):
        rid = self._make_release()
        # 审阅者发布前只能查看，不能提交
        with self.assertRaisesRegex(DomainError, "无权提交"):
            self.db.submit_release_item(rid, self.p1, self.reviewer)
        view = self.db.get_release(rid, self.reviewer)
        self.assertEqual("candidate", view["status"])
        self.assertEqual({"pending": 2, "done": 0, "failed": 0}, view["counts"])

        self.db.submit_release_item(rid, self.p1, self.ed1)
        self.db.submit_release_item(rid, self.p2, self.ed2)
        result = self.db.publish_release(rid, self.owner)
        self.assertEqual("published", result["status"])

        published = self.db.get_release(rid, self.reviewer)
        self.assertEqual(2, len(published["manifest"]["passages"]))
        first = next(p for p in published["manifest"]["passages"] if p["passage_id"] == self.p1)
        # 截止修订号 2：异文应落在第 2 层（[不可辨] 文本），缺口统计仍计入 [缺页] 对齐
        self.assertEqual(2, first["variants"][0]["layer"])
        self.assertIn("[不可辨]", first["variants"][0]["proposed_text"])
        self.assertEqual(1, first["gap_count"])
        # 已发布版本保留且不可改
        with self.assertRaisesRegex(DomainError, "已发布"):
            self.db.submit_release_item(rid, self.p1, self.ed1)
        with self.assertRaisesRegex(DomainError, "已发布"):
            self.db.publish_release(rid, self.owner)

    def test_cutoff_uses_earlier_revision(self):
        rid = self._make_release(cutoff=1)
        self.db.submit_release_item(rid, self.p1, self.ed1)
        self.db.submit_release_item(rid, self.p2, self.ed2)
        self.db.publish_release(rid, self.owner)
        manifest = self.db.get_release(rid, self.owner)["manifest"]
        first = next(p for p in manifest["passages"] if p["passage_id"] == self.p1)
        # 截止到修订 1 时，异文仍是初层“补足缺页”文本
        self.assertEqual(1, first["variants"][0]["layer"])
        self.assertIn("故人南去", first["variants"][0]["proposed_text"])

    def test_concurrent_same_paragraph_first_wins_late_reconfirms(self):
        rid = self._make_release()
        outcomes = []
        barrier = threading.Barrier(2)

        def submit(uid):
            barrier.wait()
            try:
                outcomes.append(("ok", uid, self.db.submit_release_item(rid, self.p1, uid)))
            except DomainError as exc:
                outcomes.append(("conflict", uid, str(exc)))

        t1 = threading.Thread(target=submit, args=(self.ed1,))
        t2 = threading.Thread(target=submit, args=(self.ed2,))
        t1.start(); t2.start(); t1.join(); t2.join()

        oks = [o for o in outcomes if o[0] == "ok"]
        conflicts = [o for o in outcomes if o[0] == "conflict"]
        self.assertEqual(1, len(oks)); self.assertEqual(1, len(conflicts))
        winner = oks[0][1]
        self.assertIn("重新提交并携带 confirm", conflicts[0][2])

        # 晚到者带 confirm 重新确认：done 段落不重做，仅记录确认人
        late = self.ed2 if winner == self.ed1 else self.ed1
        again = self.db.submit_release_item(rid, self.p1, late, confirm=True)
        self.assertTrue(again["reconfirmed"])
        item = self.db.get_release(rid, self.owner)["items"][0]
        self.assertEqual(winner, item["submitted_by"])
        self.assertEqual(late, item["reconfirmed_by"])
        self.assertEqual(1, item["attempts"], "已完成段落不得重新组装")

    def test_failed_item_retry_skips_done(self):
        # 新段落无任何对齐：组装失败
        p3 = self.db.add_passage(self.work, "第三节", "孤立无对齐。", self.owner)
        rid = self._make_release()
        self.db.submit_release_item(rid, self.p1, self.ed1)
        failed = self.db.submit_release_item(rid, p3, self.ed2)
        self.assertEqual("failed", failed["status"])
        self.assertIn("尚无任何版本对齐", failed["error"])

        # 全部完成前发布被拒
        with self.assertRaisesRegex(DomainError, "未完成"):
            self.db.publish_release(rid, self.owner)

        # 直接重试失败段落：仍失败，但 done 段落不受影响
        again = self.db.submit_release_item(rid, p3, self.ed2)
        self.assertEqual("failed", again["status"])
        self.assertEqual(2, self.db.get_release(rid, self.owner)["items"][2]["attempts"])

        # 补对齐后批量重试，done 段落不重做
        self.db.align_passage(p3, self.w1, "孤立无对齐。", 1, self.ed1)
        retry = self.db.retry_failed_items(rid, self.ed1)
        self.assertEqual("done", retry["retried"][0]["status"])
        self.db.submit_release_item(rid, self.p2, self.ed2)
        self.db.publish_release(rid, self.owner)
        items = self.db.get_release(rid, self.owner)["items"]
        self.assertTrue(all(i["status"] == "done" for i in items))
        self.assertEqual(1, items[0]["attempts"], "已完成段落重试时不应重做")

    def _assert_void(self, rid, fragment, viewer=None):
        viewer = self.owner if viewer is None else viewer
        with self.assertRaisesRegex(DomainError, "已作废"):
            self.db.submit_release_item(rid, self.p2, self.ed1)
        doc = self.db.get_release(rid, viewer)
        self.assertEqual("void", doc["status"])
        self.assertIn(fragment, doc["void_reason"])

    def test_owner_change_voids_candidate(self):
        rid = self._make_release()
        self.db.submit_release_item(rid, self.p1, self.ed1)
        self.db.transfer_work(self.work, self.owner2, self.owner)
        self._assert_void(rid, "负责人", viewer=self.owner2)
        # 已作废不能发布；新负责人可以另起候选稿
        with self.assertRaisesRegex(DomainError, "已作废"):
            self.db.publish_release(rid, self.owner2)
        rid2 = self.db.create_release(self.work, 2, self.owner2)
        self.assertEqual(2, self.db.get_release(rid2, self.owner2)["seq"])

    def test_editor_revoke_voids_candidate(self):
        rid = self._make_release()
        self.db.revoke_witness_editor(self.w2, self.ed2, self.owner)
        self._assert_void(rid, "授权")

    def test_lock_change_voids_candidate(self):
        rid = self._make_release()
        self.db.lock_passage(self.p2, self.owner, "定稿")
        self._assert_void(rid, "锁定")
        # 解锁同样使新候选稿作废
        rid2 = self.db.create_release(self.work, 2, self.owner)
        self.db.unlock_passage(self.p2, self.owner)
        with self.assertRaisesRegex(DomainError, "已作废"):
            self.db.submit_release_item(rid2, self.p1, self.ed1)

    def test_published_survives_voiding_events(self):
        rid = self._make_release()
        for p in (self.p1, self.p2):
            self.db.submit_release_item(rid, p, self.ed1)
        self.db.publish_release(rid, self.owner)
        self.db.lock_passage(self.p2, self.owner, "定稿")
        self.db.transfer_work(self.work, self.owner2, self.owner)
        doc = self.db.get_release(rid, self.reviewer)
        self.assertEqual("published", doc["status"])
        self.assertTrue(doc["manifest"])
        # 列表中发布稿与作废候选稿共存，历史保留
        listing = self.db.list_releases(self.work, self.reviewer)["releases"]
        self.assertEqual("published", listing[0]["status"])

    def test_cutoff_validation(self):
        with self.assertRaisesRegex(DomainError, "最大修订号"):
            self.db.create_release(self.work, 99, self.owner)
        with self.assertRaisesRegex(DomainError, "只有项目负责人"):
            self.db.create_release(self.work, 2, self.ed1)


if __name__ == "__main__":
    unittest.main()
