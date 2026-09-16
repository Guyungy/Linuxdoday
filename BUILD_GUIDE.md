# Linux.do 刷帖助手 - 多平台打包指南

## Windows 版本

Windows exe 已打包完成，位于 `dist/LinuxDoHelper_v8.0_Windows.exe`

### 直接运行
双击 `LinuxDoHelper_v8.0_Windows.exe` 即可运行

---

## macOS 版本打包指南

### 环境准备

```bash
# 1. 安装 Python 3.8+
brew install python@3.11

# 2. 安装依赖
pip3 install DrissionPage pyinstaller

# 3. 安装 Chrome 浏览器
# 从 https://www.google.com/chrome/ 下载安装
```

### 打包命令

```bash
# 进入项目目录
cd /path/to/linuxdo

# 执行打包
pyinstaller --onefile --windowed \
    --name "LinuxDoHelper_v8.0_macOS" \
    --hidden-import tkinter \
    --hidden-import tkinter.ttk \
    --hidden-import tkinter.scrolledtext \
    --hidden-import DrissionPage \
    --clean --noconfirm \
    linux_do_gui.py

# 或者使用打包脚本
python3 build.py
```

### 输出文件
- `dist/LinuxDoHelper_v8.0_macOS` (可执行文件)

### 运行方式
```bash
# 赋予执行权限
chmod +x dist/LinuxDoHelper_v8.0_macOS

# 运行
./dist/LinuxDoHelper_v8.0_macOS
```

### macOS 安全提示
首次运行可能提示"无法验证开发者"，解决方法：
1. 系统偏好设置 → 安全性与隐私 → 通用
2. 点击"仍要打开"

---

## Linux 版本打包指南

### 环境准备

```bash
# Ubuntu/Debian
sudo apt update
sudo apt install python3 python3-pip python3-tk

# CentOS/RHEL
sudo yum install python3 python3-pip python3-tkinter

# Arch Linux
sudo pacman -S python python-pip tk

# 安装依赖
pip3 install DrissionPage pyinstaller

# 安装 Chrome 浏览器
# Ubuntu/Debian
wget https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo dpkg -i google-chrome-stable_current_amd64.deb
sudo apt-get install -f
```

### 打包命令

```bash
# 进入项目目录
cd /path/to/linuxdo

# 执行打包
pyinstaller --onefile --windowed \
    --name "LinuxDoHelper_v8.0_Linux" \
    --hidden-import tkinter \
    --hidden-import tkinter.ttk \
    --hidden-import tkinter.scrolledtext \
    --hidden-import DrissionPage \
    --clean --noconfirm \
    linux_do_gui.py

# 或者使用打包脚本
python3 build.py
```

### 输出文件
- `dist/LinuxDoHelper_v8.0_Linux` (可执行文件)

### 运行方式
```bash
# 赋予执行权限
chmod +x dist/LinuxDoHelper_v8.0_Linux

# 运行
./dist/LinuxDoHelper_v8.0_Linux
```

### Linux 注意事项
1. 需要图形界面环境 (X11/Wayland)
2. 如果使用 WSL，需要配置 X Server
3. 无头服务器无法运行 GUI 程序

---

## 通用注意事项

### 运行要求
1. **Chrome 浏览器**: 必须安装 Chrome 浏览器
2. **网络连接**: 需要能访问 linux.do
3. **代理设置**: 如需代理，在程序中配置

### 常见问题

#### Q: 程序启动后没有反应
A: 检查是否安装了 Chrome 浏览器

#### Q: 提示找不到 chromedriver
A: DrissionPage 会自动下载，确保网络通畅

#### Q: macOS 提示"已损坏，无法打开"
A: 执行 `xattr -cr /path/to/LinuxDoHelper_v8.0_macOS`

#### Q: Linux 提示 tkinter 相关错误
A: 安装 python3-tk 包

---

## 从源码运行

如果打包版本有问题，可以直接从源码运行：

```bash
# 安装依赖
pip install DrissionPage

# 运行
python linux_do_gui.py
```

---

## 文件说明

```
linuxdo/
├── linux_do_gui.py          # 主程序
├── build.py                  # 打包脚本
├── README.md                 # 项目说明
├── BUILD_GUIDE.md           # 本文档
└── dist/
    └── LinuxDoHelper_v8.0_Windows.exe  # Windows 可执行文件
```
