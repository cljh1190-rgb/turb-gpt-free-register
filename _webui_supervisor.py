# -*- coding: utf-8 -*-
"""外层 Supervisor：保证 keeper 本身活着。

WebUI 进程常因 Cloak/浏览器原生崩溃被拖死；keeper 有时也会一起消失。
本进程极简、无业务依赖，只负责：
1) 端口 5000 不通 → 确保 keeper 在跑
2) keeper 心跳超时 / pid 死 → 重启 keeper
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST = "127.0.0.1"
PORT = 5000
LOG = ROOT / "_webui_supervisor.log"
PID_FILE = ROOT / "_webui_supervisor.pid"
KEEPER = ROOT / "_webui_keeper.py"
KEEPER_PID = ROOT / "_webui_keeper.pid"
HEARTBEAT = ROOT / "_webui_keeper.heartbeat"
PY = sys.executable

CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def log(msg: str) -> None:
    line = time.strftime("%Y-%m-%d %H:%M:%S") + " " + msg
    print(line, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def port_open() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect((HOST, PORT))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def pid_alive(pid: int) -> bool:
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


def read_pid(path: Path) -> int:
    try:
        return int((path.read_text(encoding="utf-8") or "0").strip().splitlines()[0] or 0)
    except Exception:
        return 0


def keeper_healthy() -> bool:
    pid = read_pid(KEEPER_PID)
    if not pid_alive(pid):
        return False
    try:
        if not HEARTBEAT.exists():
            return True  # 刚启动
        raw = HEARTBEAT.read_text(encoding="utf-8", errors="ignore").splitlines()
        ts = int((raw[0] if raw else "0") or 0)
        # 心跳超过 30s 未更新视为僵死
        if ts > 0 and (time.time() - ts) > 30:
            return False
    except Exception:
        pass
    return True


def start_keeper() -> None:
    # 不使用 CREATE_BREAKAWAY_FROM_JOB：部分环境会 WinError 5 拒绝访问
    creationflags = 0
    if os.name == "nt":
        creationflags = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    out = ROOT / "_webui_keeper_stdout.log"
    err = ROOT / "_webui_keeper_stderr.log"
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    with out.open("a", encoding="utf-8") as out_f, err.open("a", encoding="utf-8") as err_f:
        proc = subprocess.Popen(
            [PY, "-u", str(KEEPER)],
            cwd=str(ROOT),
            stdout=out_f,
            stderr=err_f,
            env=env,
            creationflags=creationflags,
            close_fds=False,
        )
    log(f"spawned keeper pid={proc.pid}")


def main() -> None:
    os.chdir(ROOT)
    # 单实例：已有 supervisor 则退出
    old = read_pid(PID_FILE)
    if old and pid_alive(old) and old != os.getpid():
        # 每分钟兜底的计划任务会再次启动本脚本；已有健康 supervisor 时安静退出，
        # 避免重复实例相互重启 keeper。
        return
    # PID 文件可能残留，当前进程是唯一实例时覆盖它。
    if old and old != os.getpid():
        log(f"replacing stale supervisor pid={old}")
        try:
            PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    # 每分钟只启动一次的计划任务若遇到 supervisor 进程被异常回收，下一轮需要能接管。
    # 这里不依赖父 PowerShell/CLI 的生命周期。

    log(f"supervisor start pid={os.getpid()}")
    try:
        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception:
        pass

    while True:
        try:
            kh = keeper_healthy()
            po = port_open()
            if not kh:
                log(f"keeper unhealthy (port_open={po}), restarting keeper")
                # 不主动杀旧 keeper（可能已死）；直接再拉一个
                # 若旧的其实还活着只是心跳文件坏了，keeper 内部单端口逻辑可共存检查
                old_k = read_pid(KEEPER_PID)
                if old_k and pid_alive(old_k):
                    try:
                        if os.name == "nt":
                            subprocess.run(
                                ["taskkill", "/F", "/T", "/PID", str(old_k)],
                                capture_output=True,
                                timeout=15,
                                creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0,
                            )
                            log(f"killed stale keeper pid={old_k}")
                        else:
                            os.kill(old_k, 9)
                    except Exception as e:
                        log(f"kill keeper failed: {e}")
                    time.sleep(0.5)
                try:
                    start_keeper()
                except OSError as e:
                    log(f"start_keeper OSError: {e}")
                    time.sleep(3)
                    continue
                time.sleep(5)
            elif not po:
                # keeper 活着但端口仍挂：等 keeper 自己拉；若长时间不行再踢 keeper
                log("port down but keeper alive, waiting keeper restart webui")
                time.sleep(8)
            else:
                time.sleep(5)
        except Exception as e:
            log(f"supervisor loop error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
