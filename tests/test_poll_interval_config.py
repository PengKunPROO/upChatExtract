"""tests/test_poll_interval_config.py - 轮询时段配置化测试"""
import datetime
import os
import shutil
import tempfile
import unittest

from monitor import get_poll_interval, interactive_setup, load_config

CFG = {"fast_interval": 60, "slow_interval": 300}

# 2026-08-17 是周一
MON_10 = datetime.datetime(2026, 8, 17, 10, 0, 0)
MON_12 = datetime.datetime(2026, 8, 17, 12, 0, 0)
MON_16 = datetime.datetime(2026, 8, 17, 16, 0, 0)
SUN_10 = datetime.datetime(2026, 8, 23, 10, 0, 0)


class TestPollIntervalConfig(unittest.TestCase):
    def test_default_active_hours_9_to_15(self):
        """默认活跃时段仍为工作日 9-15 点（向后兼容老配置）。"""
        self.assertEqual(get_poll_interval(dict(CFG), now=MON_10), 60)
        self.assertEqual(get_poll_interval(dict(CFG), now=MON_16), 300)

    def test_active_hours_configurable(self):
        """poll_start_hour/poll_end_hour 可配置，改变生效时段。"""
        cfg = dict(CFG, poll_start_hour=11, poll_end_hour=14)
        self.assertEqual(
            get_poll_interval(cfg, now=MON_10), 300, "10 点不在 11-14 活跃时段"
        )
        self.assertEqual(get_poll_interval(cfg, now=MON_12), 60)

    def test_weekend_uses_slow_even_in_active_hours(self):
        """poll_weekday_only 默认 true：周末即使 10 点也走慢轮询。"""
        self.assertEqual(get_poll_interval(dict(CFG), now=SUN_10), 300)

    def test_weekend_fast_when_weekday_only_false(self):
        """poll_weekday_only=false：周末活跃时段也走快轮询。"""
        cfg = dict(CFG, poll_weekday_only=False)
        self.assertEqual(get_poll_interval(cfg, now=SUN_10), 60)

    def test_empty_cfg_defaults(self):
        """空配置不崩溃，走默认值。"""
        self.assertEqual(get_poll_interval({}, now=MON_10), 60)
        self.assertEqual(get_poll_interval({}, now=SUN_10), 300)


class TestWizardWritesIntervalKeys(unittest.TestCase):
    def test_wizard_config_contains_all_interval_keys(self):
        """向导生成的配置包含全部轮询相关键，用户可直接改 yaml 调节。"""
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "config.yaml")
            answers = ["121", "888", "s", "http://h"]
            interactive_setup(
                p, input_fn=lambda prompt: answers.pop(0), output_fn=lambda s: None
            )
            cfg = load_config(p)
            for key in (
                "poll_start_hour",
                "poll_end_hour",
                "poll_weekday_only",
                "fast_interval",
                "slow_interval",
            ):
                self.assertIn(key, cfg, f"向导配置缺少 {key}")
        finally:
            shutil.rmtree(td)


if __name__ == "__main__":
    unittest.main()
