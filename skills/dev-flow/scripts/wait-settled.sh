#!/bin/sh
# Wait until a Herdr agent reaches a *stable* idle/done/blocked state.
# Herdr's screen-based detection can briefly report idle/done while an agent
# is still mid-turn (seen with Codex). After each settled state, this script
# checks whether the agent returns to working within the settle window and
# keeps waiting if it does. It prints nothing while waiting.
#
# usage: wait-settled.sh <target> [timeout_ms=7200000] [settle_ms=15000]
# exit:  0 stable state reached, 124 overall timeout, 1 herdr error
# output: one line "status=<state>" (or the herdr error on failure)

target=$1
timeout_ms=${2:-7200000}
settle_ms=${3:-15000}
[ -n "$target" ] || { echo "usage: $0 <target> [timeout_ms] [settle_ms]" >&2; exit 2; }

start=$(date +%s)
status() {
  herdr agent get "$target" 2>&1 | grep -o '"agent_status":"[a-z_]*"' | sed 's/"agent_status":"\(.*\)"/status=\1/' \
    || herdr agent get "$target" 2>&1
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

  # Went back to working within the settle window: the settle was spurious.
  if herdr agent wait "$target" --until working --timeout "$settle_ms" >/dev/null 2>&1; then
    continue
  fi
  status
  exit 0
done
