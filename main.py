"""程序入口：先弹加载窗口，后台启动 FastAPI，就绪后自动切换到界面。

修复点：
- 窗口先显示内嵌"加载中"页（用户立即有反馈，不再像卡死）
- 后端就绪后才 load_url，就绪失败显示错误页与日志位置
- 启动过程写入 data\\startup.log 便于诊断
"""
import atexit
import os
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_root() -> str:
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def boot_log(msg: str):
    try:
        d = os.path.join(app_root(), "data")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "startup.log"), "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except OSError:
        pass


LOADING_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:"Microsoft YaHei",sans-serif;background:#f6f7f9;
display:flex;align-items:center;justify-content:center;height:100vh;}
.box{text-align:center;}
.logo{font-size:30px;font-weight:800;color:#134e4a;letter-spacing:2px;}
.logo b{color:#0d9488;}
.spin{width:38px;height:38px;border:4px solid #0d9488;border-top-color:transparent;
border-radius:50%;margin:34px auto 20px;animation:r .9s linear infinite;}
@keyframes r{to{transform:rotate(360deg)}}
.tip{color:#86909c;font-size:14px;line-height:1.9;}
.tip small{display:block;color:#b0b7c3;font-size:12px;}
</style></head><body><div class="box">
<div class="logo">金融热点<b>编译系统</b></div>
<div class="spin"></div>
<div class="tip">正在启动本地服务…<small>首次运行需初始化组件，可能需要十几秒，请稍候</small></div>
</div></body></html>"""

ERROR_HTML = """<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:"Microsoft YaHei",sans-serif;background:#f6f7f9;
display:flex;align-items:center;justify-content:center;height:100vh;}
.box{max-width:560px;background:#fff;border:1px solid #e5e6eb;border-radius:14px;
padding:36px 40px;text-align:center;}
h2{color:#1d2129;margin:0 0 14px;font-size:20px;}
p{color:#86909c;font-size:14px;line-height:1.9;margin:6px 0;}
code{background:#f2f3f5;border-radius:6px;padding:2px 8px;color:#1d2129;}
</style></head><body><div class="box">
<h2>本地服务启动失败</h2>
<p>后端服务未能在 180 秒内就绪，请退出程序后重试。</p>
<p>若反复失败，请查看日志文件：<code>data\\startup.log</code></p>
<p style="color:#f53f3f">__ERR__</p>
</div></body></html>"""


def pick_port(start=8765, tries=20) -> int:
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start + tries


def wait_port(port: int, timeout: float = 180) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.4)
    return False


# ------------- v2.12.0：内置 RSSHub 本地服务（全自动，随程序启停） -------------
RSSHUB_PORT = 1200
_RSSHUB_PROC = None


def rsshub_bundle_dir() -> str:
    """内置 RSSHub 目录：exe（或程序根）旁的 rsshub 文件夹。"""
    root = app_root()
    d = os.path.join(root, "rsshub")
    if os.path.isfile(os.path.join(d, "node.exe")) and \
            os.path.isfile(os.path.join(d, "dist", "index.mjs")):
        return d
    return ""


def rsshub_alive(port: int = RSSHUB_PORT) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.2):
            return True
    except OSError:
        return False


def rsshub_service_status() -> dict:
    """给 server 端点用的现状描述（不探活网络，只读状态）。"""
    d = rsshub_bundle_dir()
    return {"bundled": bool(d), "port": RSSHUB_PORT, "alive": rsshub_alive(),
            "running_child": _RSSHUB_PROC is not None and _RSSHUB_PROC.poll() is None,
            "dir": d}


def start_rsshub_async():
    """后台线程：若内置组件齐全且 1200 未被占用就拉起 RSSHub，子进程随主程序退出回收。"""
    thread = threading.Thread(target=_start_rsshub_worker, daemon=True)
    thread.start()
    return thread


def _start_rsshub_worker():
    global _RSSHUB_PROC
    d = rsshub_bundle_dir()
    if not d:
        boot_log("RSSHub 内置组件未捆绑，跳过本地服务（使用公共实例）")
        return
    try:
        import urllib.request
        rx = urllib.request.urlopen("http://127.0.0.1:1200/", timeout=2) if rsshub_alive() else None
        if rx:
            rx.close()
            boot_log("RSSHub 本地服务已在运行（视为遗留实例，自动复用）")
            _set_rsshub_base_if_needed()
            return
    except OSError:
        rx = None
    exe = os.path.join(d, "node.exe")
    try:
        # v2.12.1 关键修正：CREATE_NO_WINDOW 在 subprocess 模块（os 下没有——
        # 上一版 hasattr(os,...) 恒假导致 flags 置 0，node.exe 弹出黑色控制台窗口）
        flags = subprocess.CREATE_NO_WINDOW
        env = dict(os.environ)
        env["VERCEL_ENV"] = "production"   # v2.12.0 关键：走打包 routes 注册表，否则 dev registry 扫目录失败
        # 与程序同源代理：RSSHub 抓上游站点走同一代理（PROXY_URI 官方环境变量）
        try:
            import json as _j
            p = os.path.join(app_root(), "data", "settings.json")
            if os.path.isfile(p):
                with open(p, encoding="utf-8") as f:
                    st = _j.load(f) or {}
                if str(st.get("proxy_enabled")) == "1" and st.get("proxy_url"):
                    env["PROXY_URI"] = st.get("proxy_url")
        except Exception:
            pass
        _RSSHUB_PROC = subprocess.Popen(
            [exe, "dist/index.mjs"],
            cwd=d, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags)
        boot_log(f"RSSHub 子进程已启动 pid={_RSSHUB_PROC.pid}")
    except OSError:
        boot_log("RSSHub 子进程启动失败: " + traceback.format_exc())
        return
    ok = wait_port(RSSHUB_PORT, 60)
    boot_log(f"RSSHub 本地服务就绪: {ok}")
    if ok:
        _set_rsshub_base_if_needed()


def _set_rsshub_base_if_needed():
    """内置服务就绪后，仅在实例从未配置（空）或仍在默认公共镜像时自动切到本机实例。"""
    try:
        import json as _json
        p = os.path.join(app_root(), "data", "settings.json")
        cur = ""
        if os.path.isfile(p):
            with open(p, encoding="utf-8") as f:
                cur = (_json.load(f) or {}).get("rsshub_base", "") or ""
        if cur.strip() in ("", "https://rsshub.rssforever.com"):
            from app import rsshub as rh
            rh.set_base(f"http://127.0.0.1:{RSSHUB_PORT}")
            boot_log(f"RSSHub 实例已自动设为本机 {RSSHUB_PORT}")
    except Exception:
        boot_log("RSSHub 实例自动配置失败: " + traceback.format_exc())


def on_exit():
    """程序退出：回收 RSSHub 子进程（不影响遗留/外部实例）。"""
    try:
        if _RSSHUB_PROC is not None and _RSSHUB_PROC.poll() is None:
            _RSSHUB_PROC.kill()
            boot_log("RSSHub 子进程已回收")
    except OSError:
        pass


def main():
    t0 = time.perf_counter()
    port = pick_port()
    url = f"http://127.0.0.1:{port}"
    boot_log(f"程序启动，计划端口 {port}（frozen={is_frozen()}）")
    atexit.register(on_exit)   # v2.12.0 退出时回收内置 RSSHub 子进程

    def start_backend():
        import uvicorn
        from app.server import app
        boot_log(f"后端模块加载完成，开始监听（+{time.perf_counter()-t0:.2f}s）")
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    try:
        import webview
    except Exception:
        boot_log("pywebview 不可用，退化浏览器模式")
        threading.Thread(target=start_backend, daemon=True).start()
        if wait_port(port, 180):
            webbrowser.open(url)
            start_rsshub_async()
        print(f"服务地址：{url}（关闭此窗口即退出）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sys.exit(0)

    window = webview.create_window(
        "金融热点编译系统", html=LOADING_HTML,
        width=1360, height=880, min_size=(1080, 700),
        maximized=True,   # v2.9.6：启动默认全屏最大化
        text_select=True)  # v2.7：允许选中复制文本（pywebview 默认禁用）
    try:
        window.events.shown += lambda: boot_log(
            f"加载页已显示（窗口 +{time.perf_counter()-t0:.2f}s）")  # v2.18.0 启动耗时观测
    except Exception:
        pass

    def backend_then_load():
        try:
            threading.Thread(target=start_backend, daemon=True).start()
            ok = wait_port(port, 180)
            boot_log(f"端口就绪: {ok}")
            if ok:
                window.load_url(url)
                start_rsshub_async()   # v2.12.0：界面就绪后后台拉起内置 RSSHub（不阻塞加载）
            else:
                window.load_html(ERROR_HTML.replace("__ERR__", ""))
        except Exception:
            boot_log("后端启动异常: " + traceback.format_exc())
            try:
                err = traceback.format_exc().splitlines()[-1]
                window.load_html(ERROR_HTML.replace("__ERR__", err))
            except Exception:
                pass

    try:
        # v2.18.0 private_mode=False：WebView2 用户数据目录持久化到 data\webview
        # （原默认 InPrivate 模式每次启动在 %TEMP% 重建全新 profile、退出即删，
        #   是"打开卡 1-2 秒才出加载页"的主因之一；持久化后热启动显著加快，
        #   且 localStorage（主题选择/排序记忆等）可在重启后保留）。
        # 本地静态服务端口随机挑选，避免多开实例在默认 42001 上撞车。
        webview.start(backend_then_load, private_mode=False,
                      storage_path=os.path.join(app_root(), "data", "webview"),
                      http_port=pick_port(42001, 100))
    except Exception:
        boot_log("窗口创建失败，退化浏览器模式: " + traceback.format_exc())
        threading.Thread(target=backend_then_load, daemon=True).start()
        if wait_port(port, 180):
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            sys.exit(0)


if __name__ == "__main__":
    main()
