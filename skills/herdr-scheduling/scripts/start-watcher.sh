#!/bin/sh
# 创建任务目录和 ticket，并在专用 pane 里启动 quota-watcher.sh。
#
# 子命令：
#   init  --mode A|D|watch --executor <pane> [选项]   创建任务目录和 ticket（不创建 pane、不启动进程）
#   run   <任务目录> [--require-working] [--env QW_X=N ...]
#                                                     拆 pane、核对工作区和 tab、启动 watcher、确认已启动
#   watch --executor <pane> [选项]                    init（mode=watch）加 run；监视入口和联动用这一条
#   whoami [--exclude <pane>]                        输出调度者（本 agent）的权威 pane_id、workspace_id、tab_id、source=env|discovered；
#                                                     Codex 里（CODEX_THREAD_ID 非空）环境变量过期时按类型、工作目录、状态自动发现，唯一匹配才采用
#   dispatch --executor <pane> --scheduler <pane> --scheduler-kind <kind> (--prompt <文本> | --prompt-file <文件>) [选项]
#                                                     init、发提示词并确认执行者已开始、run（不带 --require-working）；
#                                                     其他 skill 调用本 skill 派发并守护时用这一条（见 references/integration.md）
#
# init / watch 的选项：
#   --kind <kind>             执行者 kind（缺省从 herdr agent get 读取）
#   --scheduler <pane> --scheduler-kind <kind>        调度者（mode=A|D 必填）
#   --d-pane <pane> --d-kind <kind>                   方案 D 的改派目标（mode=D 必填）
#   --prompt-file <文件>      原始任务内容，复制为 prompt.txt（方案 D 改派时使用）
#   --result-file <路径>      结果文件路径（缺省为任务目录下的 result.md）
#   --notify-stop 0|1         监视模式下目标正常停下时是否通知（缺省 1；0 为静音）
#   --callback-on done|stop   mode=A|D：done（缺省）等 done 文件再回调；stop 在执行者稳定停在 idle/done 且不是额度原因时就回调
#   --callback-prompt <文本>  mode=A|D：回调调度者时发送的一行文本（缺省「使用 $herdr-scheduling 继续调度（任务：<ID>）」）
#   --marker-dir <绝对路径>   mode=A|D：完成标志所在的目录（缺省 <执行者工作目录>/.herdr-scheduling-tmp，取不到工作目录时用任务目录）。
#                             脚本自己决定标志的路径：<该目录>/<任务ID>.done，每次派发都是新文件；目录不存在时由脚本创建。
#                             执行者在结束前最后一步创建它，watcher 一看到就立即回调，回调后 watcher 删除它（目录由脚本创建且已空时一并删除）。
#                             callback_on=done 时发给执行者的样板用这个路径；stop 时脚本在提示词后追加一句让执行者创建它
# dispatch 的选项：init 的选项（--mode 缺省 A，只接受 A、D；不含 --notify-stop）加 run 的 --env；另有
#   --prompt <文本> | --prompt-file <文件>   恰好给一个。callback_on=done 时脚本在提示词后追加「写结果文件、创建 done 文件」的样板，
#                                            stop 时在提示词后追加一句「最后一步创建完成标志」；prompt.txt 保存实际发出的完整提示词。不接受 --require-working。
# run / watch 的选项：
#   --require-working         执行者必须当前是 working，否则退出码 6 且不创建 pane。
#                             只用于「还没有确认过执行者已开始」的场景；派发入口在确认过 --until working 之后
#                             看到它不是 working，说明它很快停下了（已完成或触达额度），此时不要加这个选项，
#                             让 watcher 自己判定。
#   --env QW_X=N              给 watcher 传环境变量（可重复，只接受 QW_ 开头的数字参数）
#
# 标准输出为若干行 键=值：task_id、task_dir、result_file、done_file、watcher_pane（视子命令而定）。
# 退出码：0 成功；2 参数错误；3 不在 herdr 中，或 HERDR_PANE_ID 指向不存在的 pane / 其他工作区（环境变量可能过期），Codex 里还包括自动发现调度者的 pane 没有唯一匹配；4 执行者不存在或不在当前工作区；
#         5 拆 pane 失败或新 pane 的工作区、tab 与执行者不一致（已关闭新 pane）；
#         6 不满足 --require-working；7 watcher 没有确认启动；
#         8 dispatch：执行者被阻塞（提示词可能没有送达），没有启动 watcher；
#         9 dispatch：没有确认执行者开始工作（提示词可能已送达，不要盲目重发），没有启动 watcher。
#
# 可用环境变量（测试时可缩小）：SW_ROOT 任务目录根（默认 ${TMPDIR:-/tmp}/herdr-scheduling）、
#   SW_RETRY_DELAY 读取执行者信息的重试间隔秒（2）、SW_CONFIRM_TRIES 确认启动的次数（6）、SW_CONFIRM_DELAY 间隔秒（1）。

set -u

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT=${SW_ROOT:-${TMPDIR:-/tmp}/herdr-scheduling}
SW_RETRY_DELAY=${SW_RETRY_DELAY:-2}
SW_CONFIRM_TRIES=${SW_CONFIRM_TRIES:-6}
SW_CONFIRM_DELAY=${SW_CONFIRM_DELAY:-1}

