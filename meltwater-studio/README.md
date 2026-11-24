# 内置融文研究台

这部分源码从作者本机的 `meltwater_discourse_agent/ui` 迁入溯源项目，并接入 `app.py` 的启动流程。运行溯源时，Python 启动 `ui/server.mjs`，服务只监听 `127.0.0.1:8789`。源码使用 Node.js 22+ 内置模块，没有 npm 运行依赖；需要 Chrome 和用户本人有权限访问的融文账号。

`ui/accounts.mjs` 管理当前 Chrome 账号和最多七个独立浏览器资料。账号清单保存标签、邮箱和账号 ID，页面显示当前连接状态；短时授权只留在进程内存中。未登录或连接失败的独立账号可以删除，删除操作同时清理其浏览器资料目录。当前 Chrome 账号是固定入口，不能删除。默认数据目录为 `meltwater-studio/data/`；由溯源启动时改用项目的 `data/meltwater-studio/`，两者都被 `.gitignore` 排除。

`ui/server.mjs` 按月份分页检索融文记录，导出 JSONL。`meltwater_source.py` 再核对平台命中数、文件行数和唯一文档 ID，核对通过后才入 MySQL 和本地检索索引。导出只含平台提供的元数据与短匹配片段，并非文章全文。

独立启动调试：

```powershell
node ui/server.mjs
```

测试：

```powershell
node --test ../tests/test_meltwater_accounts.mjs
```
