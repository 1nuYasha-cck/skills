#!/bin/sh
# 额度 watcher：守护一个 herdr agent，额度中断时等到恢复时间，再让同一会话继续。
# 纯 shell，不消耗模型额度；应在专用的普通 pane 里运行（见 references/quota-recovery.md）。
#
# 用法：quota-watcher.sh --ticket <任务目录>
#   任务目录由调度者按 references/dispatch.md 创建（推荐用 start-watcher.sh），内含 ticket（必需）、prompt.txt、plan、done 等。
#   ticket 的可选字段 notify_stop=0：监视模式下目标正常停下（idle / done）时不弹通知（默认 1）；
#   额度相关通知和所有异常通知（含 blocked、unknown 等需要用户处理的停下）始终发送。
#   ticket 的可选字段 callback_on=stop（缺省 done）：派发模式下执行者稳定停在 idle / done 且不是额度原因时回调调度者，
#   不再要求 done 文件；callback_prompt=<一行文本>：回调调度者时发送的句子（缺省「使用 $herdr-scheduling 继续调度（任务：<ID>）」）。
#   派发模式下执行者停在 blocked（等批准或回答，屏幕无额度文案）：通知用户一次后继续守护，等它离开 blocked，
#   最多等 QW_BLOCK_MAX 秒；监视模式不受影响。
# 退出码：0 正常结束；2 参数或 ticket 错误；3 已有 watcher 在守护同一目标；4 目标 pane 不可用；5 需要用户处理
#
# 可用环境变量（默认值适合真实使用，测试时可缩小）：
#   QW_STEP 分段 sleep 步长秒(60)  QW_GRACE Claude 自动续跑宽限期秒(300)  QW_BUFFER 其余 agent 到点缓冲秒(60)
#   QW_FAR 超过则不等待的秒数(86400)  QW_PROBE_INTERVAL 探测间隔秒(1800)  QW_PROBE_MAX 探测总上限秒(43200)
#   QW_MAX_ATTEMPTS 恢复重试上限(3)  QW_RETRIES herdr 命令瞬时失败的尝试次数(3)  QW_RETRY_DELAY 重试间隔秒(3)
#   QW_BLOCK_MAX 派发模式下等待 blocked 被处理的上限秒数(7200)
#   QW_NO_USAGE=1 不做用量查询  QW_NO_CLOSE=1 结束时不关闭本 pane
#   QP_NOW_FILE 固定当前时间的文件（测试用，此时 sleep 改为快进该文件）

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
. "$SCRIPT_DIR/quota-lib.sh"

QW_STEP=${QW_STEP:-60}
QW_GRACE=${QW_GRACE:-300}
QW_BUFFER=${QW_BUFFER:-60}
QW_FAR=${QW_FAR:-86400}
QW_PROBE_INTERVAL=${QW_PROBE_INTERVAL:-1800}
QW_PROBE_MAX=${QW_PROBE_MAX:-43200}
QW_MAX_ATTEMPTS=${QW_MAX_ATTEMPTS:-3}
QW_RETRIES=${QW_RETRIES:-3}
QW_RETRY_DELAY=${QW_RETRY_DELAY:-3}
QW_WAIT_MS=${QW_WAIT_MS:-7200000}
QW_SETTLE_MS=${QW_SETTLE_MS:-15000}
QW_BLOCK_MAX=${QW_BLOCK_MAX:-7200}
QW_NO_USAGE=${QW_NO_USAGE:-0}
QW_NO_CLOSE=${QW_NO_CLOSE:-0}

CONTINUE_TEXT='额度已恢复。如果上一项任务尚未完成，请从中断处继续；如果已完成，只回复已完成。'

TICKET_DIR=; TICKET=; PLAN=; ROOT=; LOCK=
ID=; MODE=; SCHED_PANE=; SCHED_KIND=; EXEC_PANE=; EXEC_KIND=; D_KIND=; D_PANE=; DONE_FILE=; NOTIFY_STOP=1; CALLBACK_ON=done; CALLBACK_TEXT=
ATTEMPTS=0
CLS=; RESET=; RSRC=; STATE=
EVENT_RESET=; EVENT_SRC=; USAGE_TRIED=0; USAGE_RESULT=; PROBE_DEADLINE=0; EVENT_NOTIFIED=0

