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
        "poll_start_hour": 9,
        "poll_end_hour": 15,
        "poll_weekday_only": True,
        "overlap_seconds": 1800,
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
    return {
        "pushed_ids": set(),
        "last_poll_ts": 0,
        "boot_ts": int(_time.time()),
        "followup_watch": [],   # 待观察「对方后续回复」的 UP@ 评论
    }


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
        "followup_watch": raw.get("followup_watch", []) or [],
    }


def save_state(state, path=None):
    path = path or os.path.join(base_dir(), "state.json")
    payload = {
        "pushed_ids": sorted(state["pushed_ids"]),
        "last_poll_ts": state["last_poll_ts"],
        "boot_ts": state["boot_ts"],
        "followup_watch": state.get("followup_watch", []),
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


def _truncate_context(msg, n=80):
    if not msg:
        return None
    return msg if len(msg) <= n else msg[:n] + "..."


def _build_comment(raw, parent=None):
    reply_to = raw.get("reply_to") or {}
    message = _get_message(raw)
    if raw.get("pictures"):
        message += "".join(" [图片]" for _ in raw["pictures"])
    context_full = None
    target_mid = ""
    target_uname = ""
    if reply_to:
        context_full = _get_message(reply_to) or None
    elif parent is not None:
        context_full = _get_message(parent) or None
        target_mid = str((parent.get("member") or {}).get("mid") or "")
        target_uname = (parent.get("member") or {}).get("uname", "")
    return {
        "rpid": str(raw["rpid"]),
        "ctime": raw["ctime"],
        "message": message,
        "context": _truncate_context(context_full),
        "context_full": context_full,
        "uname": (raw.get("member") or {}).get("uname", ""),
        "target_mid": target_mid,
        "target_uname": target_uname,
    }


def _build_comment_with_context(raw, all_cache):
    """构建评论，用 parent 字段从缓存回溯被回复的评论作为上下文。"""
    message = _get_message(raw)
    if raw.get("pictures"):
        message += "".join(" [图片]" for _ in raw["pictures"])
    context_full = None
    target_mid = ""
    target_uname = ""
    reply_to = raw.get("reply_to")
    if reply_to and _get_message(reply_to):
        context_full = _get_message(reply_to)
    else:
        # reply_to 为空时，用 parent rpid 从缓存找被回复的评论
        parent_rpid = raw.get("parent")
        if parent_rpid and parent_rpid in all_cache:
            parent_raw = all_cache[parent_rpid]
            parent_uname = (parent_raw.get("member") or {}).get("uname", "")
            target_mid = str((parent_raw.get("member") or {}).get("mid") or "")
            target_uname = parent_uname
            ctx_msg = _get_message(parent_raw)
            if ctx_msg:
                prefix = f"@{parent_uname}: " if parent_uname else ""
                context_full = prefix + ctx_msg
    return {
        "rpid": str(raw["rpid"]),
        "ctime": raw["ctime"],
        "message": message,
        "context": _truncate_context(context_full),
        "context_full": context_full,
        "uname": (raw.get("member") or {}).get("uname", ""),
        "target_mid": target_mid,
        "target_uname": target_uname,
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
            c["root_rpid"] = str(root_rpid)
            if c["ctime"] >= threshold_ts:
                results.append(c)
    return results


def fetch_sub_replies_since(sessdata, comment_id, root_rpid, since_ctime, opus_id=None, max_pages=20):
    """从最后一页往前翻，返回该楼中 ctime >= since_ctime 的子回复（所有人）。

    子回复按时间正序，新回复在最后几页，所以从后往前翻很快。
    """
    out = []
    referer = f"https://www.bilibili.com/opus/{opus_id}" if opus_id else "https://www.bilibili.com/"
    headers = {
        "User-Agent": UA,
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": referer,
    }
    params = {"oid": comment_id, "type": 11, "root": root_rpid, "pn": 1, "ps": 20}
    resp = _do_request("GET", REPLY_URL, headers=headers, params=params, timeout=15)
    if resp.status_code != 200:
        return out
    try:
        data = resp.json()
    except Exception:
        return out
    if data.get("code") != 0:
        return out
    page = (data.get("data") or {}).get("page") or {}
    total = page.get("count", 0)
    if total == 0:
        return out
    total_pages = min((total + 19) // 20, max_pages)

    for pn in range(total_pages, 0, -1):
        if pn != total_pages:
            _time.sleep(1)  # 防风控
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
        reps = (data.get("data") or {}).get("replies") or []
        if not reps:
            continue
        newest = max((s.get("ctime", 0) for s in reps), default=0)
        for s in reps:
            if s.get("ctime", 0) >= since_ctime:
                out.append(s)
        if newest < since_ctime:
            break  # 更早的页都是旧回复
    return out


def fetch_up_comments(sessdata, comment_id, up_uid, threshold_ts, max_pages=5, opus_id=None):
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
                c["root_rpid"] = str(rpid)
                if c["ctime"] >= threshold_ts:
                    results.append(c)
            # 接口内嵌的子回复（前几条）
            for sub in r.get("replies") or []:
                if str((sub.get("member") or {}).get("mid")) == up_uid:
                    c = _build_comment(sub, parent=r)
                    c["root_rpid"] = str(rpid)
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
# Export & Sent History
# ---------------------------------------------------------------------------
def sent_history_path():
    return os.path.join(base_dir(), "sent_history.jsonl")


def record_sent(comment):
    """推送成功后追加一条本地发送记录（jsonl），用于日后完整导出。"""
    rec = {
        "rpid": str(comment["rpid"]),
        "ctime": comment["ctime"],
        "message": comment["message"],
        "context": comment.get("context"),
        "uname": comment.get("uname", ""),
        "sent_at": int(_time.time()),
    }
    try:
        with open(sent_history_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("写入发送记录失败: %s", e)


def load_sent_history():
    """读取本地发送记录，返回 {rpid: comment}。"""
    out = {}
    try:
        with open(sent_history_path(), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                out[str(rec.get("rpid"))] = {
                    "rpid": str(rec.get("rpid")),
                    "ctime": rec.get("ctime", 0),
                    "message": rec.get("message", ""),
                    "context": rec.get("context"),
                    "uname": rec.get("uname", ""),
                }
    except FileNotFoundError:
        pass
    return out


def _write_markdown(comments, cfg, out_path, title):
    lines = [
        f"# {title}",
        "",
        f"> UP主UID：{cfg['up_uid']}",
        f"> 动态链接：https://www.bilibili.com/opus/{cfg['opus_id']}",
        f"> 导出时间：{_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 共 {len(comments)} 条",
        "",
    ]
    current_date = None
    for c in comments:
        date_str = _time.strftime("%Y-%m-%d", _time.localtime(c["ctime"]))
        time_str = _time.strftime("%H:%M:%S", _time.localtime(c["ctime"]))
        if date_str != current_date:
            current_date = date_str
            lines.extend([f"## {date_str}", ""])
        if c.get("context"):
            lines.append(f"**{time_str}**　（回复 {c['context']}）")
        else:
            lines.append(f"**{time_str}**")
        lines.append("")
        lines.append(c["message"])
        lines.append("")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out_path


def export_history(cfg, out_path=None, sent_only=False):
    """导出评论到本地 Markdown 文档。

    sent_only=True 时只导出「已推送到飞书」的评论（state.json 的 pushed_ids
    ∪ 本地 sent_history.jsonl）。
    """
    comment_id = resolve_comment_id(cfg["opus_id"])
    fetched = fetch_up_comments(
        cfg["bilibili"]["sessdata"],
        comment_id,
        cfg["up_uid"],
        threshold_ts=0,
        max_pages=10,
        opus_id=cfg["opus_id"],
    )

    merged = {c["rpid"]: c for c in fetched}
    local = load_sent_history()
    for rpid, c in local.items():
        merged.setdefault(rpid, c)  # 本地记录优先保留（B站可能已删）

    if sent_only:
        state = load_state()
        sent_ids = set(state["pushed_ids"]) | set(local.keys())
        comments = [c for c in merged.values() if c["rpid"] in sent_ids]
        title = "UP主评论汇总（已发送）"
    else:
        comments = list(merged.values())
        title = "UP主评论汇总"

    comments.sort(key=lambda c: c["ctime"])
    if out_path is None:
        date_str = _time.strftime("%Y%m%d")
        suffix = "已发送_" if sent_only else ""
        out_path = os.path.join(base_dir(), f"评论汇总_{suffix}{date_str}.md")
    return _write_markdown(comments, cfg, out_path, title)


def fetch_thread(sessdata, comment_id, root_rpid, up_uid, opus_id=None, max_pages=50):
    """拉取某一楼的完整对话：根评论 + 全部子回复（所有人），按时间升序。

    返回 [{uname, message, ctime, is_up, mid}]。
    """
    headers = {
        "User-Agent": UA,
        "Cookie": f"SESSDATA={sessdata}",
        "Referer": f"https://www.bilibili.com/opus/{opus_id}" if opus_id else "https://www.bilibili.com/",
    }
    thread = []
    seen = set()

    def _add(raw, is_root=False):
        rpid = str(raw.get("rpid"))
        if rpid in seen:
            return
        msg = _get_message(raw)
        if not msg:
            return
        seen.add(rpid)
        mid = str((raw.get("member") or {}).get("mid") or "")
        thread.append({
            "rpid": rpid,
            "uname": (raw.get("member") or {}).get("uname", ""),
            "message": msg,
            "ctime": raw.get("ctime", 0),
            "is_up": mid == up_uid,
            "mid": mid,
            "parent": str(raw.get("parent") or ""),
            "is_root": is_root,
        })

    # 根评论：从 reply/main 里找
    next_cursor = 0
    for _ in range(5):
        try:
            data = fetch_reply_page(sessdata, comment_id, page=next_cursor)
        except FetchError:
            break
        for r in (data.get("top_replies") or []) + (data.get("replies") or []):
            if str(r.get("rpid")) == str(root_rpid):
                _add(r, is_root=True)
        cur = data.get("cursor") or {}
        if cur.get("is_end"):
            break
        next_cursor = cur.get("next", 0)

    # 全部子回复
    for pn in range(1, max_pages + 1):
        params = {"oid": comment_id, "type": 11, "root": root_rpid, "pn": pn, "ps": 20}
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
        for s in replies:
            _add(s)
        if len(replies) < 20:
            break
        _time.sleep(1)  # 防风控

    thread.sort(key=lambda x: x["ctime"])
    return thread


def analyze_mentioned_replies(thread, up_uid):
    """找出一楼里「UP 主动@某人」以及该人在 UP 这条消息之后的回复。

    匹配规则：UP 的消息 m（其 parent 指向某人 X 的消息）为发起；
    取 X 在该楼中 ctime 晚于 m.ctime 的回复。按时序返回：
    [{up: entry, target_uname, target_mid, replies: [entry, ...]}]
    """
    by_rpid = {m["rpid"]: m for m in thread}
    out = []
    for m in thread:
        if not m.get("is_up"):
            continue
        parent = by_rpid.get(m.get("parent"))
        if not parent:
            continue
        if parent["mid"] == up_uid:  # UP 回复自己，不算 @ 别人
            continue
        target_mid = parent["mid"]
        replies = [
            x for x in thread
            if x["mid"] == target_mid and x["ctime"] > m["ctime"]
        ]
        out.append({
            "up": m,
            "target_uname": parent.get("uname", ""),
            "target_mid": target_mid,
            "replies": replies,
        })
    out.sort(key=lambda x: x["up"]["ctime"])
    return out


def export_replies(cfg, root_rpid, out_path=None, push=True):
    """按需拉取指定楼，提取「UP 主动@某人 → 对方后续回复」，存本地并推送。"""

    def _fmt(m):
        ts = _time.strftime("%m-%d %H:%M", _time.localtime(m["ctime"]))
        who = (m.get("uname") or "?") + ("（UP主）" if m.get("is_up") else "")
        return f"**{who}**　{ts}\n{m['message']}"

    comment_id = resolve_comment_id(cfg["opus_id"])
    thread = fetch_thread(
        cfg["bilibili"]["sessdata"], comment_id, root_rpid,
        cfg["up_uid"], opus_id=cfg["opus_id"],
    )
    exchanges = analyze_mentioned_replies(thread, cfg["up_uid"])

    lines = [
        f"# UP主动@对话 · 楼 {root_rpid}",
        "",
        f"> 动态链接：https://www.bilibili.com/opus/{cfg['opus_id']}",
        f"> 导出时间：{_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 共 {len(exchanges)} 段",
        "",
    ]
    for ex in exchanges:
        t = _time.strftime("%m-%d %H:%M", _time.localtime(ex["up"]["ctime"]))
        lines.extend([f"## UP @ {ex['target_uname']}（{t}）", "", _fmt(ex["up"]), ""])
        if ex["replies"]:
            for r in ex["replies"]:
                lines.extend([_fmt(r), ""])
        else:
            lines.extend(["（对方尚未回复）", ""])

    if out_path is None:
        out_path = os.path.join(base_dir(), f"UP@对话_{root_rpid}.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    if push:
        elements = [{
            "tag": "markdown",
            "content": (
                f"**UP主动@对话**　{len(exchanges)} 段\n"
                f"[在B站查看该动态](https://www.bilibili.com/opus/{cfg['opus_id']})"
            ),
        }]
        if not exchanges:
            elements.append({"tag": "markdown", "content": "本楼未发现 UP 主动@某人的对话。"})
        for ex in exchanges:
            panel_title = f"UP @ {ex['target_uname']} · {len(ex['replies'])} 条回复 · 点击展开"
            body = _fmt(ex["up"])
            if ex["replies"]:
                body += "\n\n---\n\n" + "\n\n".join(_fmt(r) for r in ex["replies"])
            else:
                body += "\n\n---\n\n（对方尚未回复）"
            elements.append(_collapsible_panel(panel_title, body))
        payload = {
            "msg_type": "interactive",
            "card": {
                "schema": "2.0",
                "header": {
                    "title": {"tag": "plain_text", "content": f"UP主动@对话 · 楼 {root_rpid}"},
                    "template": "turquoise",
                },
                "body": {"elements": elements},
            },
        }
        send_feishu(cfg["feishu"]["webhook"], payload)
    return out_path


# ---------------------------------------------------------------------------
# Feishu Push
# ---------------------------------------------------------------------------
def _collapsible_panel(title, content):
    """飞书卡片 2.0 折叠面板：默认折叠，点击展开显示完整 content。"""
    return {
        "tag": "collapsible_panel",
        "expanded": False,
        "header": {
            "title": {"tag": "markdown", "content": f"**{title}**"},
            "vertical_align": "center",
            "icon": {
                "tag": "standard_icon",
                "token": "down-small-ccm_outlined",
                "size": "16px 16px",
            },
            "icon_position": "right",
            "icon_expanded_angle": -180,
        },
        "border": {"color": "grey", "corner_radius": "5px"},
        "elements": [{"tag": "markdown", "content": content}],
    }


def build_comment_card(comment, opus_id=None):
    ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(comment["ctime"]))
    uname = comment.get("uname") or "UP"
    context_full = comment.get("context_full") or comment.get("context")

    elements = [{"tag": "markdown", "content": f"**时间**　{ts}"}]

    if context_full:
        preview = context_full if len(context_full) <= 80 else context_full[:80] + "..."
        elements.append(_collapsible_panel("上下文 · 点击展开", f"**预览**：{preview}\n\n---\n\n{context_full}"))
    else:
        elements.append({"tag": "markdown", "content": "**上下文**　（主动发布）"})

    elements.append({"tag": "markdown", "content": f"**内容**\n{comment['message']}"})

    foot = []
    if opus_id:
        foot.append(f"[在B站查看该动态](https://www.bilibili.com/opus/{opus_id})")
    root_rpid = comment.get("root_rpid")
    if root_rpid:
        foot.append(f"楼ID `{root_rpid}`")
    if foot:
        elements.append({"tag": "markdown", "content": "　".join(foot)})

    # UP 回复了某人 → 提示可用命令按需查看对方的回复
    if context_full and root_rpid:
        elements.append({
            "tag": "markdown",
            "content": f"查看该楼「UP主动@对话」：`python monitor.py --replies {root_rpid}`",
        })

    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": f"{uname} 新评论"},
                "template": "green",
            },
            "body": {"elements": elements},
        },
    }


def build_alert_card(title, body):
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red",
            },
            "body": {"elements": [{"tag": "markdown", "content": body}]},
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
def build_followup_reply_card(reply, up_comment, opus_id=None):
    """对方在 UP 之后回复时的补充卡片。"""
    ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(reply["ctime"]))
    uname = reply.get("uname") or "对方"
    target = up_comment.get("target_uname") or ""
    elements = [{"tag": "markdown", "content": f"**时间**　{ts}"}]
    elements.append(
        _collapsible_panel(
            "UP 的原评论 · 点击展开",
            f"**{up_comment.get('uname','UP')}**：{up_comment['message']}",
        )
    )
    elements.append({"tag": "markdown", "content": f"**{uname} 的回复**\n{reply['message']}"})
    if opus_id and up_comment.get("root_rpid"):
        elements.append({
            "tag": "markdown",
            "content": f"[在B站查看该动态](https://www.bilibili.com/opus/{opus_id})　楼ID `{up_comment['root_rpid']}`",
        })
    return {
        "msg_type": "interactive",
        "card": {
            "schema": "2.0",
            "header": {
                "title": {"tag": "plain_text", "content": f"「{uname}」回复了{('@'+target) if target else 'UP'}"},
                "template": "orange",
            },
            "body": {"elements": elements},
        },
    }


