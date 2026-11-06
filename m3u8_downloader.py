#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
M3U8 批量下载器 - Python 命令行复刻版

原项目：N_m3u8DL-CLI-SimpleG.exe -> N_m3u8DL-CLI_v3.0.1.exe -> ffmpeg.exe
复刻后：调度壳 + N_m3u8DL-RE 0.6.0（主内核） + ffmpeg 9.0.2，
        旧 N_m3u8DL-CLI 3.0.2 保留为失败回退内核。

增强（不改原有目录/清理策略）：
  * N_m3u8DL-RE 主下载层：--auto-select 自动选最高配置，-M 混流独立音轨 /
    外挂字幕，fMP4(CMAF) init+fragments、DASH/MSS、现代编码原生支持；
    --disable-update-check 彻底关闭联网更新检查。
  * RE 失败自动回退 CLI 3.0.2 + Python 合并层，两条路径都失败才报失败。
  * master 预解析与下探子播放列表：404 / 无分片 / 缺 ENDLIST 提前判失败。
  * 判定标准改为「磁盘上确有可播放产物」，不再只看内核退出码。
  * CLI 路径：精简 master 消灭交互菜单、fMP4 init+fragments 拼接 remux。
  * 线程数按超时率自适应降档、403/429 指数退避、批次内地址去重、
    失败原因写入任务日志 / 批次日志 / 最终项目统计。

运行：python m3u8_downloader.py   （或双击 M3U8Downloader.exe）

目录布局（<保存目录> 由用户指定）：
    <保存目录>\<标题>.mp4                  最终视频
    <保存目录>\<标题>.vtt / .srt          外挂字幕（与视频放在一起）
    <保存目录>\ffmpeg temp\*.log           全部日志（长期保留）
    <保存目录>\ffmpeg temp\*               下载/合并缓存（成功时除 .log 外全部删除）
    <保存目录>\ffmpeg temp\失败缓存_*\     失败任务的全部缓存（长期保留，供排查/补救）
"""

import base64
import datetime as dt
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# ---------------------------------------------------------------- 隐藏默认配置
# 与 config.txt 一一对应；启动时若同目录存在 config.txt 会覆盖（同样对用户隐藏）
DEFAULT_CONFIG = {
    "程序路径": "N_m3u8DL-CLI_v3.0.2.exe",
    "保存路径": "",                 # 空 -> 系统默认视频路径
    "代理": "",
    "请求头": "",
    "删除临时文件": "0",
    "MP4混流边下边看": "0",
    "二进制合并": "0",
    "仅解析模式": "0",
    "不写入日期": "0",
    "最大线程": "32",
    "最小线程": "16",
    "重试次数": "15",
    "超时秒数": "10",
    "停止速度": "0",
    "最大速度": "0",
    "不合并": "0",
    "不使用系统代理": "0",
    "仅合并音频": "0",
}

INSTALL_DIR = r"C:\HRSoftstore\Install\M3U8Downloader"
TEMP_DIR_NAME = "ffmpeg temp"          # 保存目录下：全部日志与下载缓存
HEVC_ENCODERS = ("libx265", "hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_mf")
H264_ENCODERS = ("libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_mf")
NO_EXPLORER = os.environ.get("M3U8DL_NO_EXPLORER", "") == "1"   # 隐藏调试开关
KEEP_TEMP = os.environ.get("M3U8DL_KEEP_TEMP", "") == "1"       # 隐藏调试开关：保留临时文件

FAILED_CACHE_PREFIX = "失败缓存_"       # 失败任务缓存归档目录，长期保留
SUBTITLE_EXTS = (".srt", ".vtt", ".webvtt", ".ass", ".ssa", ".sub", ".ttml")
# RE 支持 AV1/VP9 等现代编码，可播放性校验同步放宽
PLAYABLE_CODECS = ("h264", "hevc", "h265", "avc1", "hvc1", "av01", "av1", "vp09", "vp9")

# 主内核 N_m3u8DL-RE 0.6.0（官方 MIT，单文件 .NET AOT，无需额外运行时）。
# 它原生支持 --auto-select / --disable-update-check / 独立音轨 / fMP4 init 分片 /
# DASH / MSS / 现代编码，并由 -M 直接混流为 mp4；旧 CLI 仅作失败回退。
RE_NAME = "N_m3u8DL-RE.exe"
RE_WORK_DIR = "_re_work"               # RE 的 tmp/out 统一放在 ffmpeg temp 下
DISABLE_RE = os.environ.get("M3U8DL_DISABLE_RE", "") == "1"       # 调试：强制走旧 CLI
DISABLE_FALLBACK = os.environ.get("M3U8DL_DISABLE_FALLBACK", "") == "1"

# 下载内核：优先 3.0.2（修复 BYTERANGE / ChaCha20 / m3u8dl 协议转义），
# 旧安装目录若只有 3.0.1 也能继续运行。
CLI_CANDIDATES = ("N_m3u8DL-CLI_v3.0.2.exe", "N_m3u8DL-CLI_v3.0.1.exe")
# 官方隐藏开关：把空的 NO_UPDATE 文件放在内核同目录，可彻底关闭启动时
# 的联网更新检查（3.0.1 会去 mirror.ghproxy.com 下载 3.0.2 自更新包，
# 3 次超时各 20 秒）。该开关自 v2.5.4 起存在，3.0.2 仍保留。
NO_UPDATE_FILE = "NO_UPDATE"
FFPROBE_NAME = "ffprobe.exe"           # 新版完整 ffmpeg 构建自带，用于精确探测

CONNECT_TRIES = 3                     # 网络 m3u8 连接失败最多尝试 3 次
try:
    CONNECT_GAP = max(0, int(os.environ.get("M3U8DL_RETRY_GAP", "15")))
except ValueError:
    CONNECT_GAP = 15                 # 每次失败后等待 15 秒（隐藏开关仅供自动化测试加速）


# ---------------------------------------------------------------- 通用工具
def stamp():
    """原日志文件名格式 2026-05-28_22-25-49-747"""
    return dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")[:-3]


def hms():
    return dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def full_date():
    return dt.datetime.now().strftime("%Y/%m/%d %H:%M:%S")


def isatty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


def _restore_console_cp(saved):
    try:
        import ctypes
        k = ctypes.windll.kernel32
        k.SetConsoleCP(saved[0])
        k.SetConsoleOutputCP(saved[1])
    except Exception:
        pass


def setup_console():
    """统一 UTF-8 控制台/标准流，保证源码运行与 PyInstaller exe 的中文输入一致"""
    if os.name == "nt":
        try:
            import atexit
            import ctypes
            k = ctypes.windll.kernel32
            saved = (k.GetConsoleCP(), k.GetConsoleOutputCP())
            k.SetConsoleCP(65001)
            k.SetConsoleOutputCP(65001)
            atexit.register(_restore_console_cp, saved)
        except Exception:
            pass
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def say(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        print(msg.encode("gbk", "replace").decode("gbk"), flush=True)


def decode_console(data, prefer="gbk"):
    """子进程中文可能是 UTF-8(切换代码页后) 或 GBK(.NET 默认)，自动判别"""
    if isinstance(data, str):
        return data
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for enc in (prefer, "gbk", "cp1252"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


def safe_name(title, fallback="video"):
    clean = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(title).strip())
    clean = clean.rstrip(" .").strip()
    clean = clean[:110]
    return clean or fallback


def unique_path(path):
    """不覆盖已有文件：a.mp4 -> a(2).mp4"""
    if not os.path.exists(path):
        return path
    root, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{root}({i}){ext}"):
        i += 1
    return f"{root}({i}){ext}"


# ---------------------------------------------------------------- 临时缓存管理
def remove_any(path):
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def is_failed_cache(name):
    return str(name).startswith(FAILED_CACHE_PREFIX)


def clean_cache(root, keep=()):
    """成功任务的缓存清理：删除除 .log 与失败缓存归档以外的所有文件（夹）。

    keep 可额外保护本批次刚生成的归档；历史失败缓存一律长期保留。
    """
    if KEEP_TEMP:
        return 0
    protected = {os.path.abspath(p) for p in keep}
    removed = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.lower().endswith(".log") and os.path.isfile(path):
            continue
        if is_failed_cache(name) or os.path.abspath(path) in protected:
            continue
        remove_any(path)
        if not os.path.exists(path):
            removed += 1
    return removed


def archive_failed_cache(root, batch_ts, idx, title, extra_files=(), log_files=()):
    """合并失败时把本条任务的全部缓存移入独立目录长期保留。

    .log 与其它失败归档不动；extra_files 收回误输出的无效产物，
    log_files 复制本任务日志副本，使归档自包含、便于排查。
    """
    target = unique_path(os.path.join(
        root, f"{FAILED_CACHE_PREFIX}{batch_ts}_{idx:02d}_{safe_name(title, 'task')}"
    ))
    os.makedirs(target, exist_ok=True)
    moved = 0
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if os.path.abspath(path) == os.path.abspath(target):
            continue
        if (name.lower().endswith(".log") and os.path.isfile(path)) or is_failed_cache(name):
            continue
        try:
            shutil.move(path, os.path.join(target, name))
            moved += 1
        except OSError:
            pass
    for f in extra_files:
        if f and os.path.isfile(f):
            try:
                shutil.move(f, unique_path(os.path.join(target, os.path.basename(f))))
                moved += 1
            except OSError:
                pass
    for f in log_files:
        if f and os.path.isfile(f):
            try:
                shutil.copy2(f, unique_path(os.path.join(target, os.path.basename(f))))
                moved += 1
            except OSError:
                pass
    return target, moved


def find_subtitles(root):
    """递归收集字幕文件（跳过失败缓存归档），排除空文件。"""
    subs = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not is_failed_cache(d)]
        for name in files:
            path = os.path.join(dirpath, name)
            if name.lower().endswith(SUBTITLE_EXTS):
                try:
                    if os.path.getsize(path) > 0:
                        subs.append(path)
                except OSError:
                    continue
    return sorted(subs)


def export_subtitles(save_dir, title, subs):
    """字幕单独输出，与最终视频放在一起（同名不同扩展名）。"""
    out = []
    for src in subs:
        ext = os.path.splitext(src)[1].lower()
        if ext == ".webvtt":
            ext = ".vtt"
        dst = unique_path(os.path.join(save_dir, safe_name(title, "video") + ext))
        try:
            shutil.copy2(src, dst)
            out.append(dst)
        except OSError:
            continue
    return out


def collect_cli_logs(tool_dir, log_dir, since):
    """下载内核会在自身目录 Logs\\ 下写 .log（无路径参数），把本任务新增的归入 ffmpeg temp。

    仅移动 mtime 晚于任务开始时刻的文件，用户原有的历史日志不动。
    """
    src_dir = os.path.join(tool_dir, "Logs")
    if not os.path.isdir(src_dir) or os.path.abspath(src_dir) == os.path.abspath(log_dir):
        return 0
    moved = 0
    for name in os.listdir(src_dir):
        if not name.lower().endswith(".log"):
            continue
        path = os.path.join(src_dir, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) < since - 2:
                continue
            shutil.move(path, unique_path(os.path.join(log_dir, name)))
            moved += 1
        except OSError:
            continue
    return moved


def fmt_cmd(args):
    """按原日志风格输出命令行：仅含特殊字符/空格的参数加引号"""
    parts = []
    for a in args:
        s = str(a)
        if not s or re.search(r'[\s:=?|"\\]', s):
            parts.append('"' + s.replace('"', '\\"') + '"')
        else:
            parts.append(s)
    return " ".join(parts)


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} GB"


def default_videos_dir(cfg=None):
    """系统默认视频路径（注册表 -> config.txt 保存路径 -> ~/Videos）"""
    if os.name == "nt":
        try:
            import winreg
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            )
            val, _ = winreg.QueryValueEx(key, "My Video")
            winreg.CloseKey(key)
            if val:
                path = os.path.normpath(os.path.expandvars(str(val)))
                if os.path.isdir(path) and re.search(r"[A-Za-z]:\\", path):
                    return path
        except Exception:
            pass
    if cfg:
        configured = str(cfg.get("保存路径", "")).strip()
        if configured and os.path.isdir(configured):
            return os.path.normpath(configured)
    return os.path.join(os.path.expanduser("~"), "Videos")


def load_config(tool_dir):
    cfg = dict(DEFAULT_CONFIG)
    path = os.path.join(tool_dir, "config.txt")
    if not os.path.isfile(path):
        return cfg
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
        for item in text.split(";"):
            key, sep, val = item.partition("=")
            if not sep:
                continue
            key, val = key.strip(), val.strip()
            if val:
                try:
                    decoded = base64.b64decode(val, validate=True).decode("utf-8")
                    if decoded.isprintable():
                        val = decoded
                except Exception:
                    pass
            cfg[key] = val
    except Exception as e:
        say(f"[提示] config.txt 读取失败，使用内置默认值：{e}")
    return cfg


def app_dir():
    """源码运行时取脚本目录；PyInstaller 打包后取 exe 所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ensure_no_update(tool_dir):
    """在内核同目录放置 NO_UPDATE，禁止 CLI 启动时联网检查/下载自更新包。

    打包版已把该文件作为资源随 exe 释放；这里再做一次兜底，兼容用户
    手动替换内核或源码运行的场景。
    """
    path = os.path.join(tool_dir, NO_UPDATE_FILE)
    try:
        if not os.path.exists(path):
            with open(path, "wb"):
                pass
        return True
    except OSError:
        return False


