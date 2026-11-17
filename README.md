# annarchive — 带逐版取证的公告归档系统

公告正文会更正附件链接，附件服务器也可能在**同一 URL** 替换内容。本系统把
**正文抓取、附件下载、归档发布**拆成三个独立环节，保证读者打开某一版公告时，
看到的是那一版**实际核对过**的附件，而不是此刻服务器上恰好返回的字节。

整个项目只依赖 Python 3.10+ 标准库，内置一个可在下载途中改正文/替换文件的
**来源模拟器**，全部下载与校验均为真实 HTTP 行为，公告内容为虚构。

---

## 1. 核心设计决策：先锁定正文

题目要求在两种策略间做出选择，本系统选择 **lock-first（先锁定正文）**：

1. 正文一抓到，立即解析出它引用的附件 URL 与声明的 SHA-256，写入一条
   **不可变版本**（`versions.status='locked'`）。
2. 此后来源端的任何更正都**不能修改**这条版本；更正只在下一次抓取时产生新版本。
3. 下载任务在创建的那一刻就带上 `version_id` 外键，数据库触发器禁止改写它——
   因此**旧下载即使晚到，也物理上无法绑定到新版本**。

为什么不选"边下载边发现更正"：那种策略让一个版本的内容取决于网络时序，无法回答
"读者当时到底核对了什么"。lock-first 让每个版本成为可复现的快照，下载途中发现
的差异作为新版本和审计事件保留，而不是污染在途版本。

### 不变量如何落地

| 要求 | 实现位置 |
| --- | --- |
| 三环节独立，可单独重跑 | `pipeline.py` 的 `fetch_body` / `download_version` / `publish_version` |
| 旧下载晚到不能绑新版 | `download_jobs`、`attachment_provenance` 创建即带 `version_id`；`db.py` 中三个 `*_immutable` 触发器拒绝 UPDATE 改绑 |
| 重复附件共用存储 | blob 以实测 SHA-256 内容寻址、分片存储，重复下载只留一份物理副本 |
| 来源记录不能共用 | 每个版本的每个引用各写一行 `attachment_provenance`（独立作业号、请求/响应头、状态码、尝试次数、实测哈希） |
| 读者只看到核对过的附件 | `publish_version` 门禁：任一引用不是 `verified` 即拒绝发布 |
| 同 URL 替换内容可区分 | blob 身份是实测哈希而非 URL；替换产生新 blob，旧版本仍指向旧 blob |
| 篡改/撕裂字节不进存储 | 哈希不符的下载移入 `quarantine/`，绝不写入 `blobs/` |

---

## 2. 目录结构

```
annarchive/
  config.py       数据目录布局（blobs/inflight/quarantine/exports）
  db.py           SQLite schema、不可变触发器、仓储函数、审计事件
  hashing.py      流式 SHA-256
  parser.py       正文解析：Markdown 链接 + 校验和清单 → 冻结引用
  http_client.py  真实 HTTP 抓取/流式下载，留证全部请求响应头
  simulator.py    来源模拟器（改正文、同址替换、确定性撕裂下载）
  seed.py         虚构天文台公告与附件（含三版正文、rev B 替换字节）
  pipeline.py     三个独立环节的全部业务逻辑
  exporter.py     原始证据 ZIP 导出
  diffing.py      逐版 unified diff + 引用变化（URL 变 / 哈希变）
  jobs.py         单线程后台作业队列（Web 端重抓/下载）
  webapp.py       标准库 Web 界面（无第三方依赖，内联 SVG 引用图）
  templates.py    HTML 模板
  cli.py / __main__.py   命令行入口
tests/            16 个 unittest，全部走真实 HTTP
scripts/          run_local.sh、run_tests.sh
var/              运行期数据（已 gitignore），代码与下载字节分目录存放
```

下载的不可信数据只出现在 `var/data/blobs`（最终）、`var/data/inflight`
（在途）、`var/data/quarantine`（校验失败隔离）。应用代码从不从这些目录导入
任何模块，路径一律以参数传入；测试与运行均使用 `python -I` 隔离模式，杜绝
下载目录里被植入的 `json.py` 之类借 `sys.path` 执行。

---

## 3. 快速开始

