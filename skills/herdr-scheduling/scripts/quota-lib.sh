#!/bin/sh
# 额度识别与时间解析的纯函数（POSIX sh）。
# 只做文本处理：不调用 herdr，不 sleep，便于单独 source 测试。
# 用法：. quota-lib.sh 之后调用 qp_* 函数。文案和格式的来源见 references/quota-patterns.md。
#
# 约定：
#   - 需要「当前时间」的函数都显式接收 now（Unix 秒），保证可复现；qp_now 是取当前时间的唯一入口。
#   - 时间一律按本地时区解释；文案里带 (<IANA 时区>) 时按该时区解释；时区名无法识别则放弃（输出空）。

# 额度文案的识别模式（不区分大小写，ERE）。
QP_UNTIMED_RE='out of credits|spend cap|quota exceeded|rate limit exceeded'
QP_CODEX_LIMIT_RE='hit your usage limit'
QP_CLAUDE_LIMIT_RE='hit your [a-z ]*limit|usage limit reached'

# 当前时间（Unix 秒）。测试时可用 QP_NOW_FILE 指向一个存有秒数的文件来固定时间。
qp_now() {
  if [ -n "${QP_NOW_FILE:-}" ] && [ -f "$QP_NOW_FILE" ]; then
    cat "$QP_NOW_FILE"
  else
    date +%s
  fi
}

# 去掉数字前导 0，避免 shell 算术把 09 当成八进制。
qp_int() {
  _qi=$(printf '%s' "$1" | sed 's/^0*//')
  printf '%s' "${_qi:-0}"
}

# 8.1 取 stdin 最后 N 个非空行（默认 15）。
qp_tail() {
  grep -v '^[[:space:]]*$' | tail -n "${1:-15}"
}

# 月份英文缩写 → 数字（不区分大小写），无法识别输出空。
qp_month_num() {
  _qm=$(printf '%s' "$1" | cut -c1-3 | tr 'A-Z' 'a-z')
  case $_qm in
    jan) echo 1 ;; feb) echo 2 ;; mar) echo 3 ;; apr) echo 4 ;;
    may) echo 5 ;; jun) echo 6 ;; jul) echo 7 ;; aug) echo 8 ;;
    sep) echo 9 ;; oct) echo 10 ;; nov) echo 11 ;; dec) echo 12 ;;
    *) echo "" ;;
  esac
}

# 12 小时制转 24 小时制：qp_h24 <时> <am|pm>，输出两位数。
qp_h24() {
  _qh=$(qp_int "$1")
  case $2 in
    [Aa]*) [ "$_qh" -eq 12 ] && _qh=0 ;;
    [Pp]*) [ "$_qh" -ne 12 ] && _qh=$((_qh + 12)) ;;
  esac
  printf '%02d' "$_qh"
}

# 校验 IANA 时区名：形如 Asia/Shanghai，且系统 zoneinfo 里存在。
qp_zone_ok() {
  case $1 in
    '' | *[!A-Za-z0-9_+/-]*) return 1 ;;
  esac
  [ -f "/usr/share/zoneinfo/$1" ]
}

# 把 Unix 秒格式化：qp_fmt_epoch <秒> <date 格式，不含 +> [时区]。兼容 BSD（-r）和 GNU（-d @）。
qp_fmt_epoch() {
  if [ -n "${3:-}" ]; then
    TZ=$3 date -r "$1" "+$2" 2>/dev/null || TZ=$3 date -d "@$1" "+$2" 2>/dev/null
  else
    date -r "$1" "+$2" 2>/dev/null || date -d "@$1" "+$2" 2>/dev/null
  fi
}

# 8.4 把 "YYYY-MM-DD HH:MM" 转 Unix 秒：qp_to_epoch <时间> [时区]。兼容 BSD（-j -f）和 GNU（-d）。
qp_to_epoch() {
  _qs="$1:00"
  if [ -n "${2:-}" ]; then
    TZ=$2 date -j -f '%Y-%m-%d %H:%M:%S' "$_qs" +%s 2>/dev/null || TZ=$2 date -d "$_qs" +%s 2>/dev/null
  else
    date -j -f '%Y-%m-%d %H:%M:%S' "$_qs" +%s 2>/dev/null || date -d "$_qs" +%s 2>/dev/null
  fi
}

