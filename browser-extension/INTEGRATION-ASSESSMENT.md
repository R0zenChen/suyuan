# 将 Bypass Paywalls Clean 并入溯源 Clipper：可行性检查

检查对象：用户提供的 Bypass Paywalls Clean 源码目录，版本 4.4.6.4（2026-11-07 检查）。该源码目录不属于本项目，也不需要随本项目上传。

## 结论

技术上可以做成一个 Chrome / Edge 扩展，但不能直接把两个 `manifest.json` 拼接。溯源 Clipper 目前只在用户点击时读取当前标签页，权限为 `activeTab`、`scripting`、`storage` 和本机服务地址；Bypass Paywalls Clean 另有后台 service worker、按站点更新的请求规则、内容脚本、Cookie 操作和配置页面。它的 manifest 列出 973 条站点权限，并占用扩展的唯一 `action.default_popup`。直接合并会显著扩大权限，并改变现有“一键保存”入口。

该项目以 MIT License 发布。若移植其代码，须在发布包中保留原版权声明和许可文本；修改后的功能仍需逐站测试和维护。

## 站点示例

- `BBC USA` 规则针对 `bbc.com`，允许 Cookie，并拦截与 Tinypass 相关的请求。
- `Bloomberg (not Terminal)` 规则针对 `bloomberg.com`，排除 Terminal，涉及站点样式请求和页面脚本处理。

这说明现有扩展包含相关规则，但不证明每篇报道都能取得全文，也不等于溯源的正文提取器能自动识别处理后的页面。

## 建议的集成路径

1. 保留当前轻量 Clipper 作为默认版，继续读取用户当前浏览器里实际可见的正文。
2. 若需要单扩展版，先做可选的“增强阅读”构建：以 Bypass Paywalls Clean 为基础保留其许可与依赖，将溯源的弹窗和本机导入加入其中；默认关闭增强阅读，明确显示站点权限。
3. 首轮只验证用户实际需要的 BBC 和 Bloomberg 页面：分别检查扩展权限、页面是否显示全文、Clipper 提取字数、日期与来源、导入和研判入库。通过后再扩展站点范围。
4. 在合并前继续支持双扩展并用：原扩展负责页面呈现，溯源 Clipper 只读取当前可见内容。这个流程已经可用，不依赖两个扩展之间的私有通信。

不建议把 973 条站点权限直接加入现有 Clipper 的默认安装包。范围、权限和维护成本都与它目前的本地剪藏功能不同。