def ffprobe_exe(ffmpeg):
    """新版完整 ffmpeg 构建自带 ffprobe.exe；旧精简版没有则返回空串。"""
    path = os.path.join(os.path.dirname(ffmpeg), FFPROBE_NAME)
    return path if os.path.isfile(path) else ""


_FF_VER_CACHE = {}


def ffmpeg_version(ffmpeg):
    if ffmpeg in _FF_VER_CACHE:
        return _FF_VER_CACHE[ffmpeg]
    ver = "未知"
    try:
        r = subprocess.run([ffmpeg, "-version"], capture_output=True,
                           timeout=15, stdin=subprocess.DEVNULL)
        text = decode_console(r.stdout, "utf-8") + decode_console(r.stderr, "utf-8")
        m = re.search(r"ffmpeg version\s+(\S+)", text)
        if m:
            ver = m.group(1)
    except Exception:
        pass
    _FF_VER_CACHE[ffmpeg] = ver
    return ver


def find_tools():
    """定位 CLI 与 ffmpeg。

    打包成 exe 后优先使用 exe 内自带的二进制（sys._MEIPASS），
    因此 exe 可独立运行，不需要 bat、也不依赖原安装目录。
    """
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        candidates.append(meipass)
    candidates.append(app_dir())
    if os.environ.get("M3U8DL_HOME"):
        candidates.append(os.environ["M3U8DL_HOME"])
    candidates.append(INSTALL_DIR)
    seen = set()
    for d in candidates:
        d = os.path.normpath(d)
        if d in seen or not os.path.isdir(d):
            continue
        seen.add(d)
        cfg = load_config(d)
        names = list(CLI_CANDIDATES)
        custom = str(cfg.get("程序路径", "")).strip()
        if custom and custom not in names:
            names.append(custom)
        cli = next((os.path.join(d, n) for n in names
                    if os.path.isfile(os.path.join(d, n))), "")
        ff = os.path.join(d, "ffmpeg.exe")
        if cli and os.path.isfile(ff):
            ensure_no_update(d)      # 关闭内核联网自更新，免去每次 ~60s 的超时
            return d, cli, ff, cfg
    raise RuntimeError(
        "未找到 N_m3u8DL-RE.exe / N_m3u8DL-CLI_v3.0.2.exe / ffmpeg.exe。\n"
        "请把本程序放在原 M3U8Downloader 目录，或设置环境变量 M3U8DL_HOME 指向该目录。"
    )


def normalize_source(raw):
    """网络地址原样传入；本地文件转为绝对路径"""
    s = raw.strip().strip('"').strip("'")
    if re.match(r"^(https?|ftp)://", s, re.I):
        return s
    p = os.path.abspath(os.path.expanduser(os.path.expandvars(s)))
    if not os.path.isfile(p):
        raise ValueError(f"本地 m3u8 文件不存在：{p}")
    return p


def title_from_source(src, idx):
    if re.match(r"^(https?|ftp)://", src, re.I):
        name = src.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    else:
        name = os.path.basename(src)
    name = os.path.splitext(name)[0]
    return safe_name(name, f"video_{idx:02d}_{dt.datetime.now():%H%M%S}")


# ---------------------------------------------------------------- 进度显示
class Progress:
    def __init__(self, tag):
        self.tag = tag
        self.tty = isatty()
        self.bucket = -1
        self.last = 0.0

    def update(self, pct, extra=""):
        pct = max(0.0, min(100.0, float(pct)))
        self.last = pct
        if self.tty:
            width = 26
            n = int(width * pct / 100)
            bar = "=" * n + "." * (width - n)
            line = f"\r[{self.tag}] {pct:6.2f}% |{bar}| {extra}"
            sys.stdout.write(line.ljust(110))
            sys.stdout.flush()
        else:
            b = int(pct // 10)
            if b > self.bucket and pct > 0:
                self.bucket = b
                say(f"[{self.tag}] {pct:.1f}% {extra}")

    def done(self, ok, extra=""):
        label = "完成" if ok else "失败"
        if self.tty:
            sys.stdout.write(f"\r[{self.tag}] {label} {extra}".ljust(110) + "\n")
            sys.stdout.flush()
        else:
            say(f"[{self.tag}] {label} {extra}")


# ---------------------------------------------------------------- 日志
class TaskLog:
    def __init__(self, path, header):
        self.path = path
        with open(path, "w", encoding="utf-8-sig") as f:
            f.write("\n".join(header) + "\n\n")

    def write(self, line):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(str(line).rstrip("\n") + "\n")

    def event(self, msg, level="NORMAL"):
        self.write(f"{hms()} / ({level}) {msg}")


def write_ffreport(path, cmd, stderr_text, started, note=""):
    """复刻 ffmpeg 的 FFREPORT 报告（无需 FFREPORT 环境变量，兼容中文路径）"""
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(f"ffmpeg started on {started:%Y-%m-%d} at {started:%H:%M:%S}\n")
        f.write(f'Report written to "{path.replace(chr(92), "/")}"\n')
        f.write("Command line:\n" + fmt_cmd(cmd) + "\n\n")
        f.write(stderr_text.strip() + "\n")
        if note:
            f.write(f"\nResult: {note}\n")


# ---------------------------------------------------------------- 外挂字幕
def _http_get(url, cfg=None, timeout=12):
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    for pair in str((cfg or {}).get("请求头", "")).strip().split("|"):
        key, sep, val = pair.partition(":")
        if sep and key.strip():
            headers[key.strip()] = val.strip()
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(
        req, timeout=timeout, context=ssl._create_unverified_context()
    ) as resp:
        return resp.read()


def _resolve_ref(base, ref):
    """解析 playlist 中的相对 URI：网络用 urljoin，本地用同目录绝对路径。"""
    if is_network_url(base):
        return urllib.parse.urljoin(base, ref)
    if re.match(r"^(https?|ftp)://", ref, re.I):
        return ref
    return os.path.normpath(os.path.join(os.path.dirname(base), ref.replace("/", os.sep)))


def _read_source(src, cfg=None, timeout=12):
    if is_network_url(src):
        return _http_get(src, cfg, timeout).decode("utf-8-sig", "replace")
    with open(src, "r", encoding="utf-8-sig", errors="replace") as f:
        return f.read()


def subtitle_tracks(src, cfg=None):
    """解析 master playlist 的 #EXT-X-MEDIA:TYPE=SUBTITLES，返回 [(语言, 绝对地址)]。"""
    try:
        text = _read_source(src, cfg)
    except Exception:
        return []
    tracks = []
    for line in text.splitlines():
        if not line.startswith("#EXT-X-MEDIA") or "TYPE=SUBTITLES" not in line.upper():
            continue
        m = re.search(r'URI="([^"]+)"', line, re.I)
        if not m:
            continue
        lm = re.search(r'LANGUAGE="([^"]+)"', line, re.I)
        tracks.append(((lm.group(1) if lm else "und").strip(), _resolve_ref(src, m.group(1))))
    return tracks


def _merge_webvtt(parts):
    """WebVTT 分片合并：保留首片 WEBVTT 头，去掉后续片的头与时间戳映射。"""
    chunks = []
    for i, text in enumerate(parts):
        lines = [l for l in text.replace("\ufeff", "").splitlines()
                 if not l.strip().startswith("X-TIMESTAMP-MAP")]
        if i > 0:
            lines = [l for l in lines if not l.strip().startswith("WEBVTT")]
        body = "\n".join(lines).strip()
        if body:
            chunks.append(body)
    return "\n\n".join(chunks) + "\n" if chunks else ""


def download_subtitles(src, cache_dir, title, cfg, log, timeout=12):
    """下载并合并外挂字幕轨到缓存目录；单轨失败只告警，不影响视频下载合并。"""
    made = []
    # 本地 m3u8 同目录下的同名字幕文件
    if not is_network_url(src):
        stem = os.path.splitext(src)[0]
        for ext in SUBTITLE_EXTS:
            p = stem + ext
            try:
                if os.path.isfile(p) and os.path.getsize(p) > 0:
                    dst = unique_path(os.path.join(cache_dir, os.path.basename(p)))
                    shutil.copy2(p, dst)
                    made.append(dst)
                    log.event(f"发现本地同名字幕: {os.path.basename(p)}")
            except OSError:
                continue
    tracks = subtitle_tracks(src, cfg)
    if not tracks:
        return made
    log.event(f"发现 {len(tracks)} 条外挂字幕轨，开始下载")
    for k, (lang, uri) in enumerate(tracks, 1):
        try:
            text = _read_source(uri, cfg, timeout)
            if "#EXTM3U" in text or "#EXTINF" in text:
                refs = [l.strip() for l in text.splitlines()
                        if l.strip() and not l.startswith("#")]
                urls = [_resolve_ref(uri, r) for r in refs]
                with ThreadPoolExecutor(max_workers=8) as pool:
                    blobs = list(pool.map(lambda u: _read_source(u, cfg, timeout), urls))
                blobs = [b for b in blobs if b.strip() and ("-->" in b[:400])]
                content = _merge_webvtt(blobs)
                ext = ".vtt"
            else:
                content = text
                ext = ".srt" if not text.lstrip().startswith("WEBVTT") else ".vtt"
            if not content.strip():
                raise ValueError("字幕内容为空")
            suffix = "" if len(tracks) == 1 else f".{lang or k}"
            path = unique_path(os.path.join(
                cache_dir, f"{safe_name(title, 'task')}{suffix}{ext}"
            ))
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            log.event(f"字幕轨 [{lang}] 已保存: {os.path.basename(path)} ({len(content)} 字节)")
            made.append(path)
        except Exception as e:
            log.event(f"字幕轨 [{lang}] 下载失败（不影响视频）: {type(e).__name__}: {e}", "ERROR")
    return made


# ---------------------------------------------------------------- 下载内核
def is_network_url(src):
    return str(src).strip().lower().startswith(("http://", "https://"))


# ------------------------------------------------- 播放列表预解析 / 自动选档
# 目的：
#   1) 自行解析 master，按码率选最高可用档，避免 CLI 遇到「同组多音轨」时
#      弹出 "Please Select ... Enter Number"；stdin=DEVNULL 会让它抛「句柄无效」
#      却仍返回退出码 0，最终 0 分片。
#   2) 下探子播放列表：404 / 无分片 / 无 ENDLIST 提前判失败并给出理由，
#      不再让内核崩在 "0 不是有效的 Interval 值"。
#   3) 生成「单码率 + 每组单条 MEDIA」的精简 master 交给内核，既消灭交互，
#      又保留音轨/字幕轨声明（无 URI 的内嵌轨则去掉组引用，避免 file:// 误判）。
_ATTR_RE = re.compile(r'([A-Za-z0-9-]+)=("[^"]*"|[^,]+)')
# Dolby Vision / AV1 / VP9 或 HEVC 的 DV profile（.H150./.H120.）CLI 3.0.2 下不了
_UNSUPPORTED_VIDEO = re.compile(r"(dvh[1e]|dva1|dvav|av01|vp09|\.H1\d0\.)", re.I)
MAX_CHILD_PROBES = 5                  # 最多下探 5 个码率档，避免 100 档全部请求


def video_supported(attrs):
    """是否属于 CLI 3.0.2 + ffmpeg 可直接处理的档位；HDR(PQ/HLG) 也降权。"""
    if _UNSUPPORTED_VIDEO.search(str(attrs.get("CODECS", ""))):
        return False
    if str(attrs.get("VIDEO-RANGE", "")).strip().upper() in ("PQ", "HLG"):
        return False
    return True


def http_headers(cfg=None):
    """统一请求头（config.txt 的「请求头」用 | 分隔 key:value）"""
    headers = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "identity"}
    for pair in str((cfg or {}).get("请求头", "")).strip().split("|"):
        key, sep, val = pair.partition(":")
        if sep and key.strip():
            headers[key.strip()] = val.strip()
    return headers


