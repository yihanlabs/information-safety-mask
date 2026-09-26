from __future__ import annotations

import argparse
import secrets
import socket
import threading
import webbrowser

from .inference import configure_environment
from .network import install_local_only_guard


def main():
    parser = argparse.ArgumentParser(description="本地图片脱敏工作台")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    configure_environment()
    install_local_only_guard()
    import uvicorn

    from .api import create_app

    token = secrets.token_urlsafe(32)
    listener = socket.socket()
    listener.bind(("127.0.0.1", args.port))
    listener.listen(128)
    port = listener.getsockname()[1]
    app = create_app(token)
    if not args.no_browser:
        threading.Timer(1, lambda: webbrowser.open(f"http://127.0.0.1:{port}/#token={token}")).start()
    print(f"本地图片脱敏已启动：http://127.0.0.1:{port}")
    print("图片只在本机处理。关闭此窗口或按 Ctrl+C 结束会话并释放图片。")
    config = uvicorn.Config(app, host="127.0.0.1", port=port, access_log=False, log_level="critical")
    uvicorn.Server(config).run(sockets=[listener])


if __name__ == "__main__":
    main()
