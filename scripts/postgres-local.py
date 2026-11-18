#!/usr/bin/env python3
"""Manage this user's localhost PostgreSQL instance without sudo or Docker."""

import argparse
import os
import secrets
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import psycopg
from dotenv import dotenv_values, set_key
from psycopg import sql

REPO = Path(__file__).resolve().parents[1]
LOCAL_ROOT = REPO / ".local" / "postgres"
DATA = LOCAL_ROOT / "data"
SOCKET = LOCAL_ROOT / "socket"
PORT = 5432
ADMIN = "gpu_local_admin"
USER = "gpu_history"


def binaries():
    candidates = [
        *sorted(
            (LOCAL_ROOT / "runtime/usr/lib/postgresql").glob("*/bin"), reverse=True
        ),
        *sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True),
    ]
    found = shutil.which("pg_ctl")
    if found:
        candidates.append(Path(found).parent)
    for candidate in candidates:
        if (candidate / "pg_ctl").is_file():
            if (DATA / "PG_VERSION").exists():
                major = (DATA / "PG_VERSION").read_text().strip()
                version = (
                    subprocess.check_output(
                        [str(candidate / "pg_config"), "--version"], env=environment()
                    ).decode()
                    if (candidate / "pg_config").exists()
                    else candidate.parent.name
                )
                if major not in version:
                    continue
            return candidate
    raise RuntimeError(
        "未找到匹配 PostgreSQL 程序，请安装 PostgreSQL 或恢复 .local/postgres/runtime"
    )


def environment():
    env = os.environ.copy()
    lib = LOCAL_ROOT / "runtime/usr/lib/x86_64-linux-gnu"
    env["LD_LIBRARY_PATH"] = str(lib) + (
        ":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else ""
    )
    return env


def run(program, *args, check=True):
    return subprocess.run(
        [str(binaries() / program), *map(str, args)], env=environment(), check=check
    )


