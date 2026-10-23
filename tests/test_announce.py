# -*- coding: utf-8 -*-
"""交易所/巨潮公告原文核验(announce.py + 战法加权 + 候选审计)测试。"""

import os
import sys
import unittest
from unittest.mock import patch

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for path in (ROOT, os.path.join(ROOT, "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import pandas as pd  # noqa: E402

import quant.strategies.pick as pick  # noqa: E402
from quant.datasource import announce as ann  # noqa: E402
import scripts.quant_web as quant_web  # noqa: E402


class AssessAnnouncementsTest(unittest.TestCase):
    def _items(self, titles):
        return [{"title": t, "date": "2026-10-20", "url": "http://x/"}
                for t in titles]

    def test_blocked_keywords_remove_from_candidates(self):
        for title in ("关于筹划重大资产重组停牌的公告",
                      "关于公司股票可能被终止上市的风险提示公告",
                      "关于收到中国证监会立案调查通知的公告",
                      "关于公司股票被实施退市风险警示(*ST)的公告"):
            result = ann.assess_announcements(self._items([title]))
            self.assertEqual(result["status"], "blocked", title)
            self.assertIn(title[:8], result["summary"])

    def test_resume_and_routine_are_not_blocked(self):
        for title in ("关于公司股票复牌的公告",
                      "股票交易异常波动公告",
                      "关于控股股东增持计划暨取得专项贷款承诺函的公告",
                      "关于完成工商变更登记的公告"):
            result = ann.assess_announcements(self._items([title]))
            self.assertEqual(result["status"], "pass", title)

    def test_warning_keywords_flagged_not_removed(self):
        for title in ("关于控股股东减持股份预披露的公告",
                      "关于收到深交所年报问询函的公告",
                      "2026年半年度业绩预告(预亏)"):
            result = ann.assess_announcements(self._items([title]))
            self.assertEqual(result["status"], "warning", title)
            self.assertEqual(len(result["hits"]), 1)
            self.assertEqual(result["hits"][0]["level"], "warning")

    def test_empty_and_mixed(self):
        self.assertEqual(ann.assess_announcements([])["status"], "pass")
        self.assertEqual(ann.assess_announcements(None)["n"], 0)
        mixed = ann.assess_announcements(self._items([
            "股票交易异常波动公告", "关于收到上交所监管工作函的公告"]))
        self.assertEqual(mixed["status"], "warning")   # 异动不误伤,函件计提示


class FetchAnnouncementsTest(unittest.TestCase):
    def test_fetch_maps_titles_dates_and_pdf_urls(self):
        payload = {
            "keyBoardList": [{"code": "002164", "orgId": "9900003443",
                              "plate": "szse", "delisted": "false"}],
            "totalRecordNum": 1,
            "announcements": [
                {"announcementTitle": "股票交易异常波动公告",
                 "announcementTime": 1791763200000,  # 2026-10-12(东八区)
                 "adjunctUrl": "finalpage/2026-10-12/1225.PDF"}]}
        with patch.object(ann, "_post",
                          side_effect=[{  # orgid 查询
                              "keyBoardList": payload["keyBoardList"]},
                              payload]):
            fetched = ann.fetch_announcements("002164")
        self.assertTrue(fetched["ok"])
        self.assertEqual(fetched["items"][0]["title"], "股票交易异常波动公告")
        self.assertEqual(fetched["items"][0]["date"], "2026-10-12")
        self.assertTrue(fetched["items"][0]["url"].startswith(
            "http://static.cninfo.com.cn/finalpage/"))
        assessed = ann.assess_announcements(fetched["items"])
        self.assertEqual(assessed["status"], "pass")

    def test_fetch_delisted_or_unknown_code_returns_not_ok(self):
        with patch.object(ann, "_post", return_value={"keyBoardList": []}):
            fetched = ann.fetch_announcements("999999")
        self.assertFalse(fetched["ok"])
        self.assertIn("未收录", fetched["error"])


class PickAnnounceWeightTest(unittest.TestCase):
    M3 = pd.DataFrame([
        {"code": "001229", "name": "魅视科技", "grade": "S", "score10": 8.0,
         "evidence": "板块共振", "close": 39.0},
    ])

    def _rows(self, announces):
        rows, env_note = pick.confidence_rows(
            self.M3, None, [], {"score": 44, "label": "谨慎"}, None, set(),
            limit=12, announces=announces)
        return rows, env_note

    def test_blocked_removed_and_counted_in_env_note(self):
        rows, env_note = self._rows({
            "001229": {"status": "blocked", "hits": [{"title": "停牌公告",
                             "level": "blocked"}], "n": 1, "summary": "高风险"}})
        self.assertEqual(rows, [])
        self.assertIn("公告核验剔除 1 只", env_note)

    def test_warning_drops_confidence_by_8_capped_16(self):
        rows, _ = self._rows({
            "001229": {"status": "warning", "hits": [
                {"title": "减持公告", "level": "warning"}], "n": 3,
                "summary": "公告风险提示:减持公告"}})
        # 47 基础 -3 情绪 -8 一条 warning → 36
        self.assertEqual(rows[0]["confidence"], 36)
        self.assertEqual(rows[0]["announce"]["status"], "warning")
        rows, _ = self._rows({
            "001229": {"status": "warning", "hits": [
                {"title": "减持公告", "level": "warning"},
                {"title": "问询函", "level": "warning"},
                {"title": "预亏", "level": "warning"}], "n": 3,
                "summary": "多条"}})
        self.assertEqual(rows[0]["confidence"], 44 - 16)  # 封顶 -16
        rows, _ = self._rows({
            "001229": {"status": "pending", "hits": [], "n": 0,
                       "summary": "巨潮公告核验中"}})
        self.assertEqual(rows[0]["confidence"], 44)        # pending 不扣分
        self.assertEqual(rows[0]["announce"]["summary"], "巨潮公告核验中")


class AuditRowsTest(unittest.TestCase):
    def test_blocked_removed_warning_marked(self):
        rows = [{"code": "600111", "name": "甲", "evidence": "x"},
                {"code": "600222", "name": "乙", "evidence": "y"},
                {"code": "600333", "name": "丙", "evidence": "z"}]

        def fake_audit(code):
            return {
                "600111": {"status": "pass", "hits": [], "n": 2,
                           "summary": "近30日 2 条公告,无高风险项"},
                "600222": {"status": "blocked", "hits": [
                    {"title": "停牌", "level": "blocked"}], "n": 1,
                    "summary": "高风险公告:停牌"},
                "600333": {"status": "warning", "hits": [
                    {"title": "减持", "level": "warning", "url": "http://x/"}],
                    "n": 3, "summary": "公告风险提示:减持"},
            }[str(code).zfill(6)]

        with patch.object(quant_web, "_announce_audit",
                          side_effect=fake_audit):
            kept, blocked = quant_web._audit_rows(rows)
        self.assertEqual(blocked, 1)
        self.assertEqual([r["code"] for r in kept], ["600111", "600333"])
        self.assertIn("巨潮公告提示", kept[1]["evidence"])   # warning 进依据
        self.assertEqual(kept[0]["announce"]["status"], "pass")
        self.assertTrue(kept[1]["announce"]["url"])


class AnnouncementSectionTest(unittest.TestCase):
    def test_section_renders_levels_and_links(self):
        from quant.analysis_view import announcement_section

        blocked = announcement_section({
            "status": "blocked", "n": 1, "source": "巨潮资讯网",
            "summary": "高风险公告:停牌公告",
            "items": [{"title": "关于停牌的公告", "date": "2026-10-20",
                       "url": "http://static.cninfo.com.cn/finalpage/1.PDF"}],
            "hits": [{"title": "关于停牌的公告", "level": "blocked",
                       "url": "http://static.cninfo.com.cn/finalpage/1.PDF"}]})
        self.assertIn("高风险公告", blocked)
        self.assertIn("高风险·已剔除候选", blocked)
        self.assertIn('href="http://static.cninfo.com.cn/', blocked)  # 原文链接

        pending = announcement_section({"status": "pending", "hits": [],
                                        "n": 0, "summary": "核验中"})
        self.assertIn("正在核验", pending)

        unknown = announcement_section({"status": "unknown", "hits": [],
                                        "n": 0, "summary": "巨潮未收录该代码"})
        self.assertIn("未收录", unknown)

        clean = announcement_section({
            "status": "pass", "n": 0, "source": "巨潮资讯网",
            "summary": "近30日 0 条公告,无高风险项", "items": [], "hits": []})
        self.assertIn("近 30 日 0 条公告", clean)
        self.assertIn("无新披露", clean)


if __name__ == "__main__":
    unittest.main()
