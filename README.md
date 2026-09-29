# MediaHarvest

多平台内容采集 → 本地语音转录 → Markdown 知识库。把 B 站视频、抖音视频、小红书笔记一键「收割」成结构化的 Obsidian 笔记。

## 功能特性

- **多平台采集**
  - **Bilibili**：视频 + 弹幕 + 评论，无需登录即可采集公开内容
  - **抖音**：视频 + 评论
  - **小红书**：视频/图文笔记 + 评论；支持关键词搜索、话题聚合页抓取、博主主页批量抓取
- **本地语音转录**：基于 faster-whisper，支持 GPU（CUDA）加速与 CPU 自动回退，模型自动下载缓存
- **Markdown 知识库**：YAML frontmatter（来源、作者、时长、标签、采集时间）+ 正文 + 媒体链接，文件名规范、适合 Obsidian 直接建库
- **采集历史**：SQLite 记录每条采集状态，可在 GUI 历史页查看与预览
- **稳健的反爬应对**：浏览器指纹 HTTP 客户端（curl_cffi）、请求签名自动生成、限流退避重试（指数退避 + 随机抖动）、明确的错误提示（区分限流 / 登录态失效 / 业务错误）

## 环境要求

- Windows / macOS / Linux
- Python 3.10+
- 转录视频需要 [FFmpeg](https://ffmpeg.org/)（加入 PATH）
- GPU 转录需 NVIDIA 显卡 + CUDA 环境（可选，纯 CPU 也能跑）

## 安装与启动

```bash
pip install -r requirements.txt
python gui.py        # 图形界面
python run.py --help # 命令行模式
```

## 小红书 Cookie 配置

小红书接口需要登录态。从浏览器导出 cookie（必须包含 `a1` 与 `web_session` 两个字段），粘贴到 GUI 的 cookie 管理页验证即可。

> ⚠️ 高频请求会导致会话被服务端直接作废（code -100），此时只能重新导出 cookie。请合理设置采集条数，避免连续大量搜索。

## 目录结构

```
mediaharvest/
├── app/
│   ├── crawler/      # 各平台抓取与签名
│   ├── asr/          # faster-whisper 本地转录引擎
│   ├── core/         # 配置、cookie 管理、数据模型
│   ├── ui/           # PyQt5 界面
│   ├── export/       # Markdown 导出
│   ├── downloader.py # 媒体下载
│   └── pipeline.py   # 采集→转录→导出 流水线
├── docs/             # 设计文档
├── test_*.py         # 测试脚本（python test_xhs_parse.py 直接运行）
├── gui.py            # GUI 入口
└── run.py            # CLI 入口
```

## 免责声明

本项目仅供学习与研究用途。请遵守目标平台的服务条款，尊重内容创作者的版权，采集内容请勿用于商业用途或二次分发。使用本项目产生的任何后果由使用者自行承担。
