# 额度恢复：watcher、监视入口与方案 D

执行者因额度限制停下时，回调永远不会发生（回调靠执行者自己发，而它已经停了）。watcher 是一个**纯 shell 脚本**，不依赖任何 agent 存活，不消耗模型额度：用 `herdr agent wait` 事件式等待，识别额度中断，等到恢复时间后让**同一个会话**继续。

`<skill 目录>` 指本 skill 的 `SKILL.md` 所在目录的绝对路径。

## 1. 两个入口共用同一个 watcher

| 入口 | 谁启动 | 是否派发 | 完成时 |
| --- | --- | --- | --- |
| 派发入口（`mode=A` / `mode=D`） | 调度者按 `dispatch.md` | 是 | 发现 `done` 标志后回调调度者，正常结束并关闭自己的 pane |
| 监视入口（`mode=watch`） | 用户对任一 agent 说「盯着 <某 pane>，额度恢复后让它继续」 | 否 | 目标稳定停下且不是额度原因：通知用户，正常结束 |

### 监视入口怎么做

1. 前置检查同 `dispatch.md` 第 1 节第 1 步。
2. 目标 pane 必须由用户指明，并且在当前工作区（`workspace_id == $HERDR_WORKSPACE_ID`）；没有指明或不在当前工作区就停下询问。目标必须是 agent pane（`herdr agent get` 能取到 kind）。
3. 创建任务目录和 `ticket`：`mode=watch`，`executor_pane`、`executor_kind` 填目标，`scheduler_pane` 留空。
4. 按 `dispatch.md` 第 4 节启动 watcher，然后报告并结束本轮。

## 2. 状态机

```text
WATCH ──(执行者稳定地离开 working)──► CLASSIFY
CLASSIFY:
  done 标志存在                          → 回调调度者（记 callback=sent），正常结束
  无额度文案，派发模式                    → 通知用户（提问、被打断等），结束，保留 pane
  无额度文案，监视模式                    → 通知用户（目标已停下），正常结束
  mode=D 且有额度文案                     → 改派（见第 5 节）
  额度文案，不限时                        → 通知用户，结束，保留 pane
  额度文案，恢复时间已知且距现在 > 24 小时  → 通知用户，结束，保留 pane
  额度文案，恢复时间已知                   → WAIT_RESET
  额度文案，恢复时间未知                   → 状态栏 → 用量命令 → 仍未知则 PROBE
WAIT_RESET: 分段 sleep 到「恢复时间 + 缓冲」，Claude 自动续跑为宽限期 5 分钟，其余 60 秒
            到点后仍停着且重试未超限 → 向同一 pane 发条件式「继续」，确认转为 working
PROBE:      每 30 分钟重新判定一次，最长 12 小时，超过则通知用户并结束
重试次数已达 3 次再次限额                 → 通知用户，结束，保留 pane
```

herdr 命令（`agent get`、`agent read`）瞬时失败时最多尝试 3 次，仍失败才通知用户并结束。

判定依据：`herdr agent get` 的状态；`herdr agent read --source recent-unwrapped` 只匹配**末尾 15 行**；状态为 `unknown` 时一律读屏幕再判。herdr 没有额度专用状态，所以额度中断只能靠屏幕文案判定，规则见 `quota-patterns.md`。

## 3. 恢复时间的来源（按优先级）

1. 报错文案里的时间；
2. 屏幕底部状态栏（辅助，格式因人而异，不要求使用者配置）；
3. 用量命令：Claude 用 `claude -p "/usage"`（在 watcher 自己的 pane 里运行）；Codex 在 watcher 旁**另开探测 pane**，启动新的 Codex 会话并发 `/status`，读完立即关闭该 pane；
4. 都得不到则探测。

**用量查询不向目标 pane 发送任何输入**，因此不会打断 Claude 的自动续跑。每个限额事件最多查询一次。同一限额事件内沿用第一次解析出的恢复时间，因为到点后屏幕上的旧文案再解析，「当天时刻」会被当成次日。

## 4. 恢复动作

