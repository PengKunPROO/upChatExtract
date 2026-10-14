"""tests/test_feishu_app.py — 飞书应用版纯逻辑测试（不依赖 lark-oapi）"""
import os
import tempfile
import unittest
from unittest.mock import patch

import feishu_app_bot as app


def _entry(rpid, mid, uname, ctime, msg, parent="", is_up=False):
    return {"rpid": rpid, "mid": mid, "uname": uname, "ctime": ctime,
            "message": msg, "parent": parent, "is_up": is_up}


class TestAppConfig(unittest.TestCase):
    def test_placeholder_raises(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write("app_id: \"<x>\"\napp_secret: \"s\"\nchat_id: \"c\"\n")
            p = f.name
        try:
            with self.assertRaises(app.AppConfigError):
                app.load_app_config(p)
        finally:
            os.unlink(p)

    def test_valid(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8") as f:
            f.write("app_id: \"cli_x\"\napp_secret: \"s\"\nchat_id: \"oc_x\"\n")
            p = f.name
        try:
            cfg = app.load_app_config(p)
            self.assertEqual(cfg["app_id"], "cli_x")
        finally:
            os.unlink(p)


class TestCardV2App(unittest.TestCase):
    def test_reply_comment_has_button(self):
        c = {"rpid": "u1", "ctime": 1730000000, "message": "答", "uname": "UP",
             "context": "@张三: 问", "context_full": "@张三: 问", "root_rpid": "r1"}
        card = app.build_comment_card_v2(c, opus_id="121")
        blob = str(card)
        self.assertIn("查看对方后续回复", blob)
        self.assertIn("followups", blob)
        self.assertIn("r1", blob)

    def test_active_post_no_button(self):
        c = {"rpid": "u1", "ctime": 1730000000, "message": "主动发", "uname": "UP",
             "context": None, "context_full": None}
        card = app.build_comment_card_v2(c)
        self.assertNotIn("查看对方后续回复", str(card))
        self.assertIn("（主动发布）", str(card))

    def test_extra_elements_appended(self):
        c = {"rpid": "u1", "ctime": 1730000000, "message": "答", "uname": "UP",
             "context": "@张三: 问", "context_full": "@张三: 问", "root_rpid": "r1"}
        extra = [{"tag": "markdown", "content": "额外内容ABC"}]
        card = app.build_comment_card_v2(c, extra_elements=extra)
        self.assertIn("额外内容ABC", str(card))


class TestFollowupElements(unittest.TestCase):
    def test_with_replies(self):
        up = _entry("u1", "888", "UP", 1100, "@张三 答", parent="p1", is_up=True)
        parent = _entry("p1", "111", "张三", 1000, "问")
        replies = [_entry("p2", "111", "张三", 1200, "追问")]
        els = app.build_followup_elements(up, parent, replies)
        blob = str(els)
        self.assertIn("张三", blob)
        self.assertIn("追问", blob)

    def test_without_replies(self):
        up = _entry("u1", "888", "UP", 1100, "@张三 答", parent="p1", is_up=True)
        parent = _entry("p1", "111", "张三", 1000, "问")
        els = app.build_followup_elements(up, parent, [])
        self.assertIn("（对方尚未回复）", str(els))


class TestExchangeForUp(unittest.TestCase):
    def test_finds_parent_and_replies_after(self):
        thread = [
            _entry("p1", "111", "张三", 1000, "问"),
            _entry("u1", "888", "UP", 1100, "@张三 答", parent="p1", is_up=True),
            _entry("p0", "111", "张三", 900, "更早"),   # 早于 UP → 排除
            _entry("p2", "111", "张三", 1200, "追问"),   # 晚于 UP → 纳入
            _entry("x1", "999", "路人", 1150, "无关"),   # 非该人 → 排除
        ]
        up, parent, replies = app.exchange_for_up(thread, "u1")
        self.assertEqual(up["rpid"], "u1")
        self.assertEqual(parent["rpid"], "p1")
        got = [r["rpid"] for r in replies]
        self.assertIn("p2", got)
        self.assertNotIn("p0", got)
        self.assertNotIn("x1", got)

    def test_missing_up_returns_none(self):
        up, parent, replies = app.exchange_for_up([], "nope")
        self.assertIsNone(up)
        self.assertEqual(replies, [])


class TestCommentView(unittest.TestCase):
    def test_context_with_at(self):
        up = _entry("u1", "888", "UP", 1100, "答", parent="p1", is_up=True)
        parent = _entry("p1", "111", "张三", 1000, "被回复内容")
        view = app.comment_view_from_thread(up, parent, "r1")
        self.assertEqual(view["context_full"], "@张三: 被回复内容")
        self.assertEqual(view["root_rpid"], "r1")


class _FakeAction:
    def __init__(self, value):
        self.value = value


class _FakeContext:
    open_message_id = "om_test_1"


class _FakeEvent:
    def __init__(self, value):
        self.action = _FakeAction(value)
        self.context = _FakeContext()


class _FakeData:
    def __init__(self, value):
        self.event = _FakeEvent(value)


class TestOnCardAction(unittest.TestCase):
    cfg = {"opus_id": "121", "up_uid": "888",
           "bilibili": {"sessdata": "s"}, "feishu": {"webhook": "h"}}
    thread = [
        _entry("p1", "111", "张三", 1000, "问UP", is_up=False),
        _entry("u1", "888", "UP", 1100, "回复 @张三 : 我的看法", parent="p1", is_up=True),
        _entry("p2", "111", "张三", 1200, "感谢，那再请问", parent="u1", is_up=False),
    ]

    def test_click_updates_same_card_with_followups(self):
        captured = {}

        def fake_patch(client, message_id, card):
            captured["message_id"] = message_id
            captured["card"] = card
            return True

        data = _FakeData({"action": "followups", "root_rpid": "r1", "up_rpid": "u1"})
        with patch("monitor.load_config", return_value=self.cfg), \
             patch("monitor.resolve_comment_id", return_value="399"), \
             patch("monitor.fetch_thread", return_value=self.thread), \
             patch.object(app, "patch_card", side_effect=fake_patch):
            app.on_card_action(client=None, data=data)

        self.assertEqual(captured["message_id"], "om_test_1")
        blob = str(captured["card"])
        self.assertIn("感谢，那再请问", blob)   # 对方后续回复
        self.assertIn("张三", blob)

    def test_missing_params(self):
        data = _FakeData({"action": "followups"})
        with patch.object(app, "patch_card") as mock_patch:
            app.on_card_action(client=None, data=data)
            mock_patch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