def parse_attrs(line):
    attrs = {}
    for k, v in _ATTR_RE.findall(line):
        attrs[k.upper()] = v.strip().strip('"')
    return attrs


def _bandwidth(attrs):
    for key in ("BANDWIDTH", "AVERAGE-BANDWIDTH"):
        try:
            return int(str(attrs.get(key, "0")).split(",", 1)[0])
        except (TypeError, ValueError):
            continue
    return 0


def _resolution(attrs):
    m = re.search(r"(\d+)x(\d+)", str(attrs.get("RESOLUTION", "")))
    return int(m.group(1)) * int(m.group(2)) if m else 0


def probe_child(url, cfg=None, timeout=12):
    """对子播放列表做一次 HEAD/GET 校验，返回 (ok, status, text)。"""
    if is_network_url(url):
        try:
            req = urllib.request.Request(url, headers=http_headers(cfg))
            with urllib.request.urlopen(
                req, timeout=timeout, context=ssl._create_unverified_context()
            ) as resp:
                status = getattr(resp, "status", 200)
                if not 200 <= status < 400:
                    return False, f"HTTP {status}", ""
                return (True, f"HTTP {status}",
                        resp.read(4 << 20).decode("utf-8-sig", "replace"))
        except Exception as e:
            code = getattr(e, "code", None)
            return False, (f"HTTP {code}" if code else f"{type(e).__name__}: {e}"), ""
    try:
        if not os.path.isfile(url):
            return False, "文件不存在", ""
        with open(url, "r", encoding="utf-8-sig", errors="replace") as f:
            return True, "OK", f.read()
    except Exception as e:
        return False, f"{type(e).__name__}: {e}", ""


def analyze_playlist(src, cfg=None, timeout=12):
    """读取并下探播放列表；判定标准是「能下探到有效分片」，而非 HTTP 200。"""
    info = {
        "ok": False, "detail": "", "reason": "", "retryable": False,
        "is_master": False, "variants": [], "media": [],
        "selected": src, "selected_line": "", "selected_attrs": {},
        "select_reason": "", "child_text": "", "child_status": "",
        "segment_count": 0, "is_vod": True, "has_map": False,
        "playlist_url": src, "tried_children": [],
    }
    try:
        text = _read_source(src, cfg, timeout)
    except Exception as e:
        code = getattr(e, "code", None)
        info["detail"] = f"HTTP {code}" if code else f"{type(e).__name__}: {e}"
        info["reason"] = (f"播放列表返回 HTTP {code}（可能被 WAF 拒绝）" if code
                          else f"无法读取播放列表：{info['detail']}")
        info["retryable"] = True
        return info

    lines = [l.strip() for l in text.splitlines()]
    if "#EXT" not in text[:400]:
        info["detail"] = "非 m3u8 内容"
        info["reason"] = "响应不是 m3u8 播放列表"
        info["retryable"] = True
        return info

    variants = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF"):
            continue
        attrs = parse_attrs(line.partition(":")[2])
        uri = next((x for x in lines[i + 1:i + 5] if x and not x.startswith("#")), "")
        if uri:
            variants.append((attrs, _resolve_ref(src, uri), line))
    media = [l for l in lines if l.startswith("#EXT-X-MEDIA")]

    if variants:
        info["is_master"] = True
        info["variants"] = variants
        info["media"] = media
        ordered = sorted(variants, key=lambda v: (_bandwidth(v[0]), _resolution(v[0])),
                         reverse=True)
        supported = [v for v in ordered if video_supported(v[0])]
        skipped = len(ordered) - len(supported)
        ids = {id(v) for v in supported}
        ordered = supported + [v for v in ordered if id(v) not in ids]
        chosen = None
        for attrs, uri, raw_line in ordered[:MAX_CHILD_PROBES]:
            ok, status, child = probe_child(uri, cfg, timeout)
            info["tried_children"].append(f"{status} {uri}")
            if ok and "#EXT" in child[:400]:
                chosen = (attrs, uri, raw_line, child, status)
                break
        if not chosen:
            info["detail"] = f"子播放列表不可用（已下探 {len(info['tried_children'])} 档）"
            info["reason"] = ("master 中所有下探的码率档子播放列表均不可访问："
                              + "; ".join(info["tried_children"][:3]))
            info["retryable"] = any(
                re.search(r"HTTP (403|408|429|5\d\d)", s) for s in info["tried_children"])
            return info
        attrs, uri, raw_line, child, status = chosen
        info.update({
            "selected": uri, "selected_line": raw_line, "selected_attrs": attrs,
            "child_text": child, "child_status": status,
            "select_reason": (f"自动选择最高可用码率 {_bandwidth(attrs)} bps / "
                              f"{attrs.get('RESOLUTION', '?')}"
                              + (f"（跳过 {skipped} 个 DV/HDR 等不支持档）" if skipped else "")
                              + f"，子列表 {status}"),
        })
    else:
        info["child_text"] = text
        info["child_status"] = "OK"
        info["selected"] = src

    child = info["child_text"]
    info["segment_count"] = sum(
        1 for l in child.splitlines() if l.strip() and not l.startswith("#"))
    info["has_map"] = "#EXT-X-MAP" in child
    info["is_vod"] = ("#EXT-X-ENDLIST" in child) or ("PLAYLIST-TYPE:VOD" in child)
    if not info["is_vod"]:
        info["detail"] = "LIVE"
        info["reason"] = "不支持直播流（播放列表缺少 #EXT-X-ENDLIST / PLAYLIST-TYPE:VOD）"
        return info
    if info["segment_count"] == 0:
        info["detail"] = "EMPTY"
        info["reason"] = "播放列表总分片数为 0（失效直播流或空列表）"
        return info
    info["ok"] = True
    info["detail"] = (f"{info['child_status']} / {len(variants)} 档 / 下探 "
                      f"{info['segment_count']} 个分片" if variants
                      else f"播放列表 {info['segment_count']} 个分片")
    return info


def build_minimal_master(info, out_dir):
    """生成「单码率 + 每组单条 MEDIA」的精简 master，交给内核消灭交互菜单。"""
    if not info["is_master"] or not info["selected"]:
        return ""
    attrs, si_line = info["selected_attrs"], info["selected_line"]
    keep, drop = [], []
    for key in ("AUDIO", "SUBTITLES", "VIDEO", "CLOSED-CAPTIONS"):
        gid = attrs.get(key, "")
        if not gid:
            continue
        cand = [l for l in info["media"]
                if f'GROUP-ID="{gid}"' in l and re.search(r"TYPE=" + key, l, re.I)]
        if not cand:
            drop.append(key)
            continue
        cand.sort(key=lambda l: (("DEFAULT=YES" in l.upper()) * 4
                                 + ("AUTOSELECT=YES" in l.upper()) * 2
                                 + (1 if 'URI="' in l else 0)), reverse=True)
        line = cand[0]
        m = re.search(r'URI="([^"]+)"', line)
        if not m:
            drop.append(key)          # 无 URI = 音轨/字幕已内嵌在视频流中
            continue
        keep.append(line.replace(m.group(1),
                                 _resolve_ref(info["playlist_url"], m.group(1))))
    for key in drop:
        si_line = re.sub(rf',?\s*{re.escape(key)}="[^"]*"', "", si_line, flags=re.I)
    path = unique_path(os.path.join(out_dir, "_selected_master.m3u8"))
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(["#EXTM3U", "#EXT-X-VERSION:7"] + keep
                              + [si_line, info["selected"]]) + "\n")
        return path
    except OSError:
        return ""


def check_network_source(src, cfg=None, timeout=12):
    """兼容旧调用：返回 (可用, 摘要, 结构化信息)。"""
    info = analyze_playlist(src, cfg, timeout)
    return info["ok"], info["detail"], info