qw_log() { printf '[%s] %s\n' "$(date '+%H:%M:%S')" "$*"; }

# 休眠 N 秒；测试时（QP_NOW_FILE 存在）改为快进假时钟，不真正等待。
qw_sleep() {
  if [ -n "${QP_NOW_FILE:-}" ] && [ -f "$QP_NOW_FILE" ]; then
    echo $(( $(cat "$QP_NOW_FILE") + $1 )) >"$QP_NOW_FILE"
  else
    sleep "$1"
  fi
}

# herdr 命令瞬时失败时重试：qw_retry <次数> <间隔秒> <命令...>。成功时输出命令的标准输出并返回 0；
# 次数用尽返回 1。herdr 的错误一律是退出码 1 加 stderr 的 JSON，无法区分瞬时与永久，所以统一重试。
qw_retry() {
  _rn=$1; _rd=$2; shift 2
  _rk=0
  while :; do
    if _rout=$("$@" 2>/dev/null); then printf '%s\n' "$_rout"; return 0; fi
    _rk=$((_rk + 1))
    [ "$_rk" -ge "$_rn" ] && return 1
    qw_sleep "$_rd"
  done
}

json_str() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed 's/^[^:]*:"//; s/"$//'; }

kv_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1; }

# 原子地写入 key=value：先写临时文件再 mv。
kv_set() {
  _kf=$1; _kk=$2; _kv=$3
  { [ -f "$_kf" ] && grep -v -e "^$_kk=" "$_kf"; printf '%s=%s\n' "$_kk" "$_kv"; } >"$_kf.tmp.$$" 2>/dev/null
  mv "$_kf.tmp.$$" "$_kf"
}

# 8.8 恢复计划读写
plan_write() { kv_set "$PLAN" "$1" "$2"; kv_set "$PLAN" updated "$(qp_now)"; }
plan_read() { kv_get "$PLAN" "$1"; }

# 8.7 单实例：mkdir 原子创建锁目录；已有且进程存活则拒绝，失效则接管。
acquire_lock() {
  LOCK="$ROOT/lock-$(printf '%s' "$EXEC_PANE" | tr ':/' '--')"
  if ! mkdir "$LOCK" 2>/dev/null; then
    _lp=$(cat "$LOCK/pid" 2>/dev/null)
    if [ -n "$_lp" ] && kill -0 "$_lp" 2>/dev/null; then
      qw_log "已有 watcher（pid ${_lp}）在守护 ${EXEC_PANE}，本实例退出"
      return 1
    fi
    rm -rf "$LOCK"
    mkdir "$LOCK" 2>/dev/null || return 1
  fi
  printf '%s\n' "$$" >"$LOCK/pid"
  printf '%s\n' "${HERDR_PANE_ID:-}" >"$LOCK/pane"
  return 0
}

release_lock() {
  [ -n "$LOCK" ] && [ "$(cat "$LOCK/pid" 2>/dev/null)" = "$$" ] && rm -rf "$LOCK"
  return 0
}

agent_state_once() {
  _ao=$(herdr agent get "$1" 2>/dev/null) || return 1
  _as=$(printf '%s\n' "$_ao" | json_str agent_status)
  [ -n "$_as" ] || return 1
  printf '%s\n' "$_as"
}

# 读取 agent 状态，瞬时失败最多重试 QW_RETRIES 次；仍失败返回 1。
agent_state_of() { qw_retry "$QW_RETRIES" "$QW_RETRY_DELAY" agent_state_once "$1"; }

# 8.13 通知用户：herdr 通知（不耗模型额度）加本 pane 输出；通知失败不影响主流程。
notify_user() {
  qw_log "通知用户：$1 —— $2"
  herdr notification show "$1" --body "$2" >/dev/null 2>&1 || true
  plan_write last_notice "$1"
}