def _add_watch(state, c):
    """把「UP 主动@某人」的评论加入待观察列表，用于后续检测对方回复。"""
    watch = state.setdefault("followup_watch", [])
    if any(e.get("up_rpid") == c["rpid"] for e in watch):
        return
    watch.append({
        "root_rpid": c.get("root_rpid", ""),
        "up_rpid": c["rpid"],
        "up_ctime": c["ctime"],
        "up_message": c["message"],
        "up_uname": c.get("uname", ""),
        "target_mid": c.get("target_mid", ""),
        "target_uname": c.get("target_uname", ""),
        "created": int(_time.time()),
    })
    if len(watch) > 30:  # 只保留最近 30 条
        del watch[:-30]


def _process_followups(cfg, state, comment_id):
    """检查待观察列表：对方若在 UP 之后回复，补充推送一张卡片。"""
    watch = state.get("followup_watch", [])
    if not watch:
        return
    webhook = cfg["feishu"]["webhook"]
    max_days = cfg.get("followup_watch_days", 3)
    now = int(_time.time())
    kept = []
    for e in watch:
        if now - e.get("created", now) > max_days * 86400:
            continue  # 过期丢弃
        if not e.get("target_mid") or not e.get("root_rpid"):
            continue
        try:
            reps = fetch_sub_replies_since(
                cfg["bilibili"]["sessdata"], comment_id, e["root_rpid"],
                e["up_ctime"], opus_id=cfg["opus_id"],
            )
        except Exception as ex:
            log.warning("检查后续回复失败 root=%s: %s", e["root_rpid"], ex)
            kept.append(e)
            continue
        for s in reps:
            mid = str((s.get("member") or {}).get("mid") or "")
            rpid = str(s.get("rpid"))
            if mid != e["target_mid"] or rpid in state["pushed_ids"]:
                continue
            reply = {
                "rpid": rpid,
                "ctime": s.get("ctime", 0),
                "message": _get_message(s),
                "context": _truncate_context(f"@{e.get('up_uname', 'UP')}: {e.get('up_message', '')}"),
                "uname": (s.get("member") or {}).get("uname", ""),
            }
            up_comment = {
                "message": e.get("up_message", ""),
                "uname": e.get("up_uname", "UP"),
                "root_rpid": e["root_rpid"],
                "target_uname": e.get("target_uname", ""),
            }
            card = build_followup_reply_card(reply, up_comment, opus_id=cfg["opus_id"])
            if send_feishu(webhook, card):
                state["pushed_ids"].add(rpid)
                record_sent(reply)
                save_state(state)
                log.info("补充推送「对方回复」rpid=%s（对象 %s）", rpid, e.get("target_uname"))
        kept.append(e)
    state["followup_watch"] = kept
    save_state(state)


