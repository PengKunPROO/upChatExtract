"""tests/test_monitor.py — unittest tests for monitor.py"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

import requests

from monitor import (
    ConfigError,
    FetchError,
    load_config,
    validate_config,
    load_state,
    save_state,
    default_state,
    fetch_reply_page,
    fetch_up_comments,
    _build_comment,
    build_comment_card,
    build_alert_card,
    send_feishu,
    run_once,
    setup_logging,
    export_history,
    analyze_mentioned_replies,
    fetch_thread,
    export_replies,
    fetch_sub_replies_since,
    build_followup_reply_card,
    _add_watch,
    _process_followups,
)


# ============================================================================
# Config Tests
# ============================================================================
class TestConfig(unittest.TestCase):
    SAMPLE_YAML = """
opus_id: "1234567890123456789"
up_uid: "12345678"
poll_interval: 180
history_days: 3
bilibili:
  sessdata: "abc123"
feishu:
  webhook: "https://open.feishu.cn/hook/xxx"
"""

    def test_load_valid_config(self):
        tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        )
        try:
            tmp.write(self.SAMPLE_YAML)
            tmp.close()
            cfg = load_config(tmp.name)
            self.assertEqual(cfg["opus_id"], "1234567890123456789")
            self.assertEqual(cfg["bilibili"]["sessdata"], "abc123")
        finally:
            os.unlink(tmp.name)

    def test_missing_file_raises(self):
        with self.assertRaises(ConfigError):
            load_config("__nonexistent__.yaml")

    def test_placeholder_opus_id_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "<xxx>",
                    "up_uid": "1",
                    "poll_interval": 180,
                    "history_days": 3,
                    "bilibili": {"sessdata": "s"},
                    "feishu": {"webhook": "h"},
                }
            )

    def test_empty_sessdata_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "121",
                    "up_uid": "1",
                    "poll_interval": 180,
                    "history_days": 3,
                    "bilibili": {"sessdata": ""},
                    "feishu": {"webhook": "h"},
                }
            )

    def test_missing_feishu_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "121",
                    "up_uid": "1",
                    "poll_interval": 180,
                    "history_days": 3,
                    "bilibili": {"sessdata": "s"},
                }
            )

    def test_invalid_poll_interval_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "121",
                    "up_uid": "1",
                    "poll_interval": 0,
                    "history_days": 3,
                    "bilibili": {"sessdata": "s"},
                    "feishu": {"webhook": "h"},
                }
            )

    def test_invalid_history_days_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "121",
                    "up_uid": "1",
                    "poll_interval": 60,
                    "history_days": -1,
                    "bilibili": {"sessdata": "s"},
                    "feishu": {"webhook": "h"},
                }
            )


# ============================================================================
# State Tests
# ============================================================================
class TestState(unittest.TestCase):
    def test_default_state(self):
        s = default_state()
        self.assertIsInstance(s["pushed_ids"], set)
        self.assertEqual(len(s["pushed_ids"]), 0)
        self.assertEqual(s["last_poll_ts"], 0)
        self.assertGreater(s["boot_ts"], 0)

    def test_load_missing_returns_default(self):
        s = load_state("__nonexistent__.json")
        self.assertEqual(s["pushed_ids"], set())
        self.assertEqual(s["last_poll_ts"], 0)

    def test_save_load_roundtrip(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "state.json")
            s = {"pushed_ids": {"3", "1", "2"}, "last_poll_ts": 100, "boot_ts": 200}
            save_state(s, p)
            loaded = load_state(p)
            self.assertEqual(loaded["pushed_ids"], {"1", "2", "3"})
            self.assertEqual(loaded["last_poll_ts"], 100)
            self.assertEqual(loaded["boot_ts"], 200)
        finally:
            import shutil
            shutil.rmtree(td)

    def test_pushed_ids_sorted_in_file(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "state.json")
            save_state({"pushed_ids": {"9", "1", "5"}, "last_poll_ts": 1, "boot_ts": 1}, p)
            with open(p, encoding="utf-8") as f:
                raw = json.load(f)
            self.assertEqual(raw["pushed_ids"], ["1", "5", "9"])
        finally:
            import shutil
            shutil.rmtree(td)

    def test_atomic_write_no_temp_leftover(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "state.json")
            save_state({"pushed_ids": set(), "last_poll_ts": 1, "boot_ts": 1}, p)
            tmps = [f for f in os.listdir(td) if f.endswith(".tmp")]
            self.assertEqual(len(tmps), 0)
        finally:
            import shutil
            shutil.rmtree(td)


# ============================================================================
# Bilibili Tests
# ============================================================================
def _make_page(replies, next_cursor=0, is_end=True, top_replies=None):
    """新版 reply/main 接口返回结构。"""
    data = {
        "code": 0,
        "data": {
            "cursor": {"next": next_cursor, "is_end": is_end},
            "replies": replies,
        },
    }
    if top_replies is not None:
        data["data"]["top_replies"] = top_replies
    return data


def _r(rpid, mid, ctime, message, sub_replies=None, reply_to=None):
    d = {
        "rpid": rpid,
        "ctime": ctime,
        "member": {"mid": mid, "uname": f"u{mid}"},
        "message": message,
    }
    if sub_replies:
        d["replies"] = sub_replies
    if reply_to:
        d["reply_to"] = reply_to
    return d


def _sub(rpid, mid, ctime, message):
    return {"rpid": rpid, "ctime": ctime, "member": {"mid": mid, "uname": f"s{mid}"}, "message": message}


class TestFetchReplyPage(unittest.TestCase):
    def test_ok(self):
        with patch("monitor._do_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = _make_page([])
            mock_req.return_value = mock_resp

            result = fetch_reply_page("sess", "121", 0)
            self.assertEqual(result, _make_page([])["data"])

    def test_http_error_raises(self):
        with patch("monitor._do_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_req.return_value = mock_resp
            with self.assertRaises(FetchError) as ctx:
                fetch_reply_page("sess", "121", 0)
            self.assertEqual(ctx.exception.code, 500)

    def test_api_minus_101_raises(self):
        with patch("monitor._do_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"code": -101, "message": "not login"}
            mock_req.return_value = mock_resp
            with self.assertRaises(FetchError) as ctx:
                fetch_reply_page("sess", "121", 1)
            self.assertEqual(ctx.exception.code, -101)


class TestFetchUpComments(unittest.TestCase):
    UP = "888"

    def test_filters_up_top_level(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [_r("1", "111", 1000, "other"), _r("2", self.UP, 2000, "up msg")],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0]["rpid"], "2")

    def test_filters_up_sub_reply(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [_r("1", "111", 1000, "base", sub_replies=[_sub("3", self.UP, 3000, "sub")])],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0]["rpid"], "3")

    def test_threshold_filters_old_comments(self):
        """ctime 早于阈值的评论被逐条过滤，不会推送。"""
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [_r("1", self.UP, 900, "old"), _r("2", self.UP, 2000, "new")],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 1000)
            # old(900 < 1000) 被过滤，只保留 new(2000 >= 1000)
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0]["rpid"], "2")

    def test_max_pages_guard(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            # 模拟翻 3 页，前 2 页 is_end=False，第 3 页 is_end=True
            pages = [
                _make_page([_r("1", self.UP, 9000, "a")], next_cursor=1, is_end=False)["data"],
                _make_page([_r("2", self.UP, 9001, "b")], next_cursor=2, is_end=False)["data"],
                _make_page([_r("3", self.UP, 9002, "c")], next_cursor=0, is_end=True)["data"],
            ]
            mock_f.side_effect = pages
            comments = fetch_up_comments("s", "121", self.UP, 0, max_pages=3)
            self.assertEqual(len(comments), 3)

    def test_sorts_by_ctime(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [
                    _r("1", self.UP, 3000, "last"),
                    _r("2", self.UP, 1000, "first"),
                    _r("3", self.UP, 2000, "mid"),
                ],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual([c["ctime"] for c in comments], [1000, 2000, 3000])

    def test_context_from_reply_to(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            rt = {"rpid": "9", "message": "被回复内容"}
            mock_f.return_value = _make_page(
                [_r("2", self.UP, 2000, "回复", reply_to=rt)]
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(comments[0]["context"], "被回复内容")

    def test_context_truncates_long(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            rt = {"rpid": "9", "message": "x" * 100}
            mock_f.return_value = _make_page(
                [_r("2", self.UP, 2000, "回复", reply_to=rt)]
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(len(comments[0]["context"]), 83)  # 80 + "..."

    def test_context_from_parent_for_sub(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [
                    _r(
                        "1", "111", 1000, "parent message abc",
                        sub_replies=[_sub("3", self.UP, 3000, "sub")]
                    )
                ],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(comments[0]["context"], "parent message abc")

    def test_active_post_context_none(self):
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [_r("1", self.UP, 2000, "主动发")]
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertIsNone(comments[0]["context"])

    def test_pictures_append_placeholder(self):
        raw = {"rpid": 1, "ctime": 1000, "message": "看",
               "member": {"mid": "888"}, "pictures": [{}, {}]}
        c = _build_comment(raw)
        self.assertIn("[图片]", c["message"])

    def test_sub_reply_in_replies_field(self):
        """旧版接口二级回复在 rp['replies'] 字段。"""
        with patch("monitor.fetch_reply_page") as mock_f:
            mock_f.return_value = _make_page(
                [_r("1", "111", 1000, "parent", sub_replies=[_sub("2", self.UP, 2000, "up sub")])],
            )["data"]
            comments = fetch_up_comments("s", "121", self.UP, 0)
            self.assertEqual(len(comments), 1)
            self.assertEqual(comments[0]["rpid"], "2")
            self.assertEqual(comments[0]["context"], "parent")


# ============================================================================
# Feishu Tests
# ============================================================================
class TestFeishu(unittest.TestCase):
    def test_comment_card_active(self):
        c = {"rpid": "1", "ctime": 1730000000, "message": "内容", "context": None}
        card = build_comment_card(c)
        self.assertIn("（主动发布）", str(card))
        self.assertIn("内容", str(card))

    def test_comment_card_context(self):
        c = {"rpid": "1", "ctime": 1730000000, "message": "回复", "context": "被回复"}
        card = build_comment_card(c)
        self.assertIn("被回复", str(card))
        self.assertNotIn("（主动发布）", str(card))

    def test_alert_card(self):
        card = build_alert_card("标题", "正文")
        self.assertEqual(card["msg_type"], "interactive")
        self.assertEqual(card["card"]["header"]["template"], "red")

    def test_send_ok(self):
        with patch("monitor._do_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"code": 0}
            mock_req.return_value = mock_resp
            ok = send_feishu("http://h", {"msg_type": "text"})
            self.assertTrue(ok)

    def test_send_retry_then_ok(self):
        with patch("monitor._do_request") as mock_req:
            call_count = [0]

            def side_effect(*args, **kwargs):
                call_count[0] += 1
                r = MagicMock()
                if call_count[0] < 3:
                    r.status_code = 500
                else:
                    r.status_code = 200
                    r.json.return_value = {"code": 0}
                return r

            mock_req.side_effect = side_effect
            with patch("monitor._time.sleep", return_value=None):
                ok = send_feishu("http://h", {"msg_type": "text"}, max_retries=3)
            self.assertTrue(ok)
            self.assertEqual(call_count[0], 3)

    def test_send_all_fail(self):
        with patch("monitor._do_request") as mock_req:
            mock_resp = MagicMock()
            mock_resp.status_code = 500
            mock_req.return_value = mock_resp
            with patch("monitor._time.sleep", return_value=None):
                ok = send_feishu("http://h", {"msg_type": "text"}, max_retries=2)
            self.assertFalse(ok)


# ============================================================================
# Logging Tests
# ============================================================================
class TestLogging(unittest.TestCase):
    def test_setup_returns_logger(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "t.log")
            logger = setup_logging(p)
            self.assertEqual(logger.name, "monitor")
            logger.info("hello %s", "world")
            for h in logger.handlers[:]:
                h.flush()
                h.close()
                logger.removeHandler(h)
            with open(p, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("hello world", content)
        finally:
            import shutil
            shutil.rmtree(td)


# ============================================================================
# Main Loop Tests
# ============================================================================
class TestRunOnce(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "opus_id": "121",
            "up_uid": "888",
            "poll_interval": 180,
            "history_days": 3,
            "bilibili": {"sessdata": "sess"},
            "feishu": {"webhook": "http://h"},
        }

    def _c(self, rpid, ctime, msg, context=None):
        return {"rpid": rpid, "ctime": ctime, "message": msg, "context": context}

    def test_cold_boot_pushes_in_range(self):
        now = int(time.time())
        cutoff = now - self.cfg["history_days"] * 86400
        state = default_state()
        c_new = self._c("1", cutoff + 3600, "new")
        with patch("monitor.fetch_up_comments", return_value=[c_new]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.save_state"):
            run_once(self.cfg, state, "121")
            self.assertIn("1", state["pushed_ids"])
            sent = [c for c in mock_send.call_args_list if "新评论" in str(c[0][1])]
            self.assertEqual(len(sent), 1)

    def test_hot_boot_incremental(self):
        now = int(time.time())
        state = {"pushed_ids": {"1"}, "last_poll_ts": now - 600, "boot_ts": 0}
        old = self._c("1", now - 1200, "old")
        new = self._c("2", now - 100, "new")
        with patch("monitor.fetch_up_comments", return_value=[old, new]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.save_state"):
            run_once(self.cfg, state, "121")
            sent = [c for c in mock_send.call_args_list if "新评论" in str(c[0][1])]
            self.assertEqual(len(sent), 1)

    def test_failed_push_not_marked(self):
        state = default_state()
        c = self._c("1", int(time.time()), "x")
        with patch("monitor.fetch_up_comments", return_value=[c]), \
             patch("monitor.send_feishu", return_value=False):
            with patch("monitor.save_state") as mock_save:
                run_once(self.cfg, state, "121")
                self.assertNotIn("1", state["pushed_ids"])

    def test_cookie_failure_alert(self):
        state = default_state()
        with patch("monitor.fetch_up_comments", side_effect=FetchError(-101, "nope")), \
             patch("monitor.send_feishu", return_value=True) as mock_send:
            run_once(self.cfg, state, "121")
            alerts = [c for c in mock_send.call_args_list if "Cookie" in str(c[0][1])]
            self.assertEqual(len(alerts), 1)

    def test_rate_limit_alert(self):
        state = default_state()
        with patch("monitor.fetch_up_comments", side_effect=FetchError(-412, "blocked")), \
             patch("monitor.send_feishu", return_value=True) as mock_send:
            run_once(self.cfg, state, "121")
            alerts = [c for c in mock_send.call_args_list if "风控" in str(c[0][1])]
            self.assertEqual(len(alerts), 1)


# ============================================================================
# BuildComment Edge Cases
# ============================================================================
class TestBuildComment(unittest.TestCase):
    def test_pictures_append(self):
        raw = {"rpid": 1, "ctime": 1000, "message": "看", "member": {"mid": "1"}, "pictures": [{}, {}]}
        c = _build_comment(raw)
        self.assertIn("[图片] [图片]", c["message"])

    def test_no_pictures_no_change(self):
        raw = {"rpid": 1, "ctime": 1000, "message": "纯文本", "member": {"mid": "1"}}
        c = _build_comment(raw)
        self.assertEqual(c["message"], "纯文本")


# ============================================================================
# Export Tests
# ============================================================================
class TestExportHistory(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "opus_id": "121",
            "up_uid": "888",
            "bilibili": {"sessdata": "s"},
            "feishu": {"webhook": "http://h"},
        }

    def _run(self, comments, sent_only=False, sent_ids=None, local=None):
        td = tempfile.mkdtemp()
        out = os.path.join(td, "out.md")
        try:
            with patch("monitor.resolve_comment_id", return_value="399"), \
                 patch("monitor.fetch_up_comments", return_value=comments) as mock_f, \
                 patch("monitor.load_state", return_value={"pushed_ids": set(sent_ids or [])}), \
                 patch("monitor.load_sent_history", return_value=(local or {})):
                path = export_history(self.cfg, out_path=out, sent_only=sent_only)
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            return path, content, mock_f
        finally:
            import shutil
            shutil.rmtree(td)

    def test_writes_markdown_with_content(self):
        comments = [
            {"rpid": "1", "ctime": 1730000000, "message": "第一条", "context": None, "uname": "UP"},
            {"rpid": "2", "ctime": 1730003600, "message": "第二条\n换行", "context": "@某人: 原文", "uname": "UP"},
        ]
        path, content, _ = self._run(comments)
        self.assertIn("# UP主评论汇总", content)
        self.assertIn("共 2 条", content)
        self.assertIn("第一条", content)
        self.assertIn("第二条", content)
        self.assertIn("@某人: 原文", content)
        self.assertIn("121", content)  # opus 链接

    def test_groups_by_date(self):
        # 跨两天的评论
        comments = [
            {"rpid": "1", "ctime": 1730000000, "message": "第一天", "context": None, "uname": "UP"},
            {"rpid": "2", "ctime": 1730090000, "message": "第二天", "context": None, "uname": "UP"},
        ]
        _, content, _ = self._run(comments)
        dates = [ln for ln in content.splitlines() if ln.startswith("## ")]
        self.assertEqual(len(dates), 2)

    def test_passes_threshold_zero(self):
        _, _, mock_f = self._run([])
        self.assertEqual(mock_f.call_args.kwargs.get("threshold_ts"), 0)

    def test_empty_comments(self):
        path, content, _ = self._run([])
        self.assertIn("共 0 条", content)

    def test_sent_only_filters_by_pushed_ids(self):
        comments = [
            {"rpid": "1", "ctime": 1730000000, "message": "已发", "context": None, "uname": "UP"},
            {"rpid": "2", "ctime": 1730003600, "message": "未发", "context": None, "uname": "UP"},
        ]
        _, content, _ = self._run(comments, sent_only=True, sent_ids=["1"])
        self.assertIn("已发", content)
        self.assertNotIn("未发", content)
        self.assertIn("（已发送）", content)

    def test_sent_only_includes_local_history(self):
        # state 里没有，但本地历史里有 → 也算已发送
        comments = [
            {"rpid": "9", "ctime": 1730000000, "message": "本地记录", "context": None, "uname": "UP"},
        ]
        local = {"9": {"rpid": "9", "ctime": 1730000000, "message": "本地记录", "context": None, "uname": "UP"}}
        _, content, _ = self._run(comments, sent_only=True, sent_ids=[], local=local)
        self.assertIn("本地记录", content)

    def test_record_sent_appends_jsonl(self):
        from monitor import record_sent, load_sent_history
        td = tempfile.mkdtemp()
        try:
            path = os.path.join(td, "sent_history.jsonl")
            with patch("monitor.sent_history_path", return_value=path):
                record_sent({"rpid": "123", "ctime": 1730000000, "message": "hi",
                             "context": None, "uname": "UP"})
                record_sent({"rpid": "124", "ctime": 1730000100, "message": "yo",
                             "context": "@a: b", "uname": "UP"})
                hist = load_sent_history()
            self.assertEqual(len(hist), 2)
            self.assertEqual(hist["123"]["message"], "hi")
            self.assertEqual(hist["124"]["context"], "@a: b")
        finally:
            import shutil
            shutil.rmtree(td)


# ============================================================================
# Conversation / Thread Tests
# ============================================================================
def _raw(rpid, mid, uname, ctime, message, parent=None):
    d = {"rpid": rpid, "ctime": ctime,
         "member": {"mid": mid, "uname": uname},
         "content": {"message": message}}
    if parent:
        d["parent"] = parent
    return d


class TestAnalyzeMentionedReplies(unittest.TestCase):
    UP = "888"

    def _entry(self, rpid, mid, uname, ctime, msg, parent="", is_up=False):
        return {"rpid": rpid, "mid": mid, "uname": uname, "ctime": ctime,
                "message": msg, "parent": parent, "is_up": is_up}

    def test_up_mention_then_target_reply(self):
        thread = [
            self._entry("r1", self.UP, "UP", 1000, "根"),
            self._entry("u1", self.UP, "UP", 1100, "回复@张三: 你怎么看", parent="p1", is_up=True),
            self._entry("x1", "999", "路人", 1150, "凑热闹"),
            self._entry("p2", "111", "张三", 1200, "我觉得…"),      # 张三晚于 UP 的 @ → 采纳
            self._entry("p0", "111", "张三", 900, "更早的话"),        # 早于 → 不采纳
        ]
        # 补上 parent 指向的张三原评论
        thread.append(self._entry("p1", "111", "张三", 1050, "UP你觉得呢"))
        ex = analyze_mentioned_replies(thread, self.UP)
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["target_uname"], "张三")
        got = [r["rpid"] for r in ex[0]["replies"]]
        self.assertIn("p2", got)      # 晚于 @ 时间
        self.assertNotIn("p0", got)   # 早于 @ 时间，排除
        self.assertNotIn("x1", got)   # 路人，排除

    def test_up_reply_to_self_excluded(self):
        thread = [
            self._entry("u1", self.UP, "UP", 1100, "接上条", parent="u0", is_up=True),
            self._entry("u0", self.UP, "UP", 1000, "上条", is_up=True),
        ]
        self.assertEqual(analyze_mentioned_replies(thread, self.UP), [])

    def test_pending_when_no_reply(self):
        thread = [
            self._entry("p1", "111", "张三", 1050, "问"),
            self._entry("u1", self.UP, "UP", 1100, "@张三 答", parent="p1", is_up=True),
        ]
        ex = analyze_mentioned_replies(thread, self.UP)
        self.assertEqual(len(ex), 1)
        self.assertEqual(ex[0]["replies"], [])


class TestCardV2(unittest.TestCase):
    def test_context_in_collapsible_panel(self):
        long_ctx = "完整上下文" * 30  # > 80 字
        c = {"rpid": "1", "ctime": 1730000000, "message": "答", "context": long_ctx[:80] + "...",
             "context_full": long_ctx, "uname": "UP"}
        card = build_comment_card(c, opus_id="121")
        self.assertEqual(card["card"]["schema"], "2.0")
        blob = str(card)
        self.assertIn("collapsible_panel", blob)
        self.assertIn(long_ctx, blob)          # 完整上下文都在
        self.assertEqual(blob.count(long_ctx), 1)  # 且只出现一次
        self.assertIn("opus/121", blob)         # 可点链接

    def test_short_context_inline_no_duplicate(self):
        """短上下文：不折叠、不预览，只显示一次。"""
        c = {"rpid": "1", "ctime": 1730000000, "message": "答",
             "context": "@张三: 好的", "context_full": "@张三: 好的", "uname": "UP"}
        card = build_comment_card(c)
        blob = str(card)
        self.assertIn("@张三: 好的", blob)
        self.assertNotIn("预览", blob)               # 不出现"预览"
        self.assertNotIn("collapsible_panel", blob)  # 不折叠
        self.assertEqual(blob.count("@张三: 好的"), 1)  # 只出现一次

    def test_reply_card_shows_replies_hint(self):
        c = {"rpid": "1", "ctime": 1730000000, "message": "答", "context": "@张三: 问",
             "context_full": "@张三: 问", "uname": "UP", "root_rpid": "316508517185"}
        card = build_comment_card(c, opus_id="121")
        blob = str(card)
        self.assertIn("--replies 316508517185", blob)

    def test_active_post_no_hint(self):
        c = {"rpid": "1", "ctime": 1730000000, "message": "主动发", "context": None,
             "context_full": None, "uname": "UP"}
        card = build_comment_card(c)
        self.assertNotIn("对话记录", str(card))
        self.assertIn("（主动发布）", str(card))


class TestFetchThread(unittest.TestCase):
    def test_combines_root_and_subs(self):
        root = _raw("r1", "888", "UP", 1000, "根评论")
        sub1 = _raw("s1", "111", "张三", 1100, "问", parent="r1")
        sub2 = _raw("s2", "888", "UP", 1200, "答", parent="s1")
        page = {"code": 0, "data": {"cursor": {"is_end": True},
                                    "top_replies": [root], "replies": [root]}}
        reply_page = {"code": 0, "data": {"page": {"count": 2}, "replies": [sub1, sub2]}}

        def _resp(data):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = data
            return r

        with patch("monitor.fetch_reply_page", return_value=page["data"]), \
             patch("monitor._do_request", return_value=_resp(reply_page)):
            thread = fetch_thread("s", "399", "r1", "888", opus_id="121")
        self.assertEqual(len(thread), 3)
        self.assertEqual(thread[0]["message"], "根评论")
        self.assertTrue(thread[2]["is_up"])
        self.assertEqual(thread[1]["parent"], "r1")


class TestExportReplies(unittest.TestCase):
    def test_writes_file_and_pushes(self):
        cfg = {"opus_id": "121", "up_uid": "888",
               "bilibili": {"sessdata": "s"}, "feishu": {"webhook": "http://h"}}
        thread = [
            {"rpid": "p1", "mid": "111", "uname": "张三", "ctime": 1000, "message": "问", "parent": "", "is_up": False},
            {"rpid": "u1", "mid": "888", "uname": "UP", "ctime": 1100, "message": "@张三 答", "parent": "p1", "is_up": True},
            {"rpid": "p2", "mid": "111", "uname": "张三", "ctime": 1200, "message": "回复UP", "parent": "u1", "is_up": False},
        ]
        td = tempfile.mkdtemp()
        try:
            out = os.path.join(td, "r.md")
            with patch("monitor.resolve_comment_id", return_value="399"), \
                 patch("monitor.fetch_thread", return_value=thread), \
                 patch("monitor.send_feishu", return_value=True) as mock_send:
                path = export_replies(cfg, "r1", out_path=out)
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            self.assertIn("UP @ 张三", content)
            self.assertIn("回复UP", content)
            self.assertEqual(mock_send.call_count, 1)
        finally:
            import shutil
            shutil.rmtree(td)

    def test_empty_when_no_mention(self):
        cfg = {"opus_id": "121", "up_uid": "888",
               "bilibili": {"sessdata": "s"}, "feishu": {"webhook": "http://h"}}
        thread = [{"rpid": "r1", "mid": "888", "uname": "UP", "ctime": 1000,
                   "message": "根", "parent": "", "is_up": True}]
        td = tempfile.mkdtemp()
        try:
            out = os.path.join(td, "r.md")
            with patch("monitor.resolve_comment_id", return_value="399"), \
                 patch("monitor.fetch_thread", return_value=thread), \
                 patch("monitor.send_feishu", return_value=True):
                path = export_replies(cfg, "r1", out_path=out)
                with open(path, encoding="utf-8") as f:
                    content = f.read()
            self.assertIn("共 0 段", content)
        finally:
            import shutil
            shutil.rmtree(td)


# ============================================================================
# Follow-up (对方后续回复) Tests
# ============================================================================
class TestFollowupWatch(unittest.TestCase):
    cfg = {"opus_id": "121", "up_uid": "888",
           "bilibili": {"sessdata": "s"}, "feishu": {"webhook": "http://h"}}

    def test_add_watch_and_dedup(self):
        state = default_state()
        c = {"rpid": "u1", "ctime": 1000, "message": "@张三 答", "uname": "UP",
             "target_mid": "111", "target_uname": "张三", "root_rpid": "r1"}
        _add_watch(state, c)
        _add_watch(state, c)  # 重复不应再加
        self.assertEqual(len(state["followup_watch"]), 1)

    def test_add_watch_caps_at_30(self):
        state = default_state()
        for i in range(35):
            _add_watch(state, {"rpid": f"u{i}", "ctime": i, "message": "x", "uname": "UP",
                               "target_mid": "111", "target_uname": "张三", "root_rpid": "r1"})
        self.assertEqual(len(state["followup_watch"]), 30)

    def test_process_pushes_only_reply_to_up(self):
        state = default_state()
        state["followup_watch"] = [{
            "root_rpid": "r1", "up_rpid": "u1", "up_ctime": 1000, "up_message": "@张三 答",
            "up_uname": "UP", "target_mid": "111", "target_uname": "张三", "created": int(time.time()),
        }]
        up_msg = {"rpid": "u1", "ctime": 1000, "member": {"mid": "888", "uname": "UP"},
                  "content": {"message": "@张三 答"}}
        # 张三回复 UP 的 @ 评论 → 要
        to_up = {"rpid": "p2", "ctime": 1200, "parent": "u1",
                 "member": {"mid": "111", "uname": "张三"}, "content": {"message": "感谢"}}
        # 张三回复别人（parent 指向路人）→ 不要
        to_other = {"rpid": "p3", "ctime": 1250, "parent": "x9",
                    "member": {"mid": "111", "uname": "张三"}, "content": {"message": "回你"}}
        # 路人发言 → 不是对象 → 不要
        other = {"rpid": "x1", "ctime": 1300, "member": {"mid": "999", "uname": "路人"},
                 "content": {"message": "凑热闹"}}
        with patch("monitor.fetch_sub_replies_since", return_value=[up_msg, to_up, to_other, other]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.record_sent"), patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        self.assertIn("p2", state["pushed_ids"])
        self.assertNotIn("p3", state["pushed_ids"])   # 回复别人的，排除
        self.assertNotIn("x1", state["pushed_ids"])
        self.assertEqual(mock_send.call_count, 1)

    def test_process_skips_other_person_reply(self):
        """对象回复了别人（parent 的 mid 不是 UP）→ 不推送。"""
        state = default_state()
        state["followup_watch"] = [{
            "root_rpid": "r1", "up_rpid": "u1", "up_ctime": 1000, "up_message": "@张三 答",
            "up_uname": "UP", "target_mid": "111", "target_uname": "张三", "created": int(time.time()),
        }]
        peer = {"rpid": "x9", "ctime": 1100, "member": {"mid": "999", "uname": "路人"},
                "content": {"message": "我问"}}
        to_other = {"rpid": "p3", "ctime": 1250, "parent": "x9",
                    "member": {"mid": "111", "uname": "张三"}, "content": {"message": "回你"}}
        with patch("monitor.fetch_sub_replies_since", return_value=[peer, to_other]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.record_sent"), patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        self.assertEqual(mock_send.call_count, 0)
        self.assertNotIn("p3", state["pushed_ids"])

    def test_process_keeps_reply_to_root_when_root_is_up(self):
        """回复根评论、且根评论是 UP 发的 → 算回复 UP，保留。"""
        state = default_state()
        state["followup_watch"] = [{
            "root_rpid": "r1", "root_author_mid": "888", "up_rpid": "u1", "up_ctime": 1000,
            "up_message": "@张三 答", "up_uname": "UP", "target_mid": "111",
            "target_uname": "张三", "created": int(time.time()),
        }]
        sub = {"rpid": "p4", "ctime": 1400, "parent": "r1",
               "member": {"mid": "111", "uname": "张三"}, "content": {"message": "回复根"}}
        with patch("monitor.fetch_sub_replies_since", return_value=[sub]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.record_sent"), patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        self.assertIn("p4", state["pushed_ids"])
        self.assertEqual(mock_send.call_count, 1)

    def test_process_skips_reply_to_root_when_root_not_up(self):
        """回复根评论、但根评论不是 UP 发的 → 排除。"""
        state = default_state()
        state["followup_watch"] = [{
            "root_rpid": "r1", "root_author_mid": "999", "up_rpid": "u1", "up_ctime": 1000,
            "up_message": "@张三 答", "up_uname": "UP", "target_mid": "111",
            "target_uname": "张三", "created": int(time.time()),
        }]
        sub = {"rpid": "p4", "ctime": 1400, "parent": "r1",
               "member": {"mid": "111", "uname": "张三"}, "content": {"message": "回复根"}}
        with patch("monitor.fetch_sub_replies_since", return_value=[sub]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.record_sent"), patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        self.assertNotIn("p4", state["pushed_ids"])
        self.assertEqual(mock_send.call_count, 0)

    def test_process_skips_already_pushed(self):
        state = default_state()
        state["pushed_ids"].add("p2")
        state["followup_watch"] = [{
            "root_rpid": "r1", "up_rpid": "u1", "up_ctime": 1000, "up_message": "x",
            "up_uname": "UP", "target_mid": "111", "target_uname": "张三", "created": int(time.time()),
        }]
        sub = {"rpid": "p2", "ctime": 1200, "parent": "u1",
               "member": {"mid": "111", "uname": "张三"}, "content": {"message": "感谢"}}
        with patch("monitor.fetch_sub_replies_since", return_value=[sub]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.record_sent"), patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        self.assertEqual(mock_send.call_count, 0)

    def test_process_expires_old_watch(self):
        state = default_state()
        state["followup_watch"] = [{
            "root_rpid": "r1", "up_rpid": "u1", "up_ctime": 1000, "up_message": "x",
            "up_uname": "UP", "target_mid": "111", "target_uname": "张三",
            "created": int(time.time()) - 10 * 86400,
        }]
        with patch("monitor.fetch_sub_replies_since") as mock_fetch, \
             patch("monitor.save_state"):
            _process_followups(self.cfg, state, "399")
        mock_fetch.assert_not_called()
        self.assertEqual(state["followup_watch"], [])


class TestFollowupCard(unittest.TestCase):
    def test_card_structure(self):
        reply = {"rpid": "p2", "ctime": 1730000000, "message": "感谢", "uname": "张三"}
        up = {"message": "@张三 我的看法", "uname": "UP", "root_rpid": "r1", "target_uname": "张三"}
        card = build_followup_reply_card(reply, up, opus_id="121")
        self.assertEqual(card["card"]["schema"], "2.0")
        self.assertIn("张三", str(card))
        self.assertIn("感谢", str(card))


class TestFetchSubRepliesSince(unittest.TestCase):
    def test_backward_collects_recent(self):
        def _resp(data):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = data
            return r

        page1 = {"code": 0, "data": {"page": {"count": 40},
                  "replies": [{"rpid": "old", "ctime": 100, "member": {"mid": "1"}, "content": {"message": "old"}}]}}
        page2 = {"code": 0, "data": {"page": {"count": 40},
                  "replies": [{"rpid": "new", "ctime": 2000, "member": {"mid": "1"}, "content": {"message": "new"}}]}}
        # 调用顺序：count(page1) -> pn=2(page2) -> pn=1(page1)
        with patch("monitor._do_request", side_effect=[_resp(page1), _resp(page2), _resp(page1)]), \
             patch("monitor._time.sleep"):
            out = fetch_sub_replies_since("s", "399", "r1", since_ctime=1000)
        rpids = [s["rpid"] for s in out]
        self.assertIn("new", rpids)
        self.assertNotIn("old", rpids)


class TestProxyFallback(unittest.TestCase):
    """代理不可用时：只提示一次，后续请求直接直连。"""

    def setUp(self):
        import monitor
        self.monitor = monitor
        monitor._PROXY_DISABLED = False

    def tearDown(self):
        self.monitor._PROXY_DISABLED = False

    def test_fallback_cached_and_logged_once(self):
        calls = []

        def fake_request(method, url, **kw):
            calls.append(kw.get("proxies"))
            if kw.get("proxies") is None:
                raise requests.exceptions.ProxyError("blocked")
            return "ok"

        with patch("monitor.requests.request", side_effect=fake_request), \
             patch("monitor.log.warning") as mock_warn:
            self.monitor._do_request("GET", "http://x")
            self.monitor._do_request("GET", "http://x")
            self.monitor._do_request("GET", "http://x")

        # 第 1 次 _do_request：None(失败) + NO_PROXY(成功)；之后两次：直接 NO_PROXY
        self.assertEqual(
            calls,
            [None, self.monitor.NO_PROXY, self.monitor.NO_PROXY, self.monitor.NO_PROXY],
        )
        self.assertEqual(mock_warn.call_count, 1)

    def test_uses_system_proxy_first_when_not_disabled(self):
        seen = []

        def fake_request(method, url, **kw):
            seen.append(kw.get("proxies"))
            return "ok"

        with patch("monitor.requests.request", side_effect=fake_request):
            self.monitor._do_request("GET", "http://x")
        self.assertEqual(seen, [None])  # 未显式指定 proxies → 走系统默认


if __name__ == "__main__":
    unittest.main()
