"""Prevent accidental outbound connections in the processing application.

The model preparation script runs separately and does not install this guard.
"""

import ipaddress
import socket

_installed = False


def _local(host):
    if host is None or host == "localhost":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", errors="replace")
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def install_local_only_guard():
    global _installed
    if _installed:
        return
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def check(address):
        if isinstance(address, tuple) and not _local(address[0]):
            raise OSError("本地处理模式禁止外部网络连接")

    def connect(sock, address):
        check(address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if not _local(host):
            raise OSError("本地处理模式禁止外部域名查询")
        return original_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
    _installed = True
