# 拾录 · 视频逐字稿

一个用途：把抖音链接或本地音视频整理成 Markdown 逐字稿。已移除知识库、问答、研究、画布和旧桌面界面及其测试。

## 使用

当前电脑双击「启动.bat」，打开 http://127.0.0.1:8765。

1. 选择保存目录，填写主题，或选择已有主题文件夹。
2. 粘贴/拖入抖音作品、博主主页链接；也可导入包含链接的 txt，或上传音视频文件。
3. 点击读取标题，勾选需要的视频，再开始整理。主页默认读取 100 条。
4. 每个视频保存一个文件：`保存目录/主题/YYYY-MM-DD 标题.md`。日期优先用作品发布日期，本地文件使用整理日期。文件包含主题、精简总结、来源和逐字稿原文。重复整理相同视频会更新同一文件。

默认通过日日新 API 生成总结；语音识别在本机执行。高级设置可保存日日新 API Key 和抖音 Cookie。Windows 下密钥以当前用户 DPAPI 加密保存，不随安装包分发。原项目的日日新凭证会自动迁移。

## 安装到新电脑

需要 Windows、Python 3.10+、Chrome 或 Edge。运行 `powershell -ExecutionPolicy Bypass -File setup.ps1`，然后双击「启动.bat」。首次启动需要下载语音模型；有 NVIDIA 驱动时安装脚本会安装 CUDA 运行库依赖。无 Chrome/Edge 时可执行 `.venv\Scripts\python -m playwright install chromium`。

模型、凭证、浏览器登录态和输出文件均留在本机。安装包不包含这些内容。退出运行服务的终端可停止服务。

## 默认速度设置

small 语音模型，CUDA float16，GPU 转录并行 2、批大小 16、下载并行 4、总结并行 2。下载、转录和总结可以同时推进；重试优先复用已完成的转录。GPU 不可用时界面显示原因；显式选择 CUDA 时不会悄悄切回 CPU。

本机 RTX 5070 Ti Laptop 12GB 的实测见 `验证说明.md`。增加并行数不一定更快：本次 4 路 GPU 的总吞吐低于 2 路。GPU 只负责转录，网络下载、抖音页面加载和远程 API 总结期间低利用率正常。

主页只能读取 21 条的问题已修复：抖音后续分页响应来自 www-hj.douyin.com，旧逻辑只识别 www.douyin.com。新版本通过正常浏览器收集相同接口的各分页响应，已验证指定主页返回 100 条。若平台要求验证，点击高级设置中的浏览器验证并手动完成，再重试。

## 维护

代码在 app/，入口 web.py，无前端构建步骤。基础测试：`.venv\Scripts\python -m unittest discover -s tests -v`。requirements.lock.txt 记录本次验证环境；常规安装使用 requirements.txt 或 requirements-gpu.txt。

旧 data/workbench 保留作凭证迁移和复用已下载媒体，界面不再提供知识库。原始源码压缩包和旧版本安装包仍在上级目录，便于回退。