class ThreadGovernor:
    """按内核输出里的超时/重试信号动态调整并发档位（32/16 -> 16/8 -> 8/4 -> 4/2）。"""
    def __init__(self, cfg):
        try:
            hi = int(str(cfg.get("最大线程", "32")).strip() or 32)
            lo = int(str(cfg.get("最小线程", "16")).strip() or 16)
        except ValueError:
            hi, lo = 32, 16
        self.tiers = [(hi, lo), (max(16, hi // 2), max(8, lo // 2)), (8, 4), (4, 2)]
        self.idx = 0
        self.good = 0

    def current(self):
        return self.tiers[self.idx]

    def observe(self, stats, success):
        fails = int(stats.get("timeout", 0))
        total = max(int(stats.get("total", 0)), 1)
        before = self.current()
        # 只有「超时/重试」信号才是并发过高的证据；404 / 直播流等确定性
        # 失败与线程数无关，不调整档位（也避免出现「0 次超时却降并发」）。
        if fails >= 4 or fails / total > 0.12 or (not success and fails > 0):
            if self.idx < len(self.tiers) - 1:
                self.idx += 1
                self.good = 0
                return before, self.current(), f"超时/重试 {fails} 次（{total} 分片），降低并发"
            self.good = 0
            return None, None, ""
        if not success:
            self.good = 0
            return None, None, ""
        self.good += 1
        if self.good >= 2 and self.idx > 0:
            self.idx -= 1
            self.good = 0
            return before, self.current(), "连续任务无超时，恢复并发"
        return None, None, ""


def retry_delay(attempt, detail=""):
    """指数退避：连接失败 15/30/60s；403/429 这类 WAF 瞬时限流用 2/4/8s 快速避让。"""
    base = min(CONNECT_GAP, 2) if re.search(r"HTTP (403|429)", detail) else CONNECT_GAP
    return min(base * (2 ** (attempt - 1)), 60)


# ---------------------------------------------------------------- N_m3u8DL-RE 主下载层
def find_re(tool_dir=""):
    """定位内置 / 外置的 N_m3u8DL-RE.exe；frozen 时优先 exe 内释放的资源目录。"""
    if DISABLE_RE:
        return ""
    candidates = []
    meipass = getattr(sys, "_MEIPASS", None)
    if getattr(sys, "frozen", False) and meipass:
        candidates.append(meipass)
    candidates += [tool_dir, app_dir(), os.environ.get("M3U8DL_HOME", ""), INSTALL_DIR]
    seen = set()
    for d in candidates:
        if not d or not os.path.isdir(d):
            continue
        d = os.path.normpath(d)
        if d in seen:
            continue
        seen.add(d)
        path = os.path.join(d, RE_NAME)
        if os.path.isfile(path):
            return path
    return ""


_RE_VER_CACHE = {}


def re_version(re_exe):
    if not re_exe:
        return "-"
    if re_exe in _RE_VER_CACHE:
        return _RE_VER_CACHE[re_exe]
    ver = "未知"
    try:
        r = subprocess.run([re_exe, "--version"], capture_output=True,
                           timeout=15, stdin=subprocess.DEVNULL)
        text = decode_console(r.stdout, "utf-8") + decode_console(r.stderr, "utf-8")
        m = re.search(r"\d+\.\d+\.\d+[^\s]*", text)
        if m:
            ver = m.group(0)
    except Exception:
        pass
    _RE_VER_CACHE[re_exe] = ver
    return ver


def re_headers(cfg):
    """config.txt 的 Key:Value|Key:Value 转成 RE 的多个 -H 参数。"""
    out = []
    for pair in str(cfg.get("请求头", "")).split("|"):
        key, sep, val = pair.partition(":")
        if sep and key.strip():
            out += ["-H", f"{key.strip()}: {val.strip()}"]
    return out


def find_re_outputs(out_dir):
    """从 RE 输出目录挑出最终视频（优先 *.MUX.mp4）与全部外挂字幕。"""
    vids, subs = [], []
    if not os.path.isdir(out_dir):
        return "", []
    for name in os.listdir(out_dir):
        path = os.path.join(out_dir, name)
        if not os.path.isfile(path):
            continue
        low = name.lower()
        if low.endswith(SUBTITLE_EXTS):
            if os.path.getsize(path) > 0:
                subs.append(path)
        elif low.endswith((".mp4", ".mkv")):
            vids.append(path)
    if not vids:
        return "", sorted(subs)
    muxed = [p for p in vids if "mux" in os.path.basename(p).lower()]
    return max(muxed or vids, key=os.path.getsize), sorted(subs)


def export_re_subtitles(save_dir, title, subs):
    """保留 RE 自带的语言后缀（标题.en.srt），避免多语言字幕互相撞名。"""
    out = []
    stem = safe_name(title, "video")
    for src in subs:
        name = os.path.basename(src)
        if not name.startswith(stem):
            name = stem + "_" + name
        dst = unique_path(os.path.join(save_dir, name))
        try:
            shutil.copy2(src, dst)
            out.append(dst)
        except OSError:
            continue
    return out


def build_re_cmd(re_exe, src, task_dir, title, cfg, threads, ffmpeg, re_log, info=None):
    """一条命令完成：解析 -> 自动选轨 -> 下载 -> 合并分片 -> ffmpeg 混流 mp4。"""
    work = os.path.join(task_dir, RE_WORK_DIR)
    tmp_dir = os.path.join(work, "tmp")
    out_dir = os.path.join(work, "out")
    os.makedirs(tmp_dir, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)
    cmd = [
        re_exe, src,
        "--auto-select",                # 自动选所有类型最佳轨道，彻底消灭交互菜单
        "--disable-update-check",       # 关闭联网版本检查
        "--no-ansi-color",
        "--ui-language", "en-US",
        "--tmp-dir", tmp_dir,
        "--save-dir", out_dir,
        "--save-name", safe_name(title, "video"),
        "--ffmpeg-binary-path", ffmpeg,
        "--log-file-path", re_log,      # 所有日志落在 保存目录\ffmpeg temp
        "--no-date-info",
        # 不传 --del-after-done：RE 默认仅在成功后自行删除 tmp 分片，
        # 失败时完整保留分片供归档；成功清理也从 200+ 文件降到个位数。
        "--sub-format", "SRT",
        "-M", "format=mp4:keep=true",  # 混流独立音轨/字幕；保留轨道文件以便单输出字幕
    ]
    # 只有确实存在 DV/HDR 等不支持档时才降权 SDR，避免误过滤普通 master
    if info and any(not video_supported(v[0]) for v in info.get("variants", [])):
        cmd += ["-sv", "range=SDR:for=best"]
    try:
        n = max(1, min(int(str(cfg.get("最大线程", "16")).strip() or 16), 16))
        if threads:
            n = max(1, min(int(threads[0]), 16))
        retry = max(1, min(int(str(cfg.get("重试次数", "3")).strip() or 3), 5))
    except ValueError:
        n, retry = 16, 3
    # 不把 CLI 的 10s 超时套到 RE：RE 默认 100s，更适合限速 CDN 的大分片
    cmd += ["--thread-count", str(n), "--download-retry-count", str(retry)]
    cmd += re_headers(cfg)
    proxy = str(cfg.get("代理", "")).strip()
    if proxy:
        cmd += ["--custom-proxy", proxy]
    if str(cfg.get("不使用系统代理", "0")).strip() == "1":
        cmd += ["--use-system-proxy", "false"]
    return cmd, out_dir


_RE_LOG_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}\.\d{3})\s+(INFO|WARN|ERROR)\s*:\s*([^\r\n]{0,240})")


def _handle_re_line(raw, log, progress, stats, tail):
    """解析 RE 的进度条与阶段行，返回 (总分片数, 源编码, 百分比)。"""
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).strip()
    if not line:
        return 0, "", 0.0
    total, codec, pct = 0, "", 0.0
    if stats is not None and re.search(
            r"(超时|TimeOut|Retry|重试|下载失败|请求失败|ERROR)", line, re.I):
        stats["timeout"] = stats.get("timeout", 0) + 1
    m = re.search(r"(\d+)\s*/\s*(\d+)\s+([\d.]+)%", line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        pct = float(m.group(3))
        progress.update(pct, f"{done}/{total}")
    sm = re.search(r"(\d+)\s+Segments\b", line, re.I)
    if sm:
        total = max(total, int(sm.group(1)))
    cm = re.search(r"\b(h264|hevc|h265|av01|av1|vp09|vp9)\b", line, re.I)
    if cm:
        codec = cm.group(1).lower()
    # RE 用 \r 刷新进度，多个时间戳会粘在同一段里，先按时间戳切开
    parts = re.split(
        r"(?=\d{2}:\d{2}:\d{2}\.\d{3}\s+(?:INFO|WARN|ERROR)\s*:)", line)
    for part in parts:
        lm = _RE_LOG_RE.search(part)
        if not lm:
            continue
        label = "ERROR" if lm.group(2) == "ERROR" else "NORMAL"
        entry = f"{lm.group(1)} / ({label}) [RE] {lm.group(3).strip()}"
        log.write(entry)
        tail.append(entry)
    if len(tail) > 80:
        del tail[:-80]
    return total, codec, pct


def run_re_download(re_exe, cmd, tool_dir, log):
    """启动 RE 主内核，实时解析 x/y (p%)；以磁盘产物判定成功与否。"""
    log.event(f"启动主下载内核: {fmt_cmd(cmd)}")
    progress = Progress("下载")
    stats = {"timeout": 0, "total": 0}
    total, codec, last_pct, tail = 0, "", 0.0, []
    env = dict(os.environ)
    env.pop("FFREPORT", None)
    proc = None
    try:
        proc = subprocess.Popen(
            cmd, cwd=tool_dir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            bufsize=0, env=env,
        )
        buf = bytearray()
        while True:
            byte = proc.stdout.read(1)
            if not byte:
                if buf:
                    _handle_re_line(decode_console(bytes(buf)), log, progress, stats, tail)
                break
            if byte in (b"\r", b"\n"):
                if buf:
                    n, c, p = _handle_re_line(
                        decode_console(bytes(buf)), log, progress, stats, tail)
                    total = n or total
                    codec = c or codec
                    last_pct = max(last_pct, p)
                    buf.clear()
            else:
                buf += byte
        rc = proc.wait()
        if last_pct < 100 and rc == 0:
            progress.update(100.0, "下载/混流结束")
        progress.done(rc == 0, f"{total or '?'} 个分片, 退出码 {rc}")
        log.event(f"主内核退出，退出码 {rc}，超时/重试信号 {stats['timeout']} 次")
        stats["total"] = total
        return rc, total, codec, stats, "\n".join(tail[-40:])
    except KeyboardInterrupt:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, "用户中断")
        log.event("主内核下载被用户中断", "ERROR")
        stats["total"] = total
        return -1, total, codec, stats, "\n".join(tail[-40:])
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, str(e))
        log.event(f"主内核异常: {e}", "ERROR")
        stats["total"] = total
        return -1, total, codec, stats, f"{type(e).__name__}: {e}"


def build_cli_cmd(cli, src, work_dir, title, cfg, threads=None):
    cmd = [cli, src, "--workDir", work_dir, "--saveName", title, "--noMerge"]
    # 隐藏的默认项：全部来自 config.txt，不向用户暴露；
    # threads 由 ThreadGovernor 按失败率动态覆盖最大/最小线程。
    pairs = [
        ("最大线程", "--maxThreads", threads[0] if threads else None),
        ("最小线程", "--minThreads", threads[1] if threads else None),
        ("重试次数", "--retryCount", None),
        ("超时秒数", "--timeOut", None),
    ]
    for key, opt, override in pairs:
        val = str(override if override is not None else cfg.get(key, "")).strip()
        if val.isdigit():
            cmd += [opt, val]
    for key, opt in (("停止速度", "--stopSpeed"), ("最大速度", "--maxSpeed")):
        val = str(cfg.get(key, "0")).strip()
        if val.isdigit() and int(val) > 0:
            cmd += [opt, val]
    for key, opt in (("代理", "--proxyAddress"), ("请求头", "--headers")):
        val = str(cfg.get(key, "")).strip()
        if val:
            cmd += [opt, val]
    flags = [
        ("不使用系统代理", "--noProxy"),
        ("仅合并音频", "--enableAudioOnly"),
        ("二进制合并", "--enableBinaryMerge"),
        ("仅解析模式", "--enableParseOnly"),
        ("不写入日期", "--disableDateInfo"),
        ("删除临时文件", "--enableDelAfterDone"),
        ("MP4混流边下边看", "--enableMuxFastStart"),
    ]
    for key, opt in flags:
        if str(cfg.get(key, "0")).strip() == "1":
            cmd.append(opt)
    return cmd


def run_download(cli_exe, cmd, tool_dir, log):
    """启动 CLI，实时解析 Progress: x/y (p%)。

    返回 (成功, 总片数, 源视频编码, 超时/重试统计)；统计用于线程数自适应。
    """
    log.event(f"启动下载内核: {fmt_cmd(cmd)}")
    progress = Progress("下载")
    src_codec = ""
    total = 0
    last_pct = 0.0
    stats = {"timeout": 0, "total": 0}
    env = dict(os.environ)
    env.pop("FFREPORT", None)     # 合并由本脚本控制，清掉外部残留的 FFREPORT 设置
    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=tool_dir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            env=env,
        )
        buf = bytearray()
        while True:
            byte = proc.stdout.read(1)
            if not byte:
                if buf:
                    _handle_cli_line(decode_console(bytes(buf)), log, progress, stats)
                break
            if byte in (b"\r", b"\n"):
                if buf:
                    line = _handle_cli_line(decode_console(bytes(buf)), log, progress, stats)
                    if line:
                        if line[0]:
                            total = line[0]
                        if line[1]:
                            src_codec = line[1]
                        last_pct = max(last_pct, line[2])
                    buf.clear()
            else:
                buf += byte
        rc = proc.wait()
        if last_pct < 100 and rc == 0:
            progress.update(100.0, "分片下载结束")
        progress.done(rc == 0, f"{total or '?'} 个分片, 退出码 {rc}")
        log.event(f"下载内核退出，退出码 {rc}，超时/重试信号 {stats['timeout']} 次")
        stats["total"] = total
        return rc == 0, total, src_codec, stats
    except KeyboardInterrupt:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, "用户中断")
        log.event("下载被用户中断", "ERROR")
        stats["total"] = total
        return False, total, src_codec, stats
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, str(e))
        log.event(f"下载异常: {e}", "ERROR")
        stats["total"] = total
        return False, total, src_codec, stats


def _handle_cli_line(raw, log, progress, stats=None):
    """返回 (总分片数, 源编码, 百分比)；stats 累计超时/重试信号供线程自适应"""
    line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw).strip()
    if not line:
        return None
    total, codec, pct = 0, "", 0.0
    if stats is not None and re.search(
            r"(超时|TimeOut|AUTO RETRY|重试中|Retrying|下载失败|请求失败|连接失败)",
            line, re.I):
        stats["timeout"] = stats.get("timeout", 0) + 1

    m = re.search(r"Progress:\s*(\d+)\s*/\s*(\d+)\s*\(([\d.]+)%\)\s*(?:--\s*([^\s(]+)\s*/\s*([^\s(]+))?", line)
    if m:
        done, total = int(m.group(1)), int(m.group(2))
        pct = float(m.group(3))
        extra = ""
        if m.group(4):
            extra = f"{m.group(4)} / {m.group(5)}"
        sm = re.search(r"\(([\d.]+\s*\w+/s)", line)
        if sm:
            extra += f"  {sm.group(1)}"
        progress.update(pct, f"{done}/{total} {extra}")
        return total, codec, pct

    cm = re.search(r"Video\s+(h264|hevc|h265|av1|vp9|mpeg2video)", line, re.I)
    if cm:
        codec = cm.group(1).lower()

    # 阶段行按原日志格式 "HH:MM:SS.mmm / (NORMAL) ..." 落盘
    clean = re.sub(r"^\s+", "", line)
    if re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}\s", clean):
        level = "ERROR" if re.search(r"失败|错误|error|exception", clean, re.I) else "NORMAL"
        log.write(re.sub(r"^(\d{2}:\d{2}:\d{2}\.\d{3})\s+", rf"\1 / ({level}) ", clean))
    elif "PID" in clean or re.match(r"^\d{2}:\d{2}:\d{2}", clean):
        log.event(clean)
    return total, codec, pct


