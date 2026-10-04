# 派发入口：调度者把任务交给另一个 pane 的 agent

当前 agent 作为**调度者**时使用。herdr 命令的语法和安全规则以已安装的官方 herdr skill 和 `herdr --help` 为准。

`<skill 目录>` 指本 skill 的 `SKILL.md` 所在目录的绝对路径；脚本为 `<skill 目录>/scripts/quota-watcher.sh`、`<skill 目录>/scripts/wait-settled.sh`。

核心做法：派发后只确认对方**已开始**，随即结束本轮，不等待、不轮询；完成和额度中断都由 watcher 负责，调度者空闲期间不消耗额度。

## 1. 前置检查

1. `test "${HERDR_ENV:-}" = 1`，且 `$HERDR_PANE_ID`、`$HERDR_WORKSPACE_ID` 非空；否则说明不在 herdr 中并停止。
2. **分工必须明确**。同时具备下面三项才继续，缺任何一项就**停下询问**，不要猜：
   - 执行者的 kind（如 `claude`、`codex`）；
   - 要执行的任务内容；
   - 当前 agent 被明确要求担任调度者（用户说「你来调度」「由你派发」之类，或明确给出调度者和执行者）。
3. 方案 D 开关：只有用户**同时**明确说了「额度耗尽时不等待」和「改派到哪个 kind」才设 `mode=D` 并记下 `d_kind`；否则一律 `mode=A`，不要自行启用 D。

执行者可以和调度者是同一种 kind，但**永远是另一个 pane**，不在调度者本会话里执行。

## 2. 选择执行者 pane（只在当前工作区内）

执行 `herdr agent list`（它返回所有工作区的 agent），逐项过滤，全部满足才算候选：

1. `workspace_id` == `$HERDR_WORKSPACE_ID`；
2. `agent` == 执行者 kind；
3. `pane_id` != `$HERDR_PANE_ID`（排除调度者自己）；
4. `cwd` 等于任务工作目录（默认调度者当前目录）；
5. `agent_status` 为 `idle` 或 `done`。

- 候选唯一：复用，以其 `pane_id` 寻址。
- 候选多个：列出 `pane_id`、`tab_id`、`cwd`、`agent_status`，让用户选。
- 没有候选：在当前工作区新建：

  ```bash
  herdr pane split --pane "$HERDR_PANE_ID" --direction down --cwd <工作目录> --no-focus
  herdr agent start <名称，如 hs-claude> --kind <kind> --pane <新 pane>
  ```

  从返回 JSON 读 `pane_id`，并核对其 `workspace_id` == `$HERDR_WORKSPACE_ID`。

禁止：选其他工作区的 agent；新建 workspace、tab、worktree；使用 `--machine`；关闭自己未创建的 pane。

**方案 D 时**再选改派目标：同样的过滤条件（另排除执行者），**只找已有的**，不新建。找不到则**停止并报告，不派发**，也不退回等待。

## 3. 创建任务目录和派发单

任务目录放在 `${TMPDIR:-/tmp}/herdr-scheduling/<任务ID>/`，不写入使用者的项目。任务ID = `hs-<YYYYMMDDHHMMSS>-<执行者 pane，: 和 / 换成 ->`。

```bash
ROOT="${TMPDIR:-/tmp}/herdr-scheduling"
ID="hs-$(date +%Y%m%d%H%M%S)-$(printf %s "<执行者 pane>" | tr ':/' '--')"
TD="$ROOT/$ID"; mkdir -p "$TD"
```

写 `prompt.txt`（原始任务内容，方案 D 改派时要用）和 `ticket`（每行 `键=值`）：

```text
id=<任务ID>
mode=A|D
scheduler_pane=<$HERDR_PANE_ID>
scheduler_kind=<调度者 kind>
executor_pane=<执行者 pane>
executor_kind=<执行者 kind>
workspace_id=<$HERDR_WORKSPACE_ID>
tab_id=<执行者 pane 的 tab>
d_kind=<kind>          # 仅 mode=D
d_pane=<pane>          # 仅 mode=D
result_file=<任务目录>/result.md     # 或调度者指定的路径
done_file=<任务目录>/done
created=<Unix 秒>
```

## 4. 启动 watcher

watcher 在**被守护的执行者所在的工作区、同一个 tab** 内的专用普通 pane 里运行：

```bash
herdr pane split --pane <执行者 pane> --direction down --no-focus --cwd <工作目录>
```

从返回 JSON 读新 pane 的 `pane_id`、`workspace_id`、`tab_id`，**核对后两项与执行者一致**，不一致就关闭这个新 pane 并报告。然后：

```bash
herdr pane run <watcher pane> "sh '<skill 目录>/scripts/quota-watcher.sh' --ticket '<任务目录>'"
```

注意：`herdr pane run` 会把各参数**直接用空格拼接**后交给 pane 里的 shell，不保留引号，所以要把整条命令写成**一个带引号的字符串**，路径用单引号包住；并显式用 `sh` 执行，不依赖脚本的可执行位（通过 skill manager 安装后可能丢失）。启动后用 `herdr pane read <watcher pane> --source visible --lines 10` 看一眼，确认 watcher 已打印「开始守护」，再继续。

这个 pane 是普通终端，不是 agent，不消耗模型额度。

## 5. 派发

发给执行者的提示词自包含（没有状态文件可读）：

```text
<任务内容>

完成后请：
1. 把结果写到 <result_file>
2. 创建文件 <done_file>
不需要自己通知调度者，由 watcher 负责。
```

```bash
herdr agent prompt <执行者 pane> "<上面的提示词>" --wait --until working --until blocked --timeout 15000
```

- `--until working` 只确认对方已开始，最多 15 秒，**不等待任务完成**；之后不要运行 `wait-settled.sh`，不要 `agent read` / `agent get` 轮询。
- 返回 `blocked`：读对方最多 40 行交给用户。超时：`agent get` 一次确认状态后报告用户，不盲目重发。

## 6. 结束本轮

向用户报告：「已派发给 <pane>，watcher 在 <pane> 中守护，完成后会回调；任务ID <ID>」，然后结束本轮并进入空闲。

## 7. 被回调唤醒后

收到「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」时：

1. 读 `<任务目录>/ticket`、`plan`、`result.md`（以它们为准，不读执行者的完整输出）。
2. 向用户报告结果。`plan` 的 `state`、`attempts` 可说明期间是否发生过额度中断和恢复。
3. 不要自动派发下一个任务；需要继续时等用户指示。

## 8. 手动唤醒

watcher 异常退出时，`done` 仍在。用户可以对调度者说「使用 $herdr-scheduling 继续调度（任务：<任务ID>）」，按第 7 节处理。

## 规则

- 调度者不执行被派发的工作，也不修改执行者的产物，只写任务目录里的 `ticket`、`prompt.txt`。
- 调度者和执行者（agent）都不做阻塞等待；等待由 watcher 这个无 token 的脚本承担。
- 调度范围限定在调度者所在的 herdr 工作区。
- 一次只派发一个执行者；首版不支持并行。
