# Judger Demo 本地安装指南

**GitHub 代码 · 云端数据快照 · 本机 Ollama**

版本日期 2026 年 11 月 18 日 已核对仓库提交 4eaf085

本指南用于在一台新机器上安装裁研应用。代码从 GitHub 获取，SQLite 和 Qdrant 数据从 S3 或 Google Drive 下载到本地；目标机器使用 Ollama 下载并运行公开模型。模型未经微调，因此无需打包旧机器上的模型文件。

这里采用全新安装：复用裁判文书语料，账号、案件工作区、运行密钥和模型配置由新实例创建。旧案件及用户上传材料的整体迁移不属于本流程。

# 一 安装方案与前提

| **组件**         | **获取方式**                   | **安装后位置或用途** |
|------------------|--------------------------------|----------------------|
| 应用代码         | GitHub judger-demo             | 仓库根目录即应用目录 |
| SQLite 与 Qdrant | S3 或 Google Drive 数据包      | 仓库内 data/corpus   |
| 生成模型         | ollama pull qwen3:4b-instruct  | 本机回答与文书生成   |
| 嵌入模型         | ollama pull qwen3-embedding:4b | 本机检索向量 512 维  |

S3 和 Google Drive 仅用于分发数据包。运行时数据库保存在本机磁盘；当前代码使用嵌入式 Qdrant，不需要另装 Qdrant 服务或 Docker。

| **项目**   | **要求**                                                                            |
|------------|-------------------------------------------------------------------------------------|
| 操作系统   | macOS 或 Linux；Windows 使用 WSL2 Ubuntu，并把应用和 Ollama 都放在 WSL2 内。        |
| Python     | 建议 3.12，需有 venv，SQLite 需支持 FTS5。                                          |
| Node.js    | 本文使用 22.x，至少 22.12，并安装 npm。                                             |
| 其他工具   | Git、curl；使用 ZIP 数据包时需 unzip。                                              |
| 磁盘与网络 | 需联网下载依赖和模型。两个模型约 5 GB；结合数据、依赖和解压空间，建议预留约 20 GB。 |

执行顺序：准备环境 → 克隆代码 → 下载模型 → 下载数据 → 安装依赖 → 检查 → 启动与验收。

本文统一使用 ~/caiyan/judger-demo 作为安装目录。后续命令除特别说明外，均在该目录执行；下载地址和存储桶名称需替换为实际发布值。

# 二 安装基础软件与获取代码

## macOS

已有 Homebrew 时，安装 Git、Python 和 Node.js：

```bash
brew install git python@3.12 node@22
export PATH="$(brew --prefix node@22)/bin:$PATH"
```

随后按 Ollama 官方 macOS 安装说明安装并打开应用。没有 Homebrew 时，可从各软件官网安装对应版本。

## Ubuntu 24.04 或 WSL2 Ubuntu 24.04

以下 apt 命令以 Ubuntu 24.04 为例。其他发行版使用对应的软件包管理方式。

```bash
sudo apt update
sudo apt install -y git curl unzip python3.12 python3.12-venv
curl -fsSL https://raw.githubusercontent.com/nvm-sh/nvm/v0.41.0/install.sh \
  -o /tmp/judger-nvm-install.sh
bash /tmp/judger-nvm-install.sh
. "$HOME/.nvm/nvm.sh"
nvm install 22
nvm use 22
curl -fsSL https://ollama.com/install.sh -o /tmp/judger-ollama-install.sh
sh /tmp/judger-ollama-install.sh
```

Linux 上如 Ollama 服务尚未启动，在另一个终端运行 ollama serve，并保持该终端打开；已有服务时不要重复启动。macOS 打开 Ollama 应用即可。

## 检查版本并克隆代码

```bash
git --version
python3.12 --version
node --version
npm --version
ollama --version

mkdir -p "$HOME/caiyan"
cd "$HOME/caiyan"
git clone https://github.com/jonkiky/judger-demo.git
cd judger-demo
```

**仓库根目录应能看到 start.sh、run.py、requirements.txt、backend 和 frontend。无需再进入 chatbot-system-poc-1106 子目录。**

# 三 在本机下载模型

```bash
ollama pull qwen3:4b-instruct
ollama pull qwen3-embedding:4b
ollama list
curl -fsS http://127.0.0.1:11434/api/tags
```