# 8.13 回调调度者：只由 watcher 发送，plan.callback=sent 防止重发。
notify_scheduler() {
  [ "$(plan_read callback)" = sent ] && return 0
  [ -n "$SCHED_PANE" ] || { notify_user "任务完成，但没有调度者" "任务 $ID 已完成，结果见 $TICKET_DIR"; return 1; }
  _ss=$(agent_state_of "$SCHED_PANE") || { notify_user "任务完成，调度者已不在" "请手动处理任务 ${ID}，结果见 $TICKET_DIR"; return 1; }
  case $_ss in
    blocked) notify_user "任务完成，调度者被阻塞" "请对调度者说：${CALLBACK_TEXT}"; return 1 ;;
    working)
      _so=$(sh "$SCRIPT_DIR/wait-settled.sh" "$SCHED_PANE" "$QW_WAIT_MS" "$QW_SETTLE_MS") || {
        notify_user "任务完成，等待调度者空闲失败" "请手动唤醒调度者：${CALLBACK_TEXT}"; return 1; }
      case $_so in *blocked*) notify_user "任务完成，调度者被阻塞" "任务 $ID"; return 1 ;; esac
      ;;
  esac
  herdr agent prompt "$SCHED_PANE" "$CALLBACK_TEXT" >/dev/null 2>&1 || {
    notify_user "任务完成，回调发送失败" "请手动唤醒调度者：${CALLBACK_TEXT}"; return 1; }
  plan_write callback sent
  qw_log "已回调调度者 $SCHED_PANE"
  return 0
}

# 8.9 等待到指定 Unix 秒：分段 sleep 并对比时间戳，休眠唤醒后能自我纠正。
# 返回 0 到点；1 目标回到 working（被用户手动恢复或自行续跑）；2 目标 pane 不可用
wait_until_epoch() {
  _wt=$1
  while :; do
    _wn=$(qp_now)
    [ "$_wn" -ge "$_wt" ] && return 0
    _ws=$((_wt - _wn))
    [ "$_ws" -gt "$QW_STEP" ] && _ws=$QW_STEP
    qw_sleep "$_ws"
    _wst=$(agent_state_of "$EXEC_PANE") || return 2
    [ "$_wst" = working ] && return 1
  done
}

# 8.13 派发模式下执行者停在 blocked：等它离开 blocked，最多等 QW_BLOCK_MAX 秒。
# 返回 0 已离开 blocked；1 超时仍在 blocked；2 目标 pane 不可用。
# 轮询 herdr agent get（不耗模型额度），不用 wait-settled.sh（它遇到 blocked 会立即返回）。
wait_blocked_cleared() {
  _bd=$(( $(qp_now) + QW_BLOCK_MAX ))
  plan_write blocked_deadline "$_bd"
  while :; do
    _bs=$(agent_state_of "$EXEC_PANE") || return 2
    [ "$_bs" = blocked ] || return 0
    _bn=$(qp_now)
    [ "$_bn" -ge "$_bd" ] && return 1
    _bw=$((_bd - _bn))
    [ "$_bw" -gt "$QW_STEP" ] && _bw=$QW_STEP
    qw_sleep "$_bw"
  done
}

# 带超时地运行命令并输出其标准输出：qw_run_timeout <秒> <命令...>
qw_run_timeout() {
  _rt=$1; shift
  _rf=$(mktemp "${TMPDIR:-/tmp}/qw.XXXXXX") || return 1
  "$@" >"$_rf" 2>&1 </dev/null &
  _rp=$!
  _ri=0
  while kill -0 "$_rp" 2>/dev/null; do
    if [ "$_ri" -ge "$_rt" ]; then kill "$_rp" 2>/dev/null; break; fi
    sleep 1
    _ri=$((_ri + 1))
  done
  wait "$_rp" 2>/dev/null
  cat "$_rf"
  rm -f "$_rf"
}