def running():
    if not (DATA / "PG_VERSION").exists():
        return False
    result = subprocess.run(
        [str(binaries() / "pg_ctl"), "-D", str(DATA), "status"],
        env=environment(),
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def start():
    if not (DATA / "PG_VERSION").exists():
        raise RuntimeError("请先运行 init 创建本地实例")
    if not running():
        run("pg_ctl", "-D", DATA, "-l", LOCAL_ROOT / "server.log", "-w", "start")
    print(f"本地 PostgreSQL 已运行：127.0.0.1:{PORT}")


def initialize():
    if os.geteuid() == 0:
        raise RuntimeError("请使用项目所属普通用户运行，不要使用 sudo")
    for path in (LOCAL_ROOT, SOCKET):
        path.mkdir(parents=True, exist_ok=True)
        path.chmod(0o700)
    if not (DATA / "PG_VERSION").exists():
        password_file = LOCAL_ROOT / ".init-password"
        password_file.touch(mode=0o600, exist_ok=False)
        try:
            password_file.write_text(secrets.token_urlsafe(36) + "\n")
            run(
                "initdb",
                "-D",
                DATA,
                "--username=" + ADMIN,
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "--pwfile=" + str(password_file),
                "--no-locale",
                "-E",
                "UTF8",
            )
        finally:
            password_file.unlink(missing_ok=True)
        with (DATA / "postgresql.conf").open("a") as config:
            # SQL statements/parameters must not expose private labels in server logs.
            escaped_socket = str(SOCKET).replace("'", "''")
            config.write(
                f"\nlisten_addresses = '127.0.0.1'\nport = {PORT}\n"
                f"unix_socket_directories = '{escaped_socket}'\nunix_socket_permissions = 0700\n"
                "password_encryption = 'scram-sha-256'\nlog_statement = 'none'\n"
                "log_min_error_statement = 'panic'\nlog_error_verbosity = 'terse'\n"
                "log_parameter_max_length = 0\nlog_parameter_max_length_on_error = 0\n"
            )
    start()
    values = dotenv_values(REPO / ".env")
    mode = values.get("DATA_MODE", "mock")
    database = "gpu_history_live" if mode == "live" else "gpu_history_mock"
    password_file = LOCAL_ROOT / ".application-password"
    if not password_file.exists():
        password_file.touch(mode=0o600, exist_ok=False)
        password_file.write_text(secrets.token_urlsafe(36))
    password = password_file.read_text().strip()
    with psycopg.connect(
        dbname="postgres", user=ADMIN, host=str(SOCKET), port=PORT, autocommit=True
    ) as conn:
        if not conn.execute(
            "SELECT 1 FROM pg_roles WHERE rolname=%s", (USER,)
        ).fetchone():
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                ).format(sql.Identifier(USER), sql.Literal(password))
            )
        if not conn.execute(
            "SELECT 1 FROM pg_database WHERE datname=%s", (database,)
        ).fetchone():
            conn.execute(
                sql.SQL(
                    "CREATE DATABASE {} OWNER {} ENCODING 'UTF8' TEMPLATE template0"
                ).format(sql.Identifier(database), sql.Identifier(USER))
            )
            conn.execute(
                sql.SQL("REVOKE ALL ON DATABASE {} FROM PUBLIC").format(
                    sql.Identifier(database)
                )
            )
    url = f"postgresql://{USER}:{quote(password, safe='')}@127.0.0.1:{PORT}/{database}"
    # Verify authentication before updating project configuration.
    with psycopg.connect(url, connect_timeout=3) as conn:
        conn.execute("SELECT 1")
    env_file = REPO / ".env"
    env_file.touch(mode=0o600, exist_ok=True)
    env_file.chmod(0o600)
    for key, value in {
        "HISTORY_DATABASE_URL": url,
        "HISTORY_SYNC_ENABLED": "true",
        "HISTORY_SYNC_INTERVAL_SECONDS": "60",
        "HISTORY_CHUNK_POINTS": "30",
        "HISTORY_QUERY_FILL_CHUNKS": "2",
    }.items():
        set_key(env_file, key, value, quote_mode="always")
    print(f"已配置 {database}，账号 {USER}；密码保存在本机私有文件，未输出。")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("init", "start", "stop", "status", "psql", "backup")
    )
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize()
        elif args.command == "start":
            start()
        elif args.command == "stop":
            if running():
                run("pg_ctl", "-D", DATA, "-m", "fast", "-w", "stop")
            print("本地 PostgreSQL 已停止，数据保留。")
        elif args.command == "status":
            print("本地 PostgreSQL：" + ("运行中" if running() else "未运行"))
        else:
            values = dotenv_values(REPO / ".env")
            url = values.get("HISTORY_DATABASE_URL")
            if not url:
                raise RuntimeError("尚未配置数据库连接")
            # libpq environment avoids placing credentials on a process command line.
            from psycopg.conninfo import conninfo_to_dict

            params = conninfo_to_dict(url)
            if params.get("host") != "127.0.0.1" or params.get("port") != str(PORT):
                raise RuntimeError("此命令仅管理脚本创建的本机实例")
            env = environment()
            for key, target in (
                ("host", "PGHOST"),
                ("port", "PGPORT"),
                ("dbname", "PGDATABASE"),
                ("user", "PGUSER"),
                ("password", "PGPASSWORD"),
            ):
                env[target] = params[key]
            if args.command == "psql":
                subprocess.run([str(binaries() / "psql"), "-X"], env=env, check=True)
            else:
                backup = (
                    LOCAL_ROOT
                    / "backups"
                    / f"{params['dbname']}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.dump"
                )
                backup.parent.mkdir(mode=0o700, exist_ok=True)
                with backup.open("xb") as output:
                    backup.chmod(0o600)
                    subprocess.run(
                        [str(binaries() / "pg_dump"), "-Fc"],
                        env=env,
                        stdout=output,
                        check=True,
                    )
                print(f"备份已保存：{backup.relative_to(REPO)}")
    except (RuntimeError, OSError, psycopg.Error, subprocess.CalledProcessError):
        parser.exit(
            1,
            "本地 PostgreSQL 操作失败，请检查程序、端口、文件权限及 .local/postgres/server.log。\n",
        )


if __name__ == "__main__":
    main()