# 8.6 时长 → 秒：支持 2d15h、2h05m、40m；其他格式输出空并返回 1。
qp_dur_to_seconds() {
  printf '%s' "$1" | grep -Eq '^([0-9]+d)?([0-9]+h)?([0-9]+m)?$' || return 1
  [ -n "$1" ] || return 1
  _qd=$(printf '%s' "$1" | sed -E -n 's/^([0-9]+)d.*/\1/p')
  _qhh=$(printf '%s' "$1" | sed -E -n 's/^([0-9]+d)?([0-9]+)h.*/\2/p')
  _qmm=$(printf '%s' "$1" | sed -E -n 's/^([0-9]+d)?([0-9]+h)?([0-9]+)m$/\3/p')
  echo $(( $(qp_int "${_qd:-0}") * 86400 + $(qp_int "${_qhh:-0}") * 3600 + $(qp_int "${_qmm:-0}") * 60 ))
}

# 取出触发词（Try again at / resets / continuing automatically at 等）之后的文本。
qp_after_trigger() {
  awk '{ if (match($0, /(Try again at|[Rr]esets? at|[Rr]esets|continuing automatically at)/)) print substr($0, RSTART + RLENGTH) }'
}

# 8.3 从额度文案解析恢复时间：qp_parse_reset <now>，文案走 stdin，输出 Unix 秒；解析不出输出空。
#   当天时刻：3:45 PM / 3:45pm / 3pm / 20:44（早于或等于 now 视为次日）
#   带日期：Jan 15th, 2025 3:45 PM / Oct 6 at 12pm / 07:04 on 10 Oct（无年份取当前年；已过去不到 7 天视为已过）
#   不可解析：星期形式（Mon 12:00am）、Try again later.、无法识别的时区
qp_parse_reset() {
  _qnow=$1
  _qseg=$(tr '\n' ' ' | qp_after_trigger | head -1)
  [ -n "$_qseg" ] || return 0
  # 星期形式不猜测
  if printf '%s' "$_qseg" | grep -Eqi '^[^A-Za-z0-9]*(mon|tue|wed|thu|fri|sat|sun)[a-z]*([ ,]|$)'; then
    return 0
  fi
  # 时区后缀
  _qzone=$(printf '%s' "$_qseg" | sed -E -n 's/.*\(([A-Za-z_+-]+(\/[A-Za-z0-9_+-]+)+)\).*/\1/p')
  if [ -n "$_qzone" ] && ! qp_zone_ok "$_qzone"; then
    return 0
  fi
  _qs=$(printf '%s' "$_qseg" | sed -E 's/\([^)]*\)//g')

  # A：Jan 15th, 2025 3:45 PM
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^A-Za-z0-9]*([A-Za-z]{3})[a-z]* +([0-9]{1,2})(st|nd|rd|th)?, *([0-9]{4}) +([0-9]{1,2}):([0-9]{2}) *([AaPp][Mm]).*/\1 \2 \4 \5 \6 \7/p')
  if [ -n "$_qm" ]; then
    set -- $_qm
    _qmon=$(qp_month_num "$1"); [ -n "$_qmon" ] || return 0
    qp_to_epoch "$3-$(printf '%02d' "$_qmon")-$(printf '%02d' "$(qp_int "$2")") $(qp_h24 "$4" "$6"):$5" "$_qzone"
    return 0
  fi

  # B：Oct 6 at 12pm 或 Oct 6 at 12:30pm（无年份）
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^A-Za-z0-9]*([A-Za-z]{3})[a-z]* +([0-9]{1,2}) +at +([0-9]{1,2}):([0-9]{2}) *([AaPp][Mm]).*/\1 \2 \3 \4 \5/p')
  if [ -z "$_qm" ]; then
    _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^A-Za-z0-9]*([A-Za-z]{3})[a-z]* +([0-9]{1,2}) +at +([0-9]{1,2}) *([AaPp][Mm]).*/\1 \2 \3 00 \4/p')
  fi
  if [ -n "$_qm" ]; then
    set -- $_qm
    _qmon=$(qp_month_num "$1"); [ -n "$_qmon" ] || return 0
    qp__dated "$_qnow" "$_qmon" "$2" "$(qp_h24 "$3" "$5")" "$4" "$_qzone"
    return 0
  fi

  # C：07:04 on 10 Oct（24 小时制）
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^0-9]*([0-9]{1,2}):([0-9]{2}) +on +([0-9]{1,2}) +([A-Za-z]{3}).*/\1 \2 \3 \4/p')
  if [ -n "$_qm" ]; then
    set -- $_qm
    _qmon=$(qp_month_num "$4"); [ -n "$_qmon" ] || return 0
    qp__dated "$_qnow" "$_qmon" "$3" "$(printf '%02d' "$(qp_int "$1")")" "$2" "$_qzone"
    return 0
  fi

  # D：3:45 PM / 3:45pm；E：3pm；F：20:44
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^0-9]*([0-9]{1,2}):([0-9]{2}) *([AaPp][Mm]).*/\1 \2 \3/p')
  if [ -n "$_qm" ]; then
    set -- $_qm
    qp__clock "$_qnow" "$(qp_h24 "$1" "$3")" "$2" "$_qzone"
    return 0
  fi
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^0-9]*([0-9]{1,2}) *([AaPp][Mm]).*/\1 00 \2/p')
  if [ -n "$_qm" ]; then
    set -- $_qm
    qp__clock "$_qnow" "$(qp_h24 "$1" "$3")" "$2" "$_qzone"
    return 0
  fi
  _qm=$(printf '%s' "$_qs" | sed -E -n 's/^[^0-9]*([0-9]{1,2}):([0-9]{2}).*/\1 \2/p')
  if [ -n "$_qm" ]; then
    set -- $_qm
    qp__clock "$_qnow" "$(printf '%02d' "$(qp_int "$1")")" "$2" "$_qzone"
    return 0
  fi
  return 0
}

