"""feishu_app_bot.py — 飞书「自建应用」版机器人（独立于 webhook 版 monitor.py）

与 webhook 版的区别：
  * 用自建应用（app_id/app_secret）发消息，因此可以**更新已发出的卡片**；
  * 用长连接（WebSocket）接收卡片按钮回调，无需公网地址；
  * 评论卡片带「查看对方后续回复」按钮：点击后拉取该楼里
    「UP主动@某人 → 对方在 UP 之后回复」的内容，并**更新同一张卡片**。

本文件不修改 monitor.py 的任何行为；它只是额外复用 monitor.py 的抓取/状态函数。
依赖：lark-oapi（见 requirements-app.txt）。

用法：
  1. pip install -r requirements-app.txt
  2. 复制 app_config.example.yaml 为 app_config.yaml，填入 app_id/app_secret/chat_id
  3. python feishu_app_bot.py
"""
import json
import logging
import os
import sys
import threading
import time as _time

import monitor

log = logging.getLogger("monitor")


# ---------------------------------------------------------------------------
# App config
# ---------------------------------------------------------------------------
class AppConfigError(Exception):
    pass


def app_config_path():
    return os.path.join(monitor.base_dir(), "app_config.yaml")


def load_app_config(path=None):
    import yaml

    path = path or app_config_path()
    try:
        with open(path, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except FileNotFoundError:
        raise AppConfigError(
            f"配置文件 {path} 不存在，请从 app_config.example.yaml 复制一份并填入真实值"
        )
    required = ["app_id", "app_secret", "chat_id"]
    for key in required:
        val = cfg.get(key)
        if not val or (isinstance(val, str) and val.strip().startswith("<")):
            raise AppConfigError(f"配置项 {key} 缺失或仍为占位符，请编辑 app_config.yaml")
    return cfg


# ---------------------------------------------------------------------------
# Card building (pure, no lark dependency — unit-testable)
# ---------------------------------------------------------------------------
def followup_button(root_rpid, up_rpid):
    """卡片 2.0 按钮：点击回调服务端拉取后续回复。"""
    return {
        "tag": "button",
        "type": "primary",
        "text": {"tag": "plain_text", "content": "查看对方后续回复"},
        "behaviors": [
            {
                "type": "callback",
                "value": {
                    "action": "followups",
                    "root_rpid": str(root_rpid),
                    "up_rpid": str(up_rpid),
                },
            }
        ],
    }


def build_comment_card_v2(comment, opus_id=None, extra_elements=None):
    """评论卡片（应用版）：与 monitor.build_comment_card 同构，但回复类评论带按钮。"""
    ts = _time.strftime("%Y-%m-%d %H:%M:%S", _time.localtime(comment["ctime"]))
    uname = comment.get("uname") or "UP"
    context_full = comment.get("context_full") or comment.get("context")
    root_rpid = comment.get("root_rpid")

    elements = [{"tag": "markdown", "content": f"**时间**　{ts}"}]
    elements.append(monitor._build_context_element(context_full))

    elements.append({"tag": "markdown", "content": f"**内容**\n{comment['message']}"})

    foot = []
    if opus_id:
        foot.append(f"[在B站查看该动态](https://www.bilibili.com/opus/{opus_id})")
    if root_rpid:
        foot.append(f"楼ID `{root_rpid}`")
    if foot:
        elements.append({"tag": "markdown", "content": "　".join(foot)})

    # 只有「UP 回复某人」的评论才有对象可查后续
    if context_full and root_rpid:
        elements.append(followup_button(root_rpid, comment["rpid"]))

    if extra_elements:
        elements.extend(extra_elements)

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


def build_followup_elements(up_entry, parent_entry, replies):
    """按点击按钮后要展示的「后续回复」生成卡片元素。"""
    def _fmt(m):
        t = _time.strftime("%m-%d %H:%M", _time.localtime(m["ctime"]))
        who = (m.get("uname") or "?") + ("（UP主）" if m.get("is_up") else "")
        return f"**{who}**　{t}\n{m['message']}"

    body = _fmt(up_entry)
    if replies:
        body += "\n\n---\n\n" + "\n\n".join(_fmt(r) for r in replies)
    else:
        body += "\n\n---\n\n（对方尚未回复）"

    target = (parent_entry or {}).get("uname", "")
    title = f"对方后续回复 · @{target} · {len(replies)} 条 · 点击展开"
    return [monitor._collapsible_panel(title, body)]


def exchange_for_up(thread, up_rpid):
    """在已拉取的 thread 中，定位 up_rpid 这条 UP 消息及其对象、后续回复。"""
    by_rpid = {m["rpid"]: m for m in thread}
    up = by_rpid.get(str(up_rpid))
    if not up:
        return None, None, []
    parent = by_rpid.get(up.get("parent"))
    replies = []
    if parent:
        replies = [
            x for x in thread
            if x["mid"] == parent["mid"] and x["ctime"] > up["ctime"]
        ]
    return up, parent, replies


def comment_view_from_thread(up, parent, root_rpid):
    """把 thread 中的条目还原成 monitor 风格的 comment dict（用于重建卡片）。"""
    context_full = None
    if parent:
        prefix = f"@{parent.get('uname','')}: " if parent.get("uname") else ""
        context_full = prefix + parent["message"]
    return {
        "rpid": up["rpid"],
        "ctime": up["ctime"],
        "message": up["message"],
        "context": (context_full[:80] + "...") if context_full and len(context_full) > 80 else context_full,
        "context_full": context_full,
        "uname": up.get("uname", ""),
        "root_rpid": str(root_rpid),
    }


# ---------------------------------------------------------------------------
# Lark app client (requires lark-oapi)
# ---------------------------------------------------------------------------
def _lark():
    import lark_oapi as lark  # 延迟导入：未安装时纯函数仍可用/可测

    return lark


def send_card(client, chat_id, card):
    """用应用身份发互动卡片到群，返回 message_id 或 None。"""
    lark = _lark()
    from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody

    req = (
        CreateMessageRequest.builder()
        .receive_id_type("chat_id")
        .request_body(
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("interactive")
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.create(req)
    if not resp.success():
        log.error("发卡片失败: code=%s msg=%s", resp.code, resp.msg)
        return None
    return resp.data.message_id


def patch_card(client, message_id, card):
    """更新已发出的卡片。"""
    from lark_oapi.api.im.v1 import PatchMessageRequest, PatchMessageRequestBody

    req = (
        PatchMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            PatchMessageRequestBody.builder()
            .content(json.dumps(card, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = client.im.v1.message.patch(req)
    if not resp.success():
        log.error("更新卡片失败: code=%s msg=%s", resp.code, resp.msg)
        return False
    return True


def _toast_response(msg):
    try:
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        return P2CardActionTriggerResponse({"toast": {"type": "info", "content": msg}})
    except Exception:
        return None


def on_card_action(client, data):
    """卡片按钮回调：拉取「UP @某人 → 对方后续回复」，更新同一张卡片。"""
    try:
        value = (data.event.action.value or {}) if data.event and data.event.action else {}
        root_rpid = value.get("root_rpid")
        up_rpid = value.get("up_rpid")
        message_id = data.event.context.open_message_id
        if not (root_rpid and up_rpid and message_id):
            return _toast_response("参数不完整")

        cfg = monitor.load_config()
        comment_id = monitor.resolve_comment_id(cfg["opus_id"])
        thread = monitor.fetch_thread(
            cfg["bilibili"]["sessdata"], comment_id, root_rpid,
            cfg["up_uid"], opus_id=cfg["opus_id"],
        )
        up, parent, replies = exchange_for_up(thread, up_rpid)
        if not up:
            return _toast_response("未找到该评论")

        comment = comment_view_from_thread(up, parent, root_rpid)
        card = build_comment_card_v2(
            comment, opus_id=cfg["opus_id"],
            extra_elements=build_followup_elements(up, parent, replies),
        )
        patch_card(client, message_id, card)
        return _toast_response(f"已更新（{len(replies)} 条回复）")
    except Exception as e:
        log.error("处理按钮回调失败: %s", e, exc_info=True)
        return _toast_response("处理失败，请重试")


# ---------------------------------------------------------------------------
# Monitoring loop (sends via app instead of webhook)
# ---------------------------------------------------------------------------
def run_once_app(client, chat_id, cfg, state, comment_id):
    sessdata = cfg["bilibili"]["sessdata"]
    up_uid = cfg["up_uid"]

    if state["last_poll_ts"] == 0:
        threshold = int(_time.time()) - cfg["history_days"] * 86400
    else:
        threshold = state["last_poll_ts"] - cfg.get("overlap_seconds", 1800)

    comments = monitor.fetch_up_comments(
        sessdata, comment_id, up_uid, threshold, opus_id=cfg["opus_id"]
    )
    new_comments = [c for c in comments if c["rpid"] not in state["pushed_ids"]]
    log.info("命中 %d 条，新增 %d 条", len(comments), len(new_comments))

    gap = cfg.get("push_interval", 2)
    for c in new_comments:
        card = build_comment_card_v2(c, opus_id=cfg["opus_id"])
        if send_card(client, chat_id, card):
            state["pushed_ids"].add(c["rpid"])
            monitor.save_state(state)
            monitor.record_sent(c)
            log.info("推送成功 rpid=%s", c["rpid"])
            if len(new_comments) > 1:
                _time.sleep(gap)
        else:
            log.warning("推送失败 rpid=%s，下轮重试", c["rpid"])

    state["last_poll_ts"] = int(_time.time())
    monitor.save_state(state)


def run():
    monitor.setup_logging()
    log.info("========== 飞书应用版 启动 ==========")
    lark = _lark()

    app_cfg = load_app_config()
    client = (
        lark.Client.builder()
        .app_id(app_cfg["app_id"])
        .app_secret(app_cfg["app_secret"])
        .build()
    )
    chat_id = app_cfg["chat_id"]

    cfg = monitor.load_config()
    state = monitor.load_state()

    # 长连接：接收卡片按钮回调（后台线程）
    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_card_action_trigger(lambda data: on_card_action(client, data))
        .build()
    )
    ws = lark.ws.Client(
        app_cfg["app_id"], app_cfg["app_secret"],
        event_handler=handler, log_level=lark.LogLevel.INFO,
    )
    threading.Thread(target=ws.start, name="lark-ws", daemon=True).start()
    log.info("长连接已启动，等待卡片按钮回调...")

    comment_id = None
    while True:
        try:
            if comment_id is None:
                comment_id = monitor.resolve_comment_id(cfg["opus_id"])
                log.info("opus=%s -> comment_id=%s", cfg["opus_id"], comment_id)
            run_once_app(client, chat_id, cfg, state, comment_id)
        except Exception as e:
            log.error("本轮异常（跳过，下轮继续）: %s", e, exc_info=True)
        _time.sleep(monitor.get_poll_interval(cfg))


def list_chats():
    """列出机器人所在群的 chat_id（用于填写 app_config.yaml）。"""
    monitor.setup_logging()
    lark = _lark()
    app_cfg = load_app_config()
    client = (
        lark.Client.builder()
        .app_id(app_cfg["app_id"])
        .app_secret(app_cfg["app_secret"])
        .build()
    )
    from lark_oapi.api.im.v1 import ListChatRequest

    req = ListChatRequest.builder().page_size(100).build()
    resp = client.im.v1.chat.list(req)
    if not resp.success():
        log.error("获取群列表失败: code=%s msg=%s", resp.code, resp.msg)
        return
    log.info("机器人所在的群：")
    for c in (resp.data.items or []):
        print(f"  chat_id={c.chat_id}  name={c.name}")


def main():
    if "--help" in sys.argv[1:] or "-h" in sys.argv[1:]:
        print("用法: python feishu_app_bot.py [--list-chats] [--help]")
        print("  （无参数）     启动：长连接接收按钮回调 + 监控推送")
        print("  --list-chats   列出机器人所在群的 chat_id")
        print("  --help         显示本帮助")
        return
    if "--list-chats" in sys.argv[1:]:
        list_chats()
        return
    run()


if __name__ == "__main__":
    main()