def _first_number(text):
    m = re.search(r"(\d+)", text)
    return int(m.group(1)) if m else 0


def find_segments(task_dir):
    """递归收集分片，按 Part_N / 序号排序；跳过失败缓存归档，避免混入往批分片。"""
    segs = []
    for dirpath, dirs, files in os.walk(task_dir):
        dirs[:] = [d for d in dirs if not is_failed_cache(d)]
        for name in files:
            if name.lower().endswith(".ts"):
                segs.append(os.path.join(dirpath, name))
    if not segs:
        return [], None
    segs.sort(key=lambda p: (
        _first_number(os.path.basename(os.path.dirname(p))),
        _first_number(os.path.basename(p)),
        p,
    ))
    return segs, os.path.dirname(segs[0])


def total_duration(task_dir):
    for dirpath, dirs, files in os.walk(task_dir):
        dirs[:] = [d for d in dirs if not is_failed_cache(d)]
        if "meta.json" not in files:
            continue
        try:
            with open(os.path.join(dirpath, "meta.json"), "r", encoding="utf-8-sig") as f:
                return float(json.load(f)["m3u8Info"]["totalDuration"])
        except Exception:
            return 0.0
    return 0.0


# ---------------------------------------------------------------- 合并内核
def verify_playable(ffmpeg, path):
    """校验输出是否为可直接播放的 HEVC/H.264 视频，而非二进制拼接垃圾。

    新版完整构建自带 ffprobe.exe 时优先用它精确读取首条视频流；
    旧精简版没有 ffprobe / null muxer，回退用 "-i <file>" 解析流信息。
    """
    if not os.path.isfile(path) or os.path.getsize(path) < 1024:
        return False, "文件不存在或过小"
    probe = ffprobe_exe(ffmpeg)
    if probe:
        try:
            r = subprocess.run(
                [probe, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
            lines = decode_console(r.stdout, "utf-8").strip().splitlines()
            codec = lines[0].strip().lower() if lines else ""
            if codec:
                if codec not in PLAYABLE_CODECS:
                    return False, f"视频编码 {codec} 不在可播放编码列表"
                return True, f"Video: {codec}"
        except Exception:
            pass                      # ffprobe 异常时继续用 ffmpeg -i 兜底
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-i", path],
                           capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
        text = decode_console(r.stdout, "utf-8") + decode_console(r.stderr, "utf-8")
    except Exception as e:
        return False, f"探测失败: {e}"
    m = re.search(r"Video:\s*([A-Za-z0-9_]+)", text)
    if not m:
        return False, "未识别到视频流（非可直接播放格式）"
    codec = m.group(1).lower()
    if codec not in PLAYABLE_CODECS:
        return False, f"视频编码 {codec} 不在可播放编码列表"
    return True, f"Video: {codec}"


def segment_kind(segs):
    """容器判定：任一分片含 moof/mdat 即为 fMP4；全为 0x47 才是 MPEG-TS。

    注意 init(!MAP.ts, 含 ftyp+moov) 常被 find_segments 排到后面，不能只看首片。
    """
    saw_fmp4 = False
    saw_ts = False
    for p in segs[:12] + segs[-3:]:
        try:
            with open(p, "rb") as f:
                head = f.read(64)
        except OSError:
            continue
        if any(k in head for k in (b"moof", b"ftyp", b"moov", b"styp")):
            saw_fmp4 = True
        elif head[:1] == b"\x47":
            saw_ts = True
    if saw_fmp4:
        return "fmp4"
    return "ts" if saw_ts else "unknown"


def split_fmp4_parts(segs):
    """把 .ts 集合分成 init(ftyp/moov) 与 fragments(moof/mdat)，fragments 按序号排序。"""
    inits, frags = [], []
    for p in segs:
        try:
            with open(p, "rb") as f:
                head = f.read(64)
        except OSError:
            continue
        # init 必须是 ftyp/moov；DASH 分片常以 styp 开头，不能误判为 init
        if b"ftyp" in head or b"moov" in head:
            inits.append(p)
        else:
            frags.append(p)
    frags.sort(key=lambda p: (_first_number(os.path.basename(p)), p))
    inits.sort(key=lambda p: (0 if "!MAP" in os.path.basename(p).upper() else 1,
                              _first_number(os.path.basename(p)), p))
    return inits, frags


def find_ext_map(task_dir):
    """从内核生成的 meta.json 读 EXT-X-MAP（init 分片地址 + 字节范围）。"""
    for dirpath, dirs, files in os.walk(task_dir):
        dirs[:] = [d for d in dirs if not is_failed_cache(d)]
        if "meta.json" not in files:
            continue
        try:
            with open(os.path.join(dirpath, "meta.json"), "r",
                      encoding="utf-8-sig") as f:
                return str(json.load(f).get("m3u8Info", {}).get("extMAP", "") or "")
        except Exception:
            return ""
    return ""


def _fetch_init_bytes(ext_map, cfg=None, timeout=15):
    """缓存里没有 !MAP.ts 时，按 EXT-X-MAP 的 byterange 取回 init(ftyp+moov)。"""
    if not ext_map or "|" not in ext_map:
        return b""
    url, _, rng = ext_map.partition("|")
    m = re.search(r"(\d+)\s*@\s*(\d+)", rng)
    if not m:
        return b""
    length, offset = int(m.group(1)), int(m.group(2))
    if is_network_url(url):
        headers = http_headers(cfg)
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=timeout, context=ssl._create_unverified_context()
            ) as r:
                return r.read(length)
        except Exception:
            return b""
    try:
        with open(url, "rb") as f:
            f.seek(offset)
            return f.read(length)
    except OSError:
        return b""


def assemble_fmp4(segs, task_dir, cfg, log):
    """init + fragments 顺序拼成完整 fMP4，再交给 ffmpeg remux。

    这等价于 concat: 协议的字节级拼接，但不受命令行长度限制，因此 100+
    分片也不会降级到 concat demuxer（后者对 fMP4 会报 trex/trun 错误）。
    """
    inits, frags = split_fmp4_parts(segs)
    if not frags:
        return ""
    init_bytes = b""
    if not inits:
        init_bytes = _fetch_init_bytes(find_ext_map(task_dir), cfg)
        if not init_bytes:
            log.event("fMP4 缓存缺少 init(ftyp+moov)，且 EXT-X-MAP 不可获取", "ERROR")
            return ""
        log.event("缓存无 !MAP.ts，已按 EXT-X-MAP byterange 取回 init 分片")
    raw = os.path.join(task_dir, "_fmp4_raw.mp4")
    try:
        with open(raw, "wb") as out:
            if init_bytes:
                out.write(init_bytes)
            for p in (inits[:1] if not init_bytes else []) + frags:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out, 4 << 20)
    except OSError as e:
        log.event(f"fMP4 拼接失败: {e}", "ERROR")
        return ""
    log.event(f"fMP4 init+fragments 拼接完成: {len(inits[:1]) + len(frags)} 个分片 -> "
              f"{os.path.basename(raw)} ({human_size(os.path.getsize(raw))})")
    return raw


def probe_encoders(ffmpeg):
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                           capture_output=True, timeout=20)
        text = decode_console(r.stdout, "utf-8") + decode_console(r.stderr, "utf-8")
    except Exception:
        return set()
    names = set()
    for line in text.splitlines():
        m = re.match(r"^\s*[VAS][.A-Z]{5,7}\s+(\S+)", line)
        # 过滤 " V..... = Video" 之类的图例说明行；兼容新旧 ffmpeg 的标志位宽度
        if m and re.fullmatch(r"[A-Za-z0-9_.-]+", m.group(1)):
            names.add(m.group(1))
    return names


def probe_bsfs(ffmpeg):
    """探测可用 bitstream filter；某些新/精简构建已移除 aac_adtstoasc。"""
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-bsfs"],
                           capture_output=True, timeout=20,
                           stdin=subprocess.DEVNULL)
        text = decode_console(r.stdout, "utf-8") + decode_console(r.stderr, "utf-8")
        return {l.strip() for l in text.splitlines()
                if re.fullmatch(r"[A-Za-z0-9_.-]+", l.strip())}
    except Exception:
        return set()


def merge_plan(encoders, src_codec):
    """HEVC -> H.264 -> 二进制合并（三级降级）"""
    plan = []
    hevc = next((e for e in HEVC_ENCODERS if e in encoders), None)
    h264 = next((e for e in H264_ENCODERS if e in encoders), None)
    if hevc:
        plan.append(("HEVC", ["-c:v", hevc, "-c:a", "copy"], f"转码 {hevc}"))
    else:
        plan.append(("HEVC", None, "无可用 HEVC 编码器，无法执行，跳过"))
    if src_codec and src_codec not in ("h264", "avc"):
        if h264:
            plan.append(("H.264", ["-c:v", h264, "-c:a", "copy"], f"转码 {h264}"))
        else:
            plan.append(("H.264", ["-c", "copy"], "无 H.264 编码器，改用流复制"))
    else:
        plan.append(("H.264", ["-c", "copy"], "源为 H.264，流复制封装为 mp4（与原项目一致）"))
    return plan


def build_ffmpeg_cmd(ffmpeg, segs, seg_dir, out_path, codec_args, task_dir,
                     subtitles=(), input_path=None):
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info", "-y"]
    names = [os.path.basename(s) for s in segs]
    concat = "concat:" + "|".join(names)
    cwd = None
    if input_path:
        # fMP4 已拼成单个完整容器：直接 remux，绕开 concat demuxer 的长度/格式限制
        cmd += ["-i", os.path.abspath(input_path)]
    elif seg_dir and all(os.path.dirname(s) == seg_dir for s in segs) and len(concat) < 8000:
        cmd += ["-i", concat]
        cwd = seg_dir
    else:
        # 分片过多或跨目录：改用 concat demuxer，避免超长命令行
        list_file = os.path.join(task_dir, "_concat_list.txt")
        with open(list_file, "w", encoding="utf-8") as f:
            for s in segs:
                f.write("file '" + s.replace("\\", "/").replace("'", "'\\''") + "'\n")
        cmd += ["-f", "concat", "-safe", "0", "-i", list_file]
    # -map 是输出选项，必须放在所有 -i（含字幕输入）之后
    for sub in subtitles:
        cmd += ["-i", os.path.abspath(sub)]
    cmd += ["-map", "0:v?", "-map", "0:a?", "-map", "0:s?"]
    for i in range(len(subtitles)):
        cmd += ["-map", f"{i + 1}:s?"]
    now = dt.datetime.now().astimezone().isoformat(timespec="microseconds")
    cmd += [
        "-metadata", f"date={now}",
        "-metadata", "encoding_tool=",
        "-metadata", "title=",
        "-metadata", "copyright=",
        "-metadata", "comment=",
        "-metadata:s:a:0", "handler_name=",
        "-metadata:s:a:0", "handler=",
    ]
    cmd += codec_args
    if subtitles:
        cmd += ["-c:s", "mov_text"]     # mp4 内封字幕，-c copy 无法封装 subrip/webvtt
    # 新版 mp4 muxer 会自动处理 ADTS->ASC；旧版需显式 bsf。按实际能力选择，
    # 避免在已移除该 bsf 的构建上因未知滤镜直接失败。
    if "aac_adtstoasc" in probe_bsfs(ffmpeg):
        cmd += ["-bsf:a", "aac_adtstoasc"]
    cmd += ["-progress", "pipe:1", out_path]
    return cmd, cwd


