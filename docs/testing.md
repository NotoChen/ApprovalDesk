# 构建与验证

公开仓库保留源码、自动检查、模拟数据预览与发行说明。本机账号、配置、真实头像及逐次实机验收材料保留在本地，不进入源码包和安装包。

## GitHub Actions

[`macOS CI and Release`](../.github/workflows/macos.yml) 在 `main` 推送、Pull Request 和版本标签上执行，也可手动运行。

| 架构 | Runner | 工具链 |
| --- | --- | --- |
| Apple Silicon / arm64 | `macos-26` | Xcode 26.3 |
| Intel / x86_64 | `macos-15-intel` | Xcode 26.3 |

每个架构独立执行 Python 引擎检查、原生 XCTest、组件下载、Release 构建、DMG/ZIP 生成和安装包检查。安装包检查使用临时目录和系统 PATH，验证：

- 应用、Python、DWS、glab 和全部动态库的架构一致。
- 动态库不依赖构建机的 Homebrew 或个人 Python 路径。
- 内置 Python 能独立运行，DWS/glab 返回预期版本。
- Worker 能启动、报告正确版本并退出，不读取账号、不监听消息、不执行审批。
- DWS 在应用包外展开运行组件，运行前后应用文件及严格签名保持一致。
- ZIP 解压和 DMG 挂载后的 App 与原始构建逐文件一致，并能独立完成相同检查。
- DMG 包含 Applications 快捷方式和安装说明。

两个架构都成功后，版本标签对应的任务才会创建草稿 Release、上传四个安装文件与 `SHA256SUMS.txt`，最后公开发布。安装包会一直保留在 Release；普通 CI 产物保留 14 天。已公开的同版本安装包不会被工作流覆盖。

## 本地检查

```sh
python3 -B -m unittest discover -s Tests/EngineTests -v
python3 -B scripts/check_models.py
python3 -B scripts/release_metadata.py
```

仅安装 Command Line Tools 时，`check_models.py` 使用同一组原生断言，不依赖 XCTest。安装完整 Xcode 后可运行 `swift test -j 2`。

GUI 预览由 `scripts/render_previews.py` 渲染实际 SwiftUI 视图及 AppKit 设置窗口，全部请求使用模拟数据。它用于检查深浅色、布局、原生窗口控件和图标；真实桌面上的动态玻璃合成仍以实机显示为准。

所有自动检查都不需要钉钉或 GitLab 账号，不会发送消息或执行真实审批、合并。修改既有处理流程时，应保留引擎和原生状态模型的回归覆盖。
