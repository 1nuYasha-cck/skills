# 额度文案、状态栏与用量命令的识别规则

每种 agent kind 一节。`scripts/quota-lib.sh` 按本文件实现；新增 kind 时新增一节，并在 `qp_classify` 里注册。
「实测」一栏：**已实测**指在本机真实运行得到；**源码**指来自该 agent 的开源代码；**第三方**指来自文章或 issue。除「已实测」外，都不得当作已验证。

识别只匹配屏幕**末尾 15 行**（避免 agent 自己输出里引用额度字样造成误判）。

## 通用：分类结果

| 分类 | 含义 | watcher 动作 |
| --- | --- | --- |
| `NONE` | 末尾没有额度文案 | 派发模式通知用户；监视模式视为目标已停下 |
| `LIMIT_UNTIMED` | 额度受限且没有恢复时间（工作区无额度、超出支出上限等） | 通知用户，不等待 |
| `CLAUDE_AUTO` | Claude 自带自动续跑（`continuing automatically at`） | 只监视，到点加宽限期仍停着才介入 |
| `LIMIT_TIMED` | 额度受限，文案里能解析出恢复时间 | 等到恢复时间加缓冲，再继续 |
| `LIMIT_NO_TIME` | 额度受限，文案里解析不出恢复时间 | 依次看状态栏、用量命令；仍未知则每 30 分钟探测，最长 12 小时 |

优先级：`LIMIT_UNTIMED` > `CLAUDE_AUTO` > `LIMIT_TIMED` / `LIMIT_NO_TIME`。

## Codex

### 报错文案（来源：开源代码 `codex-rs/protocol/src/error.rs`，**未在本机实测**）

| 文案 | 分类 |
| --- | --- |
| `You've hit your usage limit.` …… `Try again at <时间>.` | `LIMIT_TIMED` |
| `You've hit your usage limit.` …… `Try again later.` | `LIMIT_NO_TIME` |
| `You've hit your usage limit for <名称>. Switch to another model now,` | 同上，按是否带时间区分 |
| `Your workspace is out of credits.` | `LIMIT_UNTIMED` |
| `You hit your spend cap set in your workspace.` | `LIMIT_UNTIMED` |
| `Quota exceeded. Check your plan and billing details.`、`rate limit exceeded: …` | `LIMIT_UNTIMED` |

时间格式（本地时间，不带时区）：当天 `3:45 PM`；跨天 `Jan 15th, 2025 3:45 PM`。

### 状态栏（已实测）

`[tui] status_line` 可选 `five-hour-limit`、`weekly-limit`，显示为 `5h 60% left · weekly 93% left`，只有剩余百分比，**没有刷新时间**，窄 pane 会被截断。因此状态栏对 Codex 不能用来推算恢复时间。

### `/status`（已实测，Codex 0.160）

没有非交互命令。在**另开的探测 pane** 里 `herdr agent start --kind codex`，再 `herdr agent prompt <pane> "/status"`。新会话不发任何请求就有限额数据，执行 `/status` 不调用模型，状态保持 `idle`。输出含：

```text
5h limit:                   [████████████████████] 99% left
                            (resets 01:44 on 4 Oct)
Weekly limit:               [███████████████████░] 93% left
                            (resets 07:04 on 10 Oct)
```

- `(resets ...)` 可能被折到下一行，解析前先合并续行。
- 时间格式：24 小时制 `20:44`、日期在前 `07:04 on 10 Oct`。
- 还可能有按模型的额外限额行，同样处理；`left` 为 0% 的窗口有效，多个窗口都满取较晚的恢复时间。

## Claude Code

### 报错文案（来源：第三方文章与 issue，**未在本机实测**；官方 changelog 只确认存在「Continue automatically at usage limit」设置和 `/rate-limit-options` 命令）

| 文案 | 分类 |
| --- | --- |
| `You've hit your session limit · resets 3:45pm` | `LIMIT_TIMED` |
| `You've hit your weekly limit · resets Mon 12:00am` | `LIMIT_NO_TIME`（星期形式不解析） |
| `Usage limit reached · continuing automatically at 3:45pm · esc to cancel` | `CLAUDE_AUTO` |
| `Usage limit reached · wrapping up` | `LIMIT_NO_TIME` |
| 旧版 `Claude usage limit reached. Your limit will reset at 3pm (America/New_York)` | `LIMIT_TIMED`（带时区按该时区解释） |

`Your usage limit has reset · press enter to continue` 仅一篇文章提到，**不作为判定依据**。

### 自带自动续跑（第三方，未证实）

默认开启，到点自动发一条固定的继续提示。失效场景：关闭终端、周限额距恢复超过 24 小时、笔记本休眠超过 30 分钟、用户在等待期间输入新内容。因此 watcher 对 `CLAUDE_AUTO` 只监视，不向该 pane 发送任何输入。

### 状态栏（已实测，辅助信号）

状态栏由使用者的 `statusLine` 配置，**默认不显示额度**。常见的自定义写法显示 `5h 87% (40m) · week 12% (2d15h)`（已用百分比加剩余时间），格式因人而异。`quota-lib.sh` 只识别这一种形态：取已用 ≥100% 的窗口，剩余时间换算为恢复时间；不匹配则放弃。精度只有分钟。

### `claude -p "/usage"`（已实测，Claude Code 2.1）

非交互，标准输出为纯文本，不需要新开会话，也不向任何 pane 发送输入：

```text
Current session: 95% used · resets Oct 3 at 9:09pm (<IANA 时区>)
Current week (all models): 14% used · resets Oct 6 at 11:59am (<IANA 时区>)
```

- 已用 ≥100% 的窗口有效，多个窗口都满取较晚的恢复时间。
- 带 IANA 时区后缀，按该时区解析；时区名无法识别则放弃。
- 交互界面里同一时间可能与非交互相差约 1 分钟（取整差异），所以到点后仍加缓冲或宽限期。

## 时间解析规则（`qp_parse_reset`）

触发词：`Try again at`、`resets`、`resets at`、`continuing automatically at`。支持：

| 形式 | 例子 | 规则 |
| --- | --- | --- |
| 当天时刻（12 小时制） | `3:45 PM`、`3:45pm`、`3pm` | 早于或等于当前时间视为次日 |
| 当天时刻（24 小时制） | `20:44` | 同上 |
| 带日期 | `Jan 15th, 2025 3:45 PM` | 原样 |
| 无年份 | `Oct 6 at 12pm`、`07:04 on 10 Oct` | 取当前年；已过去不到 7 天视为已过（立即恢复），否则取次年 |
| 带时区后缀 | `(America/New_York)` | 按该时区解释；无后缀按本地时区 |

不解析（视为恢复时间未知）：星期形式（`Mon 12:00am`）、`Try again later.`、无法识别的时区名。