def run_ffmpeg(ffmpeg, cmd, cwd, duration, log, tag):
    """执行 ffmpeg 并解析 -progress 输出，返回 (成功, stderr 全文)"""
    progress = Progress(tag)
    stderr_lines = []
    proc = None
    try:
        proc = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        def drain():
            for raw in proc.stderr:
                stderr_lines.append(decode_console(raw, "utf-8").rstrip())
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()

        total_us = duration * 1_000_000 if duration > 0 else 0
        for raw in proc.stdout:
            line = decode_console(raw, "utf-8").strip()
            kv = dict(re.findall(r"^(\w+)=(.*)$", line, re.M))
            if "out_time_us" in kv:
                try:
                    us = float(kv["out_time_us"])
                except ValueError:
                    us = 0
                pct = (us / total_us * 100) if total_us else 0
                progress.update(min(pct, 99.9), f"已处理 {us / 1e6:.1f}s")
            elif kv.get("progress") == "end":
                progress.update(100.0, "封装完成")
        rc = proc.wait()
        reader.join(timeout=5)
        stderr_text = "\n".join(stderr_lines)
        ok = rc == 0
        progress.done(ok, f"退出码 {rc}")
        return ok, stderr_text
    except KeyboardInterrupt:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, "用户中断")
        return False, "\n".join(stderr_lines)
    except Exception as e:
        if proc and proc.poll() is None:
            proc.kill()
        progress.done(False, str(e))
        return False, f"{e}\n" + "\n".join(stderr_lines)


def binary_merge(segs, out_path):
    """最终兜底：二进制顺序拼接分片（等价于 --enableBinaryMerge）"""
    with open(out_path, "wb") as out:
        for s in segs:
            with open(s, "rb") as f:
                shutil.copyfileobj(f, out, 1024 * 1024)
    return os.path.getsize(out_path) > 0


def do_merge(ffmpeg, task_dir, title, save_dir, src_codec, log, ffreport_path,
             subtitles=(), cfg=None):
    segs, seg_dir = find_segments(task_dir)
    if not segs:
        log.event("未找到任何 .ts 分片，合并失败", "ERROR")
        return False, "", "", "未找到任何 .ts 分片"
    kind = segment_kind(segs)
    duration = total_duration(task_dir)
    log.event(f"发现 {len(segs)} 个分片，总时长 {duration:.2f}s，"
              f"源编码 {src_codec or '未知'}，分片容器 {kind}")
    if subtitles:
        log.event(f"合并时嵌入 {len(subtitles)} 个字幕文件: "
                  + ", ".join(os.path.basename(s) for s in subtitles))
    encoders = probe_encoders(ffmpeg)
    log.event(f"ffmpeg 可用编码器: {', '.join(sorted(encoders)) or '仅 remux（无编码器）'}")
    plan = merge_plan(encoders, src_codec)

    # fMP4/CMAF：init(ftyp+moov) + fragments 字节级拼成完整 mp4 再 remux。
    # 这是 concat: 协议的等价做法，但不受命令行长度限制，100+ 分片也不会
    # 降级到 concat demuxer（后者对 fMP4 报 trex/trun 错误，产物不可播放）。
    fmp4_input = ""
    fail_reason = "全部合并方式失败"
    if kind == "fmp4":
        raw = assemble_fmp4(segs, task_dir, cfg or {}, log)
        if raw:
            playable, detail = verify_playable(ffmpeg, raw)
            if playable:
                fmp4_input = raw
                log.event(f"fMP4 拼接产物可播放（{detail}），采用 init+fragments remux")
                # 分片本身已是可播放容器，优先流复制，避免对 10 分钟 1080p 全片重编码
                plan = [("H.264", ["-c", "copy"],
                         "fMP4 完整容器，流复制 remux（最快，等价于 concat: 拼接）")] + plan
            else:
                fail_reason = f"fMP4 init+fragments 拼接产物不可播放（{detail}），常规合并亦失败"
                log.event(f"fMP4 拼接产物不可播放（{detail}），继续尝试常规合并", "ERROR")
        else:
            fail_reason = "fMP4 缓存缺少 init(ftyp+moov)，无法构造可播放容器"
            log.event("无法构造 fMP4 容器（缺少 init），继续尝试常规合并", "ERROR")

    tmp_out = os.path.join(task_dir, "_merging.mp4")
    stderr_all = []
    last_cmd = []
    final_note = "全部合并方式失败"

    for level, codec_args, reason in plan:
        log.event(f"[{level}] 合并策略: {reason}")
        if codec_args is None:
            say(f"      -> HEVC 无法执行（{reason}），自动降级")
            continue
        # 字幕合入失败时自动去掉字幕重试，保证视频本体仍可产出
        attempts = [(subtitles, "含字幕"), ((), "去字幕重试")] if subtitles else [((), "无字幕")]
        for subs, tag in attempts:
            if os.path.exists(tmp_out):
                os.remove(tmp_out)
            cmd, cwd = build_ffmpeg_cmd(ffmpeg, segs, seg_dir, tmp_out,
                                        codec_args, task_dir, subs,
                                        input_path=fmp4_input or None)
            last_cmd = cmd
            log.event(f"ffmpeg 命令({tag}): {fmt_cmd(cmd)}")
            ok, stderr_text = run_ffmpeg(ffmpeg, cmd, cwd, duration, log, f"合并-{level}")
            stderr_all.append(f"--- {level}/{tag} ---\n{stderr_text}")
            if not (ok and os.path.isfile(tmp_out) and os.path.getsize(tmp_out) > 1024):
                continue
            playable, detail = verify_playable(ffmpeg, tmp_out)
            if not playable:
                log.event(f"[{level}/{tag}] 输出校验不通过：{detail}，判为合并失败", "ERROR")
                continue
            final = unique_path(os.path.join(save_dir, title + ".mp4"))
            os.replace(tmp_out, final)
            log.event(f"合并成功（{tag}，{detail}），输出 {final} "
                      f"({human_size(os.path.getsize(final))})")
            final_note = f"{level} 合并成功（{tag}） -> {final}"
            write_ffreport(ffreport_path, cmd, "\n\n".join(stderr_all),
                           dt.datetime.now(), final_note)
            return True, final, level, f"{level} 合并成功（{tag}）"

    # 兜底：仅 MPEG-TS 分片允许二进制拼接，且产物必须通过可播放性校验
    if kind == "ts":
        log.event("ffmpeg 合并失败，尝试二进制拼接 MPEG-TS 分片", "ERROR")
        final = unique_path(os.path.join(save_dir, title + ".ts"))
        try:
            if binary_merge(segs, final):
                playable, detail = verify_playable(ffmpeg, final)
                if playable:
                    log.event(f"二进制合并成功（{detail}），输出 {final} "
                              f"({human_size(os.path.getsize(final))})")
                    final_note = f"二进制合并(.ts, 可播放) -> {final}"
                    write_ffreport(ffreport_path, last_cmd or ["(未执行 ffmpeg)"],
                                   "\n\n".join(stderr_all), dt.datetime.now(), final_note)
                    return True, final, "二进制合并(.ts)", "MPEG-TS 二进制拼接成功"
                log.event(f"二进制合并产物不可播放：{detail}，判为合并失败", "ERROR")
                # 无效产物收回缓存，随失败归档保留
                try:
                    shutil.move(final, unique_path(
                        os.path.join(task_dir, os.path.basename(final))))
                except OSError:
                    remove_any(final)
        except Exception as e:
            log.event(f"二进制合并异常: {e}", "ERROR")
            remove_any(final)
    else:
        log.event(f"分片为 {kind}（非 MPEG-TS），二进制拼接不可播放，判为合并失败", "ERROR")

    final_note = fail_reason
    write_ffreport(ffreport_path, last_cmd or ["(未执行 ffmpeg)"],
                   "\n\n".join(stderr_all), dt.datetime.now(), final_note)
    return False, "", "", fail_reason


# ---------------------------------------------------------------- 单条任务
def _result_dict(idx, title, src, result, path, level, segments, codec,
                 log_path, ffreport, subtitles, cache, reason, on_disk):
    return {
        "idx": idx, "title": title, "src": src, "result": result,
        "path": path, "level": level, "segments": segments, "codec": codec,
        "log": log_path, "ffreport": ffreport,
        "size": os.path.getsize(path) if path and os.path.isfile(path) else 0,
        "subtitles": subtitles, "cache": cache, "reason": reason,
        "segments_on_disk": on_disk,
    }