def run_once(cfg, state, comment_id):
    sessdata = cfg["bilibili"]["sessdata"]
    up_uid = cfg["up_uid"]
    webhook = cfg["feishu"]["webhook"]

    if state["last_poll_ts"] == 0:
        threshold = int(_time.time()) - cfg["history_days"] * 86400
        log.info("冷启动：回溯 %d 天，阈值 %s", cfg["history_days"], threshold)
    else:
        # 回看 overlap 窗口：B站评论 API 有可见性延迟（秒~分钟级），
        # 刚发布的评论可能下一轮才出现，用 last_poll_ts 原值会把
        # ctime 早于它的评论永久过滤掉。靠 pushed_ids 去重防重推。
        overlap = cfg.get("overlap_seconds", 1800)
        threshold = state["last_poll_ts"] - overlap
        log.info(
            "热启动：阈值 %s（回看 %d 秒）", threshold, overlap
        )

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
        card = build_comment_card(c, opus_id=cfg["opus_id"])
        ok = send_feishu(webhook, card)
        if ok:
            state["pushed_ids"].add(c["rpid"])
            record_sent(c)
            if c.get("target_mid"):
                _add_watch(state, c)   # UP 主动@了某人 → 观察对方后续回复
            save_state(state)
            log.info("推送成功 rpid=%s", c["rpid"])
            if len(new_comments) > 1:
                _time.sleep(push_gap)
        else:
            log.warning("推送失败 rpid=%s，下轮重试", c["rpid"])

    # 检查观察中的「对方后续回复」
    _process_followups(cfg, state, comment_id)

    state["last_poll_ts"] = int(_time.time())
    save_state(state)


