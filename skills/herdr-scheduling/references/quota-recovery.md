# 额度恢复：watcher、监视入口与方案 D

执行者因额度限制停下时，回调永远不会发生（回调靠执行者自己发，而它已经停了）。watcher 是一个**纯 shell 脚本**，不依赖任何 agent 存活，不消耗模型额度：用 `herdr agent wait` 事件式等待，识别额度中断，等到恢复时间后让**同一个会话**继续。

`<skill 目录>` 指本 skill 的 `SKILL.md` 所在目录的绝对路径。

## 1. 两个入口共用同一个 watcher

| 入口 | 谁启动 | 是否派发 | 完成时 |
| --- | --- | --- | --- |
| 派发入口（`mode=A` / `mode=D`） | 调度者按 `dispatch.md`（其他 skill 按 `integration.md`） | 是 | `callback_on=done`（缺省）：发现 `done` 标志后回调调度者；`callback_on=stop`：执行者稳定停在 `idle`/`done` 且不是额度原因就回调。回调后正常结束并关闭自己的 pane |
| 监视入口（`mode=watch`） | 用户对任一 agent 说「盯着 <某 pane>，额度恢复后让它继续」 | 否 | 目标稳定停下且不是额度原因：通知用户，正常结束 |

### 监视入口怎么做

1. 前置检查同 `dispatch.md` 第 1 节第 1 步。
2. 目标 pane 必须由用户指明，并且在当前工作区（`workspace_id == $HERDR_WORKSPACE_ID`）；没有指明或不在当前工作区就停下询问。目标必须是 agent pane（`herdr agent get` 能取到 kind）。
3. 一条命令完成创建任务目录、拆 pane、核对、启动和确认：

   ```bash
   sh '<skill 目录>/scripts/start-watcher.sh' watch --executor <目标 pane> [--notify-stop 0] [--require-working] [--env QW_X=N]...
   ```

   它写 `mode=watch` 的 `ticket`（`scheduler_pane` 为空），再按 `dispatch.md` 第 7.3 节的方式启动 watcher；输出 `task_id`、`task_dir`、`watcher_pane`，退出码含义同 `dispatch.md` 第 7.3 节。`--require-working` 只用于「还没有确认过执行者已开始」的场景。盯一个**已经停在额度上**的 pane（本来就是 `idle`），以及对刚派发、已经确认过 `working` 的执行者（如 `dev-flow` 联动，它可能很快就完成或触达额度），都**不要加**，否则会因为「当前不是 working」而放弃守护。
4. 报告并结束本轮。

## 2. 状态机