必须能看到上述两个模型名称。caiyan-qwen4b-r1 和 caiyan-embedding4b-r1 是应用网关别名，不需要用 ollama cp 额外创建。模型服务与应用在同一机器上，默认地址为 http://127.0.0.1:11434。

# 四 下载并放置语料数据

建议发布名为 judger-corpus.tar.gz 的完整语料快照，压缩包顶层为 corpus。仅有原始 judgments.sqlite3 和一个 Qdrant 目录还不够；本流程需要同时提供关键词索引、筛选索引和版本文件。

```text
corpus/
├── manifest.json
├── release.json
├── judgments.sqlite3
├── lexical.sqlite3
├── filters.sqlite3
└── qdrant/
    ├── meta.json
    └── collection/
        └── judgment_passages/
            ├── storage.sqlite
            └── ...
```

qdrant 下应保留完整目录，而不是只挑出 storage.sqlite。以上所有文件应来自同一次完成并验证的数据发布。数据发布者还应提供压缩包 SHA-256、对应代码版本和建库时的嵌入模型版本信息。

## 选择一个下载方式

S3：若已安装 AWS CLI 并配置可读取对象的身份，执行以下命令。YOUR_BUCKET 和路径均为占位符。

```bash
mkdir -p "$HOME/Downloads"
aws s3 cp "s3://YOUR_BUCKET/path/judger-corpus.tar.gz" \
  "$HOME/Downloads/judger-corpus.tar.gz"
```

也可由发布者提供 HTTPS 预签名下载地址，此时无需 AWS CLI：

```bash
DATA_URL='REPLACE_WITH_PRESIGNED_HTTPS_URL'
curl -fL "$DATA_URL" -o "$HOME/Downloads/judger-corpus.tar.gz"
```

Google Drive：打开发布者提供的共享链接，下载数据包并保存到 Downloads，文件名统一为 judger-corpus.tar.gz。此处的文件名是发布约定，并非已上线的下载地址。

## 解压到应用数据目录

先用 macOS 的 shasum -a 256 或 Linux 的 sha256sum 核对发布者提供的哈希值，再解压：

```bash
cd "$HOME/caiyan/judger-demo"
mkdir -p data
tar -xzf "$HOME/Downloads/judger-corpus.tar.gz" -C data
ls data/corpus
```

最终位置必须是 judger-demo/data/corpus，避免出现 data/corpus/corpus 或 data/data/corpus。请勿把旧机器的 workspace.sqlite3、internal.key、local-credentials.json 或 model-profile.json 混入全新安装。

若发布者暂时使用现有 data.zip，且 ZIP 内是 data/corpus/...，可用下列命令替代上面的 tar 解压，只提取语料：

```bash
unzip "$HOME/Downloads/data.zip" 'data/corpus/*' -d .
```


# 五 安装应用依赖与核对数据

**在当前仓库版本中，setup.sh 会读取旧目录 ../judgment_research_poc/index_50k。本文使用已准备好的 corpus 数据包，所以直接安装依赖和构建前端，不运行 setup.sh。**

```bash
cd "$HOME/caiyan/judger-demo"
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
npm --prefix frontend ci
npm --prefix frontend run build
export CAIYAN_DATA_DIR="$PWD/data"
export OLLAMA_BASE_URL="http://127.0.0.1:11434"
```

运行下面的只读检查，确认必要文件、模型维度、筛选版本及 SQLite 基本完整性。此检查不执行向量重建，也不启动应用。

```bash
.venv/bin/python - <<'PY'
import json, sqlite3
from pathlib import Path
from backend.app.search_filters import SCHEMA_VERSION

c = Path("data/corpus").resolve()
required = ["manifest.json", "release.json", "judgments.sqlite3",
            "lexical.sqlite3", "filters.sqlite3", "qdrant/meta.json",
            "qdrant/collection/judgment_passages/storage.sqlite"]
for name in required:
    assert (c / name).is_file(), f"Missing: {name}"
manifest = json.loads((c / "manifest.json").read_text())
release = json.loads((c / "release.json").read_text())
assert manifest["embedding_model"] == "qwen3-embedding:4b"
assert manifest["embedding_dimensions"] == 512
with sqlite3.connect(":memory:") as db:
    db.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
for name in ["judgments.sqlite3", "lexical.sqlite3", "filters.sqlite3"]:
    with sqlite3.connect((c / name).as_uri() + "?mode=ro", uri=True) as db:
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok", name
        if name == "filters.sqlite3":
            info = json.loads(db.execute(
                "SELECT body FROM filter_manifest").fetchone()[0])
            assert info["release_id"] == release["release_id"]
            assert info["schema_version"] == SCHEMA_VERSION
print("Corpus checks passed:", release["release_id"])
PY
```

