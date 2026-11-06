# cc-convert — 源文档 → 轻量 Markdown（本地按需流水线）

为 `cases` 律师案件区把 **PDF / Word / Excel / PPT** 转成同目录 `_md/` 下的轻量 Markdown 替身，让 Claude 永远先读小 MD：**省 token、可定位、保真、原件绝不被动、绝不重复转**。

- **PDF / 图片证据（jpg·png·webp…）/ OFD / 旧 `.doc·.xls·.ppt` / RTF·ODF·EPUB·HTML** → MinerU 4.0 引擎 + 盖 `<!-- PAGE n -->` 页码标记（非 PDF/图片走 4.0 原生 flash 解析，不 OCR）
- **docx/xlsx/pptx** → 原生解析（python-docx/openpyxl/python-pptx）+ `<!-- SEC: … -->` 章节锚点（**不 OCR**，保真）；原生解析失败自动兜底走 MinerU flash（引擎标 `fallback`）
- **`--to docx|html|pdf|epub|latex`** → 从归档 `middle_json.json` 秒级重渲染，不重跑 OCR
- 全程**离线**，原件只读不动，产物登记于同目录 `_转换索引.md`

## MinerU 调用机制（重要）

本工具**进程内调用 MinerU 4.0 的官方 Python SDK** `mineru.parser.parse()`（无状态、无 HTTP、无超时上限），不走 `mineru` 文档库 CLI、也不起 `mineru-kit api-server`。

接入史：v3.2.3 起 CLI 变成需起/连 `mineru-api` 的瘦 HTTP 客户端（首启健康检查 5 min 超时 + 读超时硬编码 60 s，长 OCR 必崩），故 2026-06-06 改进程内直调其内部引擎 `mineru.cli.common.do_parse` 绕开 HTTP 层。**4.0（2026-09-16）整体重写**：`do_parse` 与 `pipeline / vlm / hybrid` 后端全部移除，官方改提供无状态 SDK——正是当年绕路想要的形态，2026-09-18 起直接用 SDK（`run_mineru()`）。

> `docvortex`（4.0 的原生解析层）渲页仍用 ProcessPool(spawn)，故本工具必须以真实 `.py` + `if __name__=="__main__"` 守卫运行（已具备）；用 `python - <<EOF` 之类 stdin 方式调 SDK 会 `BrokenProcessPool`（2026-09-18 实测）。

### 4.0 产物链

`parse()` → `ParseResult.save()` 落 `markdown.md` + **`middle_json.json`**（4.0 正典中间表示，schema 2.0，可 `ParseResult.from_json` 重载并渲染为 Markdown / HTML / DOCX / EPUB / LaTeX / PDF）+ `structured_content.json` + `images/`；再由落盘后的 middle_json 渲出扁平 **Content List V1**（`<stem>_content_list.json`，含 `page_idx`），与 3.x 产物形态一致，`stamp.py` 盖页码逻辑零改动。归档到 `_md/<名>.assets/` 的是 content_list.json + middle_json.json + images/（图片名改为可读的 `page_<i>_image_body_<n>.jpg`）。

### 质量档位（tier）取代后端名

| tier | 含义 | 模型 | 本机（M3 Pro 18 GB）实测 |
|---|---|---|---|
| `flash` | 只读 PDF 文本层 / 原生解析电子文档，不跑模型 | 无 | 扫描件无 OCR，仅预览用 |
| `basic` | 小模型：版面 + OCR + 表格 + 公式，无 VLM | `MinerU-4_models_torch`（860 MB；OCR 为 PP-OCRv6 **tiny det + small rec**） | 最快，但扫描小字识别比 3.4.4 pipeline 退一档 |
| **`standard`（默认）** | 小模型 + VLM（llama.cpp GGUF） | 上者 + `MinerU2.5-Pro-2605-1.2B-GGUF`（1.2 GB） | 三样本全面快于 3.4.4，扫描件 OCR 与 3.4.4 持平 |
| `advanced` | 同 standard 模型、更多推理算力 | 同 standard | 疑难版面按需 |

3.x 旧名仍可用，`normalize_tier()` 自动映射：`pipeline→basic`、`hybrid-*→standard`、`vlm-*→advanced`（对齐上游 changelog 的迁移表）。

