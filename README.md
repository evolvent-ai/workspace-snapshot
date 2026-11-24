# Session 归档到 OSS

每次完整回复结束后，后台把**当前 session 的原始轨迹、读写文件版本、涉及的 GitHub 仓库**
压缩上传到 `oss://seed-data/workspace/sessions/`，对象始终设为 private。
便携版内置运行环境与专用只写上传身份，不要求另外安装 Python、Git 或 ossutil。

更新插件后，在 Codex **设置 → Hooks**（CLI 为 `/hooks`）审阅并信任新增或变更的 Hook。
插件使用同步 PreToolUse / PostToolUse 采集工具调用前后的文件内容，以异步 Stop 后台上传。
不需要每轮手动指定文件；普通对话即使没有文件，也会上传 session 轨迹。

每轮 ZIP 包含：

- `trajectory/rollout.jsonl`：宿主提供的当前 session 原始轨迹，保留消息、工具参数、工具结果等原始记录；不读取其他 session。
- `trajectory/stop-event.json`：本轮结束事件和最终回复，补充轨迹尚未刷入的结束信息。
- `files/accesses.jsonl`：工具、调用 ID、文件路径、读写类型/推断依据、采集时机、状态、SHA256。
- `files/blobs/<sha256>`：去重的文件内容；已采集的旧版本在覆盖、删除后仍保留。
- `repositories/*.zip`：每个 GitHub 仓库的完整当前工作区（包含未提交、未跟踪、忽略文件和依赖目录），以及可验证恢复的 `git/history.bundle`。
- `MANIFEST.json`：关联关系、仓库版本和采集缺口；有缺口时 `complete=false`，状态显示 `uploaded_partial`。

GitHub 仓库从工具实际访问文件所在的 Git remote、仓库操作和当前用户/助手消息中的
GitHub URL 识别。本地仓库自动导出，无本地 checkout 的 GitHub URL 则 clone --mirror
后导出，不执行仓库代码；私有仓库使用本机 Git 登录配置。Git worktree 通过内置 Git 实现
导出实际对象，避免只复制 `.git` 指针。浅克隆会尝试从远端另建完整镜像。
原始轨迹和完整工作区按原样保存，不沿用旧版“跳过凭据文件/依赖目录”的工作区过滤。
仓库中的符号链接按链接保存，不递归读取外部目标。

文件采集覆盖结构化文件工具、apply_patch、可识别的 shell 文件参数、Python 字面量路径，
以及 functions.exec 中常见的嵌套工具参数。Hook 不是操作系统文件审计器：任意程序内部
动态拼接的路径、目录搜索实际打开的所有文件不能保证枚举，相关调用会列入 coverage_gaps。
需要时可用 `hooks/auto_archive.py register --session-id ID --paths-json FILE --cwd DIR`
补充路径。启用前已覆盖/删除的版本无法恢复；从历史轨迹补采的是当前内容，明确标记。
子模块历史、LFS 对象历史未完整导出时也会记录缺口，不把部分归档报告成完整备份。

同一轮重复 Stop 不重复上传；每个新回复生成独立的累计快照，文件未变化也会上传。
每轮本地 ZIP、回执和状态保存在 `PLUGIN_DATA`；无该变量时为
`~/.local/share/conversation-oss-archive/`。失败保留本地包，不通过重启回复重试。
状态命令：`python3 hooks/auto_archive.py status --session-id ID`。
禁用插件或 Stop Hook 可停止自动上传。

环境变量 `CONVERSATION_OSS_DESTINATION`、`CONVERSATION_OSS_ENDPOINT` 可覆盖目标。
上传逐请求发送 Content-MD5，分片核对响应 ETag；不调用 HEAD、GET、ListParts 或 AbortMultipartUpload。

验证：`python3 -m unittest discover -s tests -v`。

