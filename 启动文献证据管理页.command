#!/bin/zsh

set -u

# Do not let an activated environment or ambient Python paths change which
# bootstrap, package, or dependencies this launcher imports.
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE VIRTUAL_ENV __PYVENV_LAUNCHER__

SCRIPT_DIR=${0:A:h}
BOOTSTRAP="$SCRIPT_DIR/scripts/macos_launcher.py"
PYTHON_OVERRIDE=${LITERATURE_EVIDENCE_PYTHON:-}

if [[ ! -f "$BOOTSTRAP" ]]; then
  print -u2 "错误：入口旁边缺少完整项目文件。请保留整个项目目录，不要只移动 .command 文件。"
  exit 2
fi

if [[ -n "$PYTHON_OVERRIDE" ]]; then
  if [[ ! -f "$PYTHON_OVERRIDE" || ! -x "$PYTHON_OVERRIDE" ]]; then
    print -u2 "错误：LITERATURE_EVIDENCE_PYTHON 不是可执行的 Python 路径。"
    exit 2
  fi
  exec "$PYTHON_OVERRIDE" -I -B "$BOOTSTRAP" "$SCRIPT_DIR" "$@"
fi

typeset -a CANDIDATES
CANDIDATES=(
  "$HOME/.local/bin/python3.12"
  "$HOME/.local/bin/python3.14"
  "$HOME/.local/bin/python3.13"
  "$HOME/.local/bin/python3.11"
  "$HOME/.local/bin/python3"
  "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
  "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
  "/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
  "/Library/Frameworks/Python.framework/Versions/3.12/bin/python3"
  "/Library/Frameworks/Python.framework/Versions/3.11/bin/python3"
  "/opt/homebrew/bin/python3"
  "/usr/local/bin/python3"
  "python3.14"
  "python3.13"
  "python3.12"
  "python3.11"
  "python3"
)

typeset FIRST_PYTHON=""
typeset RESOLVED=""
for CANDIDATE in "${CANDIDATES[@]}"; do
  if [[ "$CANDIDATE" == */* ]]; then
    RESOLVED="$CANDIDATE"
  else
    RESOLVED=$(command -v "$CANDIDATE" 2>/dev/null || true)
  fi
  [[ -n "$RESOLVED" && -x "$RESOLVED" ]] || continue
  [[ -n "$FIRST_PYTHON" ]] || FIRST_PYTHON="$RESOLVED"
  if "$RESOLVED" -I -B "$BOOTSTRAP" --check-runtime >/dev/null 2>&1; then
    exec "$RESOLVED" -I -B "$BOOTSTRAP" "$SCRIPT_DIR" "$@"
  fi
done

if [[ -n "$FIRST_PYTHON" ]]; then
  exec "$FIRST_PYTHON" -I -B "$BOOTSTRAP" --check-runtime
fi

print -u2 "错误：未找到 Python 3.11 或更新版本。"
print -u2 "请从 https://www.python.org/downloads/macos/ 获取 macOS 安装包，安装后重新双击。"
exit 2
