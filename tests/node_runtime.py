"""Locate the frontend test prerequisite without a developer-specific path."""
import shutil
from pathlib import Path

_node = shutil.which("node")
if _node is None:
    raise RuntimeError("前端测试需要 Node.js；请安装 Node.js 并将 node 放到 PATH。")
NODE = Path(_node)