# 8.15 用量查询：结果放在 USAGE_RESULT（恢复时间，Unix 秒；取不到为空）。不向目标 pane 发送任何输入。
# 不能放进 $(...) 里调用，否则「每个限额事件最多查询一次」的标记 USAGE_TRIED 回不到主进程。
query_usage() {
  USAGE_RESULT=
  [ "$QW_NO_USAGE" = 1 ] && return 0
  [ "$USAGE_TRIED" -eq 1 ] && return 0
  USAGE_TRIED=1
  case $EXEC_KIND in
    claude)
      command -v claude >/dev/null 2>&1 || return 0
      USAGE_RESULT=$(qw_run_timeout 60 claude -p "/usage" | qp_usage_reset claude "$(qp_now)")
      ;;
    codex)
      # 另开探测 pane（同一工作区、同一 tab），新会话里执行 /status，用完必须关闭。
      _qcwd=$(herdr agent get "$EXEC_PANE" 2>/dev/null | json_str cwd)
      _qo=$(herdr pane split --pane "$EXEC_PANE" --direction down --no-focus --cwd "${_qcwd:-.}" 2>&1) || return 0
      _qp=$(printf '%s\n' "$_qo" | json_str pane_id)
      [ -n "$_qp" ] || return 0
      _qres=
      if herdr agent start "hs-probe-$$" --kind codex --pane "$_qp" >/dev/null 2>&1 \
         && herdr agent wait "$_qp" --until idle --until blocked --timeout 60000 >/dev/null 2>&1 \
         && [ "$(agent_state_of "$_qp")" = idle ]; then
        herdr agent prompt "$_qp" "/status" >/dev/null 2>&1
        qw_sleep 4
        _qres=$(herdr agent read "$_qp" --source recent-unwrapped --lines 60 2>/dev/null | qp_usage_reset codex "$(qp_now)")
      fi
      herdr pane close "$_qp" >/dev/null 2>&1
      USAGE_RESULT=$(printf '%s\n' "$_qres" | grep -E '^[0-9]+$' | head -1)
      ;;
  esac
  return 0
}

event_clear() { EVENT_RESET=; EVENT_SRC=; USAGE_TRIED=0; PROBE_DEADLINE=0; EVENT_NOTIFIED=0; }

# 8.10 读取目标状态和屏幕，给出分类和恢复时间。
# 设置 CLS（ERROR|WORKING|NONE|LIMIT_UNTIMED|LIMIT_TIMED|CLAUDE_AUTO|LIMIT_NO_TIME）、RESET、RSRC、STATE。
# 同一限额事件内沿用第一次解析出的恢复时间：到点后屏幕上的旧文案再解析，「当天时刻」会被当成次日。
classify_target() {
  CLS=NONE; RESET=; RSRC=unknown
  STATE=$(agent_state_of "$EXEC_PANE") || { CLS=ERROR; return 0; }
  if [ "$STATE" = working ]; then CLS=WORKING; return 0; fi
  _cs=$(qw_retry "$QW_RETRIES" "$QW_RETRY_DELAY" herdr agent read "$EXEC_PANE" --source recent-unwrapped --lines 40) || { CLS=ERROR; return 0; }
  _ct=$(printf '%s\n' "$_cs" | qp_tail 15)
  CLS=$(printf '%s\n' "$_ct" | qp_classify "$EXEC_KIND")
  case $CLS in
    LIMIT_TIMED | CLAUDE_AUTO | LIMIT_NO_TIME)
      if [ -n "$EVENT_RESET" ]; then RESET=$EVENT_RESET; RSRC=$EVENT_SRC; return 0; fi
      RESET=$(printf '%s\n' "$_ct" | qp_parse_reset "$(qp_now)")
      [ -n "$RESET" ] && RSRC=text
      if [ -z "$RESET" ]; then
        _cv=$(herdr agent read "$EXEC_PANE" --source visible --lines 6 2>/dev/null)
        RESET=$(printf '%s\n' "$_cv" | qp_parse_statusline "$(qp_now)")
        [ -n "$RESET" ] && RSRC=statusline
      fi
      if [ -z "$RESET" ]; then
        query_usage
        RESET=$USAGE_RESULT
        [ -n "$RESET" ] && RSRC=usage
      fi
      if [ -n "$RESET" ]; then EVENT_RESET=$RESET; EVENT_SRC=$RSRC; fi
      ;;
  esac
  return 0
}

