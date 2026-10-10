# 更新记录

## 0.1.0

首个 GitHub 发行版，包含已经完成实机验收的 macOS 原生审批台功能。

- 原生 SwiftUI + AppKit 界面，支持 Liquid Glass、深浅色、菜单栏常驻和统一图标。
- 通过钉钉接收审批请求，按来源群成员筛选发送人；显示真实头像和处理进度。
- 集成 GitLab 审批、合并、合并人核验及回执策略，正确处理已由其他人合并的 MR。
- 支持监听期间查询和选择群聊，同群重选不会误报需要重新监听。
- 锁屏、手动暂停、排队、队列溢出、并发、重试、通知和启动行为均可配置。
- 默认登录 Mac 后启动、自动监听、仅驻留菜单栏；暂停或锁屏暂停期间的新消息默认丢弃。
- 内置 Python 3.14.5、DWS 1.0.62 和 glab 1.121.0，无需额外安装运行组件。
- GitHub Actions 分别构建并检查 Apple Silicon 与 Intel 的 DMG、ZIP，提供 SHA-256 校验文件。

### 下载与安装

| Mac 类型 | 推荐安装包 |
| --- | --- |
| Apple Silicon（M 系列） | `ApprovalDesk-0.1.0-macos-arm64.dmg` |
| Intel | `ApprovalDesk-0.1.0-macos-x86_64.dmg` |

要求 macOS 14 或更新版本，macOS 26 及以上使用系统 Liquid Glass。打开 DMG，将审批台拖入 Applications。首次启动只显示菜单栏图标，可从菜单栏进入设置并登录账号。已有配置会继续保留。

当前安装包使用临时签名，未经过 Apple 公证。首次下载若被 macOS 拦截，请在「系统设置 → 隐私与安全性」中选择「仍要打开」。