MODE=; EXEC=; KIND=; SCHED=; SCHED_KIND=; D_PANE=; D_KIND=; PROMPT_FILE=; RESULT_FILE=; NOTIFY_STOP=1; MARKER_DIR_OPT=; MARKER_DIR_OPT_SET=0
PROMPT_TEXT=; PROMPT_TEXT_SET=0; CALLBACK_ON=; CALLBACK_ON_SET=0; CALLBACK_PROMPT=; CALLBACK_PROMPT_SET=0; FULL_PROMPT=
REQUIRE_WORKING=0; ENVS=; TASK_DIR=
A_KIND=; A_STATE=; A_CWD=; A_WS=; A_TAB=
TASK_ID=; DONE_FILE=; MARKER_DIR=; MARKER_CREATED=0
EXCLUDE_OPT=; SELF_EXCLUDE=; SELF_SOURCE=env; ENV_STALE_PANE=; SELF_PANE=; SELF_WS=; SELF_TAB=

sw_err() { printf '%s\n' "$*" >&2; }

sw_usage() {
  sw_err "用法："
  sw_err "  start-watcher.sh init  --mode A|D|watch --executor <pane> [--kind K] [--scheduler P --scheduler-kind K] [--d-pane P --d-kind K] [--prompt-file F] [--result-file F] [--notify-stop 0|1] [--callback-on done|stop] [--callback-prompt 文本] [--marker-dir 绝对路径]"
  sw_err "  start-watcher.sh run   <任务目录> [--require-working] [--env QW_X=N]..."
  sw_err "  start-watcher.sh watch --executor <pane> [--kind K] [--notify-stop 0|1] [--require-working] [--env QW_X=N]..."
  sw_err "  start-watcher.sh whoami [--exclude <pane>]    输出调度者（本 agent）的权威 pane_id、workspace_id、tab_id、source"
  sw_err "  start-watcher.sh dispatch --executor <pane> --scheduler P --scheduler-kind K (--prompt 文本 | --prompt-file F) [--kind K] [--mode A|D --d-pane P --d-kind K] [--callback-on done|stop] [--callback-prompt 文本] [--marker-dir 绝对路径] [--result-file F] [--env QW_X=N]..."
}

json_str() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | sed 's/^[^:]*:"//; s/"$//'; }

kv_get() { sed -n "s/^$2=//p" "$1" 2>/dev/null | head -1; }

# 8.1 Codex 里环境变量过期时，自动找到调度者（本 agent）真实所在的 pane。
# 候选 = herdr agent list 里 agent 为 codex、cwd 或 foreground_cwd 等于当前目录、不在排除集（SELF_EXCLUDE）的 pane；
# 多于一个时再按状态 working / blocked 收窄（调度者正在运行这条命令）。恰好一个才采用，设置 SELF_PANE、SELF_WS、SELF_TAB；
# 零个或多个返回 3（有歧义时不猜：判错会把回调发进别人的会话）。
sw_discover_self() {
  _d1=$(pwd -P 2>/dev/null) || _d1=; _d2=$(pwd 2>/dev/null) || _d2=
  _d1=$(printf '%s' "$_d1" | sed 's#//*#/#g; s#\(.\)/$#\1#'); _d2=$(printf '%s' "$_d2" | sed 's#//*#/#g; s#\(.\)/$#\1#')   # 规整重复和结尾的 /
  [ -n "$_d1$_d2" ] || { sw_err "无法取得当前目录，不能自动发现调度者 pane。"; return 3; }
  _al=$(herdr agent list 2>&1) || { sw_err "herdr agent list 失败，不能自动发现调度者 pane：${_al}"; return 3; }
  _tab='|'
  _recs=$(printf '%s' "$_al" | awk '
    { s = s $0 }
    END {
      p = index(s, "\"agents\":[")
      if (p == 0) exit
      n = length(s); depth = 0; instr = 0; esc = 0; obj = ""; cnt = 0
      for (i = p + 10; i <= n; i++) {
        c = substr(s, i, 1)
        if (instr) {
          if (depth > 0) obj = obj c
          if (esc) esc = 0
          else if (c == "\\") esc = 1
          else if (c == "\"") instr = 0
          continue
        }
        if (c == "\"") { instr = 1; if (depth > 0) obj = obj c; continue }
        if (c == "{") { depth++; obj = obj c; continue }
        if (c == "}") {
          depth--; obj = obj c
          if (depth == 0) { print obj; obj = ""; cnt++; if (cnt >= 200) exit }
          continue
        }
        if (c == "]" && depth == 0) exit
        if (depth > 0) obj = obj c
      }
    }')
  _cands=; _n=0
  while IFS= read -r _o; do
    [ -n "$_o" ] || continue
    [ "$(printf '%s\n' "$_o" | json_str agent)" = codex ] || continue
    _op=$(printf '%s\n' "$_o" | json_str pane_id); [ -n "$_op" ] || continue
    case " $SELF_EXCLUDE " in *" $_op "*) continue ;; esac
    _oc=$(printf '%s\n' "$_o" | json_str cwd); _of=$(printf '%s\n' "$_o" | json_str foreground_cwd)
    _m=0
    for _x in "$_oc" "$_of"; do
      [ -n "$_x" ] || continue
      _x=$(printf '%s' "$_x" | sed 's#//*#/#g; s#\(.\)/$#\1#')
      { [ "$_x" = "$_d1" ] || [ "$_x" = "$_d2" ]; } && _m=1
    done
    [ "$_m" = 1 ] || continue
    _cands="${_cands}${_op}${_tab}$(printf '%s\n' "$_o" | json_str workspace_id)${_tab}$(printf '%s\n' "$_o" | json_str tab_id)${_tab}$(printf '%s\n' "$_o" | json_str agent_status)${_tab}${_oc}
"
    _n=$((_n + 1))
  done <<EOF_RECS
$_recs
EOF_RECS
  _basis="类型 codex、工作目录一致"
  if [ "$_n" -gt 1 ]; then
    _nc=; _nn=0
    while IFS="$_tab" read -r _p _w _t _st _c; do
      [ -n "$_p" ] || continue
      case $_st in working | blocked) _nc="${_nc}${_p}${_tab}${_w}${_tab}${_t}${_tab}${_st}${_tab}${_c}
