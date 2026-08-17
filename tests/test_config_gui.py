"""tests/test_config_gui.py - 配置编辑器（纯逻辑部分）测试"""
import os
import shutil
import tempfile
import unittest

from monitor import load_config


class TestFormLogic(unittest.TestCase):
    def _import(self):
        import config_gui

        return config_gui

    def test_defaults_new_config(self):
        """无现有配置时，表单默认值应能直接通过校验。"""
        gui = self._import()
        form = gui.default_form()
        cfg = gui.form_to_config(form)
        cfg["bilibili"]["sessdata"] = "abc"
        cfg["feishu"]["webhook"] = "http://h"
        cfg["opus_id"] = "121"
        cfg["up_uid"] = "888"
        load_config_dict = gui.form_to_config(form)
        # 校验通过（不抛异常）
        from monitor import validate_config

        validate_config(cfg)

    def test_form_to_config_types(self):
        """表单字符串正确转为 int/bool 类型。"""
        gui = self._import()
        form = dict(
            opus_id="121",
            up_uid="888",
            sessdata="s",
            webhook="http://h",
            history_days="3",
            push_interval="2",
            fast_interval="60",
            slow_interval="300",
            poll_start_hour="9",
            poll_end_hour="15",
            poll_weekday_only="true",
            overlap_seconds="1800",
        )
        cfg = gui.form_to_config(form)
        self.assertEqual(cfg["history_days"], 3)
        self.assertEqual(cfg["fast_interval"], 60)
        self.assertEqual(cfg["poll_start_hour"], 9)
        self.assertIs(cfg["poll_weekday_only"], True)
        self.assertEqual(cfg["overlap_seconds"], 1800)
        self.assertEqual(cfg["poll_interval"], 180)

    def test_form_to_config_invalid_int_raises(self):
        """非法数字应抛出带字段名的可读错误。"""
        gui = self._import()
        form = gui.default_form()
        form["fast_interval"] = "abc"
        with self.assertRaises(ValueError) as ctx:
            gui.form_to_config(form)
        self.assertIn("fast_interval", str(ctx.exception))

    def test_config_to_form_roundtrip(self):
        """配置 -> 表单 -> 配置 往返一致。"""
        gui = self._import()
        cfg_in = {
            "opus_id": "121",
            "up_uid": "888",
            "history_days": 3,
            "push_interval": 2,
            "fast_interval": 60,
            "slow_interval": 300,
            "poll_start_hour": 9,
            "poll_end_hour": 15,
            "poll_weekday_only": False,
            "overlap_seconds": 1800,
            "poll_interval": 180,
            "bilibili": {"sessdata": "s3"},
            "feishu": {"webhook": "http://hook"},
        }
        cfg = gui.config_to_form(cfg_in)
        cfg2 = gui.form_to_config(cfg)
        self.assertEqual(cfg_in, cfg2)

    def test_config_to_form_defaults_for_missing_keys(self):
        """旧版配置缺新键时，表单用默认值填充不崩溃。"""
        gui = self._import()
        old_cfg = {
            "opus_id": "121",
            "up_uid": "888",
            "bilibili": {"sessdata": "s"},
            "feishu": {"webhook": "http://h"},
        }
        form = gui.config_to_form(old_cfg)
        self.assertEqual(form["fast_interval"], "60")
        self.assertEqual(form["poll_start_hour"], "9")
        self.assertEqual(form["overlap_seconds"], "1800")

    def test_save_form_writes_valid_yaml(self):
        """save_form 落盘的 yaml 能被 load_config 正确读回。"""
        gui = self._import()
        td = tempfile.mkdtemp()
        try:
            path = os.path.join(td, "config.yaml")
            form = gui.default_form()
            form.update(opus_id="121", up_uid="888", sessdata="s", webhook="http://h")
            gui.save_form(form, path)
            cfg = load_config(path)
            self.assertEqual(cfg["opus_id"], "121")
            self.assertEqual(cfg["poll_weekday_only"], True)
        finally:
            shutil.rmtree(td)


if __name__ == "__main__":
    unittest.main()
