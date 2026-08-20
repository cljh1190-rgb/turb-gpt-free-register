# -*- coding: utf-8 -*-
"""
WebUI 启动入口。

用法：
    python web.py                 # 默认 http://127.0.0.1:5000，仅本地访问，不自动打开浏览器
    python web.py --open-browser  # 启动后自动打开浏览器
    python web.py --port 8000     # 换端口
    python web.py --host 0.0.0.0  # 允许局域网访问（敏感工具，自行评估）

与 CLI（python main.py）完全平行，互不影响。
"""
import argparse
import faulthandler
import logging
import os
import sys
import tempfile
import webbrowser
from pathlib import Path
from threading import Timer

from webui.app import create_app
from webui.auth import is_generated_code

# 原生崩溃（Cloak/Playwright C 扩展）时把堆栈写到 stderr，便于对照 keeper 重启时间
try:
    faulthandler.enable(file=sys.stderr, all_threads=True)
except Exception:
    pass


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)) == 0:
                    return False
                return int(exit_code.value) == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        else:
            os.kill(pid, 0)
            return True
    except Exception:
        return False


def _acquire_single_instance(port: int):
    """持有跨进程文件锁，防止同一端口启动多个 WebUI 实例。

    若锁文件里的 PID 已死（崩溃残留），自动清理并抢占，避免 Failed to fetch 后永远起不来。
    """
    lock_path = Path(tempfile.gettempdir()) / f"turb-gpt-free-register-web-{int(port)}.lock"
    handle = lock_path.open("a+", encoding="utf-8")
    handle.seek(0)
    raw = (handle.read() or "").strip()
    old_pid = 0
    try:
        old_pid = int(raw.splitlines()[0].strip() or "0")
    except Exception:
        old_pid = 0

    if old_pid and old_pid != os.getpid() and not _pid_alive(old_pid):
        # 残留锁：旧进程已死
        try:
            handle.seek(0)
            handle.truncate()
            handle.write("0")
            handle.flush()
        except Exception:
            pass

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
    handle.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError) as exc:
        # 再判断一次：若锁主已死，删文件重试一次
        handle.close()
        if old_pid and not _pid_alive(old_pid):
            try:
                lock_path.unlink(missing_ok=True)
            except Exception:
                pass
            return _acquire_single_instance(port)
        raise RuntimeError(f"端口 {port} 的 WebUI 已在运行 (pid={old_pid or '?'})") from exc
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def _release_single_instance(handle) -> None:
    if handle is None:
        return
    try:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, IOError):
        pass
    try:
        handle.close()
    except Exception:
        pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT 注册 WebUI 控制台")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址，默认仅本地 127.0.0.1")
    parser.add_argument("--port", type=int, default=5000, help="端口，默认 5000")
    parser.add_argument("--open-browser", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--auth-code", default=None, help="WebUI 授权码；也可配置 .env: WEBUI_AUTH_CODE=...")
    parser.add_argument("--verbose", action="store_true", help="详细日志")
    args = parser.parse_args()

    _setup_logging(args.verbose)
    logger = logging.getLogger(__name__)

    if args.auth_code:
        os.environ["WEBUI_AUTH_CODE"] = args.auth_code

    try:
        instance_lock = _acquire_single_instance(args.port)
    except RuntimeError as exc:
        logger.error(str(exc))
        raise SystemExit(2) from exc

    app = create_app(auth_code=args.auth_code)
    try:
        from core import registration_service as registration_svc

        reaped_zombies = registration_svc.reap_zombie_jobs()
        if reaped_zombies:
            logger.warning("已回收 %s 个进程重启残留的僵尸注册任务", reaped_zombies)
        resumed_jobs = registration_svc.resume_pending_jobs()
        if resumed_jobs:
            logger.warning("已恢复 %s 个进程重启前的排队注册任务", resumed_jobs)
    except Exception:
        logger.exception("恢复进程重启前的排队注册任务失败")
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}"
    logger.info(f"WebUI 已启动：{url}")
    if is_generated_code():
        from webui.auth import expected_auth_code
        logger.warning("未配置 WEBUI_AUTH_CODE/AUTH_CODE，已生成本次临时授权码：%s", expected_auth_code())
    if args.host in ("0.0.0.0", "::"):
        logger.warning("已绑定到所有网卡，局域网内其他设备可访问。这是敏感工具，请确认网络环境可信。")

    if args.open_browser:
        Timer(1.0, lambda: webbrowser.open(url)).start()

    # 服务实现：
    # - 优先 Waitress（生产级纯 Python WSGI，比 Flask 自带开发服务器扛并发/长连接稳得多）
    # - 未安装时回退 app.run(threaded=True)
    # 根因背景：注册线程 + CloakBrowser 与 WebUI 同进程；开发服务器在高频轮询下更易僵死/退出，
    # 表现为浏览器「失去连接」。Waitress 不能隔离原生崩溃，但能显著降低「空闲也断」的概率。
    threads = max(8, min(32, int(os.environ.get("WEBUI_THREADS", "16") or 16)))
    server_mode = str(os.environ.get("WEBUI_SERVER", "waitress") or "waitress").strip().lower()
    try:
        try:
            from waitress import serve as waitress_serve
        except ImportError:
            waitress_serve = None

        if waitress_serve is not None and server_mode != "flask":
            logger.info(
                "使用 Waitress 提供 WebUI（threads=%s）。开发服务器已弃用。",
                threads,
            )
            waitress_serve(
                app,
                host=args.host,
                port=args.port,
                threads=threads,
                channel_timeout=120,
                cleanup_interval=30,
                connection_limit=200,
                ident="turb-gpt-free-register",
            )
        else:
            logger.warning(
                "使用 Flask threaded 服务（WEBUI_SERVER=flask 或 Waitress 不可用）。"
            )
            app.run(
                host=args.host,
                port=args.port,
                debug=False,
                threaded=True,
                use_reloader=False,
            )
    finally:
        _release_single_instance(instance_lock)


if __name__ == "__main__":
    main()
