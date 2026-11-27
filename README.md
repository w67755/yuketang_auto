# 雨课堂自动学习助手（DeepSeek 版）

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg)](https://www.python.org/)
[![Selenium 4](https://img.shields.io/badge/Selenium-4.x-43B02A.svg)](https://www.selenium.dev/)
[![CI](https://github.com/w67755/yuketang_auto/actions/workflows/ci.yml/badge.svg)](https://github.com/w67755/yuketang_auto/actions/workflows/ci.yml)

一款基于 **Selenium 4 浏览器自动化 + DeepSeek API + CustomTkinter 桌面界面** 构建的雨课堂网页版辅助工具。代码采用模块化分层架构与集中式选择器注册表，支持**自动看视频、自动看课件（逐页停留 + 漏页定向补看）以及 DeepSeek AI 自动做题**。

---

## 核心特性

| 模块 | 能力说明 |
| --- | --- |
| **四大服务器一键切换** | 内置「雨课堂标准版 / 荷塘·雨课堂 / 长江·雨课堂 / 黄河·雨课堂」官方入口预设，并支持高校私有部署自定义域名 |
| **微信扫码登录态持久化** | 使用独立浏览器配置目录（`browser_profile/`）保留登录态，免重复扫码；支持填课程名自动进课或手动进课后一键接管 |
| **视频稳健观看** | 仅使用雨课堂原生倍速（`1.0x / 1.5x / 2.0x`），内置静音播放、CDP 防失焦/切屏暂停守护与意外暂停自动恢复 |
| **课件虚拟列表顺序翻页与补看** | 专门适配雨课堂左侧虚拟滚动缩略图栏：按真实页码 `1 → N` 顺序滚动定位、自动跳过已标「已完成」页、每页随机停留（默认 `5~8s`），并校验顶栏 `学习进度` 与漏页提示自动加长停留补看 |
| **DeepSeek API 自动做题** | 默认采用响应更快的 `deepseek-v4.1-flash`（下拉支持切换 `deepseek-v4-pro`、`deepseek-v4-flash`、多模型自动回退等），支持单选/多选/判断/填空/简答识别、Vue 表单回填与自动提交 |
| **异常留存与一键页面诊断** | 任务异常自动保存截图与 `dom_summary.json` 摘要并跳过继续下一项；界面提供「页面诊断」按钮，方便针对不同课程前端快速校准 `core/selectors.py` |

---

## 快速开始

### 环境要求
- Windows 10 / 11（推荐，自带 Microsoft Edge 浏览器，也可切换 Chrome）
- Python 3.10 及以上（已兼容至 Python 3.15）
- 无需手动下载浏览器驱动（Selenium 4 内置 Selenium Manager 自动匹配）

### 方式一：Windows 一键启动（推荐）
1. 克隆或下载本项目到本地：
   ```bash
   git clone https://github.com/w67755/yuketang_auto.git
   cd yuketang_auto
   ```
2. 双击运行 **`一键启动.bat`**，脚本会自动创建 `.venv` 虚拟环境、安装依赖并打开桌面控制台。

### 方式二：命令行运行
```bash
pip install -r requirements.txt
python start_gui.py
```

### 使用步骤
1. 在左侧选择你的**服务器版本**（默认 `雨课堂（标准版）` `https://www.yuketang.cn/web`），勾选需要启用的功能（看视频 / 看课件 / AI 做题）。
2. 如需 AI 自动做题，填入你的 **DeepSeek API Key**（默认模型 `deepseek-v4.1-flash`，可在下拉框切换）。
3. 点击 **「打开浏览器扫码」**，在弹出的 Edge/Chrome 窗口中用微信扫码登录。
4. 既可以在左侧填写课程名称（支持忽略空格匹配），也可以直接在浏览器里手动点进目标课程目录页或具体课件/视频页，再点击 **「开始刷课」** 一键接管。

---

## 项目架构

```text
yuketang_auto/
├── start_gui.py          # 桌面 GUI 启动入口
├── 一键启动.bat          # Windows 一键虚拟环境与启动脚本（ASCII + CRLF）
├── requirements.txt      # 精简纯 Python 依赖（selenium / customtkinter / requests）
├── config.example.json   # 公开配置模板（不含密钥）
├── app/
│   ├── config.py         # AppConfig 数据类、预设与参数边界校验
│   ├── logger.py         # 终端 + 按天文件 + GUI 线程安全队列日志
│   └── gui.py            # CustomTkinter 桌面控制台（深色高对比度日志区）
├── core/
│   ├── browser.py        # Selenium 4 浏览器生命周期、登录态持久化与 CDP 防失焦注入
│   ├── selectors.py      # 雨课堂多版本页面选择器集中配置（适配改版只需改这里）
│   ├── diagnostics.py    # 异常截图与页面 DOM 结构摘要导出
│   ├── auth.py           # 微信扫码登录检测与忽略空格课程匹配
│   └── scheduler.py      # 学习日志/章节扫描、进度满格跳过、合集展开与顺序调度
├── tasks/
│   ├── base.py           # 任务抽象基类与执行结果状态模型
│   ├── watch_video.py    # 视频播放、原生倍速与卡顿恢复
│   ├── watch_ppt.py      # 虚拟滚动缩略图按页码顺序翻页、随机停留与漏页补看
│   ├── deepseek_client.py# DeepSeek Chat Completions API 客户端与多模型回退
│   └── auto_quiz.py      # 题目提取、选项/文本回填（Vue 事件触发）与自动提交
└── tests/
    └── test_core.py      # 核心解析与配置单元测试
```

---

## 常见问题（FAQ）

**Q1：如果我的学校页面布局略有不同，部分按钮或课件未识别怎么办？**  
在进入对应页面后点击界面上的 **「页面诊断」**，程序会在 `logs/diagnostics/` 生成 `dom_summary.json`（不含敏感 Cookie）。参考其中的 `className` 与文本更新 `core/selectors.py`，或脱敏后提交 [页面适配 Issue](https://github.com/w67755/yuketang_auto/issues/new?template=selector_adaptation.md)。

**Q2：提交 PR 或自己修改代码时，API Key 和登录 Cookie 会泄露吗？**  
不会。`.gitignore` 已默认忽略 `config.json`、`browser_profile/`、`logs/` 和 `.venv/`。请勿手动强制 `git add -f config.json`。

**Q3：课件每一页停留多久合适？**  
默认 `5.0 ~ 8.0` 秒随机停留，已能覆盖绝大多数雨课堂课程的单页计分阈值。如果个别课程要求更长停留时间，可在界面直接调大区间，或依赖内置的「未满进度自动加长补看」机制。

---

## 开发测试与贡献

欢迎提交 Issue 与 Pull Request！详见 [CONTRIBUTING.md](CONTRIBUTING.md)。

```bash
# 运行单元测试
python -m unittest discover -s tests -v
```

---

## 开源协议与免责声明

- 本项目基于 [MIT License](LICENSE) 开源。
- **免责声明**：本项目仅用于学习与研究 Selenium 浏览器自动化、桌面 GUI 工程化、虚拟列表交互及大模型 API 集成技术。请合理安排学习时间，遵守所在学校及平台的使用规范，因违规使用造成的一切后果由使用者自行承担。
