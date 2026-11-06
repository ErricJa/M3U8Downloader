#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""从 ffmpeg_tools.7z 还原 FFmpeg 9.0.2 的 ffmpeg.exe / ffprobe.exe 并校验完整性。

用法：把本脚本与 ffmpeg_tools.7z 放在同一目录，运行 `python restore_tools.py`。
需要本机安装 7-Zip（或让 7z.exe 位于 PATH）。单文件版 M3U8Downloader.exe
无需执行本脚本。
"""
import hashlib
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(HERE, "ffmpeg_tools.7z")

# 压缩包内原始二进制的 SHA-256（解压后校验目标）
EXPECTED = {
    "ffmpeg.exe": "3256173f3f8bffd7df12227c68adf68025edb1832273a9530688a7bb1ed8edec",
    "ffprobe.exe": "f0d36ecbbdd3bcfac3efa078c96c7271c2e68b3810595552ac3b7f17e9a65c52",
}


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_7z():
    found = shutil.which("7z") or shutil.which("7z.exe")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
        candidate = os.path.join(base, "7-Zip", "7z.exe")
        if os.path.isfile(candidate):
            return candidate
    return ""


def main():
    if not os.path.isfile(ARCHIVE):
        print("ERROR: 未找到 ffmpeg_tools.7z，请与脚本放在同一目录。")
        return 2
    seven = find_7z()
    if not seven:
        print("ERROR: 未找到 7-Zip。请安装 7-Zip 后重试，或手动执行：7z x ffmpeg_tools.7z -o.")
        return 2

    print("解压:", ARCHIVE)
    r = subprocess.run([seven, "x", "-y", "-o" + HERE, ARCHIVE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if r.returncode != 0:
        print("ERROR: 解压失败:", r.stderr.decode("utf-8", "replace")[-300:])
        return 1

    ok = True
    for name, expected in EXPECTED.items():
        path = os.path.join(HERE, name)
        if not os.path.isfile(path):
            print("MISSING:", name)
            ok = False
            continue
        actual = sha256(path)
        matched = actual.lower() == expected.lower()
        print("%-14s %12d B  SHA256 %s" % (name, os.path.getsize(path),
                                           "OK" if matched else "MISMATCH"))
        ok = ok and matched

    print("结果:", "还原成功，与原始组件完全一致。" if ok else "校验未通过，请重新下载压缩包。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