向**同一个 pane** 发送条件式提示，依赖该会话已有的上下文，**不重发原始派发提示词**：

> 额度已恢复。如果上一项任务尚未完成，请从中断处继续；如果已完成，只回复已完成。

原因：任务中途被打断时，工作区已有部分改动，重新派发会在被改过的工作区上从头重做。发送前再核对一次状态：已经是 `working`（用户手动恢复或自行续跑）就不发送。发送后等 `--until working`（20 秒）确认；没有开始工作则通知用户。会话已不在（agent 退出、pane 关闭）时不自动恢复。

## 5. 方案 D（仅在用户明确要求时）

用户同时明确了「不等待恢复」和「改派到的 agent」才启用。改派目标在派发时就选定，**只能是当前工作区内已有的** idle/done pane（`ticket` 的 `d_pane`）。额度中断时 watcher 不等待，直接向它发送：原始任务（`prompt.txt`）加一句「前一个 agent 因额度限制中断，已做了部分工作，工作区里有未完成的改动，请先检查当前状态再继续」。成功后 `ticket` 改为 `mode=A`、`executor_pane=<d_pane>`，之后按方案 A 守护新执行者（第二次限额不再改派）。派发时找不到改派目标则停止并报告，不派发。

## 6. 通知

`herdr notification show "<标题>" --body "<正文>"`（不耗模型额度），同时打印在 watcher pane 里。调度者与执行者同账号时，额度耗尽期间调度者可能也无法响应，所以不依赖调度者转述。通知发送失败不影响主流程。

## 7. 防护与参数

- **同一目标只允许一个 watcher**：用 `mkdir` 原子锁目录 `lock-<pane>`；已有且进程存活则拒绝，进程已死则接管。
- **恢复计划**写在任务目录的 `plan`（`state`、`class`、`reset_epoch`、`reset_source`、`attempts`、`callback`、`probe_deadline` 等），watcher 丢失时可据此人工接手。
- **重新启动 watcher**：在新开的专用 pane 里再运行同一条命令（写法见 `dispatch.md` 第 4 节：`sh '<skill 目录>/scripts/quota-watcher.sh' --ticket '<任务目录>'`，整条作为一个带引号的字符串）；`plan` 里的 `attempts` 和 `callback` 会被沿用。
- 环境变量（默认值适合真实使用）：`QW_GRACE`（宽限期，300 秒）、`QW_BUFFER`（到点缓冲，60 秒）、`QW_FAR`（超过则不等待，86400 秒）、`QW_PROBE_INTERVAL`（探测间隔，1800 秒）、`QW_PROBE_MAX`（探测上限，43200 秒）、`QW_MAX_ATTEMPTS`（恢复重试上限，3）、`QW_RETRIES`（herdr 命令瞬时失败的尝试次数，3）、`QW_RETRY_DELAY`（重试间隔，3 秒）。

## 8. 结束与 pane

- 正常结束（派发模式下已完成并回调；监视模式下目标因非额度原因停下）：watcher 关闭**自己创建的** pane。
- 其余结束（非额度原因停下、不限时、超过 24 小时、重试用尽、探测超时、目标消失、回调未送达等）一律**保留 pane**，让用户能看到原因。退出码：2 参数或 `ticket` 错误；3 已有 watcher；4 目标不可用；5 需要用户处理。

## 9. 已知限制

- 真实额度耗尽下的行为**未实测**：额度期间 herdr 对 Claude 标记的状态、Codex 额度耗尽后会话能否输入、限额状态下 `claude -p "/usage"` 和新开的 Codex 会话能否正常工作，都未验证。测试用桩 herdr 和模拟文案覆盖了状态机的所有分支。
- 恢复时间未知且屏幕上的旧额度文案一直留着时，watcher 无法判断额度是否已恢复，会探测到 12 小时上限后通知用户。
- 休眠期间的行为未实测；等待用分段 `sleep` 并对比时间戳，唤醒后会自我纠正。
- GNU `date` 的分支只做了逻辑验证，没有在真实 GNU 环境验证。
