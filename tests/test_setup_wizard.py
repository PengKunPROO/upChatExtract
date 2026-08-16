"""tests/test_setup_wizard.py — 首次配置向导与 Referer 去敏测试"""
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from monitor import (
    base_dir,
    default_state,
    interactive_setup,
    load_config,
    fetch_sub_replies,
    run_once,
)


class TestBaseDir(unittest.TestCase):
    def test_base_dir_is_source_dir_when_not_frozen(self):
        import monitor
        expected = os.path.dirname(os.path.abspath(monitor.__file__))
        self.assertEqual(base_dir(), expected)


class TestInteractiveSetup(unittest.TestCase):
    def test_wizard_writes_valid_config(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "config.yaml")
            answers = ["121", "888", "sess123", "https://open.feishu.cn/hook/x"]

            ok = interactive_setup(
                p, input_fn=lambda prompt: answers.pop(0), output_fn=lambda s: None
            )
            self.assertTrue(ok)
            cfg = load_config(p)
            self.assertEqual(cfg["opus_id"], "121")
            self.assertEqual(cfg["up_uid"], "888")
            self.assertEqual(cfg["bilibili"]["sessdata"], "sess123")
            self.assertEqual(cfg["feishu"]["webhook"], "https://open.feishu.cn/hook/x")
            self.assertEqual(cfg["poll_interval"], 180)
        finally:
            shutil.rmtree(td)

    def test_wizard_refuses_overwrite_without_confirm(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "config.yaml")
            with open(p, "w", encoding="utf-8") as f:
                f.write("opus_id: '999'\n")
            ok = interactive_setup(
                p, input_fn=lambda prompt: "n", output_fn=lambda s: None
            )
            self.assertFalse(ok)
            with open(p, encoding="utf-8") as f:
                self.assertIn("999", f.read())
        finally:
            shutil.rmtree(td)

    def test_wizard_rejects_incomplete_input(self):
        td = tempfile.mkdtemp()
        try:
            p = os.path.join(td, "config.yaml")
            # 只答第 1 问，其余留空 -> 占位符 -> 校验失败
            answers = ["121", "", "", ""]
            ok = interactive_setup(
                p, input_fn=lambda prompt: answers.pop(0), output_fn=lambda s: None
            )
            self.assertFalse(ok)
            self.assertFalse(os.path.exists(p))
        finally:
            shutil.rmtree(td)


class TestRefererDesensitization(unittest.TestCase):
    def test_sub_reply_referer_uses_configured_opus(self):
        """Referer 不应再硬编码某个 opus_id，而应使用配置传入的值。"""
        with patch("monitor._do_request") as mock_req:
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {"code": 0, "data": {"page": {"count": 1}, "replies": []}}
            mock_req.return_value = resp

            fetch_sub_replies("sess", "123", "1", "888", 0, opus_id="OPUS123")

            call = mock_req.call_args_list[0]
            self.assertIn("OPUS123", call[1]["headers"]["Referer"])

    def test_run_once_passes_opus_id_to_fetch(self):
        cfg = {
            "opus_id": "121",
            "up_uid": "888",
            "poll_interval": 180,
            "history_days": 3,
            "bilibili": {"sessdata": "sess"},
            "feishu": {"webhook": "http://h"},
        }
        state = default_state()
        with patch("monitor.fetch_up_comments", return_value=[]) as mock_f, \
             patch("monitor.send_feishu"), \
             patch("monitor.save_state"):
            run_once(cfg, state, "121")
            self.assertEqual(mock_f.call_args.kwargs.get("opus_id"), "121")


if __name__ == "__main__":
    unittest.main()
