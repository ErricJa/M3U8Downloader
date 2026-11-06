#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键打包 m3u8_downloader.py -> 独立运行的 M3U8Downloader.exe

用法：python build_exe.py
产物：dist/M3U8Downloader.exe，并自动复制一份到原工具目录。

exe 内置 N_m3u8DL-RE 0.6.0（主内核）、N_m3u8DL-CLI_v3.0.2.exe（失败回退）、
ffmpeg 9.0.2、config.txt 与 NO_UPDATE，无需 bat、无需 Python 环境、无需原
安装目录即可运行。RE 用 --disable-update-check 关闭版本检查；NO_UPDATE 用于
关闭旧 CLI 启动时的联网自更新（3.0.1 会去 ghproxy 下载新版，拖慢整批任务）。
ffprobe.exe 体积与新 ffmpeg 相当，为控制单文件体积不内嵌，仅放在安装目录
供源码运行 / 手工诊断使用；程序在没有 ffprobe 时自动回退到 ffmpeg -i 探测。
"""

import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS_DIR = r"C:\HRSoftstore\Install\M3U8Downloader"
EXE_NAME = "M3U8Downloader"
RE_NAME = "N_m3u8DL-RE.exe"
SEP = os.pathsep  # Windows 为 ';'


def locate_re(tool_dir=""):
    for d in (tool_dir, TOOLS_DIR, HERE, os.environ.get("M3U8DL_HOME", "")):
        if d and os.path.isdir(d):
            p = os.path.join(d, RE_NAME)
            if os.path.isfile(p):
                return p
    return ""


def resolve_tools():
    """返回 (CLI, ffmpeg, config, RE)；RE 缺失时返回空串，程序会自动回退 CLI。"""
    sys.path.insert(0, HERE)
    import m3u8_downloader as app
    try:
        d, cli, ff, _ = app.find_tools()
        return cli, ff, os.path.join(d, "config.txt"), locate_re(d)
    except RuntimeError:
        # 原安装目录被移走时，从已有的独立 exe 中取回内置内核
        from PyInstaller.archive.readers import CArchiveReader
        old_exe = os.path.join(HERE, "dist", EXE_NAME + ".exe")
        if not os.path.isfile(old_exe):
            raise
        out = os.path.join(HERE, "_tools")
        os.makedirs(out, exist_ok=True)
        archive = CArchiveReader(old_exe)
        names = list(app.CLI_CANDIDATES) + [
            "ffmpeg.exe", "ffprobe.exe", "config.txt", app.NO_UPDATE_FILE, RE_NAME,
        ]
        for name in names:
            try:
                data = archive.extract(name)
            except Exception:
                continue                # 旧 exe 可能没有该资源
            if isinstance(data, tuple):
                data = data[1]
            with open(os.path.join(out, name), "wb") as f:
                f.write(data)
        cli = next((os.path.join(out, n) for n in app.CLI_CANDIDATES
                    if os.path.isfile(os.path.join(out, n))), "")
        ff = os.path.join(out, "ffmpeg.exe")
        if not cli or not os.path.isfile(ff):
            raise RuntimeError("旧 exe 中未找到可用的下载内核 / ffmpeg")
        print(f"已从旧 exe 提取内置内核到: {out}")
        re = os.path.join(out, RE_NAME)
        return cli, ff, os.path.join(out, "config.txt"), (
            re if os.path.isfile(re) else "")


def main():
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("缺少 PyInstaller，请先执行： python -m pip install pyinstaller")
        return 1

    cli, ff, cfg, re = resolve_tools()
    for path in (cli, ff):
        if not os.path.isfile(path):
            raise FileNotFoundError(path)

    # NO_UPDATE 与内核一起释放到 sys._MEIPASS，彻底关闭 CLI 自动更新检查
    no_update = os.path.join(HERE, "NO_UPDATE")
    if not os.path.isfile(no_update):
        with open(no_update, "wb"):
            pass

    dist = os.path.join(HERE, "dist")
    build = os.path.join(HERE, "build")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--console",
        "--name", EXE_NAME,
        "--distpath", dist,
        "--workpath", build,
        "--specpath", HERE,
        "--add-binary", f"{cli}{SEP}.",
        "--add-binary", f"{ff}{SEP}.",
        "--add-data", f"{no_update}{SEP}.",
    ]
    if re and os.path.isfile(re):
        cmd += ["--add-binary", f"{re}{SEP}."]
        print(f"内置主内核: {re}")
    else:
        print(f"警告: 未找到 {RE_NAME}，打包版将自动回退 CLI 3.0.2")
    if os.path.isfile(cfg):
        cmd += ["--add-data", f"{cfg}{SEP}."]
    cmd.append(os.path.join(HERE, "m3u8_downloader.py"))

    print("打包中（约 1-3 分钟）...", flush=True)
    subprocess.check_call(cmd, cwd=HERE)

    exe = os.path.join(dist, EXE_NAME + ".exe")
    if not os.path.isfile(exe):
        raise RuntimeError("未生成 exe，请检查上方 PyInstaller 输出")
    shutil.rmtree(build, ignore_errors=True)
    size = os.path.getsize(exe) / 1024 / 1024
    print(f"完成: {exe} ({size:.1f} MB)")
    if os.path.isdir(TOOLS_DIR):
        target = os.path.join(TOOLS_DIR, EXE_NAME + ".exe")
        shutil.copy2(exe, target)
        print(f"已复制到: {target}")
    else:
        print(f"提示: 原工具目录 {TOOLS_DIR} 不存在，已跳过复制；请手动拷贝上面的 exe")
    return 0


if __name__ == "__main__":
    sys.exit(main())
