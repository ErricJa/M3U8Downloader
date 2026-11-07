# 写在前面

我是项目作者。本项目原始文件config.txt、ffmpeg.exe、N_m3u8DL-CLI-SimpleG.exe、N_m3u8DL-CLI_v3.0.1.exe来源于互联网。我借助Ai工具将本文件合并打包成为单文件独立运行，并将功能加以改进和完善。
从程序转写到项目上传均使用workbuddy的Hy4工具。项目编写采用自然语言向智能体描述需求，由AI进行优化处理最后形成此版本。程序框架在此不动，各种内核可以自行上网更新，同步在PY源码中进行改动。
项目上传同步使用workbuddy进行操作。使用技能GitHub和连接器连到本账户进行自动化上传，同时本说明文档也由AI自动撰写。这段写在前面除外。
文件ReadMe.log为项目迭代历史，如有需要可自行阅读。技术人员或技术爱好者可自取项目文件进行自定义优化。
主程序国内镜像：链接：https://share.weiyun.com/5JsS5qEj 密码：i6qb2d，通过该链接可直接下载立即可用的m3u8_downloader.exe主程序。

如有侵权尽可通过邮箱1276744099@qq.com联系我进行处理。


# M3U8Downloader

Windows 下批量下载 m3u8/HLS 并输出 MP4 的命令行工具，当前版本 **V9**。
原工作流为 `N_m3u8DL-CLI-SimpleG.exe → N_m3u8DL-CLI_v3.0.1.exe → ffmpeg.exe`，
本项目用 Python 标准库做非 GUI 复刻，现以 `N_m3u8DL-RE 0.6.0` 为主下载内核、
`FFmpeg 9.0.2` 负责混流，旧 CLI 3.0.2 保留为失败回退。

## 快速开始

```bash
git clone https://github.com/ErricJa/M3U8Downloader.git
cd M3U8Downloader
python restore_parts.py          # 一次性还原主程序 exe（约 1 秒，自动校验 SHA-256）
```

还原后直接双击 **`M3U8Downloader.exe`**：单文件、自包含，内嵌 RE / CLI 3.0.2 /
ffmpeg / config / NO_UPDATE，无需 Python、bat、原安装目录，也**不需要**解压 FFmpeg 组件。

其他用法：

- 源码运行：`python m3u8_downloader.py`（需同目录放 `N_m3u8DL-RE.exe`、`ffmpeg.exe` 等组件，
  可再运行 `python restore_tools.py` 解压组件）。
- 重新打包：`python build_exe.py` → `dist/M3U8Downloader.exe`。

启动后逐条输入「m3u8 地址 + 视频标题」，地址留空结束本批；保存目录直接回车取系统默认视频路径。

## 为什么主程序以分片形式入库

上传环境仅放行 `api.github.com`：`github.com` 的 git / LFS 端点与 `uploads.github.com`
均不可达，无法 `git push` 或使用 Git LFS；而 GitHub REST API 单次请求最多只能承载约
40 MB 内容，54 MB 的主程序 exe 与 53 MB 的组件包会被拒绝。因此这两个文件被切成
**30 MiB 分片**入库，`restore_parts.py` 按序合并并校验 SHA-256，还原结果与原件逐字节一致。

```
parts/M3U8Downloader.exe.part01 / .part02   -> M3U8Downloader.exe  (51.6 MiB)
parts/ffmpeg_tools.7z.part01   / .part02   -> ffmpeg_tools.7z      (53.0 MiB)
```

`ffmpeg_tools.7z` 是 `ffmpeg.exe`（100.54 MiB）+ `ffprobe.exe`（100.35 MiB）的
7z solid 无损压缩包（210 MB → 53 MiB），两个 exe 单文件均超过 GitHub 100 MB 硬限制。
**只有用源码运行时才需要解压它**；单文件 exe 已内嵌 ffmpeg，直接双击不受影响。

```bash
python restore_tools.py          # 需要本机安装 7-Zip；自动查找 7z.exe 并校验
# 或：7z x ffmpeg_tools.7z -o.
```

`ffprobe` 是可选的精确探测组件：缺失时程序自动回退 `ffmpeg -i` 完成成品可播放性校验，
功能不受影响。

## 仓库文件

| 文件 | 大小 | 说明 |
| --- | --- | --- |
| `parts/*.part01..02` | 共 104.6 MiB | 主程序 exe 与 FFmpeg 组件包的分片 |
| `restore_parts.py` | 3 KB | 合并分片、还原主程序与组件包（零依赖） |
| `restore_tools.py` | 3 KB | 解压 `ffmpeg_tools.7z` 并校验两个 exe |
| `m3u8_downloader.py` | 100 KB | 主程序源码（2316 行） |
| `build_exe.py` | 5 KB | PyInstaller onefile 打包脚本 |
| `config.txt` | 361 B | 原版 Base64 配置 |
| `NO_UPDATE` | 0 B | 关闭旧 CLI 联网自更新的隐藏开关 |
| `ReadMe.log` | 25 KB | V1–V9 完整更新日志 |
| `m3u8链接下载问题分析报告.md` | 18 KB | 12 条真实链接的三层诊断报告 |
| `N_m3u8DL-RE.exe` | 12.7 MiB | 主下载内核 v0.6.0-beta（MIT，NativeAOT，无需 .NET） |
| `N_m3u8DL-CLI_v3.0.2.exe` | 1.35 MiB | 失败回退内核 |
| `N_m3u8DL-CLI_v3.0.1.exe` | 1.34 MiB | 原版内核，保留兼容 |
| `SHA256SUMS.txt` | 2 KB | 仓库内全部文件的 SHA-256 校验清单 |

