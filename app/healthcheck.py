"""容器健康检查：供 compose healthcheck 调用，退出码 0 表示健康。"""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8080")
try:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as r:
        sys.exit(0 if r.status == 200 else 1)
except Exception:
    sys.exit(1)
