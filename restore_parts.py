#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 parts/ 下的分片合并还原为完整文件，并逐字节校验。

用法：在仓库根目录运行 `python restore_parts.py`。
仅依赖 Python 标准库，不需要 7-Zip。还原成功后得到：
  - M3U8Downloader.exe   最终单文件主程序（可直接双击运行）
  - ffmpeg_tools.7z      FFmpeg 组件压缩包（源码运行时才需要解压）

原因：上传环境只能使用 GitHub REST API，单次请求无法承载 50 MB+ 文件，
也无法访问 github.com 的 git / LFS 端点，故大文件以分片入库。
"""
import hashlib
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PARTS_DIR = os.path.join(HERE, "parts")

# 目标文件 -> (分片数, 还原后 SHA-256)
TARGETS = {
    "M3U8Downloader.exe": (2, "3e1a1ab3701ae579dadc9a93183b1628ad0ed371569aa4663cc81f4291dc4726"),
    "ffmpeg_tools.7z": (2, "75f538b7c98b6361f0462a0bce94ad40fc1d83c6e608a2c1f8fe1f626d00a82c"),
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def restore(name, count, expected):
    out_path = os.path.join(HERE, name)
    if os.path.isfile(out_path) and sha256(out_path).lower() == expected:
        print("%-24s 已存在且校验通过，跳过" % name)
        return True

    names = ["%s.part%02d" % (name, i) for i in range(1, count + 1)]
    paths = [os.path.join(PARTS_DIR, n) for n in names]
    missing = [n for n, p in zip(names, paths) if not os.path.isfile(p)]
    if missing:
        print("ERROR: 缺少分片:", ", ".join(missing))
        return False

    print("合并 %-24s <- %d 个分片 ..." % (name, count), end="", flush=True)
    tmp = out_path + ".merging"
    with open(tmp, "wb") as out:
        for p in paths:
            with open(p, "rb") as f:
                while True:
                    buf = f.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
    os.replace(tmp, out_path)
    actual = sha256(out_path)
    ok = actual.lower() == expected.lower()
    print(" %12d B  SHA256 %s" % (os.path.getsize(out_path), "OK" if ok else "MISMATCH"))
    if not ok:
        print("  期望:", expected)
        print("  实际:", actual)
    return ok


def main():
    ok = all(restore(n, c, s) for n, (c, s) in TARGETS.items())
    if not ok:
        print("还原失败，请检查 parts/ 是否完整。")
        return 1
    print("\n主程序已还原：M3U8Downloader.exe（自包含，可直接运行）。")
    if os.path.isfile(os.path.join(HERE, "restore_tools.py")):
        ans = input("是否同时解压 FFmpeg 组件？[y/N] ").strip().lower()
        if ans in ("y", "yes"):
            return subprocess.call([sys.executable, os.path.join(HERE, "restore_tools.py")])
    print("如需独立 ffmpeg.exe / ffprobe.exe，可稍后运行：python restore_tools.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