# 只有时刻：取今天（在该时区），早于或等于 now 则取次日。qp__clock <now> <HH> <MM> [时区]
qp__clock() {
  _qday=$(qp_fmt_epoch "$1" %Y-%m-%d "$4")
  [ -n "$_qday" ] || return 0
  _qe=$(qp_to_epoch "$_qday $2:$3" "$4")
  [ -n "$_qe" ] || return 0
  if [ "$_qe" -le "$1" ]; then
    _qday=$(qp_fmt_epoch $(($1 + 86400)) %Y-%m-%d "$4")
    _qe=$(qp_to_epoch "$_qday $2:$3" "$4")
  fi
  printf '%s\n' "$_qe"
}

# 有月日无年份：取当前年；已过去不到 7 天视为已过（原样返回，调用方会立即恢复），否则取次年。
# qp__dated <now> <月> <日> <HH> <MM> [时区]
qp__dated() {
  _qyr=$(qp_fmt_epoch "$1" %Y "$6")
  [ -n "$_qyr" ] || return 0
  _qe=$(qp_to_epoch "$_qyr-$(printf '%02d' "$2")-$(printf '%02d' "$(qp_int "$3")") $4:$5" "$6")
  [ -n "$_qe" ] || return 0
  if [ "$_qe" -le "$1" ] && [ $(($1 - _qe)) -ge 604800 ]; then
    _qe=$(qp_to_epoch "$((_qyr + 1))-$(printf '%02d' "$2")-$(printf '%02d' "$(qp_int "$3")") $4:$5" "$6")
  fi
  printf '%s\n' "$_qe"
}

# 8.5 从屏幕底部文本（状态栏）推算恢复时间：qp_parse_statusline <now>。
#   识别 "5h 87% (40m)"、"week 12% (2d15h)"，取已用 >= 100% 的窗口，两个都满取较晚的；其余输出空。
qp_parse_statusline() {
  _qnow=$1
  _qtxt=$(cat)
  _qbest=
  for _qlabel in 5h week; do
    _qm=$(printf '%s\n' "$_qtxt" | grep -Eo "(^|[^A-Za-z0-9])$_qlabel [0-9]{1,3}% \([0-9dhm]+\)" | head -1)
    [ -n "$_qm" ] || continue
    _qpair=$(printf '%s' "$_qm" | sed -E 's/^.*(5h|week) ([0-9]{1,3})% \(([0-9dhm]+)\).*$/\2 \3/')
    set -- $_qpair
    [ "$(qp_int "$1")" -ge 100 ] || continue
    _qsec=$(qp_dur_to_seconds "$2") || continue
    _qe=$((_qnow + _qsec))
    if [ -z "$_qbest" ] || [ "$_qe" -gt "$_qbest" ]; then _qbest=$_qe; fi
  done
  [ -z "$_qbest" ] || printf '%s\n' "$_qbest"
}

