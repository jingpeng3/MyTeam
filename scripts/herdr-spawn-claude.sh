#!/usr/bin/env bash
# 在 Herdr 里新开一个 pane / tab、拉起 Claude Code 会话，并把请求发给它。
# 既可在 Herdr 内部 pane 运行，也可在外部普通终端运行（只要 Herdr server 在跑）。
# 默认「提交即返回」：被拉起的 Claude 在自己的 pane 里后台继续跑，不阻塞调用方；
# 需要同步等它跑完再返回时加 -f。
#
# 用法:
#   scripts/herdr-spawn-claude.sh <session> "你的请求文本"
#   scripts/herdr-spawn-claude.sh -n mytask -d down -t 180000 team "你的请求文本"
#   scripts/herdr-spawn-claude.sh -W w7 team "在指定 workspace 新开 tab 执行"
#
# 位置参数:
#   <session>   Herdr session 名（必填，如 team；ls ~/.config/herdr/sessions/ 可查）
#   <request>   要执行的请求文本（必填）
#
# 选项:
#   -n NAME     agent 名（默认 claude_<时间戳>，需匹配 [a-z][a-z0-9_-]{0,31} 且唯一）
#   -d DIR      分屏方向 right|down（默认 right；仅内部 pane 分屏时生效）
#   -t MS       prompt 等待超时毫秒（默认 120000）
#   -w          新开一个 tab（独立窗口）而不是分屏
#   -W WSID     指定目标 workspace id（如 w7）；不填则用 focused 的那个
#   -f          前台/跟随：阻塞等待被拉起的 agent 跑完（默认不等，提交即返回）
#   -t MS       -f 跟随时的等待超时毫秒（默认 120000）
set -euo pipefail

name=""
direction="right"
timeout_ms=120000
new_tab=0
ws_override=""
follow=0

while getopts "n:d:t:wW:f" opt; do
  case "$opt" in
    n) name="$OPTARG" ;;
    d) direction="$OPTARG" ;;
    t) timeout_ms="$OPTARG" ;;
    w) new_tab=1 ;;
    W) ws_override="$OPTARG" ;;
    f) follow=1 ;;
    *) echo "用法: $0 [-n NAME] [-d right|down] [-t MS] [-w] [-W WSID] [-f] <session> \"请求文本\"" >&2; exit 2 ;;
  esac
done
shift $((OPTIND - 1))

session="${1:-}"
request="${2:-}"
[[ -n "$session" ]] || { echo "错误: 缺少 session 名（第一个参数，如 team）" >&2; exit 2; }
[[ -n "$request" ]] || { echo "错误: 缺少请求文本（第二个参数）" >&2; exit 2; }
command -v jq >/dev/null || { echo "错误: 需要 jq" >&2; exit 1; }

# 统一的 herdr 调用入口：始终带上 --session
h() { herdr --session "$session" "$@"; }

# 校验 Herdr server 是否在跑（这样外部终端也能用）
# 注意：先把输出抓到变量再 grep，避免 `... | grep -q` 在 pipefail 下因 grep 提前关管道
# 给 herdr 发 SIGPIPE（退出 141）、被 pipefail 取成非零而误判为“未运行”。
server_status="$(h status server 2>/dev/null || true)"
if ! grep -q 'status: running' <<<"$server_status"; then
  echo "错误: session '$session' 的 Herdr server 未运行或连不到（ls ~/.config/herdr/sessions/ 查可用 session）" >&2
  exit 1
fi

in_pane=0
[[ "${HERDR_ENV:-}" == "1" && -n "${HERDR_PANE_ID:-}" ]] && in_pane=1

[[ -n "$name" ]] || name="claude_$(date +%s)"

# 解析目标 workspace：优先 -W，其次注入的，最后取 focused 的
resolve_ws() {
  if [[ -n "$ws_override" ]]; then echo "$ws_override"; return; fi
  if [[ -n "${HERDR_WORKSPACE_ID:-}" ]]; then echo "$HERDR_WORKSPACE_ID"; return; fi
  h workspace list | jq -r '.result.workspaces | (map(select(.focused)) + .)[0].workspace_id'
}

# 1) 开 pane
if [[ "$in_pane" == "1" && "$new_tab" == "0" ]]; then
  # 内部：当前 tab 分屏
  pane_id=$(h pane split --current --direction "$direction" --cwd "$PWD" --no-focus \
            | jq -r '.result.pane.pane_id')
else
  # 外部终端，或 -w：在目标 workspace 新开 tab（不需要 --current）
  ws=$(resolve_ws)
  [[ -n "$ws" && "$ws" != "null" ]] || { echo "错误: 未能确定目标 workspace（用 -W 指定）" >&2; exit 1; }
  pane_id=$(h tab create --workspace "$ws" | jq -r '.result.root_pane.pane_id')
fi
[[ -n "$pane_id" && "$pane_id" != "null" ]] || { echo "错误: 未能解析新 pane 的 pane_id" >&2; exit 1; }
echo "新 pane: $pane_id"

# 2) 在该 pane 起 Claude；起完等它进入可输入状态
#    --dangerously-skip-permissions 作为 native agent 参数，放在 -- 之后透传给 claude
h agent start "$name" --kind claude --pane "$pane_id" -- --dangerously-skip-permissions
h agent wait "$name" --timeout 30000 || true
echo "已启动 agent: $name"

# 3) 发请求。默认「提交即返回」——不因被拉起的 agent 跑长任务而阻塞调用方。
#    只有 -f 跟随模式才 --wait 阻塞到它进入 idle/done/blocked（最多 timeout 毫秒）。
if [[ "$follow" == "1" ]]; then
  h agent prompt "$name" "$request" --wait --timeout "$timeout_ms"
  echo "已提交请求并等待完成。查看结果:"
else
  h agent prompt "$name" "$request"
  echo "已提交请求（不等待，agent 在后台继续跑）。查看进度:"
fi
echo "  herdr --session $session agent read $name --source recent-unwrapped --lines 120"