def run_task_re(tool_dir, cli_exe, ffmpeg, cfg, idx, count, src, title,
                save_dir, batch_ts, task_dir, re_exe, governor=None):
    """RE 主下载层：单条命令完成解析/自动选轨/下载/合并/混流。

    以「磁盘上存在可播放的 mp4」判定成功；主内核失败则保留全部缓存后
    自动回退到 CLI 3.0.2 + Python 合并层，两条路径都失败才报失败理由。
    """
    if not re_exe:
        return run_task(tool_dir, cli_exe, ffmpeg, cfg, idx, count, src,
                        title, save_dir, batch_ts, task_dir, governor)

    say(f"\n[{idx}/{count}] {title}")
    say(f"      来源: {src}")
    log_dir = task_dir
    base = os.path.join(log_dir, f"{batch_ts}_{idx:02d}_{safe_name(title, 'task')}")
    log_path = base + ".log"
    ffreport_path = base + "_ffreport.log"
    re_log = base + "_re_kernel.log"

    clean_cache(task_dir)                # 干净起点（历史失败归档自动跳过）
    info = analyze_playlist(src, cfg)
    threads = governor.current() if governor else None
    cmd, out_dir = build_re_cmd(re_exe, src, task_dir, title, cfg, threads,
                                ffmpeg, re_log, info)
    log = TaskLog(log_path, [
        f"LOG {dt.datetime.now():%Y/%m/%d}",
        f"Save Path: {save_dir}",
        f"Log Path: {log_dir}",
        f"Task Start: {full_date()}",
        f"Task CommandLine: {fmt_cmd(cmd)}",
        f"Main Kernel: {os.path.basename(re_exe)} {re_version(re_exe)} (update check disabled)",
        f"Fallback Kernel: {os.path.basename(cli_exe)}",
        f"FFmpeg: {ffmpeg_version(ffmpeg)}",
        (f"Threads: {threads[0]}/{threads[1]} (adaptive)" if threads
         else "Threads: config default"),
        f"Task Index: {idx}/{count}",
        f"Video Title: {title}",
        f"Source: {src}",
        f"Playlist Probe: {info['detail']}",
        f"Auto Select: {info['select_reason'] or '(RE --auto-select / 单码率)'}",
        f"Probe Child: {'; '.join(info['tried_children'][:3]) or '-'}",
        f"RE Work Dir: {os.path.join(task_dir, RE_WORK_DIR)}",
        f"Cache Dir: {task_dir}",
    ])
    started = time.time()
    stats = {"timeout": 0, "total": 0}
    final_path, level, result = "", "", "FAILED"
    subtitle_out, failed_cache = [], ""
    fail_reason, re_tail = "", ""
    segments_on_disk, total, src_codec = 0, 0, ""

    if info["ok"]:
        log.event(f"播放列表预检通过：{info['detail']}")
    else:
        log.event(f"播放列表预检失败：{info['reason']}", "ERROR")

    # 确定性失败（直播流 / 子列表全 404 / 0 分片）无需启动内核、也无需回退
    deterministic = (not info["ok"] and not info["retryable"])
    if deterministic:
        fail_reason = info["reason"]
        log.event(f"任务失败：{fail_reason}", "ERROR")
        say(f"      -> [!!] 任务失败：{fail_reason}")
    else:
        attempts = CONNECT_TRIES if is_network_url(src) else 1
        cur = info
        try:
            for attempt in range(1, attempts + 1):
                if attempt > 1:
                    clean_cache(task_dir)
                    cur = analyze_playlist(src, cfg)
                    log.event(f"连接探测 [{attempt}/{attempts}]: "
                              f"{'OK' if cur['ok'] else 'FAILED'} - {cur['detail']}",
                              "NORMAL" if cur["ok"] else "ERROR")
                    if cur["ok"]:
                        threads = governor.current() if governor else None
                        cmd, out_dir = build_re_cmd(
                            re_exe, src, task_dir, title, cfg, threads,
                            ffmpeg, re_log, cur)
                if not cur["ok"]:
                    fail_reason = cur["reason"]
                else:
                    rc, total, src_codec, stats, re_tail = run_re_download(
                        re_exe, cmd, tool_dir, log)
                    video, subs = find_re_outputs(out_dir)
                    playable, why = (verify_playable(ffmpeg, video) if video
                                     else (False, "主内核未输出 mp4/mkv 文件"))
                    if playable:
                        ext = os.path.splitext(video)[1].lower() or ".mp4"
                        final_path = unique_path(os.path.join(
                            save_dir, safe_name(title, "video") + ext))
                        shutil.move(video, final_path)
                        subtitle_out = export_re_subtitles(save_dir, title, subs)
                        segments_on_disk = total or cur["segment_count"]
                        level = (f"N_m3u8DL-RE {re_version(re_exe).split('+')[0]} "
                                 f"混流({ext.lstrip('.')})")
                        result = "SUCCESS"
                        log.event(f"主内核产出可播放视频：{why}，输出 {final_path}")
                        if subtitle_out:
                            log.event(f"字幕已单独输出 {len(subtitle_out)} 份: "
                                      + ", ".join(os.path.basename(p) for p in subtitle_out))
                        say(f"      -> [OK] 成功  方式: {level}  输出: {final_path}")
                        break
                    fail_reason = (f"主内核未产出可播放视频：{why}"
                                   f"（退出码 {rc}，{stats['timeout']} 次超时/重试信号）")
                    log.event(fail_reason, "ERROR")
                if attempt < attempts:
                    gap = retry_delay(attempt, cur["detail"])
                    msg = f"失败，{gap} 秒后进行第 {attempt + 1} 次尝试（共 {attempts} 次）"
                    log.event(msg, "ERROR")
                    say(f"      -> [!!] {msg}")
                    time.sleep(gap)
            else:
                if attempts > 1 and not fail_reason:
                    fail_reason = f"连接/下载 {attempts} 次均失败（{cur['detail']}）"
                log.event(f"主内核 {attempts} 次尝试均失败：{fail_reason}", "ERROR")
        except KeyboardInterrupt:
            log.event("任务被用户中断", "ERROR")
            fail_reason = "用户中断"
            raise
        except Exception as e:
            log.event(f"主内核任务异常: {e}", "ERROR")
            say(f"      -> [!!] 异常: {e}")
            fail_reason = f"主内核任务异常: {type(e).__name__}: {e}"

    # RE 报告（保留主内核命令行与阶段输出，格式与 ffmpeg 报告一致）
    try:
        write_ffreport(ffreport_path, cmd, re_tail,
                       dt.datetime.fromtimestamp(started),
                       f"Main Kernel: {os.path.basename(re_exe)} {re_version(re_exe)}\n"
                       f"Result: {result}\nNote: {fail_reason or '成功'}")
    except OSError:
        pass

    if governor:
        old, new, why = governor.observe(stats, result == "SUCCESS")
        if why:
            log.event(f"线程自适应: {old[0]}/{old[1]} -> {new[0]}/{new[1]}（{why}）")
            say(f"      -> 线程自适应: {new[0]}/{new[1]}（{why}）")

    log.write("")
    log.write(f"Task End: {full_date()}")
    log.write(f"Elapsed: {time.time() - started:.1f}s")
    log.write(f"File: {final_path or '(无)'}")
    log.write(f"Result: {result}")
    log.write(f"Segments On Disk: {segments_on_disk}")
    log.write(f"Mux Method: {level or '(无)'}")
    log.write(f"Fail Reason: {fail_reason or '-'}")
    log.write(f"Subtitles: {', '.join(os.path.basename(p) for p in subtitle_out) or '(无)'}")

    if result == "SUCCESS":
        if KEEP_TEMP:
            log.write(f"Cache Dir (kept): {task_dir}")
        else:
            removed = clean_cache(task_dir)
            log.event(f"任务成功，缓存已清理 {removed} 项（仅保留 .log）")
            log.write(f"Cache Cleaned: {removed} item(s) - all except .log")
        return _result_dict(idx, title, src, result, final_path, level, total,
                            src_codec, log_path, ffreport_path, subtitle_out,
                            failed_cache, "", segments_on_disk)

    # 主内核失败：先保留全部缓存与日志副本，再回退旧内核
    failed_cache, kept = archive_failed_cache(
        task_dir, batch_ts, idx, title,
        log_files=(log_path, re_log, ffreport_path),
    )
    log.event(f"主内核失败，已保留全部缓存 {kept} 项: {failed_cache}", "ERROR")
    log.write(f"Failed Cache: {failed_cache}")
    log.write(f"Cache Retained: {kept} item(s)")
    try:
        shutil.copy2(log_path, unique_path(base + "_re.log"))
    except OSError:
        pass

    if not DISABLE_FALLBACK and not deterministic:
        log.event("自动回退到 CLI 3.0.2 + Python 合并层重试", "ERROR")
        say(f"      -> 主内核失败（{fail_reason}），自动回退旧内核重试...")
        res = run_task(tool_dir, cli_exe, ffmpeg, cfg, idx, count, src,
                       title, save_dir, batch_ts, task_dir, governor)
        # 两条路径都失败时，最终统计同时给出主内核与回退内核的失败理由
        if res.get("result") != "SUCCESS" and fail_reason:
            res["reason"] = (f"主内核失败（{fail_reason}）；回退 "
                             f"{os.path.basename(cli_exe)} 仍失败："
                             f"{res.get('reason') or '未知原因'}")
        return res

    say(f"      -> [!!] 任务失败：{fail_reason}")
    say(f"      -> 缓存已保留: {failed_cache}")
    return _result_dict(idx, title, src, "FAILED", "", "", total, src_codec,
                        log_path, ffreport_path, [], failed_cache,
                        fail_reason, 0)


def run_task(tool_dir, cli_exe, ffmpeg, cfg, idx, count, src, title, save_dir,
             batch_ts, task_dir, governor=None):
    say(f"\n[{idx}/{count}] {title}")
    say(f"      来源: {src}")
    log_dir = task_dir                           # 日志与缓存同在 保存目录\ffmpeg temp
    base = os.path.join(log_dir, f"{batch_ts}_{idx:02d}_{safe_name(title, 'task')}")
    log_path = base + ".log"
    ffreport_path = base + "_ffreport.log"

    def nonempty_segs():
        out = []
        for s in find_segments(task_dir)[0]:
            try:
                if os.path.getsize(s) > 0:
                    out.append(s)
            except OSError:
                pass
        return out

    # 干净起点必须在生成精简 master 之前，否则会被清掉
    clean_cache(task_dir)
    info = analyze_playlist(src, cfg)
    threads = governor.current() if governor else None
    cli_src = src
    if info["is_master"] and info["ok"]:
        cli_src = build_minimal_master(info, task_dir) or src
    cmd = build_cli_cmd(cli_exe, cli_src, task_dir, title, cfg, threads=threads)

    log = TaskLog(log_path, [
        f"LOG {dt.datetime.now():%Y/%m/%d}",
        f"Save Path: {save_dir}",
        f"Log Path: {log_dir}",
        f"Task Start: {full_date()}",
        f"Task CommandLine: {fmt_cmd(cmd)}",
        f"CLI Kernel: {os.path.basename(cli_exe)} (auto update disabled by NO_UPDATE)",
        f"FFmpeg: {ffmpeg_version(ffmpeg)}",
        (f"Threads: {threads[0]}/{threads[1]} (adaptive)" if threads
         else "Threads: config default"),
        f"Task Index: {idx}/{count}",
        f"Video Title: {title}",
        f"Source: {src}",
        f"Playlist Probe: {info['detail']}",
        f"Auto Select: {info['select_reason'] or '(单码率 / 非 master)'}",
        f"Probe Child: {'; '.join(info['tried_children'][:3]) or '-'}",
        f"CLI Source: {cli_src}",
        f"Cache Dir: {task_dir}",
    ])
    started = time.time()
    ok_dl, total, src_codec = False, 0, ""
    final_path, level, result = "", "", "FAILED"
    subtitles, subtitle_out, failed_cache = [], [], ""
    fail_reason = ""
    stats = {"timeout": 0, "total": 0}
    segments_on_disk = 0

    if info["ok"]:
        log.event(f"播放列表预检通过：{info['detail']}")
        if info["select_reason"]:
            log.event(info["select_reason"])
        if info["is_master"] and cli_src != src:
            log.event(f"已生成精简 master（单码率 + 每组单条 MEDIA）交给内核: {cli_src}")
    else:
        log.event(f"播放列表预检失败：{info['reason']}", "ERROR")
        if not info["retryable"]:
            fail_reason = info["reason"]

    try:
        # 确定性失败（直播流 / 子列表全 404 / 0 分片）不重试，直接报理由
        if not info["ok"] and not info["retryable"]:
            attempts = 0
        else:
            attempts = CONNECT_TRIES if is_network_url(src) else 1
        cur = info
        for attempt in range(1, attempts + 1):
            ok_dl = False
            if attempt > 1:
                clean_cache(task_dir)
                cur = analyze_playlist(src, cfg)
                log.event(f"连接探测 [{attempt}/{attempts}]: "
                          f"{'OK' if cur['ok'] else 'FAILED'} - {cur['detail']}",
                          "NORMAL" if cur["ok"] else "ERROR")
                cli_src = src
                if cur["is_master"] and cur["ok"]:
                    cli_src = build_minimal_master(cur, task_dir) or src
                threads = governor.current() if governor else None
                cmd = build_cli_cmd(cli_exe, cli_src, task_dir, title, cfg,
                                    threads=threads)
            if cur["ok"]:
                ok_dl, total, src_codec, stats = run_download(
                    cli_exe, cmd, tool_dir, log)
                made = nonempty_segs()
                # 分级回退：精简 master 仍无分片时，直接把选定子列表交给内核
                if not made and cur["is_master"] and cli_src != cur["selected"]:
                    log.event("精简 master 未产出分片，回退为直接下载选定子播放列表", "ERROR")
                    clean_cache(task_dir)
                    fb = build_cli_cmd(cli_exe, cur["selected"], task_dir, title,
                                       cfg, threads=threads)
                    ok_dl, total, src_codec, stats = run_download(
                        cli_exe, fb, tool_dir, log)
                    made = nonempty_segs()
                segments_on_disk = len(made)
                # 判定标准改为「磁盘上确有有效分片」，不再只看内核退出码
                if ok_dl and segments_on_disk == 0:
                    ok_dl = False
                    fail_reason = ("下载内核退出码 0 但磁盘无有效分片"
                                   "（多音轨交互 / 子列表失效）")
                    log.event(fail_reason, "ERROR")
                elif not ok_dl:
                    fail_reason = (f"下载内核执行失败（退出码非 0，"
                                   f"{stats['timeout']} 次超时/重试信号）")
            else:
                fail_reason = cur["reason"]

            if ok_dl:
                break
            if attempt < attempts:
                gap = retry_delay(attempt, cur["detail"])
                msg = (f"连接失败，{gap} 秒后进行第 {attempt + 1} 次尝试"
                       f"（共 {attempts} 次，指数退避）")
                log.event(msg, "ERROR")
                say(f"      -> [!!] {msg}")
                time.sleep(gap)
        else:
            if attempts > 1 and not fail_reason:
                fail_reason = f"连接/下载 {attempts} 次均失败（{cur['detail']}）"
            if attempts:
                log.event(f"连接/下载 {attempts} 次均失败，跳过该任务：{fail_reason}", "ERROR")
                say(f"      -> [!!] 任务失败：{fail_reason}")

        if ok_dl:
            # CLI 3.0.x 不下载外挂字幕，需自行解析 m3u8 的 SUBTITLES 轨并合并
            # （此时已确认有分片，避免 0 分片任务白下 8 条字幕轨）
            subtitles = find_subtitles(task_dir)
            made = download_subtitles(src, task_dir, title, cfg, log)
            subtitles = list(dict.fromkeys(subtitles + made))
            log.event(f"开始合并分片...（磁盘有效分片 {segments_on_disk} 个）")
            ok_mux, final_path, level, mux_reason = do_merge(
                ffmpeg, task_dir, title, save_dir, src_codec, log,
                ffreport_path, subtitles, cfg
            )
            result = "SUCCESS" if ok_mux else "FAILED"
            fail_reason = "" if ok_mux else mux_reason
            if ok_mux and subtitles:
                subtitle_out = export_subtitles(save_dir, title, subtitles)
                log.event(f"字幕已单独输出 {len(subtitle_out)} 份: "
                          + ", ".join(os.path.basename(p) for p in subtitle_out))
            say(f"      -> {'[OK] 成功' if ok_mux else '[!!] 失败'}  方式: {level or '-'}"
                + (f"  输出: {final_path}" if final_path else ""))
            if not ok_mux:
                say(f"      -> 失败原因: {fail_reason}")
        else:
            log.event(f"任务失败：下载未成功，跳过合并（{fail_reason}）", "ERROR")
            say(f"      -> [!!] 任务失败：{fail_reason}")
    except KeyboardInterrupt:
        log.event("任务被用户中断", "ERROR")
        say("      -> [!!] 任务被中断")
        fail_reason = "用户中断"
        raise
    except Exception as e:
        log.event(f"任务异常: {e}", "ERROR")
        say(f"      -> [!!] 异常: {e}")
        fail_reason = f"任务异常: {type(e).__name__}: {e}"

    # 线程自适应：按本任务内核输出的超时/重试信号调整后续任务并发
    if governor:
        old, new, why = governor.observe(stats, result == "SUCCESS")
        if why:
            log.event(f"线程自适应: {old[0]}/{old[1]} -> {new[0]}/{new[1]}（{why}）")
            say(f"      -> 线程自适应: {new[0]}/{new[1]}（{why}）")

    # 下载内核自身的 .log 也一并归入日志目录，保证「所有 log 都在 ffmpeg temp」
    moved = collect_cli_logs(tool_dir, log_dir, started)
    if moved:
        log.event(f"下载内核日志已归集 {moved} 份到 {log_dir}")

    log.write("")
    log.write(f"Task End: {full_date()}")
    log.write(f"Elapsed: {time.time() - started:.1f}s")
    log.write(f"File: {final_path or '(无)'}")
    log.write(f"Result: {result}")
    log.write(f"Segments On Disk: {segments_on_disk}")
    log.write(f"Mux Method: {level or '(无)'}")
    log.write(f"Fail Reason: {fail_reason or '-'}")
    log.write(f"Subtitles: {', '.join(os.path.basename(p) for p in subtitle_out) or '(无)'}")

    # 成功：仅保留 log，删除缓存；失败：把本条任务的全部缓存归档长期保留
    if KEEP_TEMP:
        log.write(f"Cache Dir (kept): {task_dir}")
    elif result == "SUCCESS":
        removed = clean_cache(task_dir)
        log.event(f"任务成功，缓存已清理 {removed} 项（仅保留 .log）")
        log.write(f"Cache Cleaned: {removed} item(s) - all except .log")
        log.write(f"Cache Dir (kept): {task_dir}")
    else:
        failed_cache, kept = archive_failed_cache(
            task_dir, batch_ts, idx, title,
            extra_files=[final_path] if final_path else [],
            log_files=(log_path, ffreport_path),
        )
        log.event(f"任务失败，已保留全部缓存 {kept} 项: {failed_cache}", "ERROR")
        say(f"      -> 缓存已保留: {failed_cache}")
        log.write(f"Failed Cache: {failed_cache}")
        log.write(f"Cache Retained: {kept} item(s)")
        log.write(f"Cache Dir (kept): {task_dir}")

    return {
        "idx": idx, "title": title, "src": src, "result": result,
        "path": final_path, "level": level, "segments": total,
        "codec": src_codec, "log": log_path, "ffreport": ffreport_path,
        "size": os.path.getsize(final_path) if final_path and os.path.isfile(final_path) else 0,
        "subtitles": subtitle_out, "cache": failed_cache,
        "reason": fail_reason, "segments_on_disk": segments_on_disk,
    }