### 4.0.2 三样本复测（2026-09-18，同机同文件，wall clock 含进程启动）

| 样本 | 3.4.4 pipeline | 4.0.2 basic | 4.0.2 standard | 正文字数（CJK） |
|---|---|---|---|---|
| A 仲裁申请书（文本层，2 页） | 22.8 s | 12.4 s | 18.3 s（warm 7.5 s） | 995 / 995 / 995 |
| B 不动产登记证明（扫描，2 页） | 20.7 s | 6.3 s | 11.6 s | 231 / 230 / 233 |
| C 一审判决（文本层，19 页） | 18.3 s | 15.1 s | 14.3 s | 11400 / 11409 / 11409 |

判定：**默认 `standard`**。basic 在 B 末行小字出现字序错乱 + 2 处认错（"存在禁止…"→"尚来 在…将止…"），standard 与 3.4.4 逐字一致且多补回 2 字；3.x "pipeline 快 6–20×"的前提（VLM 慢）在 llama.cpp GGUF 下已不成立。

### 保真守卫（`_disable_upstream_text_normalization()`）

docvortex 0.4.12 对 PDF 模型输出**硬编码**"全角英数 + 13 个符号（：．／＼－＿％＋＝＠＃＆＊）→ 半角"且无开关（`docvortex/content/normalization.py`），实测把源件的「原告：」改成「原告:」。与本工具"逐字忠于源件"冲突，故 `run_mineru()` 在 `parse()` 前把该模块的前置正则换成永不匹配、整体短路为恒等（对 MinerU 任何 import 方式都生效）。核验：A 样本源文本层全角「：」11 处，产物 11 处。另两处上游行为**未干预**（记录备查）：① 文本层 PDF 中汉字与数字间的空格 4.0 按 pdfium 原样保留（"1967 年 11 月"），3.x 曾剥掉；② 文本层的加粗 span 渲为 `**…**`。

### 版本复核记录

- 2026-06-06 v3.2.3 → 改 `do_parse` 直调（HTTP 层两处致命点见上）
- 2026-08-25 v3.4.4 逐条复核：do_parse 签名不变；后端真名改 `vlm-engine / hybrid-engine`
- 2026-09-18 **v4.0.2 迭代二**：`engine.py` 抽层；图片证据 / OFD / 旧 Office / RTF·ODF·EPUB·HTML 入转换；Office 原生失败兜底；`--to` 重渲染；复核层默认逐页 Vision + **行级包含信号**（抓"漏一行"）+ **Vision 补行草稿**（缺行按前一行锚定插回）+ 可选重识别第三意见；145 测试绿。**40 页扫描件基准复跑**：4.0 standard 转换 212 s（约 5 s/页），3.x 静默吞掉的 4 处条款中 3 处已原生抽出；`--review` 57 s 精确命中剩余真漏 [9, 11, 14]、零误报（旧比率信号对 p.9 一行 31 字 / p.11 一段 71 字均不触发，行级信号补上）
- 2026-09-18 **v4.0.2**：SDK 化迁移 + tier + 默认 standard + 保真守卫 + 归档 middle_json；119 测试绿；模型改用 `~/.mineru/models/`（`mineru-kit models download`），旧 `~/.cache/huggingface/hub/models--opendatalab--{PDF-Extract-Kit-1.0,MinerU2.5-Pro-2605-1.2B}`（4.6 GB）与 `.venv3`（3.4.4，1.9 GB）不再使用，已于迁移验收后删除（`.venv3` 残留空目录 2026-10-07 清除）；旧 `~/mineru.json` 4.0 不再读取（配置改 `~/.mineru/config.yaml`，未建则用默认）
- 2026-09-26 **v4.0.7**（docvortex 0.4.25）：随系统分批升级装上，当时未单独复核；10-08 补记，当前在用
- 2026-10-08 **v4.0.10 评估后回滚**（docvortex 0.5.11，Rust 原生 PDF 文本内核）：守卫静态与运行时均有效、155 测试绿，但同输入新旧 A/B 显示——standard 档中文判决书 41 页 / 合同 14 页逐字一致、预热后耗时持平（12.7 vs 12.8 s，无提速）、英文仅加宽字距大写被拆（`TH E PARTIES`）；**flash 档明显退化**：中文合同 14 页 61 处（换行处汉字间插空格如「工 期」「施工人 员」、下划线填空字段的 `<strong><u>` 丢失或错位）、英文贷款协议悬挂编号错位（`(1)` 移位、`"Facility (4) Agent"` 插进定义词）。OFD / 旧 Office / Office 兜底均强制 flash，故回滚至 4.0.7（`uv pip sync` 回升级前快照 `~/Backups/cc-convert/freeze-pre-4.0.10-20261008.txt`，回滚后 flash 产物与旧版逐字一致）。**再升前置条件**：上游修复 flash 中文换行空格与编号定位，并复跑同组 A/B（standard + flash × 中文合同 + 英文协议）
- 2026-10-31 **默认先 Windows、不可用回退 Mac**（律师裁定）：新增 `--local`；`--remote` 改为"强制只用 Windows"；`remote_convert` 拆出 `dispatch_ready` / `run_dispatched`；测试 `tests/test_autoroute.py`。
- 2026-10-20 **Windows 3090 算力工位接入**（代码不改转换路径，默认行为逐字节不变）：`--remote` 在 Windows 原地转换（standard 3.6 s/页，Mac 14.8 s/页）；`--review --second-opinion glm` 加 GLM-OCR 第二意见（比测捕获 MinerU 漏读/误读 94%、误报约 2.5 处/页，只提示不写回）；120 页 A/B 判定 **不改默认档位**（advanced 在电子版打平、扫描件互有胜负（standard 12 胜 / advanced 10 胜），且聊天截图页整页丢字 5/60）；新增 `ocr-lock.yaml` 版本锁定、`remote/ocr_upgrade_check.py` 升级检查、`remote/regress.py` 回归门。详见 `remote/README.md` 与 `~/claude-skills/_analysis/2026-10-20-OCR第二引擎比测.md`；171 测试绿

