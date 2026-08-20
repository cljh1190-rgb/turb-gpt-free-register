# -*- coding: utf-8 -*-
"""Start/stop dedicated registration xray. NEVER binds 10808. Isolated from user v2ray."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
XRAY = Path(os.environ.get("REG_XRAY_BIN", r"D:\TIZI\bin\xray\xray.exe"))
PID_FILE = ROOT / "xray.pid"
CFG = ROOT / "config.json"
LOG = ROOT / "xray.log"
POOL_OK = ROOT / "pool_ok.json"
POOL = ROOT / "pool.json"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            h = k.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            code = ctypes.c_ulong()
            k.GetExitCodeProcess(h, ctypes.byref(code))
            k.CloseHandle(h)
            return int(code.value) == 259
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def stop() -> None:
    pid = 0
    if PID_FILE.exists():
        try:
            pid = int((PID_FILE.read_text(encoding="utf-8") or "0").strip() or "0")
        except Exception:
            pid = 0
    if pid and _pid_alive(pid):
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"], capture_output=True)
        except Exception:
            pass
    if os.name == "nt":
        # kill any xray whose cmdline mentions reg_proxy
        subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='xray.exe'\" | "
                "Where-Object { $_.CommandLine -match 'reg_proxy' } | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }",
            ],
            capture_output=True,
        )
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def start() -> int:
    if not XRAY.exists():
        raise SystemExit(f"xray not found: {XRAY}")
    if not CFG.exists():
        raise SystemExit(f"missing {CFG}, run build_xray_config.py")
    # if already up with listening base port, reuse
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", 17891))
        s.close()
        if PID_FILE.exists():
            try:
                return int(PID_FILE.read_text(encoding="utf-8").strip())
            except Exception:
                pass
    except Exception:
        try:
            s.close()
        except Exception:
            pass
    stop()
    time.sleep(0.3)
    # WMI create = outside parent job object on Windows
    if os.name == "nt":
        cmdline = f'"{XRAY}" run -c "{CFG}"'
        ps = (
            f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
            f"-Arguments @{{ CommandLine = '{cmdline.replace(chr(39), chr(39)+chr(39))}'; "
            f"CurrentDirectory = '{str(ROOT).replace(chr(39), chr(39)+chr(39))}' }}; "
            f"Write-Output $r.ProcessId"
        )
        # simpler: use wmic / Win32 via python ctypes-free subprocess with powershell
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"$p = Start-Process -FilePath '{XRAY}' -ArgumentList 'run','-c','{CFG}' "
                f"-WorkingDirectory '{ROOT}' -WindowStyle Hidden -PassThru; $p.Id",
            ],
            capture_output=True,
            text=True,
        )
        # Start-Process still may be in job; use Win32_Process.Create
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{"
                f"CommandLine='\"{XRAY}\" run -c \"{CFG}\"'; CurrentDirectory='{ROOT}'"
                "}; if ($r.ReturnValue -ne 0) {{ throw \"WMI $($r.ReturnValue)\" }}; $r.ProcessId",
            ],
            capture_output=True,
            text=True,
        )
        out = (completed.stdout or "").strip().splitlines()
        if completed.returncode != 0 or not out:
            raise SystemExit(f"start failed: {completed.stdout} {completed.stderr}")
        pid = int(out[-1].strip())
    else:
        with LOG.open("a", encoding="utf-8") as log:
            proc = subprocess.Popen([str(XRAY), "run", "-c", str(CFG)], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
        pid = proc.pid
    PID_FILE.write_text(str(pid), encoding="utf-8")
    # wait listen
    for _ in range(20):
        try:
            s = socket.socket()
            s.settimeout(0.5)
            s.connect(("127.0.0.1", 17891))
            s.close()
            break
        except Exception:
            time.sleep(0.25)
    else:
        raise SystemExit("reg-proxy started but port 17891 not listening")
    print(f"reg-proxy xray pid={pid}")
    return pid


def load_pool_proxies() -> list[str]:
    for path in (POOL_OK, POOL):
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        out = []
        for x in data:
            p = str(x.get("proxy") or "").strip()
            if p:
                out.append(p)
        if out:
            return out
    return []


def ensure_running() -> list[str]:
    """Ensure dedicated proxy is up; return proxy URL list for PROXY_POOL."""
    try:
        start()
    except SystemExit as e:
        # if already partially up, still return pool
        print(f"[reg-proxy] start note: {e}")
    return load_pool_proxies()


def status() -> None:
    pid = 0
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text(encoding="utf-8").strip() or "0")
        except Exception:
            pid = 0
    print(f"pid={pid} alive={_pid_alive(pid)} proxies={len(load_pool_proxies())}")


if __name__ == "__main__":
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "start").lower()
    if cmd == "stop":
        stop(); print("stopped")
    elif cmd == "status":
        status()
    elif cmd == "ensure":
        ps = ensure_running()
        print("pool", len(ps))
        for p in ps[:5]:
            print(" ", p)
    else:
        start(); status()