# 8.11 向同一 pane 发条件式「继续」，并确认已恢复工作。
# 返回 0 已恢复；1 发送前已自行恢复；2 失败（已通知用户）
resume_target() {
  _rs=$(agent_state_of "$EXEC_PANE") || { notify_user "无法恢复：目标 pane 已不在" "$EXEC_PANE"; return 2; }
  [ "$_rs" = working ] && return 1
  herdr agent prompt "$EXEC_PANE" "$CONTINUE_TEXT" >/dev/null 2>&1 || {
    notify_user "无法恢复：发送继续提示失败" "$EXEC_PANE 可能被阻塞或已退出，请手动处理"; return 2; }
  if herdr agent wait "$EXEC_PANE" --until working --timeout 20000 >/dev/null 2>&1; then
    ATTEMPTS=$((ATTEMPTS + 1))
    plan_write attempts "$ATTEMPTS"
    qw_log "已让 $EXEC_PANE 继续（第 $ATTEMPTS 次）"
    return 0
  fi
  notify_user "已发送继续提示，但执行者没有开始工作" "$EXEC_PANE 请手动查看"
  return 2
}

# 8.12 方案 D：不等待，改派给预先选定的 pane。成功后改为方案 A 守护新执行者。
reassign_to_d_target() {
  [ -n "$D_PANE" ] || { notify_user "无法改派：没有改派目标" "任务 $ID"; return 1; }
  _ds=$(agent_state_of "$D_PANE") || { notify_user "无法改派：改派目标已不在" "$D_PANE"; return 1; }
  case $_ds in idle | done) ;; *) notify_user "无法改派：改派目标不可用（${_ds}）" "$D_PANE"; return 1 ;; esac
  _dp=$(cat "$TICKET_DIR/prompt.txt" 2>/dev/null)
  herdr agent prompt "$D_PANE" "$_dp

（前一个 agent 因额度限制中断，已做了部分工作，工作区里有未完成的改动。请先检查当前状态，再继续完成上述任务。）" >/dev/null 2>&1 || {
    notify_user "无法改派：发送失败" "$D_PANE"; return 1; }
  herdr agent wait "$D_PANE" --until working --timeout 20000 >/dev/null 2>&1 || {
    notify_user "已改派但对方没有开始工作" "$D_PANE"; return 1; }
  kv_set "$TICKET" executor_pane "$D_PANE"; kv_set "$TICKET" executor_kind "$D_KIND"; kv_set "$TICKET" mode A
  release_lock
  EXEC_PANE=$D_PANE; EXEC_KIND=$D_KIND; MODE=A
  acquire_lock || return 1
  qw_log "已改派给 ${D_PANE}，之后按方案 A 守护"
  return 0
}

finish_normal() {
  plan_write state DONE
  qw_log "正常结束"
  release_lock
  trap - EXIT
  if [ "$QW_NO_CLOSE" != 1 ] && [ -n "${HERDR_PANE_ID:-}" ]; then
    herdr pane close "$HERDR_PANE_ID" >/dev/null 2>&1
  fi
  exit 0
}

finish_abnormal() {
  plan_write state NEEDS_USER
  qw_log "异常结束，保留 pane：$1"
  exit "${2:-5}"
}

load_ticket() {
  ID=$(kv_get "$TICKET" id); MODE=$(kv_get "$TICKET" mode)
  SCHED_PANE=$(kv_get "$TICKET" scheduler_pane); SCHED_KIND=$(kv_get "$TICKET" scheduler_kind)
  EXEC_PANE=$(kv_get "$TICKET" executor_pane); EXEC_KIND=$(kv_get "$TICKET" executor_kind)
  D_KIND=$(kv_get "$TICKET" d_kind); D_PANE=$(kv_get "$TICKET" d_pane)
  DONE_FILE=$(kv_get "$TICKET" done_file)
  NOTIFY_STOP=$(kv_get "$TICKET" notify_stop)
  [ "$NOTIFY_STOP" = 0 ] || NOTIFY_STOP=1   # 缺失、为空或取值不合法一律按 1（通知）
  CALLBACK_ON=$(kv_get "$TICKET" callback_on)
  [ "$CALLBACK_ON" = stop ] || CALLBACK_ON=done   # 缺失、为空或取值不合法一律按 done
  CALLBACK_TEXT=$(kv_get "$TICKET" callback_prompt)
  [ -n "$CALLBACK_TEXT" ] || CALLBACK_TEXT="使用 \$herdr-scheduling 继续调度（任务：${ID}）"
  [ -n "$ID" ] && [ -n "$EXEC_PANE" ] && [ -n "$EXEC_KIND" ] || return 1
  case $MODE in A | D | watch) ;; *) return 1 ;; esac
  [ -n "$DONE_FILE" ] || DONE_FILE="$TICKET_DIR/done"
  return 0
}

