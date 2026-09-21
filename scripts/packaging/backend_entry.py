"""PyInstaller console entry: GUI server and MCP share one frozen runtime."""
from literature_evidence_mcp.desktop import main

if __name__ == "__main__":
    raise SystemExit(main())
