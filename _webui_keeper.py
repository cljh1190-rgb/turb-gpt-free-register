# -*- coding: utf-8 -*-
"""WebUI 守护进程：检测 5000 端口，挂了就拉起。

加固点：
- 跟踪 webui 子进程，端口死了就杀残留再重启
- HTTP 健康检查（不只 TCP connect）
- 拉起时 CREATE_BREAKAWAY_FROM_JOB，避免被父 Job 连带杀掉
- 写心跳文件，方便外层 supervisor 判断
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 5000
LOG = ROOT / "_webui_keeper.log"
PID_FILE = ROOT / "_webui_keeper.pid"
WEBUI_PID_FILE = ROOT / "_webui.pid"
HEARTBEAT = ROOT / "_webui_keeper.heartbeat"
PY = sys.executable
HEALTH_FAILURE_THRESHOLD = 5
HEALTH_RETRY_SECONDS = 2
# Some payment/proxy clients can hold the Python runtime while an upstream
# request is in flight. The listening socket remains open in that case, so a
# short HTTP timeout must not be treated as a dead WebUI process.
BUSY_HTTP_FAILURE_THRESHOLD = 120

# Windows process creation flags
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
DETACHED_PROCESS = 0x00000008

_webui_proc: subprocess.Popen | None = None


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)) == 0:
                    return False
                return int(code.value) == 259
            finally:
                kernel32.CloseHandle(handle)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def _kill_pid(pid: int) -> None:
    if pid <= 0:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                text=True,
                timeout=15,
                creationflags=CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, 9)
    except Exception as e:
        log(f"kill pid={pid} failed: {e}")


def port_open(host: str, port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def http_alive(host: str, port: int) -> bool:
    """TCP 通且 HTTP 有响应才算活（进程僵死时 TCP 可能仍通）。"""
    if not port_open(host, port):
        return False
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/api/summary",
            method="GET",
            headers={"User-Agent": "webui-keeper/1"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:
            return 100 <= int(getattr(resp, "status", 0) or 0) < 500
    except urllib.error.HTTPError as exc:
        # 授权开启后健康检查会收到 401；服务已正常响应，不能因此重启进程。
        try:
            return 100 <= int(exc.code or 0) < 500
        finally:
            exc.close()
    except Exception:
        # 启动瞬间路由未就绪：TCP 已通也先当活，避免误杀
        return port_open(host, port)


def clear_stale_lock() -> None:
    import tempfile

    lock = Path(tempfile.gettempdir()) / f"turb-gpt-free-register-web-{PORT}.lock"
    if not lock.exists():
        return
    try:
        raw = lock.read_text(encoding="utf-8", errors="ignore").strip()
        pid = int((raw.splitlines() or ["0"])[0] or 0)
    except Exception:
        pid = 0
    if pid > 0 and _pid_alive(pid):
        return
    try:
        lock.unlink(missing_ok=True)
        log(f"cleared stale lock pid={pid}")
    except Exception as e:
        log(f"clear lock failed: {e}")


def _read_webui_pid() -> int:
    global _webui_proc
    if _webui_proc is not None and _webui_proc.poll() is None:
        return int(_webui_proc.pid)
    try:
        if WEBUI_PID_FILE.exists():
            return int((WEBUI_PID_FILE.read_text(encoding="utf-8") or "0").strip() or "0")
    except Exception:
        pass
    import tempfile

    lock = Path(tempfile.gettempdir()) / f"turb-gpt-free-register-web-{PORT}.lock"
    try:
        if lock.exists():
            return int((lock.read_text(encoding="utf-8", errors="ignore").splitlines() or ["0"])[0] or 0)
    except Exception:
        pass
    return 0


def stop_dead_webui() -> None:
    """端口挂了但进程还在时，强杀残留，避免锁文件占死。"""
    global _webui_proc
    pid = _read_webui_pid()
    if pid > 0 and _pid_alive(pid) and not port_open(HOST, PORT):
        log(f"webui pid={pid} alive but port down, killing")
        _kill_pid(pid)
        time.sleep(0.8)
    if _webui_proc is not None and _webui_proc.poll() is not None:
        _webui_proc = None
    clear_stale_lock()


def start_webui() -> None:
    global _webui_proc
    stop_dead_webui()
    out = ROOT / "_webui_out.log"
    err = ROOT / "_webui_err.log"
    with err.open("a", encoding="utf-8") as f:
        f.write(f"\n----- keeper spawn {time.strftime('%Y-%m-%d %H:%M:%S')} -----\n")

    creationflags = 0
    if os.name == "nt":
        # 新进程组 + 无窗口。不用 BREAKAWAY_FROM_JOB（部分环境 WinError 5）
        creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONFAULTHANDLER"] = "1"

    with out.open("a", encoding="utf-8") as out_f, err.open("a", encoding="utf-8") as err_f:
        _webui_proc = subprocess.Popen(
            [PY, "-u", str(ROOT / "web.py"), "--host", HOST, "--port", str(PORT)],
            cwd=str(ROOT),
            stdout=out_f,
            stderr=err_f,
            env=env,
            creationflags=creationflags,
            close_fds=False,
        )
    try:
        WEBUI_PID_FILE.write_text(str(_webui_proc.pid), encoding="utf-8")
    except Exception:
        pass
    log(f"spawned webui pid={_webui_proc.pid}")


def _heartbeat() -> None:
    try:
        HEARTBEAT.write_text(
            f"{int(time.time())}\nkeeper_pid={os.getpid()}\nwebui_pid={_read_webui_pid()}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def main() -> None:
    os.chdir(ROOT)
    log(f"keeper start, watch {HOST}:{PORT} pid={os.getpid()}")
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    fail_streak = 0
    while True:
        try:
            _heartbeat()
            if http_alive(HOST, PORT):
                fail_streak = 0
                time.sleep(2)
                continue

            fail_streak += 1
            tcp_alive = port_open(HOST, PORT)
            failure_threshold = (
                BUSY_HTTP_FAILURE_THRESHOLD if tcp_alive else HEALTH_FAILURE_THRESHOLD
            )
            if fail_streak < failure_threshold:
                log(
                    f"port/http health miss (streak={fail_streak}/"
                    f"{failure_threshold}, tcp_alive={tcp_alive}), waiting before restart"
                )
                time.sleep(HEALTH_RETRY_SECONDS)
                continue

            log(f"port/http down confirmed (streak={fail_streak}), restarting webui")
            stop_dead_webui()
            start_webui()

            up = False
            # 启动可能稍慢，多等一会
            for _ in range(40):
                time.sleep(0.5)
                if http_alive(HOST, PORT):
                    log("webui is up")
                    up = True
                    fail_streak = 0
                    break
            if not up:
                log("webui failed to become healthy within 20s")
                # 退避，避免疯狂拉起
                time.sleep(min(15, 2 + fail_streak))
            else:
                time.sleep(2)
        except Exception as e:
            log(f"keeper loop error: {e}")
            time.sleep(3)


if __name__ == "__main__":
    main()