## 用法

```bash
./cc-convert <文件或目录> [选项]
```

| 选项 | 含义 |
|---|---|
| （默认） | 单文件转；或目录**单层**内的源文档 |
| `--recursive` | 目录递归进子目录（默认仅单层；防"指案件根误发上千次 OCR"） |
| `--force` | 已转也重转（覆盖三闸去重） |
| `--scan` | 只扫描、补全索引"未转"行，不转换 |
| `--migrate` | 存量两步走：①用现有 legacy JSON 盖页码（纯新增 `_md/`，零删除）②列"去膨胀清单"（**只列不删**，逐案确认后手动删） |
| `--min-image-kb N` | 小于 N KB 的图片不保留（过滤图标/头像/logo/扫描全能王水印），并清理 md 中失效图链。**默认 8KB**；`0`=不过滤 |
| `--no-images` | 完全不保留任何图片，仅留 OCR 文本 + JSON（证据扫描件正文不依赖图片时最省空间） |
| `--full-assets` | 保留全部原始输出（含 `_raw/`），**且不过滤图片**（等价 `--min-image-kb 0`） |
| `-t/--tier {standard,basic,advanced,flash}` | MinerU 4.0 质量档位，默认 `standard`（见上表）；`-b/--backend` 与 3.x 旧名仍可用，自动映射 |
| `--image-sources` | 目录扫描时把图片（微信截图 / 拍照证据）也当源文件 OCR；**单文件指名图片无需此开关**。图片不进 `SOURCE_EXTS`，故 `scan_pending.py` 巡检不会被证据夹里上百张图刷屏 |
| `--to FMT` | 不重跑 OCR，从 `_md/<名>.assets/middle_json.json` 重渲染为 `docx/html/pdf/epub/latex`，落 `_md/<名>.<fmt>`（19 页判决实测 0.5–1 s）。3.x 时期转换件无 middle_json，需先 `--force` 重转；被 `--min-image-kb` 过滤掉的小图以 1×1 透明 PNG 占位 |
| `--review` | OCR 复核：对已转的单份 PDF / 图片**逐页**跑 Apple Vision 交叉（2026-09-18 起默认＝原 `--deep`；40 页扫描件实测 12 s），产出复核包 + `corr.draft.yaml` 草稿（MinerU 缺行按 Vision 补回、整页被吞用 Vision 全文；见下节） |
| `--quick` | 配合 `--review`：退回只核启发式存疑页 + 空/短页的旧 `suspect` 模式 |
| `--third-opinion` | 配合 `--review`：额外用 advanced 档对入包页重识别（默认不跑：基准实测漏行是版面没框到，advanced/basic 都补不回、约 13 s/页） |
| `--deep` | 已是默认行为，保留兼容 |
| `--apply-review <yaml>` | 把修正清单 YAML 就地写回 stamped MD（改页 + `## 复核溯源` + front-matter 标 `reviewed`）；**只接受单个 PDF 目标** |
| `--force-drop-corrections` | 配合 `--force`：允许重转清洗掉已复核修正（否则已复核文件 `--force` 会被响亮拒绝） |

