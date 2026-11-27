# 贡献指南（Contributing）

感谢你关注并参与 `yuketang_auto` 开源项目！为保持代码整洁、易维护，提交代码或 Issue 时请遵循以下约定。

## 代码架构原则

1. **职责单一、避免硬编码散落**：
   - 所有雨课堂前端 CSS/XPath 选择器与文本关键词统一维护在 `core/selectors.py`；
   - 界面逻辑（`app/gui.py`）与自动化业务调度（`core/`、`tasks/`）保持解耦，不在 GUI 线程中执行阻塞的浏览器操作。
2. **关键机制保留清晰注释**：
   - 针对虚拟滚动、CDP 防失焦、Vue 表单事件同步等容易踩坑的逻辑，请在注释中说明“为什么这么写”。
3. **严禁提交隐私数据**：
   - 切勿提交 `config.json`、`browser_profile/`、`logs/` 或任何个人 API Key / 学号 / 课程截图隐私信息。

## 本地开发与测试

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# 语法检查与单元测试
.venv\Scripts\python.exe -m compileall -q app core tasks tests start_gui.py
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

## 提交规范建议

建议使用简洁的语义化前缀：
- `feat:` 新增功能或适配新页面类型
- `fix:` 修复 Bug 或选择器失效
- `perf:` 性能优化
- `docs:` 文档与注释完善
- `chore:` 依赖、CI 或配置调整
