"""tests/test_missed_comments.py - 漏推评论回归测试

场景：评论在上一轮轮询前几秒发布（ctime < last_poll_ts），但 B站 API
可见性延迟到下一轮才返回。旧逻辑用 threshold=last_poll_ts 会永久过滤
该评论；修复后应回看 overlap_seconds 窗口，靠 pushed_ids 去重防重推。
"""
import inspect
import time
import unittest
from unittest.mock import patch

import monitor
from monitor import default_state, fetch_up_comments, run_once


def _comment(rpid, ctime, msg="x"):
    return {"rpid": rpid, "ctime": ctime, "message": msg, "context": None}


class TestThresholdOverlap(unittest.TestCase):
    """热启动阈值应回看 overlap 秒，避免 API 可见性延迟导致的永久漏推。"""

    CFG = {
        "opus_id": "121",
        "up_uid": "888",
        "poll_interval": 180,
        "history_days": 3,
        "bilibili": {"sessdata": "sess"},
        "feishu": {"webhook": "http://h"},
    }

    def test_late_visible_comment_is_pushed(self):
        """评论 ctime 早于 last_poll_ts（但晚于 last_poll_ts-overlap）时仍应推送。"""
        now = int(time.time())
        state = {"pushed_ids": set(), "last_poll_ts": now - 60, "boot_ts": 0}
        # API 延迟：评论 ctime = last_poll_ts - 30（发布于上轮轮询前 30 秒）
        late = _comment("42", now - 90, "late visible comment")

        with patch("monitor.fetch_up_comments", return_value=[late]) as mock_f, \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.save_state"):
            run_once(self.CFG, state, "121")
            threshold = mock_f.call_args.args[3]
            self.assertLess(
                threshold,
                late["ctime"],
                "threshold 必须小于迟到评论的 ctime，否则该评论被永久过滤",
            )
            sent = [c for c in mock_send.call_args_list if "新评论" in str(c[0][1])]
            self.assertEqual(len(sent), 1)

    def test_overlap_configurable(self):
        """overlap_seconds 可配置，阈值 = last_poll_ts - overlap_seconds。"""
        now = int(time.time())
        state = {"pushed_ids": set(), "last_poll_ts": now - 60, "boot_ts": 0}
        cfg = dict(self.CFG, overlap_seconds=3600)

        with patch("monitor.fetch_up_comments", return_value=[]) as mock_f, \
             patch("monitor.send_feishu"), patch("monitor.save_state"):
            run_once(cfg, state, "121")
            threshold = mock_f.call_args.args[3]
            self.assertAlmostEqual(threshold, now - 60 - 3600, delta=5)

    def test_default_overlap_is_1800(self):
        """未配置时默认回看 1800 秒（30 分钟，覆盖 B站 API 常见可见性延迟）。"""
        now = int(time.time())
        state = {"pushed_ids": set(), "last_poll_ts": now - 60, "boot_ts": 0}

        with patch("monitor.fetch_up_comments", return_value=[]) as mock_f, \
             patch("monitor.send_feishu"), patch("monitor.save_state"):
            run_once(self.CFG, state, "121")
            threshold = mock_f.call_args.args[3]
            self.assertAlmostEqual(threshold, now - 60 - 1800, delta=5)

    def test_pushed_ids_still_dedupes_within_overlap(self):
        """overlap 窗口内已推送过的评论不得重推（pushed_ids 去重仍然有效）。"""
        now = int(time.time())
        state = {"pushed_ids": {"42"}, "last_poll_ts": now - 60, "boot_ts": 0}
        already = _comment("42", now - 90, "already pushed")

        with patch("monitor.fetch_up_comments", return_value=[already]), \
             patch("monitor.send_feishu", return_value=True) as mock_send, \
             patch("monitor.save_state"):
            run_once(self.CFG, state, "121")
            sent = [c for c in mock_send.call_args_list if "新评论" in str(c[0][1])]
            self.assertEqual(len(sent), 0)

    def test_cold_boot_threshold_unchanged(self):
        """冷启动（last_poll_ts=0）回看 history_days，不受 overlap 影响。"""
        state = default_state()

        with patch("monitor.fetch_up_comments", return_value=[]) as mock_f, \
             patch("monitor.send_feishu"), patch("monitor.save_state"):
            run_once(self.CFG, state, "121")
            threshold = mock_f.call_args.args[3]
            expected = int(time.time()) - 3 * 86400
            self.assertAlmostEqual(threshold, expected, delta=5)


class TestMaxPagesDefault(unittest.TestCase):
    def test_fetch_up_comments_default_max_pages_is_5(self):
        """mode=2 按热度分页，默认扫描页数 3->5 降低新评论沉底漏抓概率。"""
        sig = inspect.signature(fetch_up_comments)
        self.assertEqual(sig.parameters["max_pages"].default, 5)


class TestConsoleHardening(unittest.TestCase):
    def test_disable_quick_edit_does_not_crash(self):
        """Windows 下禁用 QuickEdit 不抛异常；非 Windows 平台跳过。"""
        monitor._disable_quick_edit()  # 冒烟：真实调用不应崩溃


if __name__ == "__main__":
    unittest.main()