> **图片过滤（默认开）**：MinerU 会把 PDF 里每个嵌入小图（微信头像、图标、logo、水印）都抽成独立文件，实测可占 assets 体积的 ~80%。默认仅保留 ≥8KB 的图片（关键截图/印章/签名/照片），小图标自动剔除并清理对应 `![]()` 图链。需要全留时用 `--full-assets` 或 `--min-image-kb 0`。

## OCR 复核层（拍照/扫描证据保真）

针对 OCR **不规范证据**（拍照/扫描不清）的糊字、缺块、整段漏抽，提供一个**独立、按需**的复核层——**不影响日常转换**（复核是单独命令，普通 `cc-convert X.pdf` 一字节不变）。三层分工：①Python 纯文字启发式挑存疑页（零成本）；②Apple Vision（`ocrmac`）对存疑页 + **空/超短页**（`--deep` 则每页）二次识别，**以「MinerU 漏内容」为主信号**（Vision 中文显著多于 MinerU → 疑似整段被吞；MinerU 该页 CJK 为 0 时改判 Vision CJK ≥10 → 整页被吞）+ 严重乱码为辅，**按可疑度排序**、表格页豁免乱码信号；③人看原图裁断修正。**Apple Vision 仅作第二意见，绝不替换 MinerU 主引擎。**

**vision_agrees 降噪标注**：启发式入包、但 Vision 交叉高度一致（sim≥0.95 且无漏内容）的页标 `vision_agrees: true` 并在 CLI 摘要单列——多为证书/编号页排版性误报，人工裁断可整批略过（实测某 456 页证据卷 57 个入包页中 43 页属此类）。

**空/短页兜底（2026-07-02 加固）**：suspect 模式下正文 <10 字的页（"MinerU 整页吞掉"的输出形态恰是空页）一律送 Vision 交叉，Vision 读出 ≥10 个汉字才入包（真版式空页零误报）；复核侧还独立核对 pypdfium2 真实页数——MD 缺尾页标记时按缺页走同一判定流入包（`missing_marker: true`，修复需 `--force` 重转）。实测战果：某案 61 页民事判决书（MinerU 3.2.3 正式转换产物）被查出 **24 页正文被静默吞掉、仅剩页码数字**，旧机制（短页豁免）永远抓不到。

> **实测校准（真实 40 页扫描件，2026-07）**：`--deep` 从「40 页全标」收敛到「5 页按可疑度排序」，排首位的页 MinerU 静默漏抽了整条合同条款（经原图核对确认）；加固后旧基准 5 页分数逐位复现、零回归。诚实局限：本层可靠抓**漏内容（整段/整页） / 严重乱码**并排序，但**不追干净页上的单字认错**（真实数据无法在不误报海量下单独拎出），仅含 ≤10 字手写批注的页也会漏；明显乱码仍由启发式那层兜底。

三步工作流（针对单份 PDF）：

```bash
# 1. 产出复核包：_md/<名>.assets/review/packet.yaml + page-*.png
./cc-convert "某案/证据.pdf" --review          # 默认只核存疑页
./cc-convert "某案/证据.pdf" --review --deep    # 每页交叉，捞隐蔽单字错

# 2. 人看 page-*.png 原图裁断，写修正清单 corr.yaml：
#    coverage: deep
#    corrections:
#      - page: 3
#        corrected_text: <该页改后的完整正文>
#        changes: [{old: 玉, new: 王, basis: 原件 p.3}]

# 3. 工具就地写回 stamped MD（改页 + 文末 ## 复核溯源 + front-matter 标 reviewed）
./cc-convert "某案/证据.pdf" --apply-review corr.yaml
```

