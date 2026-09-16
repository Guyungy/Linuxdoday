# 构建、安装 Linuxdoday.app

Linuxdoday 可构建为标准 macOS `.app` 应用包，然后安装到“应用程序”并通过 Finder、Spotlight 或启动台打开。不需要用户在每次启动时打开终端。

## 本机构建与安装

```bash
chmod +x mac_setup.sh build_mac_app.sh install_mac_app.sh
./build_mac_app.sh
./install_mac_app.sh
```

默认安装到 `~/Applications/Linuxdoday.app` 并立即打开，不需要管理员密码。

如需安装到所有用户共用的 `/Applications`：

```bash
./install_mac_app.sh --system
```

## 输出

- 应用包：`dist/Linuxdoday.app`
- Bundle ID：`com.guyungy.linuxdoday`
- 最低系统：macOS 12
- 架构：与构建 Mac 一致（Apple Silicon 构建 arm64，Intel Mac 构建 x86_64）

## 签名说明

本地构建使用 ad-hoc 签名，适合在自己的 Mac 上安装。如果要把 `.app` 发给其他人，建议使用 Apple Developer ID 签名并进行 notarization，否则 Gatekeeper 可能显示未验证开发者提示。