"; _nn=$((_nn + 1)) ;; esac
    done <<EOF_C
$_cands
EOF_C
    if [ "$_nn" -eq 1 ]; then _cands=$_nc; _n=1; _basis="类型 codex、工作目录一致、状态 working/blocked（在运行这条命令）"; fi
  fi
  if [ "$_n" -ne 1 ]; then
    if [ "$_n" -eq 0 ]; then sw_err "没有找到调度者所在的 pane：herdr agent list 里没有类型 codex 且工作目录为 ${_d2:-$_d1} 的 agent。"
    else
      sw_err "找到 ${_n} 个类型 codex 且工作目录一致的候选，无法唯一确定哪个是调度者（不猜，以免回调发进别人的会话）："
      while IFS="$_tab" read -r _p _w _t _st _c; do [ -n "$_p" ] && sw_err "  ${_p}（工作区 ${_w}，状态 ${_st}）"; done <<EOF_L
$_cands
EOF_L
    fi
    return 3
  fi
  IFS="$_tab" read -r SELF_PANE SELF_WS SELF_TAB _rest <<EOF_ONE
$_cands
EOF_ONE
  [ -n "$SELF_PANE" ] && [ -n "$SELF_WS" ] || { sw_err "候选缺少 pane_id 或 workspace_id，无法采用。"; return 3; }
  sw_err "调度者已自动确认为 pane ${SELF_PANE}（工作区 ${SELF_WS}），依据：${_basis}。环境变量 HERDR_PANE_ID=${HERDR_PANE_ID:-空} 已过期（Codex 共享 app-server 守护进程带来的旧值），本次运行使用这个值；建议今后用 codex --no-daemon 启动 Codex。"
  return 0
}

# 8.2 Codex 里判定环境变量是否过期；过期则自动发现并改写 HERDR_PANE_ID / HERDR_WORKSPACE_ID / HERDR_TAB_ID。
sw_codex_env() {
  _stale=0
  if [ -n "${HERDR_PANE_ID:-}" ]; then
    if _pg=$(herdr pane get "$HERDR_PANE_ID" 2>/dev/null); then
      _pw=$(printf '%s\n' "$_pg" | json_str workspace_id)
      _pa=$(printf '%s\n' "$_pg" | json_str agent)
      _ps=$(printf '%s\n' "$_pg" | grep -o '"agent_session":{[^}]*}' | json_str value)
      if [ "$_pw" != "${HERDR_WORKSPACE_ID:-}" ]; then _stale=1
      elif [ -n "$_ps" ]; then [ "$_ps" = "$CODEX_THREAD_ID" ] || _stale=1
      elif [ -n "$_pa" ] && [ "$_pa" != codex ]; then _stale=1
      fi
    else
      _stale=1
    fi
    [ "$_stale" = 1 ] || return 0
    ENV_STALE_PANE=$HERDR_PANE_ID
  fi
  SELF_EXCLUDE="${EXEC:-} ${D_PANE:-} ${EXCLUDE_OPT:-}"
  if sw_discover_self; then
    export HERDR_PANE_ID=$SELF_PANE HERDR_WORKSPACE_ID=$SELF_WS
    export HERDR_TAB_ID=$SELF_TAB   # 缺失时为空：whoami 再用 herdr pane get 补，不残留过期的旧值
    SELF_SOURCE=discovered
    return 0
  fi
  # 环境变量本来就为空：保持原行为（不拦截，只比较执行者与调度者）；有值但判为过期：无法修正，退出码 3
  [ -n "$ENV_STALE_PANE" ] || return 0
  sw_err "HERDR_PANE_ID=${ENV_STALE_PANE} 已过期或指向别的 pane，而且无法自动确定真实的 pane。"
  sw_err "处理：用 codex --no-daemon 启动 Codex（用 herdr 启动时在命令末尾加 -- --no-daemon）；或请用户确认真实的 pane、工作区、tab 后，用 HERDR_PANE_ID=… HERDR_WORKSPACE_ID=… HERDR_TAB_ID=… 作为前缀重新运行。"
  return 3
}

# 8.3 检查运行环境
sw_check_env() {
  SELF_SOURCE=env; ENV_STALE_PANE=
  if [ "${HERDR_ENV:-}" != 1 ] || [ -z "${HERDR_WORKSPACE_ID:-}" ]; then
    sw_err "不在 herdr 中：需要 HERDR_ENV=1 且 HERDR_WORKSPACE_ID 非空"
    return 3
  fi
  command -v herdr >/dev/null 2>&1 || { sw_err "找不到 herdr 命令"; return 3; }
  # Codex 里（CODEX_THREAD_ID 非空）：共享 app-server 守护进程可能带来旧的 HERDR_*，过期时自动发现
  if [ -n "${CODEX_THREAD_ID:-}" ]; then sw_codex_env; return $?; fi
  # 8.4 HERDR_PANE_ID 非空时必须指向真实存在、且在当前工作区的 pane；否则环境变量可能已过期。
  if [ -n "${HERDR_PANE_ID:-}" ]; then
    _pg=$(herdr pane get "$HERDR_PANE_ID" 2>/dev/null) || {
      sw_err "HERDR_PANE_ID=${HERDR_PANE_ID} 指向的 pane 不存在。环境变量可能已过期（例如 Codex 的 app-server 守护进程在更早的 herdr pane 里启动，所有经它启动的 Codex agent 继承了它的旧环境）。"
      sw_err "处理：Codex 用 codex --no-daemon 启动（用 herdr 启动时在命令末尾加 -- --no-daemon）；或请用户确认真实的 pane、工作区、tab 后，用 HERDR_PANE_ID=… HERDR_WORKSPACE_ID=… HERDR_TAB_ID=… 作为前缀重新运行。若确认环境正确，可重试一次。"
      return 3
    }
    _pw=$(printf '%s\n' "$_pg" | json_str workspace_id)
    if [ "$_pw" != "$HERDR_WORKSPACE_ID" ]; then
      sw_err "HERDR_PANE_ID=${HERDR_PANE_ID} 在工作区 ${_pw}，与 HERDR_WORKSPACE_ID=${HERDR_WORKSPACE_ID} 不一致，环境变量可能已过期（见上一条提示的处理办法）。"
      return 3
    fi
  fi
  return 0
}

