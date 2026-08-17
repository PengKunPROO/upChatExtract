"""tests/test_monitor.py — unittest tests for monitor.py"""
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch, MagicMock

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

    def test_negative_overlap_seconds_raises(self):
        with self.assertRaises(ConfigError):
            validate_config(
                {
                    "opus_id": "121",
                    "up_uid": "1",
                    "poll_interval": 60,
                    "history_days": 3,
                    "overlap_seconds": -1,
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

    def test_hot_boot_rechecks_overlap_for_delayed_comment(self):
        """接口延迟出现、ctime 早于上次轮询的评论仍应被去重窗口捕获。"""
        state = {"pushed_ids": set(), "last_poll_ts": 2000, "boot_ts": 0}
        delayed = self._c("delayed", 1995, "late visibility")
        thresholds = []

        def fake_fetch(sessdata, comment_id, up_uid, threshold, **kwargs):
            thresholds.append(threshold)
            return [delayed] if delayed["ctime"] >= threshold else []

        with patch("monitor.fetch_up_comments", side_effect=fake_fetch), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.save_state"), \
             patch("monitor._time.time", return_value=2100):
            run_once(self.cfg, state, "121")

        self.assertEqual(thresholds, [1700])
        self.assertIn("delayed", state["pushed_ids"])
        self.assertEqual(mock_send.call_count, 1)

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


if __name__ == "__main__":
    unittest.main()
