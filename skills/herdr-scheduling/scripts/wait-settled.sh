#!/bin/sh
# 等待 herdr agent 稳定地进入 idle / done / blocked（或 unknown）状态。
#
# 背景：herdr 靠屏幕判断状态，agent 仍在一轮对话中途时可能短暂显示 idle / done（Codex 上出现过）。
# 本脚本在每次进入非 working 状态后，再观察一段「稳定窗口」：窗口内重新回到 working 就认为是误报，
# 继续等待。等待期间不输出任何内容。
#
# 用法：wait-settled.sh <target> [timeout_ms=7200000] [settle_ms=15000]
# 退出码：0 已稳定；124 总超时；1 herdr 报错；2 参数错误
# 输出：一行 status=<状态>（失败时输出 herdr 的错误原文）

target=$1
timeout_ms=${2:-7200000}
settle_ms=${3:-15000}
[ -n "$target" ] || { echo "usage: $0 <target> [timeout_ms] [settle_ms]" >&2; exit 2; }

start=$(date +%s)

status() {
  out=$(herdr agent get "$target" 2>&1) || { echo "$out"; return 1; }
  printf '%s\n' "$out" | grep -o '"agent_status":"[a-z_]*"' | head -1 | sed 's/"agent_status":"\(.*\)"/status=\1/'
}

while :; do
  remaining=$(( timeout_ms - ( $(date +%s) - start ) * 1000 ))
  if [ "$remaining" -le 0 ]; then status; exit 124; fi

  if ! err=$(herdr agent wait "$target" --timeout "$remaining" 2>&1 >/dev/null); then
    case $err in
      *timeout*) status; exit 124 ;;
      *) echo "$err"; exit 1 ;;
    esac
  fi

  # 稳定窗口内又回到 working：这次 idle / done 是误报，继续等。
  if herdr agent wait "$target" --until working --timeout "$settle_ms" >/dev/null 2>&1; then
    continue
  fi
  status || exit 1
  exit 0
done