# 8.14 主流程：按技术方案 7.5 的状态机运行。
main() {
  [ "${1:-}" = --ticket ] && [ -n "${2:-}" ] || { echo "usage: $0 --ticket <任务目录>" >&2; exit 2; }
  TICKET_DIR=$(CDPATH= cd -- "$2" 2>/dev/null && pwd) || { echo "任务目录不存在：$2" >&2; exit 2; }
  TICKET=$TICKET_DIR/ticket; PLAN=$TICKET_DIR/plan; ROOT=$(dirname "$TICKET_DIR")
  [ -f "$TICKET" ] && load_ticket || { echo "ticket 缺失或字段不全：$TICKET" >&2; exit 2; }
  acquire_lock || exit 3
  trap release_lock EXIT
  trap 'exit 130' INT TERM

  ATTEMPTS=$(plan_read attempts); ATTEMPTS=${ATTEMPTS:-0}
  [ -n "$(plan_read callback)" ] || plan_write callback pending
  plan_write attempts "$ATTEMPTS"
  plan_write watcher_pane "${HERDR_PANE_ID:-}"
  plan_write state WATCH
  qw_log "开始守护 ${EXEC_PANE}（${EXEC_KIND}，模式 ${MODE}，任务 ${ID}）"

  SKIP_WAIT=0
  while :; do
    if [ "$SKIP_WAIT" -ne 1 ]; then
      _try=0
      while :; do
        _out=$(sh "$SCRIPT_DIR/wait-settled.sh" "$EXEC_PANE" "$QW_WAIT_MS" "$QW_SETTLE_MS"); _rc=$?
        [ "$_rc" -ne 1 ] && break
        _try=$((_try + 1))
        [ "$_try" -ge "$QW_RETRIES" ] && break
        qw_sleep "$QW_RETRY_DELAY"
      done
      case $_rc in
        0) ;;
        124) continue ;;
        *) notify_user "目标 pane 不可用" "${EXEC_PANE}：$_out"; finish_abnormal "目标 pane 不可用" 4 ;;
      esac
    fi
    SKIP_WAIT=0

    if [ "$MODE" != watch ] && [ -e "$DONE_FILE" ]; then
      plan_write state CALLBACK
      if notify_scheduler; then finish_normal; else finish_abnormal "回调未送达" 5; fi
    fi

    classify_target
    case $CLS in
      ERROR) notify_user "读取目标失败" "$EXEC_PANE"; finish_abnormal "读取目标失败" 4 ;;
      WORKING) event_clear; continue ;;
      NONE)
        event_clear
        if [ "$MODE" = watch ]; then
          # 只有「监视模式下正常停下」（idle / done）可以静音；blocked（等批准或回答）、unknown 等
          # 需要用户处理的停下始终通知，否则 watcher 退出后就没有任何东西在盯着它了。
          if [ "$NOTIFY_STOP" = 0 ] && { [ "$STATE" = idle ] || [ "$STATE" = done ]; }; then
            qw_log "目标已停下，原因不是额度（notify_stop=0，不弹通知）：${EXEC_PANE} 状态 ${STATE}"
            plan_write last_notice "目标已停下（静音）"
          else
            notify_user "目标已停下，原因不是额度" "$EXEC_PANE 状态 $STATE"
          fi
          finish_normal
        fi
        if [ "$STATE" = blocked ]; then
          # R5：执行者在等批准或回答。通知一次，继续守护，等它离开 blocked 后回到循环顶部重新判定。
          plan_write state BLOCKED
          notify_user "执行者在等待批准或回答" "$EXEC_PANE 处理后 watcher 会继续守护；最多等待 $((QW_BLOCK_MAX / 60)) 分钟"
          wait_blocked_cleared; _b=$?
          case $_b in
            0) plan_write state WATCH; SKIP_WAIT=1; continue ;;
            1) notify_user "等待处理超时" "$EXEC_PANE 仍在 blocked，已停止守护"; finish_abnormal "blocked 超时" 5 ;;
            *) notify_user "目标 pane 不可用" "$EXEC_PANE"; finish_abnormal "目标 pane 不可用" 4 ;;
          esac
        fi
        if [ "$CALLBACK_ON" = stop ] && { [ "$STATE" = idle ] || [ "$STATE" = done ]; }; then
          plan_write state CALLBACK
          if notify_scheduler; then finish_normal; else finish_abnormal "回调未送达" 5; fi
        fi
        notify_user "执行者已停下，但没有完成标志，原因不是额度" "$EXEC_PANE 状态 ${STATE}，请查看（提问、被打断或崩溃）"
        finish_abnormal "非额度原因停下" 5 ;;
    esac

    # 额度限制
    plan_write class "$CLS"
    if [ "$MODE" = D ]; then
      if reassign_to_d_target; then event_clear; continue; fi
      finish_abnormal "改派失败" 5
    fi
    if [ "$CLS" = LIMIT_UNTIMED ]; then
      notify_user "额度受限且没有恢复时间" "${EXEC_PANE}：工作区无额度、超出支出上限等，不会自动等待"
      finish_abnormal "不限时类额度" 5
    fi
    if [ "$ATTEMPTS" -ge "$QW_MAX_ATTEMPTS" ]; then
      notify_user "恢复重试已达上限" "$EXEC_PANE 已重试 $ATTEMPTS 次仍被限额，请手动处理"
      finish_abnormal "重试用尽" 5
    fi

    if [ -n "$RESET" ]; then
      _n=$(qp_now)
      if [ $((RESET - _n)) -gt "$QW_FAR" ]; then
        notify_user "恢复时间超过 $((QW_FAR / 3600)) 小时，不自动等待" "$EXEC_PANE 预计 $(qp_fmt_epoch "$RESET" '%F %H:%M') 恢复（来源 ${RSRC}）"
        finish_abnormal "恢复时间太远" 5
      fi
      if [ "$CLS" = CLAUDE_AUTO ]; then _extra=$QW_GRACE; else _extra=$QW_BUFFER; fi
      _t=$((RESET + _extra))
      plan_write state WAIT_RESET; plan_write reset_epoch "$RESET"; plan_write reset_source "$RSRC"
      if [ "$EVENT_NOTIFIED" -eq 0 ]; then
        EVENT_NOTIFIED=1
        notify_user "额度已限，等待恢复" "$EXEC_PANE 预计 $(qp_fmt_epoch "$RESET" '%F %H:%M') 恢复（来源 ${RSRC}），到点后自动继续"
      fi
      wait_until_epoch "$_t"; _w=$?
      case $_w in
        1) event_clear; continue ;;
        2) notify_user "目标 pane 不可用" "$EXEC_PANE"; finish_abnormal "目标 pane 不可用" 4 ;;
      esac
      plan_write state RESUMING
      resume_target; _r=$?
      case $_r in
        0 | 1) event_clear; continue ;;
        *) finish_abnormal "恢复失败" 5 ;;
      esac
    else
      _n=$(qp_now)
      if [ "$PROBE_DEADLINE" -eq 0 ]; then
        PROBE_DEADLINE=$((_n + QW_PROBE_MAX))
        plan_write state PROBE; plan_write probe_deadline "$PROBE_DEADLINE"; plan_write reset_source unknown
        notify_user "额度已限，恢复时间未知" "$EXEC_PANE 每 $((QW_PROBE_INTERVAL / 60)) 分钟探测一次，最长 $((QW_PROBE_MAX / 3600)) 小时"
      fi
      if [ "$_n" -ge "$PROBE_DEADLINE" ]; then
        notify_user "探测超时，恢复时间仍未知" "$EXEC_PANE 请手动处理"
        finish_abnormal "探测超时" 5
      fi
      wait_until_epoch $((_n + QW_PROBE_INTERVAL)); _w=$?
      case $_w in
        1) event_clear; continue ;;
        2) notify_user "目标 pane 不可用" "$EXEC_PANE"; finish_abnormal "目标 pane 不可用" 4 ;;
      esac
      SKIP_WAIT=1
    fi
  done
}

main "$@"