# 8.6 校验要拼进命令字符串的路径：不能含单引号或换行
sw_quote_ok() {
  _nl=$(printf '\n.'); _nl=${_nl%.}
  case $1 in
    *"'"* | *"${_nl}"*) return 1 ;;
  esac
  return 0
}

# 校验写进 ticket 的字段：拒绝换行（防止注入额外字段），再按正则校验格式。用法：sw_check_val <选项名> <正则> <值>
SW_RE_PANE='^[A-Za-z0-9:_.-]+$'
SW_RE_KIND='^[A-Za-z0-9_-]+$'
sw_check_val() {
  _nl=$(printf '\n.'); _nl=${_nl%.}
  case $3 in *"${_nl}"*) sw_err "$1 不能含换行"; return 2 ;; esac
  printf '%s' "$3" | grep -Eq "$2" || { sw_err "$1 格式不合法：$3"; return 2; }
  return 0
}

sw_env_ok() { printf '%s' "$1" | grep -Eq '^QW_[A-Z_]+=[0-9]+$'; }

# 8.4 读取执行者的 kind、状态、cwd、工作区、tab；设置 A_*。返回 4 表示不存在或不在当前工作区。
sw_agent_info() {
  _try=0; _o=
  while :; do
    if _o=$(herdr agent get "$1" 2>/dev/null); then break; fi
    _try=$((_try + 1))
    if [ "$_try" -ge 3 ]; then sw_err "执行者不存在或无法读取：$1"; return 4; fi
    sleep "$SW_RETRY_DELAY"
  done
  A_KIND=$(printf '%s\n' "$_o" | json_str agent)
  A_STATE=$(printf '%s\n' "$_o" | json_str agent_status)
  A_CWD=$(printf '%s\n' "$_o" | json_str cwd)
  A_WS=$(printf '%s\n' "$_o" | json_str workspace_id)
  A_TAB=$(printf '%s\n' "$_o" | json_str tab_id)
  if [ "$A_WS" != "${HERDR_WORKSPACE_ID:-}" ]; then
    sw_err "执行者 $1 不在当前工作区（${A_WS} != ${HERDR_WORKSPACE_ID:-}）"
    return 4
  fi
  return 0
}

# 解析选项；SW_ALLOW 是当前子命令允许的选项（空格分隔），SW_SUB、SW_HINT 用于错误提示。
# 未知选项、对当前子命令无效的选项、缺少取值都返回 2。
SW_ALLOW=; SW_SUB=; SW_HINT=
sw_parse() {
  while [ $# -gt 0 ]; do
    case $1 in
      --mode | --executor | --kind | --scheduler | --scheduler-kind | --d-pane | --d-kind | --prompt | --prompt-file | --result-file | --notify-stop | --callback-on | --callback-prompt | --marker-dir | --exclude | --require-working | --env) ;;
      *) sw_err "未知选项：$1"; return 2 ;;
    esac
    case " $SW_ALLOW " in
      *" $1 "*) ;;
      *) sw_err "选项 $1 对 ${SW_SUB} 无效${SW_HINT:+：$SW_HINT}"; return 2 ;;
    esac
    case $1 in
      --require-working) ;;
      *) [ $# -ge 2 ] || { sw_err "选项 $1 需要一个值"; return 2; } ;;
    esac
    case $1 in
      --mode) MODE=${2:-}; shift 2 ;;
      --executor) EXEC=${2:-}; shift 2 ;;
      --kind) KIND=${2:-}; shift 2 ;;
      --scheduler) SCHED=${2:-}; shift 2 ;;
      --scheduler-kind) SCHED_KIND=${2:-}; shift 2 ;;
      --d-pane) D_PANE=${2:-}; shift 2 ;;
      --d-kind) D_KIND=${2:-}; shift 2 ;;
      --prompt) PROMPT_TEXT=${2:-}; PROMPT_TEXT_SET=1; shift 2 ;;
      --prompt-file) PROMPT_FILE=${2:-}; shift 2 ;;
      --result-file) RESULT_FILE=${2:-}; shift 2 ;;
      --notify-stop) NOTIFY_STOP=${2:-}; shift 2 ;;
      --callback-on) CALLBACK_ON=${2:-}; CALLBACK_ON_SET=1; shift 2 ;;
      --callback-prompt) CALLBACK_PROMPT=${2:-}; CALLBACK_PROMPT_SET=1; shift 2 ;;
      --marker-dir) MARKER_DIR_OPT=${2:-}; MARKER_DIR_OPT_SET=1; shift 2 ;;
      --exclude) EXCLUDE_OPT=${2:-}; shift 2 ;;
      --require-working) REQUIRE_WORKING=1; shift ;;
      --env)
        sw_env_ok "${2:-}" || { sw_err "--env 只接受 QW_ 开头的数字参数，如 QW_GRACE=600：${2:-}"; return 2; }
        ENVS="${ENVS:+$ENVS }$2"; shift 2 ;;
      *) sw_err "未知选项：$1"; return 2 ;;
    esac
  done
  return 0
}