```text
WATCH ──(执行者稳定地离开 working)──► CLASSIFY
CLASSIFY:
  done 标志存在                          → 回调调度者（记 callback=sent），正常结束
  无额度文案，派发模式，状态 blocked       → 通知用户一次，继续守护：等执行者离开 blocked，最多 QW_BLOCK_MAX 秒，
                                            之后回到 CLASSIFY；超时 → 通知用户，结束，保留 pane
  无额度文案，派发模式，callback_on=stop，状态 idle/done → 有 done 文件：立即回调，正常结束；
                                            没有：观察 QW_STOP_GRACE 秒（回到 working/blocked 则继续守护；标志出现则回调）；
                                            到期仍停着 → 通知用户并回调调度者，之后继续守护，执行者恢复并做完后再回调一次（至多等 QW_RESUME_MAX 秒）
  无额度文案，派发模式，其他                → 通知用户（提问、被打断、unknown 等），结束，保留 pane
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

`blocked` 只在派发模式下继续守护（用户在执行者 pane 里批准或回答后，watcher 不丢）；监视模式下 `blocked` 仍是通知后正常结束。屏幕上有额度文案的 `blocked`（如额度菜单）不走这条，走额度流程。`blocked` 等待用 `herdr agent get` 每 `QW_STEP`（60 秒）查一次，不耗模型额度。

判定依据：`herdr agent get` 的状态；读屏只匹配**末尾 15 行**；状态为 `unknown` 时一律读屏幕再判。**读屏来源按状态选**：`blocked` 读 `--source visible`，其他状态读 `--source recent-unwrapped`——herdr 在 agent 处于 `working` 或 `blocked`（界面使用备用屏幕）时拒绝 `recent-unwrapped`，返回 `agent_not_idle`，而 `visible` 始终可读。遇到 `agent_not_idle` 时先重新取状态：已回到 `working` 就继续等待（不算错误），否则改读 `visible` 重试；其他读屏失败重试 `QW_RETRIES` 次，仍失败就通知用户（通知里带 herdr 的错误原文）并以退出码 4 结束。herdr 没有额度专用状态，所以额度中断只能靠屏幕文案判定，规则见 `quota-patterns.md`。

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

**回调句和回调条件**：`ticket` 的 `callback_prompt`（`start-watcher.sh` 的 `--callback-prompt`）指定回调时发给调度者的一行文本，缺省「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」；调度者被阻塞、等待空闲失败、发送失败时通知里提示的「请手动唤醒」也用这句。`callback_on`（`--callback-on done|stop`）缺省 `done`，字段缺失或取值不合法按 `done`；监视模式不读这两个字段。

**静音选项**：`ticket` 的可选字段 `notify_stop=0`（`start-watcher.sh` 的 `--notify-stop 0`）只关闭一种通知——**监视模式下目标正常停下**（状态为 `idle` 或 `done`，「目标已停下，原因不是额度」），适合被守护的 agent 每个阶段都会正常结束的场景。**`blocked`（在等批准或回答）、`unknown` 等需要用户处理的停下不受静音影响，照常通知**。额度已限、恢复时间未知、不限时、超过 24 小时、重试用尽、探测超时、目标不可用、恢复失败、改派失败、回调失败，以及派发模式下「执行者停下但没有完成标志」的通知**始终发送**，不能被静音。默认 `1`，字段缺失或取值不合法按 `1` 处理。

## 7. 防护与参数

- **同一目标只允许一个 watcher**：用 `mkdir` 原子锁目录 `lock-<pane>`；已有且进程存活则拒绝，进程已死则接管。
- **恢复计划**写在任务目录的 `plan`（`state`、`class`、`reset_epoch`、`reset_source`、`attempts`、`callback`、`probe_deadline` 等），watcher 丢失时可据此人工接手。
- **重新启动 watcher**：运行 `sh '<skill 目录>/scripts/start-watcher.sh' run "<任务目录>"`（写法见 `dispatch.md` 第 7.3 节；手工写法是在新开的专用 pane 里整串运行 `sh '<skill 目录>/scripts/quota-watcher.sh' --ticket '<任务目录>'`）；`plan` 里的 `attempts` 和 `callback` 会被沿用。
- 环境变量（默认值适合真实使用）：`QW_GRACE`（宽限期，300 秒）、`QW_BUFFER`（到点缓冲，60 秒）、`QW_FAR`（超过则不等待，86400 秒）、`QW_PROBE_INTERVAL`（探测间隔，1800 秒）、`QW_PROBE_MAX`（探测上限，43200 秒）、`QW_MAX_ATTEMPTS`（恢复重试上限，3）、`QW_RETRIES`（herdr 命令瞬时失败的尝试次数，3）、`QW_RETRY_DELAY`（重试间隔，3 秒）、`QW_BLOCK_MAX`（派发模式下等待 `blocked` 被处理的上限，7200 秒）、`QW_STOP_GRACE`（`stop` 模式稳定停下后等待完成标志的观察期，60 秒，0 为立即回调）、`QW_STOP_POLL`（观察期轮询间隔，5 秒）、`QW_RESUME_MAX`（兜底回调之后等待执行者恢复的上限，7200 秒）。

## 8. 结束与 pane

- 正常结束（派发模式下已完成并回调；监视模式下目标因非额度原因停下）：watcher 关闭**自己创建的** pane。
- 其余结束（非额度原因停下且无法回调、`blocked` 等待超时、不限时、超过 24 小时、重试用尽、探测超时、目标消失、回调未送达等）一律**保留 pane**，让用户能看到原因。退出码：2 参数或 `ticket` 错误；3 已有 watcher；4 目标不可用；5 需要用户处理（含 `blocked` 等待超时、`callback_on=stop` 时回调未送达）。

## 9. 已知限制

- 真实额度耗尽下的行为**未实测**：额度期间 herdr 对 Claude 标记的状态、Codex 额度耗尽后会话能否输入、限额状态下 `claude -p "/usage"` 和新开的 Codex 会话能否正常工作，都未验证。测试用桩 herdr 和模拟文案覆盖了状态机的所有分支。
- 恢复时间未知且屏幕上的旧额度文案一直留着时，watcher 无法判断额度是否已恢复，会探测到 12 小时上限后通知用户。
- 派发模式下 `blocked`（等批准或回答）后继续守护的行为只用桩 herdr 验证：真实 agent 的审批提示能否被 herdr 稳定标记为 `blocked`、批准后状态变化的时序，以及最长 `QW_BLOCK_MAX` 的等待在真机上的表现，均未验证。
- herdr 对某些 agent 的状态检测不可靠：实测 Codex 在启动阶段约 17 秒、以及某些运行中会报 `done` 而实际仍在工作，屏幕在这段时间内也没有变化，所以「稳定停下」不能当作「已完成」；`stop` 模式因此有观察期，skill 因此让执行者创建自己指定的完成标志（路径 `<标志目录>/<任务ID>.done`）作为确定的完成信号。
- 环境变量可能过期：Codex 的共享 app-server 守护进程若在更早的 herdr pane 里启动，经它启动的所有 Codex agent 的工具子进程继承它的旧 `HERDR_PANE_ID` 等变量（`herdr pane current` 同样依赖这些变量，无法纠正）。`start-watcher.sh` 在 Codex 里（`CODEX_THREAD_ID` 非空）检测到过期时，用 `herdr agent list` 按类型、工作目录、状态自动找到调度者真实所在的 pane，只在唯一匹配时采用，否则退出码 3；`whoami` 子命令输出结果。局限：旧值指向的 pane 仍存在、是 codex 且没有 `agent_session` 可比对时无法判定过期。根治办法是用 `codex --no-daemon` 启动 Codex。
- 读屏降级到 `visible` 时只含当前可见屏幕，历史行比 `recent-unwrapped` 少；`blocked` 时审批提示和额度菜单都在可见区域内，判定足够。`agent_not_idle` 已在 Claude 执行者上实测（`working` 和 `blocked` 都触发）；Codex 在这两种状态下的读屏行为未实测，但处理逻辑不依赖 agent 种类。
- 休眠期间的行为未实测；等待用分段 `sleep` 并对比时间戳，唤醒后会自我纠正。
- GNU `date` 的分支只做了逻辑验证，没有在真实 GNU 环境验证。
