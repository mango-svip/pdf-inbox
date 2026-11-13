#!/usr/bin/env python3
"""极简 TCP 转发代理：打印双向字节流，用于诊断 CUPS <-> IPP 服务交互。"""
import socket
import sys
import threading

LISTEN = ("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 8641)
TARGET = ("127.0.0.1", int(sys.argv[2]) if len(sys.argv) > 2 else 8631)


def pipe(src, dst, tag):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            head = data[:160]
            print(f"{tag} {len(data)}B {head!r}", flush=True)
            dst.sendall(data)
    except Exception as exc:
        print(f"{tag} ERR {exc!r}", flush=True)
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except OSError:
            pass


def main():
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(LISTEN)
    srv.listen(8)
    print(f"proxy {LISTEN} -> {TARGET}", flush=True)
    while True:
        client, _ = srv.accept()
        up = socket.create_connection(TARGET)
        threading.Thread(target=pipe, args=(client, up, ">> CUPS"), daemon=True).start()
        threading.Thread(target=pipe, args=(up, client, "<< SRV "), daemon=True).start()


if __name__ == "__main__":
    main()