- **就地写回、不另存覆盖件**：下次读的那份 MD 本身即最保真，无"记得读第二文件"的漏。写回由工具（`--apply-review`）完成、非徒手改；文末 `## 复核溯源` 逐条记 `页｜原文｜改为｜依据`（原文可回溯），front-matter 加 `reviewed`/`reviewed_coverage`（`suspect`/`deep`）/`corrections`。
- **单次全量复核模型**：`corr.yaml` 应是对该文书的一次性完整提交——溯源表按本次清单重建（非累积），对已复核文件再 `--apply-review` 会覆盖旧溯源表并提示。**原始 OCR 存档始终保留在 `_md/<名>.assets/content_list.json`**（及复核包 `packet.yaml` 的 `mineru_text`），是权威可回溯底本，不受写回影响。
- **防损坏**：写回前校验解析页数与 front-matter 声明一致、修正未引用不存在页码，任一异常即中止且不动原 MD；溯源块以机器哨兵 `<!-- CC-REVIEW-PROVENANCE -->` 锚定剥离，杜绝正文含 `## 复核溯源` 时误删内容。
- **重转防清洗**：已复核文件 `--force` 会被拒绝，确需重转加 `--force-drop-corrections`（否则修正被静默清掉）。
- **仅 PDF**；Office 原生解析本就保真、不进复核。`--apply-review` 只接受单个 PDF 目标（一份清单只对应一份文书，防跨文书误写）。

### 第二意见与远程转换（Windows 3090，2026-10-20）

- `cc-convert X.pdf --review --second-opinion glm`：在 Vision 交叉之外，另把页面送 Windows WSL2 上的 GLM-OCR 独立重识别，逐页与 MinerU 比对，分歧条写进复核包 `glm_flags`（`glm_only`＝MinerU 可能漏读；`differ`＝同位读法不同；`mineru_only`＝MinerU 长段高度重复，疑复读）。**只提示：GLM 文本不进 `corr.draft.yaml`、不写回**（视觉大模型在看不清的页会编造）。Windows 离线 / 未登录 / 显卡被占 → 复核包记 `second_opinion_error`，本次仅 Vision。
- **默认路由（2026-10-31 起）**：不加参数转换 cases / library 下的 PDF 或图片时，先做 Windows 预检并等 Drive 同步到位，通过即交 Windows 原地转换（产物经 Drive 同步回 Mac）；不通过打印 `[route] Windows 不可用（原因），本次在 Mac 本机转换` 并在本机转。派活之后再出错只报错、不在本机重做（避免两端同写 `_md/`）。docx 等原生解析件、已转件（未加 `--force`）、`--scan/--review/--to` 等不走此路由；Windows 端自身运行时不触发。
- `cc-convert X.pdf --remote`：强制只用 Windows，不可用时报错退出，不本地兜底。
- `cc-convert X.pdf --local`（或环境变量 `CC_CONVERT_ROUTE=local`）：强制本机，不试 Windows。
- 部署、排障、升级流程：`remote/README.md`；版本以 `ocr-lock.yaml` 为准。

## OCR 巡检（scan_pending.py）

SessionStart hook 用的只读巡检器：列出案件区未转换源文档，按案件聚合输出（全部已转时零输出）。未转判定直接复用 `cc_convert.converted_state` 三闸——与转换工具单一判定源。

| 行为 | 说明 |
|---|---|
| CWD 不在 cases 下 / 在排除区内 | 静默退出（路径守卫） |
| CWD = cases 根 | 全区扫描，目录级 mtime 过滤（默认近 30 天，`--days N` 覆盖，`0`=不过滤），不报根级散件 |
| CWD 在案件子树内 | 仅扫该案件，全量判定 |
| 目录排除 | `1.签约信息*`、`2.事务资料*`、`4.录音资料*`、`1.零散文件*`、纯净区、`_*`/`.*`/`*.assets`/UUID 目录 |
| 文件排除 | `CC-`/`TC-` 前缀自产文书、`~$`/`.` 开头（排除≠禁转，手动转换不受限） |
| 输出 | 按案件聚合，默认最多 10 行案件，`--all` 不截断 |
| 只读 | 不写 `_转换索引.md`（登记仍由转换/`--scan` 时做） |