```bash
python3 -m venv .venv
# 让隔离模式也能导入本包（venv 的 site-packages 下加一个 .pth 指向仓库）
echo "$PWD" > "$(.venv/bin/python -c 'import site; print(site.getsitepackages()[0])')/annarchive.pth"

# 一条命令起模拟器 + Web UI（数据在 ./var）
./scripts/run_local.sh
# 浏览器打开 http://127.0.0.1:8000
```

或手动分步骤（便于观察每个环节）：

```bash
.venv/bin/python -I -m annarchive simulator --port 8080 --sim-root var/sim &
.venv/bin/python -I -m annarchive seed --base-url http://127.0.0.1:8080

URL=http://127.0.0.1:8080/announcements/yunhe-bulletin-2026-11
.venv/bin/python -I -m annarchive fetch    --data var/data --url "$URL" --slug yunhe-bulletin-2026-11
.venv/bin/python -I -m annarchive download --data var/data --version-id 1
.venv/bin/python -I -m annarchive publish  --data var/data --version-id 1
.venv/bin/python -I -m annarchive export   --data var/data --version-id 1
.venv/bin/python -I -m annarchive events   --data var/data
```

自包含演示（临时目录、真实 HTTP、自动走完三个版本）：

```bash
.venv/bin/python -I -m annarchive seed-demo
```

---

## 4. 虚构场景剧本

云河天文台（虚构机构）发布观测公报：

- **v1**：CSV 链接正确；PDF 链接误写为 `bulletin-draft.pdf`（404），校验和清单
  已给出正确 PDF 的哈希 → 下载失败，**版本不得发布**。
- **v2**：正文更正 PDF 链接为 `bulletin.pdf`，附件字节未变 → 全部 `verified`，
  可发布。
- **v3**：`bulletin.pdf` 在**同一 URL** 被替换为 rev B 字节，正文校验和更新 →
  新 blob、新版本；CSV 字节不变，跨版本**共用 blob**但来源记录各自独立。

### 来源模拟器的两种突变能力

1. 管理接口（Web 的 `/sim` 页或 CLI `correct-body` / `replace-file`）可在任意
   时刻改正文或同址替换文件；配合 `?chunk_delay=` 慢速下载即可复现
   "下载一半时来源发生变更"。
2. 确定性**撕裂下载**，无需竞态：

   ```
   GET /files/bulletin.pdf?torn_after=120&torn_to=<旧内容id>&chunk_delay=0.05
   ```

   响应以分块传输返回：前 120 字节是旧内容，其余取自替换内容。归档端实测哈希
   不等于任何声明值，附件进隔离区，版本保持 `failed`，并完整留证响应头
   （含 `X-Torn`）。

---

## 5. Web 界面

| 页面 | 能力 |
| --- | --- |
| `/` | 公告列表、按来源抓取/重抓（后台作业 + 轮询） |
| `/announcements?slug=...` | 版本时间线、逐版状态、进入对照/关系图 |
| `/versions/<id>` | 锁定正文、逐附件来源记录与核对结果、下载/发布按钮、证据导出 |
| `/diff?a=&b=` | 正文 unified diff，引用变化标注 `url-changed` / `hash-changed` |
| `/graph?slug=...` | 版本 → 引用 → blob 的内联 SVG 引用关系图 |
| `/sim` | 模拟器控制台：途中改正文、同址替换、撕裂 URL 说明 |
| `/jobs`、`/api/jobs` | 后台作业状态（HTML / JSON） |
| `/events` | 完整审计事件流 |
| `/versions/<id>/export` | 下载原始证据 ZIP |

证据 ZIP 内含：锁定正文原文、正文响应头、每个附件独立的
`provenance.json`/请求头/响应头/`blob.bin`、`events.jsonl` 和总清单
`manifest.json`；其中每个 `blob.bin` 的实测 SHA-256 与来源记录一致。

---

## 6. 测试

```bash
./scripts/run_tests.sh        # 等价于 .venv/bin/python -I -m unittest discover -s tests -v
```

16 个用例覆盖：解析器确定性与去重、三阶段门禁、更正产生新版本、同址替换产生新
blob、blob 共用而来源独立、幂等重抓、撕裂下载隔离、下载途中改正文版本不被污染、
晚到作业无法改绑（触发器）、证据 ZIP 哈希自洽、逐版 diff、Web 全路由与作业队列。

所有测试都在临时目录启动真实模拟器、走真实回环 HTTP，不 mock 网络层。