def get_poll_interval(cfg, now=None):
    """活跃时段（默认工作日 9:00-15:00）快轮询，其余时段慢轮询。

    时段与节奏全部可配置（config.yaml）：
      poll_start_hour / poll_end_hour  活跃时段起止小时（默认 9 / 15）
      poll_weekday_only                仅工作日快轮询（默认 true）
      fast_interval / slow_interval    快/慢轮询间隔秒数（默认 60 / 300）
    """
    import datetime

    if now is None:
        now = datetime.datetime.now()
    start = cfg.get("poll_start_hour", 9)
    end = cfg.get("poll_end_hour", 15)
    weekday_only = cfg.get("poll_weekday_only", True)
    fast = cfg.get("fast_interval", 60)
    slow = cfg.get("slow_interval", 300)

    in_active = start <= now.hour < end
    if weekday_only and now.weekday() >= 5:
        in_active = False
    return fast if in_active else slow


def _disable_quick_edit():
    """Windows 下禁用控制台 QuickEdit 模式。

    双击运行 exe 的控制台默认开启 QuickEdit：用户点击窗口选中文字会
    冻结整个进程（表现为"程序卡死/不再推送"，需重启）。禁用后点击
    窗口不再冻结；复制请用右键->标记以外的 Ctrl+Shift+C 或窗口菜单。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        STD_INPUT_HANDLE = -10
        ENABLE_QUICK_EDIT_MODE = 0x0040
        ENABLE_EXTENDED_FLAGS = 0x0080
        handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if not handle or handle == ctypes.c_void_p(-1).value:
            return
        mode = ctypes.c_uint()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return
        new_mode = (mode.value & ~ENABLE_QUICK_EDIT_MODE) | ENABLE_EXTENDED_FLAGS
        kernel32.SetConsoleMode(handle, new_mode)
    except Exception:
        pass  # 非交互控制台（管道/服务）下失败不影响功能


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
    _disable_quick_edit()
    setup_logging()
    log.info("========== 启动 ==========")
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("用法: python monitor.py [--export] [--export-all] [--replies <楼ID>] [--config] [--setup] [--help]")
        print("  （无参数）    开始监控，config.yaml 不存在时自动打开配置窗口")
        print("  --export      导出已推送(已发送)的评论记录为本地 Markdown 文档")
        print("  --export-all  导出UP主全部历史评论为本地 Markdown 文档")
        print("  --replies <楼ID>  按需查看该楼里「UP主动@某人 → 对方回复」，存本地并推送到飞书")
        print("  --config      打开图形配置窗口（保存后开始监控）")
        print("  --setup       运行终端配置向导（无人值守场景）")
        print("  --help        显示本帮助")
        return
    if "--replies" in sys.argv[1:]:
        idx = sys.argv.index("--replies")
        if idx + 1 >= len(sys.argv):
            log.error("--replies 需要跟一个楼ID，例如：python monitor.py --replies 316508517185")
            return
        root_rpid = sys.argv[idx + 1]
        try:
            cfg = load_config()
        except ConfigError as e:
            log.error("%s", e)
            return
        log.info("正在拉取楼 %s，查找 UP 主动@对话...", root_rpid)
        try:
            out = export_replies(cfg, root_rpid)
            log.info("已导出并推送到飞书：%s", out)
        except Exception as e:
            log.error("拉取失败: %s", e, exc_info=True)
        return
    if "--export" in sys.argv[1:] or "--export-all" in sys.argv[1:]:
        sent_only = "--export-all" not in sys.argv[1:]
        try:
            cfg = load_config()
        except ConfigError as e:
            log.error("%s", e)
            return
        log.info("正在拉取评论，请稍候（含楼中楼，可能需要几分钟）...")
        try:
            out = export_history(cfg, sent_only=sent_only)
            log.info("已导出到 %s", out)
        except Exception as e:
            log.error("导出失败: %s", e, exc_info=True)
        return
    if "--config" in sys.argv[1:]:
        _open_config_editor()
        cfg = load_config()
        state = load_state()
        _run_loop(cfg, state)
        return
    if "--setup" in sys.argv[1:]:
        interactive_setup()
        return
    try:
        cfg = load_config()
    except ConfigError as e:
        log.warning("%s", e)
        log.info("未找到有效配置，打开配置窗口...")
        if not _open_config_editor():
            log.info("未完成配置，退出。可运行程序加 --config 参数重新配置")
            return
        cfg = load_config()
    state = load_state()
    _run_loop(cfg, state)


def _open_config_editor():
    """优先打开图形配置窗口；tkinter 不可用时回退终端向导。

    返回 True 表示配置已就绪。
    """
    try:
        import config_gui

        return config_gui.open_config_window()
    except ImportError:
        log.warning("图形界面不可用（缺少 tkinter），改用终端向导")
        return interactive_setup()


def _run_loop(cfg, state):
    try:
        comment_id = resolve_comment_id(cfg["opus_id"])
        log.info("opus=%s -> comment_id=%s", cfg["opus_id"], comment_id)
    except Exception as e:
        log.error("解析 comment_id 失败: %s，将在循环中重试", e)
        comment_id = None
    log.info(
        "监控 up_uid=%s 活跃时段=%d-%d点(仅工作日=%s) 快轮询=%ds 慢轮询=%ds 冷启动=%s",
        cfg["up_uid"],
        cfg.get("poll_start_hour", 9),
        cfg.get("poll_end_hour", 15),
        cfg.get("poll_weekday_only", True),
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
