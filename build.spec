# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir 模式，内嵌 web 前端与信源配置。
不包含任何 API 密钥（密钥由用户在设置页输入，存于 exe 旁 data/ 目录）。"""
from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []

# pywebview（Windows winforms/edgechromium 后端）必须整体收集
for pkg in ("webview",):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h
hiddenimports += collect_submodules("apscheduler")
hiddenimports += ["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.loops.asyncio",
                  "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
                  "uvicorn.protocols.websockets.auto",
                  "uvicorn.lifespan.on"]

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas + [
        ("web", "web"),
        ("app/fetcher/sources.yaml", "app/fetcher"),
        ("DEVELOPMENT.md", "."),
        ("README.md", "."),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "numpy", "pandas", "PIL", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="金融热点编译系统",
    debug=False,
    strip=False,
    upx=False,
    console=False,
    icon="assets/icon.ico",
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="金融热点编译系统",
)
