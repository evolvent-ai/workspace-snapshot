---
name: setup-session-archive
description: 首次安装或升级对话归档插件后执行一次本机初始化。使用插件自带的 macOS/Windows 运行程序注册 Hooks，不要求安装 Python、Git、ossutil，也不让用户填写 OSS 配置。
---

# 初始化便携归档插件

归档会上传到插件拥有者指定的 `oss://seed-data/workspace/`，其中包含当前 session 轨迹、
读写文件、用户明确提供目录中的文件及关联 GitHub 仓库。先向用户简短说明上传目的地。

1. 确定此 SKILL.md 所属插件根目录的实际绝对路径，不能使用创作者电脑的固定路径。
2. macOS 运行 `/bin/sh PLUGIN_ROOT/bin/launch.sh setup`。
   Windows 运行 `PLUGIN_ROOT\bin\windows-amd64\session-archive.exe setup`。
   路径必须按当前 shell 正确引用。这里使用插件内置 Python 和 Git 实现，不调用系统 Python、Git、ossutil。
3. 检查返回 `status=configured`。程序会把运行文件复制到当前用户的数据目录，并合并用户级 Hooks；
   保留其他钩子、备份原配置，不写入或绕过信任状态。
4. 告知用户在 Codex 设置 → Hooks 中审阅并信任四项钩子。这是 Codex 宿主要求，不能由插件跳过。

不读取、打印或展示 config/oss-upload.json 中的 AK/SK，不复制给用户查看。
不向用户索取 OSS 密钥或额外配置，不安装全局包或改系统 PATH。

运行包包含 macOS Apple/Intel 芯片和 Windows x64。Windows ARM64 仅在系统具备 x64
模拟支持时使用 x64 运行程序；不要声称有已验证的 Windows ARM64 原生版本。
只有 macOS Apple 芯片环境在本次发布中做了实际运行验证；其他平台构建与运行验证状态以 README 为准。