```bash
python3 scan_pending.py            # 无需 venv，stdlib-only
```

hook 接线在 `~/.claude/settings.json` SessionStart（matcher `startup|clear`，timeout 10s，PYTHONIOENCODING=utf-8，fail-silent），命令带 **`--hook`**：输出 hook JSON——`systemMessage`（横幅直接显示给用户）+ `additionalContext`（进 Claude 上下文）。纯文本 stdout 只进上下文、用户不可见（2026-06-11 实测教训），手动运行不带 `--hook` 即纯文本。

## 转换期防遗漏守卫（2026-07-02）

- **页数守恒**：转换时用 pypdfium2 核对源 PDF 真实页数；MinerU 少产页则**尾部补空页占位** + front-matter `warnings:` 响亮警告（补出的空页会被复核层短页路径自动送 Vision——守恒补位让丢页现形，Vision 负责抓赃）。MinerU v1 形态若缺**中间页**，按完整区间补位，**杜绝后续页码整体前移的静默重编号**。
- **text_layer 标注**：front-matter 新增 `text_layer: none|partial|full`（pypdfium2 抽样判定）——`none/partial` 即拍照/扫描件，是双轨规则"扫描证据建议复核"的机器信号。
- **闭环提示**：转换出警告或检出扫描件时，自动打印 `💡 …建议复核：./cc-convert '<文件>' --review`（只提示，不自动执行）。

## 去重三闸（任一命中即跳过，除非 `--force`）

1. 同目录 `_md/<同名>.md` 且 front-matter `source: ../<原名>` 匹配 → `done`（跨格式重名的消歧文件 `<名>.<ext>.md` 同样按 `source` 权威识别——`mdpages.md_for()` 统一解析，review/apply-review/migrate 共用）
2. 旧 PaddleOCR sidecar `<原名>.pdf_by_*.md` → `legacy`
3. MinerU UUID 乱目录 `<原名>.pdf-<uuid>/` → `legacy`

## 产物

```
<目录>/
├── 民事判决书.pdf            ← 原件，只读
├── _md/
│   ├── 民事判决书.md          ← 带页码（主用）
│   ├── 民事判决书.assets/     ← content_list.json + images（存档，罕用）
│   └── 服务合同.md            ← 章节锚点（Office，无页码）
└── _转换索引.md              ← 登记表
```

- 同目录跨格式重名（`foo.pdf` 与 `foo.docx`）→ MD 名加扩展名消歧（`foo.pdf.md`），front-matter `source` 为权威键。
- **纯净区例外**：`4.提交微法院的电子资料存档` 连派生件也不写。

## 测试

```bash
.venv/bin/pytest -v     # stamp/index/office/dedup+migrate + 复核层（vision_ocr/pdf_render/mdpages/review/apply_review/review_cli）+ 保真守卫（fidelity_guard，需 docvortex）
```

## 环境（Phase 0，一次性）

```bash
uv venv --python 3.12 .venv
uv pip install --python .venv -U "mineru>=4.0,<5" python-docx openpyxl python-pptx pytest
uv pip install --python .venv ocrmac pyyaml                  # OCR 复核层：Apple Vision + 清单解析（pypdfium2 随 mineru 已装）
uv pip install --python .venv "httpx[socks]"                 # 系统设 SOCKS 代理（ALL_PROXY=socks5://…）时模型下载经 httpx 须 socksio；无代理时闲置
.venv/bin/mineru-kit models download --tier standard --source huggingface   # 小模型包 860 MB + GGUF VLM 1.2 GB → ~/.mineru/models/
.venv/bin/mineru-kit models verify --tier standard
```

> Apple Silicon 装基础包即自带 Torch（小模型走 MPS）+ llama.cpp（VLM）；不要再用 3.x 的 `mineru[all]` / `mineru-models-download`。`/Applications/MinerU.app` 是 Electron GUI，与本 CLI 是两套、不共享。
