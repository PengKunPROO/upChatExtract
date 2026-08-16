"""monitor.py — B站UP主评论监控推送到飞书"""
import json
import logging
import os
import signal as _signal
import sys
import tempfile
import time as _time

import requests
import yaml

log = logging.getLogger("monitor")

API_URL = "https://api.bilibili.com/x/v2/reply/main"
REPLY_URL = "https://api.bilibili.com/x/v2/reply/reply"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
class ConfigError(Exception):
    pass


def base_dir():
    """程序基准目录：PyInstaller 打包后为 exe 所在目录，否则为脚本所在目录。

    配置文件、状态、日志默认都放在此目录，保证从任何工作目录启动行为一致。
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def load_config(path=None):
    path = path or os.path.join(base_dir(), "config.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigError(
            f"配置文件 {path} 不存在，请从 config.example.yaml 复制一份并填入真实值"
        )
    if not isinstance(cfg, dict):
        raise ConfigError("配置文件格式错误：应为 YAML 键值映射")
    validate_config(cfg)
    return cfg


def validate_config(cfg):
    bili = cfg.get("bilibili") or {}
    fs = cfg.get("feishu") or {}
    required = {
        "opus_id": cfg.get("opus_id"),
        "up_uid": cfg.get("up_uid"),
        "bilibili.sessdata": bili.get("sessdata"),
        "feishu.webhook": fs.get("webhook"),
    }
    for key, val in required.items():
        if val is None or (
            isinstance(val, str) and (val.strip() == "" or val.strip().startswith("<"))
        ):
            raise ConfigError(f"配置项 {key} 缺失或仍为占位符，请编辑 config.yaml")
    for key in ("poll_interval", "history_days"):
        val = cfg.get(key)
        if not isinstance(val, int) or val <= 0:
            raise ConfigError(f"配置项 {key} 必须为正整数，当前为 {val!r}")


def interactive_setup(path=None, input_fn=input, output_fn=print):
    """首次运行交互式配置向导：引导用户填写 4 项必填信息并写入 config.yaml。

    返回 True 表示配置已写入，False 表示用户取消或输入无效。
    """
    path = path or os.path.join(base_dir(), "config.yaml")
    output_fn("=" * 62)
    output_fn("  B站UP主评论监控 → 飞书推送 · 首次配置向导")
    output_fn("  需要 4 项信息，按提示填写即可（带 * 的步骤见说明）")
    output_fn("=" * 62)

    if os.path.exists(path):
        ans = input_fn(f"检测到已有 {path}，覆盖它吗？(y/N): ").strip().lower()
        if ans != "y":
            output_fn("已取消，保留现有配置。")
            return False

    output_fn("")
    output_fn("[1/4] 要监控的B站动态 opus ID")
    output_fn("     打开动态页 https://www.bilibili.com/opus/<这串数字>，复制这串数字")
    opus_id = input_fn("opus_id: ").strip()

    output_fn("")
    output_fn("[2/4] UP主的 mid（用户ID）")
    output_fn("     打开UP主主页 https://space.bilibili.com/<这串数字>，复制这串数字")
    up_uid = input_fn("up_uid: ").strip()

    output_fn("")
    output_fn("[3/4] B站 SESSDATA（需已对该UP主充电）")
    output_fn("     浏览器登录B站 → F12 → Application → Cookies → bilibili.com → SESSDATA")
    sessdata = input_fn("SESSDATA: ").strip()

    output_fn("")
    output_fn("[4/4] 飞书群机器人 webhook 地址")
    output_fn("     飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人 → 复制webhook")
    webhook = input_fn("webhook: ").strip()

    cfg = {
        "opus_id": opus_id or "<未填写>",
        "up_uid": up_uid or "<未填写>",
        "poll_interval": 180,
        "history_days": 3,
        "push_interval": 2,
        "fast_interval": 60,
        "slow_interval": 300,
        "bilibili": {"sessdata": sessdata or "<未填写>"},
        "feishu": {"webhook": webhook or "<未填写>"},
    }
    try:
        validate_config(cfg)
    except ConfigError as e:
        output_fn("")
        output_fn(f"配置校验失败：{e}")
        output_fn("请重新运行：python monitor.py --setup")
        return False

    with open(path, "w", encoding="utf-8") as f:
        f.write("# 由首次配置向导生成，可手动编辑\n")
        yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False)

    output_fn("")
    output_fn(f"配置已保存到 {path}，开始监控...")
    return True


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def default_state():
    return {"pushed_ids": set(), "last_poll_ts": 0, "boot_ts": int(_time.time())}


def load_state(path=None):
    path = path or os.path.join(base_dir(), "state.json")
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default_state()
    return {
        "pushed_ids": set(raw.get("pushed_ids", [])),
        "last_poll_ts": raw.get("last_poll_ts", 0),
        "boot_ts": raw.get("boot_ts", int(_time.time())),
    }


def save_state(state, path=None):
    path = path or os.path.join(base_dir(), "state.json")
    payload = {
        "pushed_ids": sorted(state["pushed_ids"]),
        "last_poll_ts": state["last_poll_ts"],
        "boot_ts": state["boot_ts"],
    }
    dir_ = os.path.dirname(os.path.abspath(path) if os.path.dirname(path) else ".") or "."
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp", prefix="state_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Bilibili Reply Fetching
# ---------------------------------------------------------------------------
class FetchError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"[{code}] {message}")


NO_PROXY = {"http": None, "https": None}


def _do_request(method, url, **kwargs):
    """先走系统默认代理；遇到代理/SSL 错误自动绕过直连。"""
    try:
        return requests.request(method, url, **kwargs)
    except (requests.exceptions.ProxyError, requests.exceptions.SSLError):
        log.warning("系统代理失败，自动切换直连")
        kwargs["proxies"] = NO_PROXY
        return requests.request(method, url, **kwargs)


def resolve_comment_id(opus_id):
    """从 opus 页面 HTML 解析评论接口真正需要的 oid（comment_id_str）。"""
    import re

    url = f"https://www.bilibili.com/opus/{opus_id}"
    headers = {"User-Agent": UA}
    resp = _do_request("GET", url, headers=headers, timeout=15)
    if resp.status_code != 200:
        raise FetchError(resp.status_code, f"opus 页面 HTTP {resp.status_code}")
    m = re.search(r'"comment_id_str"\s*:\s*"(\d+)"', resp.text)
    if not m:
        raise FetchError(-1, "无法从 opus 页面解析 comment_id_str")
    return m.group(1)


def fetch_reply_page(sessdata, comment_id, page=0):
    """新版 reply/main 接口，mode=2 按热度，top_replies + replies 都返回。"""
    headers = {
        "User-Agent": UA,
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": "https://www.bilibili.com/",
    }
    params = {"type": 11, "oid": comment_id, "mode": 2, "next": page}
    resp = _do_request("GET", API_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        raise FetchError(resp.status_code, f"HTTP {resp.status_code}")
    data = resp.json()
    if data.get("code") != 0:
        raise FetchError(data.get("code", -1), data.get("message", "unknown"))
    return data["data"]


def _get_message(raw):
    """B站新版接口评论文本在 content.message，旧版在 message。"""
    if not raw:
        return ""
    content = raw.get("content") or {}
    return (content.get("message") or raw.get("message") or "").strip()


def _build_comment(raw, parent=None):
    reply_to = raw.get("reply_to") or {}
    message = _get_message(raw)
    if raw.get("pictures"):
        message += "".join(" [图片]" for _ in raw["pictures"])
    context = None
    if reply_to:
        ctx_msg = _get_message(reply_to)
        if len(ctx_msg) > 80:
            ctx_msg = ctx_msg[:80] + "..."
        context = ctx_msg or None
    elif parent is not None:
        ctx_msg = _get_message(parent)
        if len(ctx_msg) > 80:
            ctx_msg = ctx_msg[:80] + "..."
        context = ctx_msg or None
    return {
        "rpid": str(raw["rpid"]),
        "ctime": raw["ctime"],
        "message": message,
        "context": context,
        "uname": (raw.get("member") or {}).get("uname", ""),
    }


def _build_comment_with_context(raw, all_cache):
    """构建评论，用 parent 字段从缓存回溯被回复的评论作为上下文。"""
    message = _get_message(raw)
    if raw.get("pictures"):
        message += "".join(" [图片]" for _ in raw["pictures"])
    context = None
    reply_to = raw.get("reply_to")
    if reply_to and _get_message(reply_to):
        ctx_msg = _get_message(reply_to)
        if len(ctx_msg) > 80:
            ctx_msg = ctx_msg[:80] + "..."
        context = ctx_msg
    else:
        # reply_to 为空时，用 parent rpid 从缓存找被回复的评论
        parent_rpid = raw.get("parent")
        if parent_rpid and parent_rpid in all_cache:
            parent_raw = all_cache[parent_rpid]
            parent_uname = (parent_raw.get("member") or {}).get("uname", "")
            ctx_msg = _get_message(parent_raw)
            if ctx_msg:
                if len(ctx_msg) > 80:
                    ctx_msg = ctx_msg[:80] + "..."
                prefix = f"@{parent_uname}: " if parent_uname else ""
                context = prefix + ctx_msg
    return {
        "rpid": str(raw["rpid"]),
        "ctime": raw["ctime"],
        "message": message,
        "context": context,
        "uname": (raw.get("member") or {}).get("uname", ""),
    }


def fetch_sub_replies(sessdata, comment_id, root_rpid, up_uid, threshold_ts, max_pages=20, opus_id=None):
    """从最后一页往前翻子回复，遇到全旧页就停止。缓存全部子回复用于回溯上下文。"""
    results = []
    all_cache = {}  # rpid -> raw reply，用于按 parent 回溯上下文
    referer = f"https://www.bilibili.com/opus/{opus_id}" if opus_id else "https://www.bilibili.com/"
    headers = {
        "User-Agent": UA,
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": referer,
    }

    # 先拉第1页，拿到总条数
    params = {"oid": comment_id, "type": 11, "root": root_rpid, "pn": 1, "ps": 20}
    resp = _do_request("GET", REPLY_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        log.warning("楼中楼接口 %s（rpid=%s），跳过子回复", resp.status_code, root_rpid)
        return results
    try:
        data = resp.json()
    except Exception:
        log.warning("楼中楼返回非 JSON（rpid=%s），跳过", root_rpid)
        return results
    if data.get("code") != 0:
        return results
    page_info = (data.get("data") or {}).get("page") or {}
    total_count = page_info.get("count", 0)
    if total_count <= 3:
        return results

    total_pages = min((total_count + 19) // 20, max_pages)

    # 处理第1页
    replies = (data.get("data") or {}).get("replies") or []
    for s in replies:
        all_cache[s.get("rpid")] = s

    # 从最后一页往前翻，遇到全旧页就停
    for pn in range(total_pages, 1, -1):
        _time.sleep(1)
        params["pn"] = pn
        resp = _do_request("GET", REPLY_URL, headers=headers, params=params, timeout=15)
        if resp.status_code != 200:
            break
        try:
            data = resp.json()
        except Exception:
            break
        if data.get("code") != 0:
            break
        replies = (data.get("data") or {}).get("replies") or []
        if not replies:
            break
        newest = max((s.get("ctime", 0) for s in replies), default=0)
        for s in replies:
            all_cache[s.get("rpid")] = s
        if newest < threshold_ts:
            break

    # 构建 UP 的评论，用 parent 回溯上下文
    for rpid, s in all_cache.items():
        if str((s.get("member") or {}).get("mid")) == up_uid:
            c = _build_comment_with_context(s, all_cache)
            if c["ctime"] >= threshold_ts:
                results.append(c)
    return results


def fetch_up_comments(sessdata, comment_id, up_uid, threshold_ts, max_pages=3, opus_id=None):
    """新版 reply/main 接口拉一级评论（含 top_replies）+ 楼中楼接口拉子回复。"""
    results = []
    next_cursor = 0
    for _ in range(max_pages):
        data = fetch_reply_page(sessdata, comment_id, page=next_cursor)
        all_replies = list(data.get("top_replies") or []) + list(data.get("replies") or [])
        if not all_replies:
            break
        oldest_in_page = min((r.get("ctime", 0) for r in all_replies), default=0)
        for r in all_replies:
            rpid = r.get("rpid")
            if str((r.get("member") or {}).get("mid")) == up_uid:
                c = _build_comment(r)
                if c["ctime"] >= threshold_ts:
                    results.append(c)
            # 接口内嵌的子回复（前几条）
            for sub in r.get("replies") or []:
                if str((sub.get("member") or {}).get("mid")) == up_uid:
                    c = _build_comment(sub, parent=r)
                    if c["ctime"] >= threshold_ts:
                        results.append(c)
            # 楼中楼接口拉全部子回复
            rcount = r.get("rcount") or 0
            if rcount > 3:
                sub_replies = fetch_sub_replies(
                    sessdata, comment_id, rpid, up_uid, threshold_ts, opus_id=opus_id
                )
                results.extend(sub_replies)
        cursor = data.get("cursor") or {}
        if cursor.get("is_end") or oldest_in_page < threshold_ts:
            break
        next_cursor = cursor.get("next", 0)
    # 按 rpid 去重
    seen = set()
    deduped = []
    for c in results:
        if c["rpid"] not in seen:
            seen.add(c["rpid"])
            deduped.append(c)
    deduped.sort(key=lambda c: c["ctime"])
    return deduped


# ---------------------------------------------------------------------------
# Feishu Push
# ---------------------------------------------------------------------------
def build_comment_card(comment):
    ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(comment["ctime"]))
    context_text = comment.get("context")
    if context_text is None:
        context_text = "（主动发布）"
    uname = comment.get("uname") or "UP"
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"{uname} 新评论"},
                "template": "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**时间**\n{ts}",
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**上下文**\n{context_text}",
                            },
                        },
                    ],
                },
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**内容**\n{comment['message']}",
                    },
                },
            ],
        },
    }


def build_alert_card(title, body):
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            ],
        },
    }


def send_feishu(webhook, payload, max_retries=3):
    last_error = ""
    for i in range(max_retries):
        try:
            resp = _do_request("POST", webhook, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("code") == 0:
                    return True
                last_error = f"feishu code {data.get('code')}: {data.get('msg', '')}"
            else:
                last_error = f"HTTP {resp.status_code}"
        except requests.RequestException as e:
            last_error = str(e)
        if i < max_retries - 1:
            _time.sleep(2 ** i)
    log.error("飞书推送失败（重试%d次后）: %s", max_retries, last_error)
    return False


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging(log_path=None):
    log_path = log_path or os.path.join(base_dir(), "monitor.log")
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    for h in logger.handlers[:]:
        logger.removeHandler(h)
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger


# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------
def run_once(cfg, state, comment_id):
    sessdata = cfg["bilibili"]["sessdata"]
    up_uid = cfg["up_uid"]
    webhook = cfg["feishu"]["webhook"]

    if state["last_poll_ts"] == 0:
        threshold = int(_time.time()) - cfg["history_days"] * 86400
        log.info("冷启动：回溯 %d 天，阈值 %s", cfg["history_days"], threshold)
    else:
        threshold = state["last_poll_ts"]

    try:
        comments = fetch_up_comments(
            sessdata, comment_id, up_uid, threshold, opus_id=cfg["opus_id"]
        )
    except FetchError as e:
        if e.code == -101:
            log.error("Cookie 失效: %s", e)
            send_feishu(
                webhook,
                build_alert_card(
                    "【需处理】Cookie 失效",
                    "请重新复制 SESSDATA 到 config.yaml。\n\n"
                    "步骤：浏览器 F12 → Application → Cookies → bilibili.com → SESSDATA。",
                ),
            )
        elif e.code in (-412, -509, 403):
            log.error("B站风控: %s", e)
            send_feishu(
                webhook,
                build_alert_card(
                    "【需处理】B站风控",
                    f"已触发风控（code={e.code}），本轮跳过，等几轮自动重试或更换 cookie。",
                ),
            )
        else:
            log.error("拉取失败: %s", e)
        return

    new_comments = [c for c in comments if c["rpid"] not in state["pushed_ids"]]
    log.info("命中 UP 评论 %d 条，新增 %d 条", len(comments), len(new_comments))

    push_gap = cfg.get("push_interval", 2)
    for c in new_comments:
        card = build_comment_card(c)
        ok = send_feishu(webhook, card)
        if ok:
            state["pushed_ids"].add(c["rpid"])
            save_state(state)
            log.info("推送成功 rpid=%s", c["rpid"])
            if len(new_comments) > 1:
                _time.sleep(push_gap)
        else:
            log.warning("推送失败 rpid=%s，下轮重试", c["rpid"])

    state["last_poll_ts"] = int(_time.time())
    save_state(state)


def get_poll_interval(cfg):
    """交易时段（周一至周五 9:00-15:00）快轮询，其他时段慢轮询。"""
    import datetime

    now = datetime.datetime.now()
    trading_hours = cfg.get("trading_hours", {"start": 9, "end": 15})
    fast = cfg.get("fast_interval", 60)
    slow = cfg.get("slow_interval", 300)

    is_weekday = now.weekday() < 5  # 0=Mon ... 4=Fri
    in_trading = (
        is_weekday
        and trading_hours["start"] <= now.hour < trading_hours["end"]
    )
    return fast if in_trading else slow


def _force_utf8_console():
    """Windows 下统一控制台为 UTF-8，避免 exe 中文输出乱码。"""
    if sys.platform == "win32":
        try:
            os.system("chcp 65001 >nul")
            for stream in (sys.stdout, sys.stderr):
                if hasattr(stream, "reconfigure"):
                    stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main():
    _force_utf8_console()
    setup_logging()
    log.info("========== 启动 ==========")
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("用法: python monitor.py [--setup] [--help]")
        print("  （无参数）  开始监控，config.yaml 不存在时自动进入首次配置向导")
        print("  --setup    重新运行配置向导")
        print("  --help     显示本帮助")
        return
    if "--setup" in sys.argv[1:]:
        interactive_setup()
        return
    try:
        cfg = load_config()
    except ConfigError as e:
        log.warning("%s", e)
        log.info("未找到有效配置，进入首次配置向导...")
        if not interactive_setup():
            log.info("未完成配置，退出。可运行 python monitor.py --setup 重新配置")
            return
        cfg = load_config()
    state = load_state()
    try:
        comment_id = resolve_comment_id(cfg["opus_id"])
        log.info("opus=%s -> comment_id=%s", cfg["opus_id"], comment_id)
    except Exception as e:
        log.error("解析 comment_id 失败: %s，将在循环中重试", e)
        comment_id = None
    log.info(
        "监控 up_uid=%s 交易时段=%ds 非交易=%ds 冷启动=%s",
        cfg["up_uid"],
        cfg.get("fast_interval", 60),
        cfg.get("slow_interval", 300),
        state["last_poll_ts"] == 0,
    )

    def _shutdown(signum, frame):
        raise KeyboardInterrupt

    _signal.signal(_signal.SIGINT, _shutdown)
    try:
        _signal.signal(_signal.SIGTERM, _shutdown)
    except AttributeError:
        pass

    try:
        while True:
            try:
                if comment_id is None:
                    comment_id = resolve_comment_id(cfg["opus_id"])
                    log.info("opus=%s -> comment_id=%s", cfg["opus_id"], comment_id)
                run_once(cfg, state, comment_id)
            except Exception as e:
                log.error("本轮异常（跳过，下轮继续）: %s", e, exc_info=True)
            interval = get_poll_interval(cfg)
            _time.sleep(interval)
    except KeyboardInterrupt:
        log.info("收到退出信号，写回状态")
        save_state(state)
        log.info("========== 退出 ==========")


if __name__ == "__main__":
    main()