协议参考：[Codex Hooks](https://learn.chatgpt.com/docs/hooks)、
[ossutil 上传](https://www.alibabacloud.com/help/en/oss/developer-reference/upload-objects-6)。

## 0.3.1 修复

- 修复中文长正文和 heredoc 脚本被误当路径而中断归档；历史调用单项失败记入清单。
- 拒绝 transcript 与 Stop 的 session ID 不一致，排除属于后续轮次的文件记录。
- 仓库识别不再把脚本或测试中的 GitHub URL 字符串当作实际仓库操作。
- OSS 目录显示真实 session ID 与 turn ID；旧版已上传对象保持原路径。
- `files/latest/` 保留可读的原始路径文件，`files/paths.json` 提供来源映射。

## 0.3.2 用户引用目录递归采集

支持 Files mentioned by the user 的目录附件、用户消息中的明确目录路径，以及手动登记的目录。
UserPromptSubmit 保存输入目录文件快照，Stop 从当前 session 轨迹恢复目录选择并再次采集，
因此早期版本只记录路径的历史消息也能在新一轮归档时补采当前目录内容。
Markdown、图片、ZIP、隐藏文件和嵌套子目录都会保存，不需要先被单文件工具读取。
文件直接出现在 `files/latest/` 的原绝对路径树中，同时进入内容去重的 blobs 和 paths.json。
空目录保留；MANIFEST 的 referenced_directories 列出文件数、总字节和采集时间。

仅递归用户明确提供/登记的目录。环境 cwd、workspace_roots、工具 ls/rg 搜索偶遇目录、
助手举例目录不会触发整目录递归。符号链接不跟随到外部目标；不可读、缺失与跳过项会记录。
已有 OSS 包保持原样，新归档使用新增采集规则。


## 0.4.0 便携运行环境与只写上传

目标固定为 `oss://seed-data/workspace/`，杭州 endpoint。上传身份由插件维护者打包，
仅需要该前缀的 `oss:PutObject` 权限，不需要读取权限，也不调用上传后 HEAD/stat。
普通上传与分片上传都通过 Content-MD5 服务端校验，分片同时核对 ETag。
内置凭据保存在 config/oss-upload.json，不得回显或作为普通采集文件读取；源码树不保存凭据。
不要求独立授权服务。拥有插件包的人可以提取共享只写凭据，维护者负责其权限范围和轮换。

内置 macOS arm64、macOS x64、Windows x64 的 Python 运行环境、纯 Python Git 实现和
原生启动程序；生产路径不调用系统 Python、Git、ossutil。私有 GitHub 仓库仍受使用者自身
访问权限约束，无权限的远端导出会记录缺口，不能靠 OSS 上传身份读取 GitHub。

首次安装后运行插件的“初始化”流程，程序自动合并用户级 Hooks，使用者在 Codex 设置中
信任四项 Hook 即可。不安装系统软件、不改 PATH，不自动修改任何 Hook 信任哈希。
本次实际运行验证平台为 macOS arm64；macOS x64 和 Windows x64 已打包构建，未在本机
进行原生系统运行验证。Windows ARM64 需系统 x64 模拟支持。


## GitHub 源码与安装包

本仓库保存源码、测试、权限策略和可复现构建入口。源码不包含真实 AK/SK、个人会话轨迹、
本机回执、CPython 运行时或已生成的 ZIP。可直接安装的便携包通过本仓库的私有 Releases 分发；
这些便携包按插件拥有者的要求包含专用只写上传凭据，必须保持在授权的私有范围内。
GitHub 源码推送不会自动更新 ChatGPT 账户中已发布的插件版本。

使用者下载私有 Release 的插件 ZIP，通过 Plugin Creator 或可用的 ZIP 上传入口安装，运行
插件提供的“初始化”流程，并在 Codex 设置 → Hooks 审阅信任四项钩子。无需另装 Python、Git、ossutil。

### 开发验证

构建机器需要 Python 3.12+ 和 Go（使用者不需要这些开发工具）。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
CONVERSATION_OSS_PORTABLE_GIT=1 PYTHONPATH=hooks .venv/bin/python -m unittest discover -s tests -v
```

### 构建便携插件

在 macOS 或 Linux 构建主机上，将 `config/oss-upload.example.json` 复制到仓库外的私有文件，
由维护者填入仅有 `seed-data/workspace/*` 的 `oss:PutObject` 权限的凭据。不要将真实凭据提交 Git。

```bash
.venv/bin/python scripts/build_portable.py --credentials-file /private/path/oss-upload.json
```

构建会核对 `deploy/runtime-assets.json` 中固定版本的官方 CPython SHA256，打包纯 Python Git
依赖并交叉编译三个原生启动程序，输出到 `dist/`。运行时和生成物位于 Git 忽略目录。
实际构建和运行验证状态见上面的 0.4.0 说明。
