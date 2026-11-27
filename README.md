# Workspace Snapshot

归档当前 Codex session 的原始轨迹、工具读写文件版本、用户明确引用目录的递归子文件，以及关联 GitHub 仓库的工作区和 Git 历史。
每次完整回复结束后，后台上传到 `oss://seed-data/workspace/`，对象保持私有。

## 通过公司市场安装

市场已拆到独立私有仓库 [evolvent-ai/codex-marketplace](https://github.com/evolvent-ai/codex-marketplace)。
在桌面端 **插件 → 添加市场** 中填写：

- Source：`evolvent-ai/codex-marketplace`
- Git ref：`main`
- Sparse paths：留空

选择 **Evolvent AI** 市场，安装 **Workspace Snapshot**，运行初始化，再到 **设置 → Hooks** 信任四项。
CLI 等价操作：

```bash
codex plugin marketplace add evolvent-ai/codex-marketplace --ref main
codex plugin add workspace-snapshot@evolvent-ai
```

市场只存清单；通过 git-subdir 引用本仓库的 `plugins/workspace-snapshot/`。首次安装插件仍需下载约 90 MB 的完整便携包。
两个仓库都保持 Private，使用者需要两者的读取权限。

## 分发与凭据

根目录是无真实凭据的开发源码。`plugins/workspace-snapshot/` 是可安装的市场包装层，包含完整便携 ZIP 和带 SHA256 校验的原生启动器。
便携 ZIP 按维护者授权内置共享只写身份，仅需要指定前缀的 `oss:PutObject`；拥有安装包的人可以提取该身份，因此仅限授权私有分发。
运行程序不调用系统 Python、Git 或 ossutil。私有 GitHub 仓库仍需要使用者自身的访问权限。

## 归档内容与边界

- `trajectory/rollout.jsonl`、`trajectory/stop-event.json`：当前 session 轨迹及结束事件。
- `files/accesses.jsonl`、`files/blobs/`：工具关联路径、版本内容及采集依据。
- `files/latest/`、`files/paths.json`：保留原路径的文件副本与映射。
- `repositories/*.zip`：工作区与可恢复的 Git history.bundle。
- `MANIFEST.json`：session 身份、引用目录统计、完整性和采集缺口。

用户明确提供目录时递归采集 Markdown、图片、ZIP、隐藏文件与子目录；不展开搜索偶遇目录，不跟随符号链接逃出输入树。
Hook 不等同于 OS 文件审计：任意程序内部动态访问不能保证枚举，历史丢失版本无法恢复；缺口标记为 uploaded_partial，不能冒充完整。
上传逐请求发送 Content-MD5，并核对分片 ETag，不调用 HEAD/GET/ListParts/AbortMultipartUpload。

## 开发与构建

开发机器需要 Python 3.12+ 和 Go，终端使用者不需要安装这些开发工具。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.txt
CONVERSATION_OSS_PORTABLE_GIT=1 PYTHONPATH=hooks .venv/bin/python -m unittest discover -s tests -v
.venv/bin/python scripts/build_portable.py --credentials-file /private/path/oss-upload.json
python3 scripts/update_marketplace_payload.py --payload /private/path/workspace-snapshot-NEW.zip --version NEW_VERSION
```

真实凭据不得提交为源码；使用 config/oss-upload.example.json 了解格式。
运行时下载和校验固定在 deploy/runtime-assets.json；市场 payload 更新后必须重新编译 bootstrap，不能使用 Git LFS 指针替换 ZIP。

## 兼容迁移

插件和仓库从 conversation-oss-archive 更名为 workspace-snapshot，既有数据目录 `~/.local/share/conversation-oss-archive/` 保留，避免丢失历史回执和重复运行。
初始化会替换同一归档器的旧用户级 Hook，不自动修改信任状态。旧市场入口应改为新的公司市场。
本次包包含 macOS arm64、macOS x64、Windows x64；实际原生验证平台为 macOS arm64，其他平台为交叉构建。
