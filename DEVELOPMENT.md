# 开发与发布说明

本文档面向维护者，记录测试、打包和 GitHub 发布方式。产品安装与操作步骤见 [使用说明书](使用说明书.md)。

## 本地验证

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -m py_compile app.py meltwater_source.py
node --test tests/test_meltwater_accounts.mjs
node --check meltwater-studio/ui/server.mjs
node --check static/library.js
```

启动后可访问 `http://127.0.0.1:8000/api/meltwater/status` 检查内置融文服务。融文实际检索需要有权限的账号；自动化测试不使用真实登录令牌。

## 生成发布包

Windows 启动器源代码在 `launcher/Program.cs`，可用 `pwsh -File launcher/build.ps1` 重新编译。

```powershell
python tools/package_release.py --platform windows-x64 --version 0.1.0
python tools/package_release.py --platform macos-arm64 --version 0.1.0
python tools/package_release.py --platform linux-x64 --version 0.1.0
```

生成的 ZIP 存于被忽略的 `dist/`。本机可验证 Windows 包；macOS 和 Linux 的运行检查应在相应系统上完成。[发布工作流](.github/workflows/release.yml) 使用 Windows、macOS、Linux 运行器执行测试与启动检查。将 `v` 开头的版本标签推送到已配置的 GitHub 仓库后，工作流会把三个 ZIP 附到 GitHub Release。

## 提交范围

提交应用源码、[使用说明书](使用说明书.md)、`requirements.txt`、`.env.example`、浏览器扩展和内置融文研究台。`.gitignore` 排除 `.env`、`.venv/`、`data/`、`dist/`、`output/` 与 `课程作业资料-不上传/`。提交前检查暂存文件，避免上传 API Key、数据库密码、账号资料、私人 PDF 原文或融文导出记录。课程任务书、评分说明和产品设计说明书只存于本机私有文件夹。
