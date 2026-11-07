"""路径与目录管理：兼容源码运行与 PyInstaller 打包（onedir/onefile）。"""
import os
import sys


def is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def app_root() -> str:
    """程序根目录：exe 所在目录或源码根目录。"""
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_root() -> str:
    """资源根目录（web 静态文件、sources.yaml）：打包后在 _MEIPASS 或 exe 旁。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
        cand = os.path.join(base, "web")
        if os.path.isdir(cand):
            return base
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


APP_ROOT = app_root()
BUNDLE_ROOT = bundle_root()
DATA_DIR = os.path.join(APP_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "finbrief.db")


def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)


WEB_DIR = os.path.join(BUNDLE_ROOT, "web")
SOURCES_YAML = os.path.join(BUNDLE_ROOT, "app", "fetcher", "sources.yaml")
if not os.path.isfile(SOURCES_YAML):  # 打包后 yaml 可能被收在别的相对位置
    alt = os.path.join(BUNDLE_ROOT, "sources.yaml")
    if os.path.isfile(alt):
        SOURCES_YAML = alt