# ---------------------------------------------------------------- 交互
def collect_tasks():
    tasks = []
    seen = set()                 # 同一批次内按地址去重，避免重复下载生成 (2) 文件
    idx = 1
    say("\n  依次输入 [m3u8 地址] 与 [视频标题]，可重复添加；地址处直接回车结束添加。")
    say("  地址支持 http(s) 网络链接，也支持本地 .m3u8 文件路径。")
    say("-" * 68)
    while True:
        try:
            raw = input(f"[任务 #{idx}] m3u8 地址（回车结束）: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if not raw:
            break
        try:
            src = normalize_source(raw)
        except ValueError as e:
            say(f"      [!!] {e}，请重新输入")
            continue
        try:
            raw_title = input(f"          视频标题（回车用默认）: ").strip()
        except (EOFError, KeyboardInterrupt):
            raise
        if src in seen:
            say("      [提示] 该地址已在本批中，自动去重跳过")
            continue
        seen.add(src)
        title = safe_name(raw_title, "") if raw_title else title_from_source(src, idx)
        tasks.append((src, title))
        say(f"      已添加: {title}")
        idx += 1
    return tasks


def ask_save_dir(cfg):
    default_dir = default_videos_dir(cfg)
    say("")
    try:
        raw = input(f"下载保存目录（回车 = 系统默认视频路径 {default_dir}）: ").strip().strip('"')
    except (EOFError, KeyboardInterrupt):
        raise
    path = os.path.abspath(os.path.expanduser(os.path.expandvars(raw))) if raw else default_dir
    try:
        os.makedirs(path, exist_ok=True)
    except Exception as e:
        say(f"[!!] 无法创建目录 {path}：{e}，改用默认路径")
        path = default_dir
        os.makedirs(path, exist_ok=True)
    return path


def open_folder(path):
    if NO_EXPLORER:
        return
    try:
        if os.name == "nt":
            os.startfile(path)                       # noqa: S606 - Windows 资源管理器
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        say(f"[提示] 无法自动打开资源管理器：{e}")


def write_batch_log(log_dir, batch_ts, results, save_path):
    path = os.path.join(log_dir, f"{batch_ts}_batch.log")
    with open(path, "w", encoding="utf-8-sig") as f:
        f.write(f"LOG {dt.datetime.now():%Y/%m/%d}\n")
        f.write(f"Batch Start: {full_date()}\n")
        f.write(f"Save Path: {save_path}\n")
        f.write(f"Tasks: {len(results)}\n\n")
        for r in results:
            f.write(
                f"[{r['idx']}/{len(results)}] {r['title']}\n"
                f"    Source : {r['src']}\n"
                f"    Download: {'OK' if r.get('segments_on_disk') else 'FAILED'} ({r.get('segments_on_disk', r['segments'])} segments, {r['codec'] or 'unknown'})\n"
                f"    Mux    : {r['level'] or '-'}\n"
                f"    Result : {r['result']}\n"
                f"    Reason : {r.get('reason') or '-'}\n"
                f"    File   : {r['path'] or '-'} ({human_size(r['size']) if r['size'] else '-'})\n"
                f"    Subs   : {', '.join(os.path.basename(p) for p in r.get('subtitles', [])) or '-'}\n"
                f"    Cache  : {r.get('cache') or '-'}\n"
                f"    Log    : {r['log']}\n\n"
            )
        ok = sum(1 for r in results if r["result"] == "SUCCESS")
        f.write(f"Batch End: {full_date()}\n")
        f.write(f"Summary: SUCCESS={ok} FAILED={len(results) - ok}\n")
        failed = [r for r in results if r["result"] != "SUCCESS"]
        if failed:
            f.write("\nFailed Tasks (reason):\n")
            for r in failed:
                f.write(f"  [{r['idx']}/{len(results)}] {r['title']}: {r.get('reason') or '未知原因'}\n")
    return path


# ---------------------------------------------------------------- 主循环
def main():
    setup_console()
    tool_dir, cli_exe, ffmpeg, cfg = find_tools()
    re_exe = find_re(tool_dir)          # 主内核；缺失或禁用时自动走旧 CLI
    governor = ThreadGovernor(cfg)      # 跨批次保留：按超时率动态收敛并发
    round_no = 0

    while True:                       # 程序自动重启：每批结束后回到输入界面
        round_no += 1
        say("")
        say("=" * 68)
        say(f"  M3U8 批量下载器 (Python 复刻版)   第 {round_no} 轮")
        say("=" * 68)
        if re_exe:
            say(f"  主下载内核: {os.path.basename(re_exe)} {re_version(re_exe)}"
                f"   |   回退内核: {os.path.basename(cli_exe)}")
            say(f"  合并/混流: ffmpeg {ffmpeg_version(ffmpeg)}（独立音轨、字幕、fMP4 原生支持）")
            say("  自动选最高配置、版本检查已关闭（--disable-update-check）")
        else:
            say(f"  下载内核: {os.path.basename(cli_exe)}   |   合并内核: ffmpeg {ffmpeg_version(ffmpeg)}")
            say("  内核自动更新检查: 已关闭（NO_UPDATE）")
        say("  内核与 ffmpeg 已内置于本 exe；其他参数按 config.txt 默认值运行（已隐藏）")

        try:
            tasks = collect_tasks()
        except (EOFError, KeyboardInterrupt):
            say("\n已退出。")
            return

        if not tasks:
            say("\n未添加任何任务，返回输入界面（程序已自动重启）。")
            continue

        try:
            save_dir = ask_save_dir(cfg)
        except (EOFError, KeyboardInterrupt):
            say("\n已退出。")
            return

        batch_ts = stamp()
        work_root = os.path.join(save_dir, TEMP_DIR_NAME)
        os.makedirs(work_root, exist_ok=True)
        say("")
        say(f"保存目录: {save_dir}")
        say(f"日志目录: {work_root}   (全部 .log 均保存于此，长期保留)")
        say(f"缓存目录: {work_root}   (成功仅留 log；失败则保留该任务全部缓存)")
        say(f"本批任务: {len(tasks)} 条，开始执行...")
        say("=" * 68)

        results = []
        try:
            for i, (src, title) in enumerate(tasks, 1):
                results.append(run_task_re(
                    tool_dir, cli_exe, ffmpeg, cfg, i, len(tasks),
                    src, title, save_dir, batch_ts, work_root, re_exe, governor,
                ))
        except KeyboardInterrupt:
            say("\n[!!] 本批任务被用户中断")

        ok = sum(1 for r in results if r["result"] == "SUCCESS")
        say("")
        say("=" * 68)
        say("  本批结果")
        say("-" * 68)
        for r in results:
            mark = "[OK] " if r["result"] == "SUCCESS" else "[!!] "
            say(f"  {mark}[{r['idx']}/{len(tasks)}] {r['title']}"
                f"  |  {r['level'] or '未完成'}"
                f"  |  {human_size(r['size']) if r['size'] else '-'}")
            if r["result"] != "SUCCESS" and r.get("reason"):
                say(f"        失败原因: {r['reason']}")
        say(f"  成功 {ok} / 共 {len(results)}")
        failed = [r for r in results if r["result"] != "SUCCESS"]
        if failed:
            say("-" * 68)
            say("  失败项目统计（含原因）")
            for r in failed:
                say(f"    [{r['idx']}/{len(tasks)}] {r['title']} -> "
                    f"{r.get('reason') or '未知原因'}")
            say(f"  失败 {len(failed)} 项的完整缓存与日志保留在: {work_root}")
        if results:
            batch_log = write_batch_log(work_root, batch_ts, results, save_dir)
            say(f"  批次日志: {batch_log}")
            say(f"  全部日志（.log / _ffreport.log / _batch.log）均已保存在: {work_root}")
        if KEEP_TEMP:
            say(f"  [调试] 临时文件已保留于: {work_root}")
        else:
            clean_cache(work_root)
            say(f"  缓存清理完成：成功任务缓存已删除；ffmpeg temp、全部日志与失败缓存均保留: {work_root}")
            failed = [n for n in os.listdir(work_root) if is_failed_cache(n)]
            if failed:
                say(f"  失败任务缓存归档 {len(failed)} 个（保留供排查）: "
                    + ", ".join(failed[:3]) + (" ..." if len(failed) > 3 else ""))
            leftover = [
                n for n in os.listdir(work_root)
                if not (n.lower().endswith(".log") and os.path.isfile(os.path.join(work_root, n)))
                and not is_failed_cache(n)
            ]
            if leftover:
                say(f"  [提示] 仍有 {len(leftover)} 项缓存未能删除（可能正被占用）")
        if not NO_EXPLORER:
            say(f"  正在打开资源管理器: {save_dir}")
        open_folder(save_dir)
        say("=" * 68)
        say("程序已自动重启，可继续添加下一批任务（Ctrl+C 退出）。")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        say("\n已退出。")
    except Exception as exc:
        say(f"\n[!!] 启动失败: {exc}")
        sys.exit(1)
