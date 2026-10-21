#!/usr/bin/env bash
# 安装 agent-team 的 `team` CLI（editable：改代码即时生效，无需重装）。可重复执行。
#
# 用法:
#   scripts/install.sh            # 安装/重装 team CLI
#   scripts/install.sh --dev      # 另建开发环境 .venv（pytest/ruff），用于跑测试与静态检查
#   scripts/install.sh --uninstall
#
# 用 uv tool（无 uv 时退回 pipx）为 CLI 建独立环境，`team` 落在 ~/.local/bin：
# 不碰系统 Python（Homebrew Python 是 externally-managed，直接 pip install 会被拒），也不读 pip 镜像配置。
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
pkg="agent-team"
dev=0
uninstall=0
for arg in "$@"; do
  case "${arg}" in
    --dev) dev=1 ;;
    --uninstall) uninstall=1 ;;
    -h|--help) sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "未知参数: ${arg}（见 --help）" >&2; exit 2 ;;
  esac
done

if command -v uv >/dev/null 2>&1; then
  tool=uv
elif command -v pipx >/dev/null 2>&1; then
  tool=pipx
else
  echo "✗ 需要 uv 或 pipx 其一。安装 uv：brew install uv（或见 https://docs.astral.sh/uv/）" >&2
  exit 1
fi

if [[ $uninstall -eq 1 ]]; then
  if [[ ${tool} == uv ]]; then uv tool uninstall "${pkg}"; else pipx uninstall "${pkg}"; fi
  echo "✓ 已卸载 team CLI"
  exit 0
fi

echo "→ 用 ${tool} 安装 team CLI（editable，源码：${root}）"
if [[ ${tool} == uv ]]; then
  uv tool install --editable "${root}" --force
else
  pipx install --editable "${root}" --force
fi

if [[ $dev -eq 1 ]]; then
  if [[ ${tool} != uv ]]; then
    echo "✗ --dev 需要 uv（用 uv run 跑 pytest/ruff）" >&2
    exit 1
  fi
  echo "→ 建开发环境 ${root}/.venv（含 pytest/ruff）"
  (cd "${root}" && uv sync --extra dev)
fi

# 校验：team 必须在 PATH 上且能跑通（不在 PATH 时多半是 ~/.local/bin 没加进 PATH）
if ! command -v team >/dev/null 2>&1; then
  bindir="$HOME/.local/bin"
  echo "⚠ team 已装到 ${bindir}，但它不在 PATH 中。把下面这行加进 shell 配置（如 ~/.zshrc）后重开终端：" >&2
  echo "    export PATH=\"${bindir}:\$PATH\"" >&2
  exit 1
fi
(cd "${root}" && team agent list >/dev/null)
echo "✓ team 安装完成：$(command -v team)"

if ! command -v herdr >/dev/null 2>&1; then
  echo "⚠ 未找到 herdr：worker 靠它拉起，真跑前需安装（macOS：brew install herdr，或见 https://herdr.dev）" >&2
fi
[[ $dev -eq 1 ]] && echo "  开发自检：uv run --extra dev pytest / uv run --extra dev ruff check"
echo "  试试：team --help"