> `M3U8Downloader.exe`、`ffmpeg_tools.7z`、`ffmpeg.exe`、`ffprobe.exe` 为还原产物，
> 不直接入库（已列入 `.gitignore`），避免重复提交。

校验仓库文件完整性（PowerShell）：

```powershell
Get-Content SHA256SUMS.txt | ForEach-Object {
  $h,$_,$name = $_ -split '\s+',3
  if ((Get-FileHash -Algorithm SHA256 $name).Hash.ToLower() -ne $h) { "MISMATCH: $name" }
}
```

## 本仓库的提交方式

本次通过 GitHub REST Git Data API 直接创建 blob、构建 tree 与 commit 上传，
**未使用 Git LFS 与 git push**。因此克隆不需要 git-lfs，也没有 LFS 指针文件。

## 运行期目录

```
<保存目录>\<标题>.mp4                 成品视频
<保存目录>\<标题>.<lang>.srt/.vtt     独立字幕
<保存目录>\ffmpeg temp\*.log          全部日志（长期保留）
<保存目录>\ffmpeg temp\*              下载缓存（成功仅保留 .log）
<保存目录>\ffmpeg temp\失败缓存_*\    失败任务全量缓存（长期保留）
```

## 主要能力

批量输入与去重、master 自动选最高配置、多音轨与多字幕、fMP4/CMAF、DASH/MSS、
HEVC/H.264/AV1/VP9、错误地址 3 次重连、403 指数退避、线程自适应、
僵尸直播提前判失败、RE 失败自动回退 CLI 并在统计中给出理由、
字幕内嵌与独立输出、失败缓存归档、打开资源管理器、批次自动重启。

完整版本演进、每版解决的问题与最终核验数据见 `ReadMe.log`。

## 第三方组件

`N_m3u8DL-RE`（MIT）、`N_m3u8DL-CLI`、`FFmpeg` 均为各自作者的作品，遵循各自许可证；
本仓库仅做集成与分发，未修改其二进制文件。


---

## V10 版本更新（2026-11-07）

> 本节为追加内容。上方「# 写在前面」及原有全部段落保持原样未改动。
> 完整迭代历史见 `ReadMe.log` 末尾追加的 V10 章节。

**当前版本：V10**（程序头与启动横幅已标注 `APP_VERSION = "V10"`）

### 解决的问题

1. 视频地址只能逐条手敲，批量任务无法一次导入。
2. 批量清单没有统一的保存目录写法，每条都要交互询问。
3. 保存路径带引号需人工去除；输入不存在盘符时静默改默认路径，既不报错也不给重输机会。
4. 程序头缺少版本号与清单格式说明。

### 新增能力

- **视频地址处可填三种**：网络 m3u8 地址 / 本地 `.m3u8` 文件 / 本地 `.txt` 批量清单；本地路径允许带引号。
- **TXT 清单格式**（每行只写内容，不添加任何标识）：

  ```
  https://example.com/a.m3u8      第 1 行：视频地址
  第一集                          第 2 行：文件名
  "D:/list/b.m3u8"                第 3 行：视频地址（本地文件，可带引号）
  第二集                          第 4 行：文件名
                                  （末尾空一行隔断）
  "E:/我的视频"                    下一行：保存目录（可空缺）
  ```

  - 奇数行 = 视频地址、偶数行 = 文件名，成对出现；
  - 末尾空一行隔断，下一行填写保存目录，**可空缺**（留空或不写该行）；
  - 清单写了保存目录 → 直接读取使用；未写 → 按原流程询问；
  - 清单内相对路径按清单文件所在目录解析；文件名带 `.mp4` 等后缀自动去除；
  - 兼容 UTF-8(BOM) / UTF-16(BOM) / GBK；本地路径与保存目录均可带引号。

- **保存路径分级判定**：已存在的文件夹直接使用；磁盘存在但文件夹不存在则按路径创建（含多级目录）；磁盘不存在则报错，要求重新输入路径，或直接回车使用系统默认视频路径；直接回车 = 系统默认视频路径；支持仅输入盘符 `E:`。
- **程序头**：docstring 与启动横幅增加版本号和完整 TXT 格式说明。

### 本次更新的文件

| 文件 | 说明 |
| --- | --- |
| `m3u8_downloader.py` | V10 输入层（清单解析、引号剥离、保存路径分级） |
| `parts/M3U8Downloader.exe.part01 / .part02` | V10 主程序分片（重新打包，51.6 MiB） |
| `restore_parts.py` | 主程序 SHA-256 校验值同步更新 |
| `SHA256SUMS.txt` | 校验清单重新生成 |
| `ReadMe.log` | 末尾追加 V10 更新日志（既有内容未改动） |
| `README.md` | 末尾追加本节（既有内容未改动） |

### 验证

`py_compile` 通过；V10 输入层 **36 项自检 PASS 36 / FAIL 0**（清单四种边界、编码兼容、引号、磁盘分级、模拟交互、原流程兼容）；重新打包的 exe 内嵌 5 项资源齐全，实际启动横幅显示 `V10` 与 TXT 格式说明，退出码 0。

克隆后仍按原方式还原：`python restore_parts.py` → 双击 `M3U8Downloader.exe`。
