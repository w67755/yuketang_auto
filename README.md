# 雨课堂自动学习助手（DeepSeek 版）

基于 **Selenium 4 浏览器自动化 + DeepSeek API** 重构的雨课堂（默认适配**雨课堂标准版** `www.yuketang.cn/web`，内置**雨课堂 / 荷塘 / 长江 / 黄河**四大服务器一键切换及自定义域名）桌面辅助工具，支持**自动看视频、自动看课件（逐页停留与未看页自动补看）、以及 DeepSeek AI 自动做题提交**。

## 核心特性

- **视频稳健观看**：仅使用雨课堂原生支持的 `1.0x / 1.5x / 2.0x` 倍速，支持静音播放、CDP 防失焦/切屏暂停注入与意外暂停自动恢复，保障观看进度 100% 计入。
- **课件逐页停留 + 漏页定向补看**：每页按可配置的随机时长区间（默认 `5~8 秒`）停留；翻完后自动检测「您还有以下页面未看」提示与未读页码，自动点击「继续观看」并加长停留定向补看（默认最多 2 轮）。
- **DeepSeek API 自动做题**：沿用 DeepSeek API 接入方式，自动识别单选/多选/判断/填空/简答题，兼容 Vue 表单状态同步并自动提交。
- **单浏览器顺序调度 + 模式自由切换**：自动识别课程下的视频、课件与习题并依次执行，避免多开导致雨课堂心跳冲突；可在界面独立勾选只刷视频、只看课件或只做题。
- **微信扫码登录态持久化**：通过独立浏览器配置目录（`browser_profile/`）保留登录态，无需每次重复扫码；支持按课程名自动进入，也支持手动进课后一键接管。
- **异常自动留存与页面诊断**：单个任务失败时自动保存截图与页面 DOM 摘要至 `logs/diagnostics/` 并继续下一个任务；界面提供「页面结构诊断」按钮，方便根据不同课程版本微调 `core/selectors.py`。

## 目录结构

```text
yuketang_auto/
├── start_gui.py          # 桌面 GUI 启动入口
├── 一键启动.bat          # Windows 一键创建虚拟环境、安装依赖并启动
├── requirements.txt      # Python 依赖清单（selenium / customtkinter / requests）
├── config.example.json   # 配置模板（不含密钥，已纳入 Git）
├── app/
│   ├── config.py         # 数据类配置模型、边界校验与 JSON 持久化
│   ├── logger.py         # 统一日志器（终端 + 日志文件 + GUI 线程安全队列）
│   └── gui.py            # CustomTkinter 桌面控制台界面
├── core/
│   ├── browser.py        # Selenium 4 浏览器管理（自动驱动、登录态持久化、防失焦注入）
│   ├── selectors.py      # 长江雨课堂多版本页面选择器集中配置（改版时仅需改这里）
│   ├── diagnostics.py    # 异常截图与页面 DOM 摘要导出
│   ├── auth.py           # 微信扫码登录检测与课程查找导航
│   └── scheduler.py      # 课程目录扫描、任务识别与顺序调度引擎
├── tasks/
│   ├── base.py           # 任务执行器基类与结果状态模型
│   ├── watch_video.py    # 视频播放与防暂停监控
│   ├── watch_ppt.py      # 课件逐页随机停留与未看页补看循环
│   ├── deepseek_client.py# DeepSeek Chat Completions API 客户端与答案解析
│   └── auto_quiz.py      # 题目提取、答案回填与自动提交
└── tests/
    └── test_core.py      # 核心纯逻辑单元测试
```

## 快速开始

### 方式一：Windows 一键启动（推荐）
直接双击运行 `一键启动.bat`，脚本会自动创建 `.venv` 虚拟环境、安装依赖并打开桌面窗口。

### 方式二：命令行手动运行
```bash
pip install -r requirements.txt
python start_gui.py
```

### 使用步骤
1. 在左侧选择你的服务器版本（默认已适配 **雨课堂标准版 `https://www.yuketang.cn/web`** 与 Edge 浏览器，也可一键切换到荷塘/长江/黄河或填自定义网址；如需 AI 做题请填入 DeepSeek API Key）。
2. 点击 **「打开浏览器扫码」**，在弹出的浏览器窗口中用微信扫码登录（登录态会自动保存）。
3. 你可以直接手动点进目标课程目录页或具体课件/视频页，再点击 **「开始刷课」** 一键接管；或在左侧填写课程名称后直接点击 **「开始刷课」**。
4. 如果某门课程前端布局较特殊导致无法识别元素，点击 **「页面结构诊断」**，查看 `logs/diagnostics/` 下的 `dom_summary.json` 即可在 `core/selectors.py` 中快速补充对应选择器。

## 上传到 GitHub

本仓库已预置 `.gitignore`，会自动忽略包含 API Key 的 `config.json`、包含微信登录 Cookie 的 `browser_profile/` 以及 `logs/` 目录，确保隐私安全。

首次推送到你自己的 GitHub 仓库前，执行以下命令即可：

```bash
# 如尚未设置 Git 身份，先配置你的 GitHub 用户名与邮箱
git config user.name "你的用户名"
git config user.email "你的邮箱@example.com"

# 关联你的 GitHub 远程仓库并推送
git remote add origin https://github.com/<你的用户名>/<你的仓库名>.git
git branch -M main
git push -u origin main
```

## 运行单元测试

```bash
python -m unittest discover -s tests -v
```

## 免责声明

本项目仅供学习 Selenium 浏览器自动化、GUI 工程化与大模型 API 集成技术研究使用，请合理安排学习时间并遵守所在学校与平台的相关规定。