**检查通过后，当前 run.py 会复用 release.json 和版本匹配的 filters.sqlite3，因此正常启动无需原始 CSV，也无需下载相邻的 index_50k 目录。**

如果缺少筛选索引，或其版本与代码不一致，程序会尝试读取原始 CSV。请取得与当前代码匹配的完整快照；不要通过改写 manifest.json 或 release.json 来伪造版本。

# 六 检查模型并首次启动

模型未微调意味着可从 Ollama 重新下载标准模型。现有 Qdrant 向量仍与建库时的嵌入模型绑定：名称、权重版本和 512 维设置应匹配。维度正确只代表形状正确，不保证不同模型产生的向量能混用。

发布者若提供了建库模型指纹，请与 /api/tags 的 digest 对照。若权重版本不同，应先核实兼容性；确需更换嵌入模型时，重新构建索引。全新实例不复制旧 model-profile.json，网关首次成功检查时会记录本机模型配置。

## 检查生成与嵌入功能

```bash
ollama run qwen3:4b-instruct "请只回复：本地模型已就绪"

curl -fsS http://127.0.0.1:11434/api/embed \
  -H 'Content-Type: application/json' \
  -d '{"model":"qwen3-embedding:4b","input":"房屋纠纷","dimensions":512}' \
  | .venv/bin/python -c \
  'import json,sys; v=json.load(sys.stdin)["embeddings"][0]; print(len(v))'
```

第二条命令应输出 512。首次加载模型可能较慢；如果调用失败，先检查 Ollama 服务、模型下载状态及本机可用内存。

## 启动应用

```bash
cd "$HOME/caiyan/judger-demo"
export CAIYAN_DATA_DIR="$PWD/data"
export OLLAMA_BASE_URL="http://127.0.0.1:11434"
chmod +x start.sh
./start.sh
```

保持该终端运行。启动器会启动 API、LiteLLM 网关、检索服务和后台 worker；SQLite 与本地 Qdrant 由这些进程直接使用。

| **端口** | **用途**           |
|----------|--------------------|
| 11434    | Ollama 模型服务    |
| 8765     | Web 页面与应用 API |
| 8766     | 应用内部模型网关   |
| 8767     | 应用内部检索服务   |

## 首次登录

在目标机器浏览器打开 http://127.0.0.1:8765。另开终端，读取新实例自动生成的账号密码：

```bash
cd "$HOME/caiyan/judger-demo"
cat data/local-credentials.json
```

首次启动会创建工作区数据库、检查点、上传目录和内部密钥。网页可立即检索已有裁判文书；“我的案件”初始为空，需要自行新建案件和上传材料。

# 七 验收与日常启动

在安装验收中完成以下检查。仅打开网页或 /healthz 返回 alive，不能证明模型与检索功能都可用。

- 登录成功，工作台显示语料数量；当前 50k 数据发布应为 50,000 份文书、83,353 个向量片段，其他发布以 release.json 为准。

- 登录后在同一浏览器访问 http://127.0.0.1:8765/api/v1/health，services.search 和 services.gateway 均应为 ready。

- 分别执行关键词检索和语义检索，打开返回文书并核对原文。

- 创建一个测试案件，上传 TXT 或 DOCX，选择来源并完成一次 AI 问答。

- 生成或下载一份 Word 文档，确认上传、后台任务和导出流程可用。

以后启动时，先确认 Ollama 已运行，再重复上一页“启动应用”的命令。按 Ctrl+C 停止应用进程；Ollama 独立运行，需要单独退出。不要让两套进程同时使用同一份 Qdrant 目录。

## 若安装在远程实例

当前应用只监听回环地址。在自己的电脑建立 SSH 转发，再打开本机浏览器；用户名和实例地址替换为实际值：

```bash
ssh -L 8765:127.0.0.1:8765 USER@INSTANCE_HOST
```

## 常见问题