# 8.15 解析用量命令输出：qp_usage_reset <claude|codex> <now>，输出走 stdin。
#   claude：Current session / Current week 各一段，"NN% used ... resets ..."，已用 >= 100% 的窗口有效。
#   codex ：5h limit: / Weekly limit: 各一段（含按模型的额外限额），"NN% left (resets ...)"，left 为 0 的窗口有效。
#   先把输出合并成一行再按窗口切段，因此 Codex 把 (resets ...) 折到下一行也能解析。多个窗口都满取较晚的。
qp_usage_reset() {
  _qkind=$1; _qnow=$2
  case $_qkind in
    claude) _qsplit='Current session|Current week' ;;
    codex)  _qsplit='5h limit:|Weekly limit:' ;;
    *) return 0 ;;
  esac
  _qsegs=$(tr '\n' ' ' | sed -E 's/[[:space:]]+/ /g' | awk -v re="$_qsplit" '{ gsub(re, "\n&"); print }')
  _qbest=
  _qold=$IFS
  IFS='
'
  for _qline in $_qsegs; do
    IFS=$_qold
    case $_qkind in
      claude)
        _qpct=$(printf '%s' "$_qline" | sed -E -n 's/.* ([0-9]{1,3})% used.*/\1/p')
        [ -n "$_qpct" ] && [ "$(qp_int "$_qpct")" -ge 100 ] || { IFS='
'; continue; }
        ;;
      codex)
        _qpct=$(printf '%s' "$_qline" | sed -E -n 's/.* ([0-9]{1,3})% left.*/\1/p')
        [ -n "$_qpct" ] && [ "$(qp_int "$_qpct")" -eq 0 ] || { IFS='
'; continue; }
        ;;
    esac
    _qe=$(printf '%s\n' "$_qline" | qp_parse_reset "$_qnow")
    if [ -n "$_qe" ] && { [ -z "$_qbest" ] || [ "$_qe" -gt "$_qbest" ]; }; then _qbest=$_qe; fi
    IFS='
'
  done
  IFS=$_qold
  [ -z "$_qbest" ] || printf '%s\n' "$_qbest"
}

# 8.2 对屏幕末尾文本分类：qp_classify <claude|codex>，文本走 stdin。
#   输出：NONE | LIMIT_UNTIMED | CLAUDE_AUTO | LIMIT_TIMED | LIMIT_NO_TIME
#   优先级：不限时类 > Claude 自动续跑 > 通用额度文案（按能否解析出时间区分）。
qp_classify() {
  _qkind=$1
  case $_qkind in
    claude | codex) ;;
    *) echo NONE; return 0 ;;
  esac
  _qtxt=$(cat)
  if printf '%s\n' "$_qtxt" | grep -Eqi "$QP_UNTIMED_RE"; then echo LIMIT_UNTIMED; return 0; fi
  if [ "$_qkind" = claude ] && printf '%s\n' "$_qtxt" | grep -Eqi 'continuing automatically at'; then
    echo CLAUDE_AUTO; return 0
  fi
  case $_qkind in
    claude) _qre=$QP_CLAUDE_LIMIT_RE ;;
    codex)  _qre=$QP_CODEX_LIMIT_RE ;;
  esac
  if printf '%s\n' "$_qtxt" | grep -Eqi "$_qre"; then
    if [ -n "$(printf '%s\n' "$_qtxt" | qp_parse_reset "$(qp_now)")" ]; then
      echo LIMIT_TIMED
    else
      echo LIMIT_NO_TIME
    fi
  else
    echo NONE
  fi
}
