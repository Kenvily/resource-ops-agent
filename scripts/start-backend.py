#!/usr/bin/env python3
"""Development entrypoint: start local PostgreSQL, migrate schema, then exec the backend."""

import argparse
import asyncio
import errno
import os
import socket
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=17979)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("port 必须为 1–65535")

    try:
        socket.inet_pton(socket.AF_INET, args.host)
    except OSError:
        parser.error("host 必须为有效的 IPv4 地址")

    os.chdir(REPO)
    stage = "端口检查"

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((args.host, args.port))

        from app.core.config import Settings
        from app.history.store import HistoryStore
        from psycopg.conninfo import conninfo_to_dict

        stage = "配置校验"
        settings = Settings()
        url = settings.history_database_url.get_secret_value()

        if url:
            stage = "数据库启动与结构校验"
            params = conninfo_to_dict(url)

            if (
                params.get("host") in ("127.0.0.1", "localhost")
                and params.get("port", "5432") == "5432"
                and (REPO / ".local/postgres/data/PG_VERSION").exists()
            ):
                subprocess.run(
                    [sys.executable, "scripts/postgres-local.py", "start"],
                    check=True,
                )

            async def prepare():
                store = HistoryStore(settings)
                try:
                    await store.open()
                    await store.initialize()
                finally:
                    await store.close()

            asyncio.run(prepare())
            print("数据库已就绪，结构与数据契约校验通过。", flush=True)

        stage = "后端进程启动"
        os.execv(
            sys.executable,
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--app-dir",
                "backend",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--no-access-log",
                "--no-proxy-headers",
                *(["--reload", "--reload-dir", "backend"] if args.reload else []),
            ],
        )

    except KeyboardInterrupt:
        return

    except OSError as error:
        if stage == "端口检查" and error.errno == errno.EADDRINUSE:
            parser.exit(
                1,
                f"端口 {args.host}:{args.port} 已被占用，未重复启动后端。\n"
                f"查看占用进程：ss -ltnp 'sport = :{args.port}'\n",
            )

        parser.exit(
            1,
            f"调试启动失败（{stage}，系统错误码 {error.errno}）；未启动后端。\n",
        )

    except Exception:  # noqa: BLE001 - do not expose connection strings or credentials
        parser.exit(
            1,
            f"调试启动失败（{stage}），请按 docs/OPERATIONS.md 排查；未启动后端。\n",
        )


if __name__ == "__main__":
    main()