# 8.5 校验参数、生成任务ID、创建任务目录并写 ticket（原子写入）
sw_write_ticket() {
  case $MODE in A | D | watch) ;; *) sw_err "--mode 必须是 A、D 或 watch"; return 2 ;; esac
  [ -n "$EXEC" ] || { sw_err "缺少 --executor"; return 2; }
  case $NOTIFY_STOP in 0 | 1) ;; *) sw_err "--notify-stop 只能是 0 或 1"; return 2 ;; esac
  if [ "$CALLBACK_ON_SET" = 1 ]; then
    case $CALLBACK_ON in done | stop) ;; *) sw_err "--callback-on 只能是 done 或 stop"; return 2 ;; esac
  fi
  if [ "$MODE" = watch ] && { [ "$CALLBACK_ON_SET" = 1 ] || [ "$CALLBACK_PROMPT_SET" = 1 ] || [ "$MARKER_DIR_OPT_SET" = 1 ]; }; then
    sw_err "mode=watch 没有调度者，不能指定 --callback-on、--callback-prompt、--marker-dir"; return 2
  fi
  if [ "$MARKER_DIR_OPT_SET" = 1 ]; then
    [ -n "$MARKER_DIR_OPT" ] || { sw_err "--marker-dir 不能为空"; return 2; }
    case $MARKER_DIR_OPT in /*) ;; *) sw_err "--marker-dir 需要绝对路径：$MARKER_DIR_OPT"; return 2 ;; esac
    sw_quote_ok "$MARKER_DIR_OPT" || { sw_err "--marker-dir 含单引号或换行，无法安全拼进命令：$MARKER_DIR_OPT"; return 2; }
  fi
  if [ "$CALLBACK_PROMPT_SET" = 1 ]; then
    [ -n "$CALLBACK_PROMPT" ] || { sw_err "--callback-prompt 不能为空"; return 2; }
    sw_check_val --callback-prompt '^[^-]' "$CALLBACK_PROMPT" || return 2
    [ "${#CALLBACK_PROMPT}" -le 500 ] || { sw_err "--callback-prompt 超过 500 字符"; return 2; }
  fi
  sw_check_val --executor "$SW_RE_PANE" "$EXEC" || return 2
  [ -z "$KIND" ] || sw_check_val --kind "$SW_RE_KIND" "$KIND" || return 2
  [ -z "$RESULT_FILE" ] || sw_check_val --result-file '^.+$' "$RESULT_FILE" || return 2
  [ -z "$PROMPT_FILE" ] || sw_check_val --prompt-file '^.+$' "$PROMPT_FILE" || return 2
  if [ "$MODE" != watch ]; then
    # 自动发现了调度者的真实 pane，而调用方传的 --scheduler 还是过期的环境变量值：替换，否则回调会发错会话
    if [ "$SELF_SOURCE" = discovered ] && [ -n "$ENV_STALE_PANE" ] && [ "$SCHED" = "$ENV_STALE_PANE" ]; then
      sw_err "--scheduler ${SCHED} 是过期的环境变量值，已替换为自动确认的调度者 ${SELF_PANE}"
      SCHED=$SELF_PANE
    fi
    [ -n "$SCHED" ] && [ -n "$SCHED_KIND" ] || { sw_err "mode=${MODE} 需要 --scheduler 和 --scheduler-kind"; return 2; }
    sw_check_val --scheduler "$SW_RE_PANE" "$SCHED" || return 2
    sw_check_val --scheduler-kind "$SW_RE_KIND" "$SCHED_KIND" || return 2
    # 执行者永远是另一个 pane：不能是调度者，也不能是调用者自己（dispatch 会真的向执行者发提示词）
    [ "$EXEC" != "$SCHED" ] || { sw_err "执行者不能是调度者本身（执行者永远是另一个 pane）：$EXEC"; return 2; }
    [ -z "${HERDR_PANE_ID:-}" ] || [ "$EXEC" != "$HERDR_PANE_ID" ] || { sw_err "执行者不能是当前 pane（执行者永远是另一个 pane）：$EXEC"; return 2; }
  fi
  if [ "$MODE" = D ]; then
    [ -n "$D_PANE" ] && [ -n "$D_KIND" ] || { sw_err "mode=D 需要 --d-pane 和 --d-kind"; return 2; }
    sw_check_val --d-pane "$SW_RE_PANE" "$D_PANE" || return 2
    [ "$D_PANE" != "$EXEC" ] && [ "$D_PANE" != "$SCHED" ] && { [ -z "${HERDR_PANE_ID:-}" ] || [ "$D_PANE" != "$HERDR_PANE_ID" ]; } || { sw_err "--d-pane 不能是执行者、调度者或当前 pane：$D_PANE"; return 2; }
    sw_check_val --d-kind "$SW_RE_KIND" "$D_KIND" || return 2
    [ -n "$PROMPT_FILE" ] || [ -n "$PROMPT_TEXT" ] || sw_err "警告：mode=D 没有 --prompt-file，改派时只会发送说明文字"
  fi
  if [ -n "$PROMPT_FILE" ] && [ ! -f "$PROMPT_FILE" ]; then sw_err "--prompt-file 不存在：$PROMPT_FILE"; return 2; fi
  sw_quote_ok "$ROOT" || { sw_err "任务目录根含单引号或换行，无法安全拼进命令：$ROOT"; return 2; }

  sw_agent_info "$EXEC" || return $?
  [ -n "$KIND" ] || KIND=$A_KIND
  [ -n "$KIND" ] || { sw_err "无法确定执行者 kind，请用 --kind 指定"; return 2; }

  mkdir -p "$ROOT" || { sw_err "无法创建 $ROOT"; return 2; }
  ROOT=$(CDPATH= cd -- "$ROOT" && pwd) || return 2   # 规范化（去掉双斜杠等），init 与 run 输出的路径一致
  sw_quote_ok "$ROOT" || { sw_err "任务目录根含单引号或换行，无法安全拼进命令：$ROOT"; return 2; }
  _p=$(printf '%s' "$EXEC" | tr ':/' '--')
  _ts=$(date +%Y%m%d%H%M%S); TASK_ID="hs-${_ts}-${_p}"; _n=1
  while ! mkdir "$ROOT/$TASK_ID" 2>/dev/null; do
    _n=$((_n + 1)); TASK_ID="hs-${_ts}-${_p}-${_n}"
    [ "$_n" -gt 999 ] && { sw_err "无法生成唯一任务ID"; return 2; }
  done
  TASK_DIR=$ROOT/$TASK_ID
  [ -n "$RESULT_FILE" ] || RESULT_FILE=$TASK_DIR/result.md
  DONE_FILE=$TASK_DIR/done
  MARKER_DIR=; MARKER_CREATED=0
  if [ "$MODE" != watch ]; then
    # 完成标志由脚本指定：<标志目录>/<任务ID>.done，每次派发都是新文件
    MARKER_DIR=$MARKER_DIR_OPT
    [ -n "$MARKER_DIR" ] || { [ -n "$A_CWD" ] && MARKER_DIR=${A_CWD%/}/.herdr-scheduling-tmp; }
    if [ -n "$MARKER_DIR" ] && ! sw_quote_ok "$MARKER_DIR"; then
      if [ "$MARKER_DIR_OPT_SET" = 1 ]; then rmdir "$TASK_DIR" 2>/dev/null; sw_err "标志目录含单引号或换行：$MARKER_DIR"; return 2; fi
      MARKER_DIR=   # 缺省目录不合规：退回任务目录
    fi
    if [ -n "$MARKER_DIR" ]; then
      [ -d "$MARKER_DIR" ] || { mkdir -p "$MARKER_DIR" 2>/dev/null && MARKER_CREATED=1; }
      if [ -d "$MARKER_DIR" ] && [ -w "$MARKER_DIR" ]; then
        DONE_FILE=$MARKER_DIR/$TASK_ID.done
        if [ -e "$DONE_FILE" ]; then rmdir "$TASK_DIR" 2>/dev/null; sw_err "完成标志已存在：${DONE_FILE}（请删除后重试）"; return 2; fi
      elif [ "$MARKER_DIR_OPT_SET" = 1 ]; then
        rmdir "$TASK_DIR" 2>/dev/null; sw_err "无法创建或写入标志目录：$MARKER_DIR"; return 2
      else
        MARKER_DIR=; MARKER_CREATED=0   # 缺省目录不可写：退回任务目录
      fi
    fi
  fi
  [ -z "$PROMPT_FILE" ] || cp "$PROMPT_FILE" "$TASK_DIR/prompt.txt"

  {
    printf 'id=%s\n' "$TASK_ID"
    printf 'mode=%s\n' "$MODE"
    if [ "$MODE" = watch ]; then printf 'scheduler_pane=\nscheduler_kind=\n'; else printf 'scheduler_pane=%s\nscheduler_kind=%s\n' "$SCHED" "$SCHED_KIND"; fi
    printf 'executor_pane=%s\nexecutor_kind=%s\n' "$EXEC" "$KIND"
    printf 'workspace_id=%s\ntab_id=%s\n' "$A_WS" "$A_TAB"
    if [ "$MODE" = D ]; then printf 'd_kind=%s\nd_pane=%s\n' "$D_KIND" "$D_PANE"; fi
    printf 'result_file=%s\ndone_file=%s\n' "$RESULT_FILE" "$DONE_FILE"
    [ -z "$MARKER_DIR" ] || printf 'marker_dir=%s\nmarker_dir_created=%s\n' "$MARKER_DIR" "$MARKER_CREATED"
    printf 'notify_stop=%s\n' "$NOTIFY_STOP"
    if [ "$MODE" != watch ]; then
      printf 'callback_on=%s\n' "${CALLBACK_ON:-done}"
      [ -z "$CALLBACK_PROMPT" ] || printf 'callback_prompt=%s\n' "$CALLBACK_PROMPT"
    fi
    printf 'created=%s\n' "$(date +%s)"
  } >"$TASK_DIR/ticket.tmp.$$" && mv "$TASK_DIR/ticket.tmp.$$" "$TASK_DIR/ticket"
  return 0
}

sw_print_task() {
  printf 'task_id=%s\ntask_dir=%s\nresult_file=%s\ndone_file=%s\n' "$TASK_ID" "$TASK_DIR" "$RESULT_FILE" "$DONE_FILE"
}

# 8.8 init 子命令
sw_cmd_init() {
  SW_SUB=init; SW_HINT='--require-working、--env 只用于 run/watch/dispatch，--prompt 只用于 dispatch'
  SW_ALLOW='--mode --executor --kind --scheduler --scheduler-kind --d-pane --d-kind --prompt-file --result-file --notify-stop --callback-on --callback-prompt --marker-dir'
  sw_parse "$@" || return $?
  sw_check_env || return $?
  sw_write_ticket || return $?
  sw_print_task
}

# 8.7 run 子命令：解析参数后交给 sw_do_run
sw_cmd_run() {
  [ $# -ge 1 ] || { sw_usage; return 2; }
  TASK_DIR=$1; shift
  SW_SUB=run; SW_HINT='只用于 init/watch，请在 init 时指定（run 不会改动已写好的 ticket）'
  SW_ALLOW='--require-working --env'
  sw_parse "$@" || return $?
  EXEC=$(kv_get "$TASK_DIR/ticket" executor_pane)   # 自动发现调度者时把执行者排除在候选之外
  sw_check_env || return $?
  sw_do_run
}

# 拆 pane、核对工作区和 tab、启动 watcher、确认已启动。使用全局 TASK_DIR、ENVS、REQUIRE_WORKING。
sw_do_run() {
  [ -f "$TASK_DIR/ticket" ] || { sw_err "任务目录里没有 ticket：$TASK_DIR"; return 2; }
  TASK_DIR=$(CDPATH= cd -- "$TASK_DIR" && pwd) || return 2
  sw_quote_ok "$TASK_DIR" && sw_quote_ok "$SCRIPT_DIR" || { sw_err "路径含单引号或换行，无法安全拼进命令"; return 2; }
  EXEC=$(kv_get "$TASK_DIR/ticket" executor_pane)
  TASK_ID=$(kv_get "$TASK_DIR/ticket" id)
  [ -n "$EXEC" ] || { sw_err "ticket 缺少 executor_pane"; return 2; }

  sw_agent_info "$EXEC" || return $?
  if [ "$REQUIRE_WORKING" = 1 ] && [ "$A_STATE" != working ]; then
    sw_err "执行者当前状态是 ${A_STATE:-未知}，不是 working（--require-working）。"
    sw_err "如果你还没有确认过它已开始，请先确认再重试；如果已经确认过 --until working，说明它很快停下了（已完成或触达额度）——去掉 --require-working 重新运行，由 watcher 判定（已有 done 则立即回调，有额度文案则恢复，否则通知）。"
    return 6
  fi

  _so=$(herdr pane split --pane "$EXEC" --direction down --no-focus --cwd "${A_CWD:-.}" 2>&1) || { sw_err "拆 pane 失败：$_so"; return 5; }
  WP=$(printf '%s\n' "$_so" | json_str pane_id)
  WW=$(printf '%s\n' "$_so" | json_str workspace_id)
  WT=$(printf '%s\n' "$_so" | json_str tab_id)
  [ -n "$WP" ] || { sw_err "拆 pane 后没有读到 pane_id：$_so"; return 5; }
  if [ "$WW" != "$A_WS" ] || [ "$WT" != "$A_TAB" ]; then
    sw_err "新 pane ${WP} 的工作区、tab（${WW}、${WT}）与执行者（${A_WS}、${A_TAB}）不一致，已关闭它"
    herdr pane close "$WP" >/dev/null 2>&1
    return 5
  fi

  _cmd="${ENVS:+$ENVS }sh '$SCRIPT_DIR/quota-watcher.sh' --ticket '$TASK_DIR'"
  if ! herdr pane run "$WP" "$_cmd" >/dev/null 2>&1; then
    sw_err "在 ${WP} 中启动 watcher 失败，已关闭它"
    herdr pane close "$WP" >/dev/null 2>&1
    return 7
  fi

  _i=0; _scr=
  while [ "$_i" -lt "$SW_CONFIRM_TRIES" ]; do
    sleep "$SW_CONFIRM_DELAY"
    _scr=$(herdr pane read "$WP" --source visible --lines 12 2>/dev/null)
    case $_scr in
      *开始守护*)
        printf 'task_id=%s\ntask_dir=%s\nresult_file=%s\ndone_file=%s\nwatcher_pane=%s\n' \
          "$TASK_ID" "$TASK_DIR" "$(kv_get "$TASK_DIR/ticket" result_file)" "$(kv_get "$TASK_DIR/ticket" done_file)" "$WP"
        return 0 ;;
      *已有\ watcher*) sw_err "已有 watcher 在守护 ${EXEC}："; sw_err "$_scr"; return 7 ;;
    esac
    _i=$((_i + 1))
  done
  sw_err "watcher 没有确认启动（${WP} 保留，供查看）：$_scr"
  return 7
}

# 8.9 watch 子命令：init（mode=watch）加 run
sw_cmd_watch() {
  MODE=watch
  SW_SUB=watch; SW_HINT=
  SW_ALLOW='--mode --executor --kind --scheduler --scheduler-kind --d-pane --d-kind --prompt-file --result-file --notify-stop --require-working --env'
  sw_parse "$@" || return $?
  MODE=watch
  sw_check_env || return $?
  sw_write_ticket || return $?
  sw_do_run; _wrc=$?
  # init 已成功而 run 失败：任务目录保留，告诉调用者在哪里，便于用 run 重试
  [ "$_wrc" -eq 0 ] || sw_err "任务目录已保留：${TASK_DIR}（可用 run 重试）"
  return "$_wrc"
}

# 8.6 组装实际发出的完整提示词（设置 FULL_PROMPT）：stop 追加「最后一步创建完成标志」；done 追加写结果文件、创建 done 文件的样板。
sw_compose_prompt() {
  _rf=$(kv_get "$TASK_DIR/ticket" result_file)
  _df=$(kv_get "$TASK_DIR/ticket" done_file)
  if [ "${CALLBACK_ON:-done}" = stop ]; then
    FULL_PROMPT="${PROMPT_TEXT}

完成后的最后一步：创建空文件 ${_df}（调度用的完成标志，不要写入内容）。中途停下提问时不要创建。"
    return 0
  fi
  FULL_PROMPT="${PROMPT_TEXT}

完成后请：
1. 把结果写到 ${_rf}
2. 创建文件 ${_df}
不需要自己通知调度者，由 watcher 负责。"
}

# 8.7 向执行者发完整提示词并确认它开始工作。返回 0 已开始；8 被阻塞；9 没有确认开始；4 执行者不存在。
# herdr 以 --until working --until blocked 返回成功，就说明它已经观察到执行者开始（working）或被阻塞（blocked）；
# 此后执行者即使很快做完（读到 idle/done），也算已开始，watcher 必须照常启动（已有 done 或停下会立即回调）。
# 命令失败（stalled、timeout 等）时才靠再读一次状态判断。不重发提示词：失败后是否已送达无法确定，重发可能让任务重复执行。
sw_do_prompt() {
  _pe=$(herdr agent prompt "$EXEC" "$FULL_PROMPT" --wait --until working --until blocked --timeout 15000 2>&1); _prc=$?
  sw_agent_info "$EXEC" || return $?
  case $A_STATE in
    working) return 0 ;;
    blocked)
      if [ "$_prc" -eq 0 ]; then
        sw_err "执行者 ${EXEC} 在收到提示词后进入 blocked（等批准或回答），没有启动 watcher；请先读它的屏幕"
      else
        sw_err "执行者 ${EXEC} 被阻塞（等批准或回答），提示词没有送达，没有启动 watcher：${_pe}"
      fi
      return 8 ;;
  esac
  [ "$_prc" -eq 0 ] && return 0
  sw_err "没有确认执行者 ${EXEC} 开始工作（当前状态 ${A_STATE:-未知}），没有启动 watcher；提示词可能已送达，不要盲目重发，请先读它的屏幕"
  sw_err "herdr 返回：${_pe}"
  return 9
}

# 8.8 dispatch 子命令：init、发提示词并确认开始、run（不带 --require-working）
sw_cmd_dispatch() {
  SW_SUB=dispatch; SW_HINT='dispatch 已确认执行者开始，不需要 --require-working'
  SW_ALLOW='--mode --executor --kind --scheduler --scheduler-kind --d-pane --d-kind --prompt --prompt-file --result-file --callback-on --callback-prompt --marker-dir --env'
  MODE=A
  sw_parse "$@" || return $?
  case $MODE in A | D) ;; *) sw_err "dispatch 的 --mode 只能是 A 或 D"; return 2 ;; esac
  if [ "$PROMPT_TEXT_SET" = 1 ] && [ -n "$PROMPT_FILE" ]; then sw_err "--prompt 和 --prompt-file 只能给一个"; return 2; fi
  if [ "$PROMPT_TEXT_SET" != 1 ] && [ -z "$PROMPT_FILE" ]; then sw_err "需要 --prompt 或 --prompt-file"; return 2; fi
  if [ -n "$PROMPT_FILE" ]; then
    [ -f "$PROMPT_FILE" ] || { sw_err "--prompt-file 不存在：$PROMPT_FILE"; return 2; }
    PROMPT_TEXT=$(cat "$PROMPT_FILE"); PROMPT_FILE=
  fi
  [ -n "$PROMPT_TEXT" ] || { sw_err "提示词不能为空"; return 2; }
  case $PROMPT_TEXT in -*) sw_err "提示词不能以 - 开头（会被 herdr 当成选项）"; return 2 ;; esac
  sw_check_env || return $?
  sw_write_ticket || return $?
  sw_compose_prompt
  printf '%s\n' "$FULL_PROMPT" >"$TASK_DIR/prompt.txt"
  sw_do_prompt; _drc=$?
  if [ "$_drc" -ne 0 ]; then
    printf 'task_id=%s\ntask_dir=%s\n' "$TASK_ID" "$TASK_DIR"
    sw_err "任务目录已保留：${TASK_DIR}"
    return "$_drc"
  fi
  REQUIRE_WORKING=0
  sw_do_run; _drc=$?
  [ "$_drc" -eq 0 ] || sw_err "提示词已送达、执行者正在工作，但 watcher 没有启动；任务目录已保留：${TASK_DIR}（可用 run 重试）"
  return "$_drc"
}

# whoami 子命令：输出调度者（本 agent）的权威 pane 信息；Codex 里环境变量过期时自动发现
sw_cmd_whoami() {
  SW_SUB=whoami; SW_HINT='whoami 只接受 --exclude'
  SW_ALLOW='--exclude'
  sw_parse "$@" || return $?
  [ -z "$EXCLUDE_OPT" ] || sw_check_val --exclude "$SW_RE_PANE" "$EXCLUDE_OPT" || return 2
  sw_check_env || return $?
  [ -n "${HERDR_PANE_ID:-}" ] || { sw_err "HERDR_PANE_ID 为空，无法确定调度者的 pane。"; return 3; }
  _wt=${HERDR_TAB_ID:-}
  [ -n "$_wt" ] || _wt=$(herdr pane get "$HERDR_PANE_ID" 2>/dev/null | json_str tab_id)
  printf 'pane_id=%s\nworkspace_id=%s\ntab_id=%s\nsource=%s\n' "$HERDR_PANE_ID" "$HERDR_WORKSPACE_ID" "$_wt" "$SELF_SOURCE"
}

# 8.10 入口：分发子命令
main() {
  _sub=${1:-}
  [ $# -gt 0 ] && shift
  case $_sub in
    init) sw_cmd_init "$@" ;;
    run) sw_cmd_run "$@" ;;
    watch) sw_cmd_watch "$@" ;;
    dispatch) sw_cmd_dispatch "$@" ;;
    whoami) sw_cmd_whoami "$@" ;;
    *) sw_usage; return 2 ;;
  esac
}

main "$@"
exit $?
