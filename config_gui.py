"""config_gui.py - tkinter 配置编辑器（图形化改配置，监控仍在控制台运行）

用法：
    python monitor.py --config    # 打开配置窗口
    双击 exe 且无 config.yaml     # 自动打开配置窗口
    python monitor.py --setup     # 保留原终端向导（无人值守场景）

纯逻辑函数（default_form/config_to_form/form_to_config/save_form）与
tkinter 界面分离，前者可单元测试。
"""
import os

import yaml

DEFAULTS = {
    "opus_id": "",
    "up_uid": "",
    "sessdata": "",
    "webhook": "",
    "history_days": "3",
    "push_interval": "2",
    "fast_interval": "60",
    "slow_interval": "300",
    "poll_start_hour": "9",
    "poll_end_hour": "15",
    "poll_weekday_only": "true",
    "overlap_seconds": "1800",
}

INT_KEYS = (
    "history_days",
    "push_interval",
    "fast_interval",
    "slow_interval",
    "poll_start_hour",
    "poll_end_hour",
    "overlap_seconds",
)


def default_form():
    """新建配置的表单默认值（全部为字符串，便于 UI 绑定）。"""
    return dict(DEFAULTS)


def config_to_form(cfg):
    """配置字典 -> 表单字典（缺失键用默认值填充）。"""
    form = dict(DEFAULTS)
    form["opus_id"] = str(cfg.get("opus_id") or "")
    form["up_uid"] = str(cfg.get("up_uid") or "")
    form["sessdata"] = str((cfg.get("bilibili") or {}).get("sessdata") or "")
    form["webhook"] = str((cfg.get("feishu") or {}).get("webhook") or "")
    for key in INT_KEYS:
        if cfg.get(key) is not None:
            form[key] = str(cfg[key])
    form["poll_weekday_only"] = "true" if cfg.get("poll_weekday_only", True) else "false"
    return form


def form_to_config(form):
    """表单字典 -> 配置字典。非法数字抛 ValueError（带字段名）。"""
    cfg = {}
    for key in ("opus_id", "up_uid"):
        cfg[key] = (form.get(key) or "").strip()
    for key in INT_KEYS:
        raw = str(form.get(key, "")).strip()
        try:
            cfg[key] = int(raw)
        except ValueError:
            raise ValueError(f"配置项 {key} 需要填数字，当前填的是「{raw}」")
    weekday_raw = str(form.get("poll_weekday_only", "true")).strip().lower()
    cfg["poll_weekday_only"] = weekday_raw not in ("false", "0", "no", "否")
    cfg["poll_interval"] = 180  # 保留字段（兼容旧校验逻辑）
    cfg["bilibili"] = {"sessdata": (form.get("sessdata") or "").strip()}
    cfg["feishu"] = {"webhook": (form.get("webhook") or "").strip()}
    return cfg


def save_form(form, path):
    """表单 -> 校验 -> 写盘。校验失败抛 ConfigError/ValueError。"""
    from monitor import validate_config

    cfg = form_to_config(form)
    validate_config(cfg)
    with open(path, "w", encoding="utf-8") as f:
        f.write("# 由配置编辑器生成，可手动编辑\n")
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)
    return cfg


# ---------------------------------------------------------------------------
# tkinter 界面
# ---------------------------------------------------------------------------
FIELDS = [
    # (键, 标签, 提示, 是否宽输入)
    ("opus_id", "动态 opus ID", "动态页 bilibili.com/opus/<这串数字>", False),
    ("up_uid", "UP主 mid", "UP主主页 space.bilibili.com/<这串数字>", False),
    ("sessdata", "B站 SESSDATA", "F12 -> Application -> Cookies -> bilibili.com", True),
    ("webhook", "飞书 webhook", "飞书群 -> 设置 -> 群机器人 -> 自定义机器人", True),
]

