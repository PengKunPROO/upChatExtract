# 飞书「自建应用」版机器人（可选，独立于 webhook 版）

`feishu_app_bot.py` 是**独立**于 `monitor.py` 的可选方案，用来实现：
**评论卡片上带「查看对方后续回复」按钮，点击后在「同一张卡片」里展开
「UP 主动@某人 → 对方在 UP 之后回复」的内容。**

群机器人 webhook 只能"发"不能"改"，所以"同一张卡片内点击展开"必须用自建应用。
本方案**不修改也不影响** `monitor.py` 的行为，可单独使用或与 webhook 版并存。

---

## 一、安装依赖

```bash
pip install -r requirements-app.txt
```

> 依赖 `lark-oapi`（用于长连接接收按钮回调）。若你的网络装不上，可换国内镜像：
> `pip install lark-oapi -i https://pypi.tuna.tsinghua.edu.cn/simple`

**若 pip 走代理卡死/报 `check_hostname requires server_hostname`**（旧版 pip 自带 urllib3 的代理 bug），
用下面方式绕过：先用能联网的工具把 wheel 下到本地，再离线安装：

```bash
# 1) 用你已开的代理下载 wheel（也可用浏览器/下载器下）
#    需要：lark-oapi, requests-toolbelt, pycryptodome, websockets, httpx, httpcore, h11, anyio, sniffio, certifi, idna
# 2) 离线安装（不走网络）
pip install --no-index --find-links=<wheel所在目录> --user lark-oapi
```

> 已知：`--user` 可避开系统 Scripts 目录写权限问题导致安装回滚。

---

## 二、创建飞书自建应用

1. 打开 [飞书开放平台](https://open.feishu.cn/) → 开发者后台 → **创建企业自建应用**
2. 记下 **App ID**（`cli_...`）和 **App Secret**
3. **添加应用能力** → 机器人

## 三、配置权限

在「权限管理」里开通（至少）：
- `im:message`（获取与发送单聊、群组消息）
- `im:message:send_as_bot`（以应用身份发消息）

## 四、配置事件订阅（长连接）

1. 进入「事件与回调」→ **订阅方式** 选 **长连接**（无需公网地址）
2. 添加**回调**：`card.action.trigger`（卡片回传交互）

## 五、发布应用

在「版本管理与发布」创建版本并发布（企业内应用一般管理员可直接通过）。
把机器人**加进目标群**：群设置 → 群机器人 → 添加机器人 → 选择该应用。

## 六、配置 app_config.yaml

```bash
copy app_config.example.yaml app_config.yaml
```

```yaml
app_id: "cli_xxxxxxxx"
app_secret: "你的 App Secret"
chat_id: "oc_xxxxxxxx"
```

**获取 `chat_id`**：把机器人加进群后运行：

```bash
python feishu_app_bot.py --list-chats
```

会打印机器人所在群的 `chat_id` 和群名，复制对应群的 `chat_id`。

## 七、运行

```bash
python feishu_app_bot.py
```

- 监控 UP 新评论，用应用身份把评论卡片发到群
- UP **回复某人**的评论，卡片上会有「查看对方后续回复」按钮
- 点击按钮 → 本地长连接收到回调 → 拉取该楼后续 → **更新同一张卡片**

---

## 与 webhook 版（monitor.py）的区别

| | monitor.py（群机器人 webhook） | feishu_app_bot.py（自建应用） |
|---|---|---|
| 发送消息 | webhook，一条单向 | 应用 API，可更新 |
| 卡片按钮回调 | 不支持 | 支持（长连接） |
| 能否更新已发卡片 | 不能 | 能（同一张卡片展开后续） |
| 配置复杂度 | 一个 webhook URL | 建应用 + 权限 + 事件订阅 + chat_id |
| 依赖 | requests/pyyaml | 额外需要 lark-oapi |

两者共用同一份 `config.yaml`（B站相关配置）；`app_config.yaml` 仅应用版使用。
不要同时运行两个（会重复推送）。
