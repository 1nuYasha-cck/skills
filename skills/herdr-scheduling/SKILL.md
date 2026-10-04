---
name: herdr-scheduling
description: 在 herdr 里把任务派发给另一个 pane 的 agent 并守护它：调度者派发后立即结束本轮不空等，执行者完成后由后台 watcher 回调；执行者触发额度限制时，watcher 等到恢复时间后让同一会话继续。支持两个入口：派发（需明确指定调度者和执行者）、对已有 pane 单独启动额度监视；也可被其他 skill 在其流程里调用来派发并守护（见调用接口）。仅在用户明确提到 herdr 并要求让某个 agent 执行任务、或要求监视某个 pane 的额度，或其他 skill 明确要求调用本 skill 时使用。需要 HERDR_ENV=1。
---

# herdr-scheduling

## 目标

解决通过 herdr 派发任务时的两个问题：

1. **调度者空等**：派发后只确认对方已开始，随即结束本轮，不空耗额度。
2. **执行者额度中断**：回调靠执行者自己发，额度耗尽后永远不会发生。由一个不消耗模型额度的 watcher 识别中断，等到恢复时间后让同一会话继续，并负责完成后回调调度者。

herdr 命令的语法和安全规则以已安装的官方 herdr skill 和 `herdr --help` 为准；本 skill 不修改、不复制它。

## 前置检查

`test "${HERDR_ENV:-}" = 1`，且 `$HERDR_PANE_ID`、`$HERDR_WORKSPACE_ID` 非空；不满足就说明不在 herdr 中并停止。

## 选择入口

| 用户的意思 | 入口 | 读取 |
| --- | --- | --- |
| 让某个 agent（kind）去执行任务，你当调度者 | 派发入口 | [references/dispatch.md](references/dispatch.md) |
| 盯着某个已有的 pane，额度恢复后让它继续 | 监视入口 | [references/quota-recovery.md](references/quota-recovery.md) 第 1 节 |
| 其他 skill 的流程要求「派发给另一个 pane 的 agent 并守护」，并给出提示词和回调句 | 调用接口 | [references/integration.md](references/integration.md) |
| watcher 的判定、恢复、方案 D、故障处理 | — | [references/quota-recovery.md](references/quota-recovery.md) |
| 额度文案、状态栏、`/usage`、`/status` 的识别规则 | — | [references/quota-patterns.md](references/quota-patterns.md) |

## 硬规则

- **分工必须明确**：派发前必须有执行者 kind、任务内容，并且当前 agent 被明确要求担任调度者；缺任何一项就停下询问，不猜测。被其他 skill 调用时，分工由调用方给出（调用方的分工配置视为用户已指定）。监视入口必须由用户指明目标 pane。
- **执行者永远是另一个 pane**，即使与调度者是同一种 kind；不在调度者本会话里执行。
- **默认走方案 A**（等恢复后让同一会话继续）。方案 D（不等待、改派）仅当用户**同时**明确说了「额度耗尽时不等待」和「改派到哪个 kind」才启用；启用后找不到改派目标就停止并报告。
- 只在调度者所在的 herdr 工作区内操作：不选其他工作区的 agent，不新建 workspace、tab、worktree，不使用 `--machine`，不关闭自己未创建的 pane。
- watcher 在被守护 agent 所在工作区、同一 tab 内的专用普通 pane 里运行；用 `start-watcher.sh` 启动，它会核对工作区和 tab。派发入口必须**先派发并确认执行者已开始，再启动 watcher**：`start-watcher.sh dispatch` 一条命令按这个顺序完成。
- 恢复时**不重发原始派发提示词**，只向同一 pane 发条件式「继续」；用量查询**不向目标 pane 发送输入**。
- 调度者和执行者都不做阻塞等待；唤醒只靠回调或用户手动唤醒。
- 额度文案、herdr 在额度期间的行为未在真实限额下验证，不得说成已验证（见 `quota-patterns.md` 和 `quota-recovery.md` 第 9 节）。

## 脚本

位于 `<skill 目录>/scripts/`（`<skill 目录>` 指本文件所在目录的绝对路径）：

| 脚本 | 作用 |
| --- | --- |
| `quota-watcher.sh` | watcher 主程序：`quota-watcher.sh --ticket <任务目录>`，应在专用 pane 里运行 |
| `quota-lib.sh` | 额度文案分类、时间解析、状态栏和用量输出解析的纯函数 |
| `start-watcher.sh` | 启动辅助：`dispatch`（建任务目录、发提示词并确认开始、启动 watcher，派发入口和调用接口用这一条）、`init`（只建任务目录和 `ticket`）、`run <任务目录>`（拆 pane、核对、启动 watcher、确认）、`watch`（监视入口一条命令）；退出码见 `dispatch.md` 第 3、7 节 |
| `wait-settled.sh` | 等 agent 稳定地进入 idle / done / blocked，过滤掉对话中途的短暂误报 |

均为 POSIX sh，不依赖 `jq`、`python3`。运行时数据在 `${TMPDIR:-/tmp}/herdr-scheduling/` 下，不写入使用者的项目。