SPIN_FIELDS = [
    ("fast_interval", "快轮询间隔(秒)", 10, 3600),
    ("slow_interval", "慢轮询间隔(秒)", 30, 86400),
    ("poll_start_hour", "活跃开始(时)", 0, 23),
    ("poll_end_hour", "活跃结束(时)", 1, 24),
    ("overlap_seconds", "防漏推回看(秒)", 0, 86400),
    ("history_days", "冷启动回溯(天)", 1, 30),
    ("push_interval", "连推间隔(秒)", 0, 60),
]


def open_config_window(config_path=None):
    """打开配置编辑器窗口。返回 True 表示配置已保存。"""
    import tkinter as tk
    from tkinter import messagebox, ttk

    from monitor import ConfigError, load_config

    if config_path is None:
        from monitor import base_dir

        config_path = os.path.join(base_dir(), "config.yaml")

    existing = {}
    if os.path.exists(config_path):
        try:
            existing = load_config(config_path)  # 已有配置先过一遍校验
        except ConfigError:
            existing = {}
            # 校验失败的旧配置仍可编辑：绕过校验直接读
            try:
                with open(config_path, encoding="utf-8") as f:
                    existing = yaml.safe_load(f) or {}
            except Exception:
                existing = {}
    form = config_to_form(existing)

    root = tk.Tk()
    root.title("UP评论监控 · 配置")
    root.resizable(False, False)
    saved = {"ok": False}
    entries = {}

    def _save(event=None):
        for key, getter in entries.items():
            form[key] = getter()
        try:
            save_form(form, config_path)
        except (ValueError, ConfigError) as e:
            messagebox.showerror("保存失败", str(e), parent=root)
            return
        saved["ok"] = True
        messagebox.showinfo(
            "已保存", f"配置已保存到\n{config_path}\n\n关闭本窗口后将开始监控", parent=root
        )
        root.destroy()

    main = ttk.Frame(root, padding=12)
    main.grid()

    ttk.Label(main, text="必填信息", font=("", 10, "bold")).grid(
        row=0, column=0, columnspan=3, sticky="w", pady=(0, 4)
    )
    row = 1
    for key, label, hint, wide in FIELDS:
        ttk.Label(main, text=label).grid(row=row, column=0, sticky="w", pady=2)
        var = tk.StringVar(value=form[key])
        ent = ttk.Entry(main, textvariable=var, width=52 if wide else 30)
        ent.grid(row=row, column=1, sticky="we", padx=6, pady=2)
        entries[key] = var.get
        ttk.Label(main, text=hint, foreground="#888").grid(
            row=row, column=2, sticky="w"
        )
        row += 1

    ttk.Separator(main).grid(row=row, columnspan=3, sticky="we", pady=8)
    row += 1
    ttk.Label(main, text="轮询与推送（可选，默认值适合大多数场景）", font=("", 10, "bold")).grid(
        row=row, column=0, columnspan=3, sticky="w", pady=(0, 4)
    )
    row += 1
    col = 0
    for key, label, lo, hi in SPIN_FIELDS:
        ttk.Label(main, text=label).grid(row=row, column=col, sticky="w", pady=2)
        var = tk.IntVar(value=int(form[key]))
        sp = ttk.Spinbox(main, from_=lo, to=hi, textvariable=var, width=8)
        sp.grid(row=row, column=col + 1, sticky="w", padx=(2, 14), pady=2)
        entries[key] = var.get
        col += 2
        if col >= 6:
            col = 0
            row += 1
    if col:
        row += 1

    wd_var = tk.BooleanVar(value=form["poll_weekday_only"] == "true")
    ttk.Checkbutton(
        main, text="仅工作日启用快轮询（勾选=周末用慢轮询）", variable=wd_var
    ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(6, 2))
    entries["poll_weekday_only"] = lambda: "true" if wd_var.get() else "false"
    row += 1

    btns = ttk.Frame(main)
    btns.grid(row=row, column=0, columnspan=3, sticky="e", pady=(12, 0))
    ttk.Button(btns, text="保存并开始监控", command=_save).pack(side="right")

    root.bind("<Return>", _save)
    root.mainloop()
    return saved["ok"]


if __name__ == "__main__":
    open_config_window()