| **现象**                    | **检查与处理**                                                                             |
|-----------------------------|--------------------------------------------------------------------------------------------|
| 找不到 index_50k 或原始 CSV | 确认没有运行旧 setup.sh；检查 data/corpus/release.json、filters.sqlite3 及其版本是否齐全。 |
| Ollama 或批准的模型不可用   | 检查 11434 端口以及 ollama list，确认两个模型名称完全匹配。                                |
| 本地模型映射已变化          | 新安装是否误带旧 model-profile.json；已投入使用的实例要按模型变更处理，不直接删文件绕过。  |
| Qdrant 文件锁冲突           | 停止重复的应用或检索进程；每份本地 Qdrant 存储只由一套检索进程打开。                       |
| 筛选版本或发布版本不一致    | 重新取得匹配本代码提交的完整数据包，避免混用不同发布中的 SQLite 和 Qdrant。                |
| 前端构建失败或找不到 npm    | 确认 Node.js 22.x 至少 22.12，检查当前终端 PATH，再运行 npm ci 和 npm run build。          |
| 启动正常但模型回复失败      | 查看 gateway.log 和 worker.log；确认模型测试成功及有足够内存，CPU 推理可能较慢。           |

日志位于应用 data/logs 下，可在另一个终端查看：

```bash
cd "$HOME/caiyan/judger-demo"
tail -n 80 data/logs/api.log data/logs/search.log \
  data/logs/gateway.log data/logs/worker.log
```

# 附录 数据发布与参考资料

## 数据发布者的打包约定

本附录供准备 S3 或 Google Drive 数据包的人使用，安装者已有完整数据包时可跳过。先停止使用语料目录的应用和检索进程，再从已验证实例的完整 data/corpus 打包。停机后仍存在的 SQLite WAL 文件应与数据库一起保留，不能只复制主数据库。

```bash
cd /path/to/working/judger-demo
tar -czf "$HOME/Downloads/judger-corpus.tar.gz" -C data corpus
```

发布压缩包时同时提供以下信息：

- 真实下载链接与访问方式，以及压缩包的 SHA-256。

- 数据 release_id、文书数量、片段数量，以及对应的 Git 提交或发布版本。

- 建库用的嵌入模型名称、权重或 digest、维度及必要的预处理说明。

macOS 计算压缩包校验值：

```bash
shasum -a 256 "$HOME/Downloads/judger-corpus.tar.gz"
```

Linux 计算压缩包校验值：

```bash
sha256sum "$HOME/Downloads/judger-corpus.tar.gz"
```

该语料包不包含旧账号、工作区数据库、用户上传材料、内部密钥、模型配置或模型权重。语料发布与旧实例整体备份是两个不同用途。

## 版本与参考资料

本指南针对 2026 年 11 月 18 日核对的仓库提交 4eaf0857baec0571256aae823700d027e233d929。若后续仓库调整了安装脚本或数据格式，请优先使用对应发布版本的说明。

1. [项目代码与版本](https://github.com/jonkiky/judger-demo/tree/4eaf0857baec0571256aae823700d027e233d929)

2. [启动流程 run.py](https://github.com/jonkiky/judger-demo/blob/4eaf0857baec0571256aae823700d027e233d929/run.py)

3. [现有 setup.sh 与原始索引依赖](https://github.com/jonkiky/judger-demo/blob/4eaf0857baec0571256aae823700d027e233d929/setup.sh)

4. [Ollama macOS 安装](https://docs.ollama.com/macos)

5. [Ollama Linux 安装](https://docs.ollama.com/linux)

6. [生成模型 qwen3 4b instruct](https://ollama.com/library/qwen3:4b-instruct)

7. [嵌入模型 qwen3 embedding 4b](https://ollama.com/library/qwen3-embedding:4b)

8. [Ollama 嵌入 API 与 dimensions 参数](https://docs.ollama.com/api/embed)

9. [AWS CLI S3 下载命令](https://docs.aws.amazon.com/cli/latest/reference/s3/cp.html)

10. [AWS CLI 安装](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

11. [Google Drive 下载文件](https://support.google.com/drive/answer/2423534?hl=zh-Hans)

12. [nvm 安装与 Node.js 版本管理](https://github.com/nvm-sh/nvm)

13. [Homebrew Python 3.12](https://formulae.brew.sh/formula/python@3.12)

14. [Homebrew Node.js 22](https://formulae.brew.sh/formula/node@22)
