# B站UP主评论监控 → 飞书推送

监控 B 站 opus 动态评论区中 **UP 主本人**的新评论/回复，近实时推送到飞书群。

适合想第一时间收到某位 UP 主在动态评论区发言的场景（如付费动态下的文字内容）。

## 快速开始（最简单：直接下载 exe）

1. 到本仓库 [Releases](../../releases) 页面下载最新版 `up-chat-monitor.exe`
2. 双击运行，首次启动会进入**交互式配置向导**，按提示填写 4 项信息：
   - **opus ID**：动态页 `https://www.bilibili.com/opus/<这串数字>`
   - **up_uid**：UP主主页 `https://space.bilibili.com/<这串数字>`
   - **SESSDATA**：见下方"获取 B 站 SESSDATA"
   - **飞书 webhook**：见下方"创建飞书群机器人"
3. 配置保存后自动开始监控，`Ctrl+C` 安全退出

配置文件 `config.yaml`、运行状态 `state.json`、日志 `monitor.log` 都生成在 exe 同目录。

## 获取 B 站 SESSDATA（Cookie）

> **前提**：你已对该 UP 主充电（否则评论接口返回空）。

1. 浏览器登录 B 站
2. 按 F12 打开开发者工具 → **Application**（应用）标签
3. 左侧 Storage → Cookies → 点 `.bilibili.com`
4. 找到 `SESSDATA`，双击 Value 列复制

> SESSDATA 形如 `xxxxxx%2Cxxxxx`，有时效性（通常 30 天），过期后需重新配置。

**备选方案**：从 Network 标签中任选一个 `api.bilibili.com` 的请求，在 Request Headers 的 `Cookie` 字段中复制 `SESSDATA=xxx` 部分。

## 创建飞书群机器人 webhook

1. 飞书群 → 设置 → 群机器人 → 添加机器人 → **自定义机器人**，这里需要注意，只能在电脑版飞书上添加机器人，手机版无法添加机器人
2. 复制 webhook 地址，形如 `https://open.feishu.cn/open-apis/bot/v2/hook/xxx`

## 使用 Python 源码运行

```bash
pip install -r requirements.txt
python monitor.py          # 首次运行自动进入配置向导
python monitor.py --setup  # 重新配置
python monitor.py --help
```

## 自己编译 exe

Windows 下双击 `build.bat`（需 Python 3.10+），产物在 `dist\up-chat-monitor.exe`。

```bash
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onefile --console --name up-chat-monitor monitor.py
```

向本仓库推送 `v*` 标签会通过 GitHub Actions 自动编译并发布 Release。

## 配置文件说明

配置向导会生成 `config.yaml`，也可以手动复制 `config.example.yaml` 编辑：

```yaml
opus_id: "<opus静态ID>"         # 监控的动态 ID
up_uid: "<UP主的mid>"           # UP主的用户 ID
poll_interval: 180              # 轮询间隔，秒
history_days: 3                 # 冷启动回溯天数
push_interval: 2                # 连续推送间隔，秒
fast_interval: 60               # 工作日 9:00-15:00 的快轮询间隔
slow_interval: 300              # 其余时段的慢轮询间隔

bilibili:
  sessdata: "<你的SESSDATA>"    # 敏感，请勿公开/提交

feishu:
  webhook: "<飞书群机器人webhook URL>"
```

## 冷启动 / 热启动

- **冷启动**（无 `state.json` 或手动删除）：推送近 `history_days` 天内 UP 的历史评论，然后是正常增量。
- **热启动**（有 `state.json`）：纯增量，只推上次轮询之后的新评论。
- 删除 `state.json` = 重置，下次启动重新冷启动。

## 推送内容

飞书互动卡片，包含：
- **时间**：评论发布时间
- **上下文**：被回复的评论内容（截断 80 字）；主动发布则显示"（主动发布）"
- **内容**：评论正文（图片以 `[图片]` 占位）

## 告警

脚本会通过飞书推送以下告警：
- **Cookie 失效**（B站接口返回 -101）：提示重新配置 SESSDATA
- **触发风控**（B站接口返回 -412/-509 或 HTTP 403）：提示等待或更换 cookie

## 文件说明

| 文件 | 说明 |
|------|------|
| `monitor.py` | 主程序：配置、抓取、过滤、推送、主循环 |
| `cdp_extract.py` | 实验性调试工具（用 Edge CDP 打开页面提取评论），日常使用不需要 |
| `config.example.yaml` | 配置模板，不含敏感值 |
| `config.yaml` | 实际配置（含 cookie，已被 gitignore，请勿提交） |
| `state.json` | 运行状态，自动生成（已推送的评论 ID 集合） |
| `monitor.log` | 运行日志 |
| `build.bat` | Windows 一键编译脚本 |

## 运行测试

```bash
python -m unittest discover -s tests -v
```

## 免责声明

- 本项目仅供个人学习与监控**公开评论**使用；付费动态需自行取得访问权限，请遵守 B 站与飞书的相关服务条款。
- `SESSDATA` 等同于你的 B 站登录凭证，请勿泄露或提交到公开仓库。

## License

[MIT](LICENSE)
