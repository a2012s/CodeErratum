# Changelog

## [0.4.9] — 2026-11-23

### Fixed

- **发现的 detail 论据不再固定英文，跟随界面语言**：0.4.7 做双语时，输出语言指令
  （`language_directive`）只做到了 executor 一层——`execute_review → run_review →
  _review_with → review_unit` 整条链都没传 `output_lang`，证伪轮的 prompt 同样没传，
  所以模型始终按默认 prompt 语言（英文）写 `claim` / `detail` / `suggestion`，界面切中文
  也没用。现在 pipeline 在运行起点（请求线程里语言已就位时）捕获语言并显式传值：
  审查与证伪两条路径都收到指令，证伪裁决的标签（「反例 / 错误假设」）也随语言切换；
  语言随 run 固化，审查进行中切换界面语言不影响本次输出。enum 值与代码标识符不译，
  解析与锚点校验不受影响。新增 `tests/test_output_language.py`（8 项）锁住
  指令内容、审查 prompt、证伪 prompt、pipeline 捕获四个环节。

## [0.4.8] — 2026-11-23

### Fixed

- **运行结束后右侧停在实时卡片，详细报告不再自动出现**：流式路径收到 `done` 帧后只更新了
  左侧日志，右侧还停在"实时发现"列表——每条只有一句 claim（模型约定 claim ≤ 60 字符），
  而旧版运行结束时右侧直接是完整报告（代码片段、detail 论据、修改建议、证伪轮裁决）。
  这就是"右边没原来详细"的真相：不是报告少了内容，是**报告根本没被打开**。跟踪路径
  （轮询）在 0.4.6 已有 `openRun`，流式路径漏了。现在 `done` 后自动刷新历史并打开报告，
  与跟踪路径一致。新增布局测试锁住两条路径都必须 `openRun`。
- **实时卡片只有标题没有论据**：SSE 的 finding 帧补上 `detail`（触发条件 + 影响 + 原因），
  卡片里以「详情」折叠展示——默认收起不刷屏，展开即有完整判断依据。

## [0.4.7] — 2026-11-23

### Added

- **模型配置只填一次，新项目自动沿用**：过去模型清单（端点、key、席位、mode）只存在被审仓库的
  `.codeerratum/config.json` 里，所以每加一个项目，设置抽屉都是空的，同一批 key 要重打一遍。
  现在配置分三层解析：**项目文件 → 用户级 `~/.codeerratum/config.json` → 环境变量**，项目文件
  仍然优先，单个仓库可以钉住不同的模型组合。保存时默认勾选「同时设为新项目的默认配置」，
  把当前清单写到用户级（0600，目录 0700）；取消勾选则只写本项目。界面会明确显示当前来源是
  「本地配置文件 / 全局默认配置 / 环境变量」，`/api/meta` 的 `source` 相应多一个 `user`。
  配套改动：`__KEEP__`（沿用已存 key）现在也会去默认层找，否则一个正在用默认配置的项目第一次
  保存时会把自己的 key 清空；`pipeline.available_models()` 与 CLI 运行同样走这条链，模型选择器
  不会列出与真实运行不一致的清单；`CODEERRATUM_HOME` 可覆盖用户级目录（测试与便携安装）。
  测试新增 `tests/test_config_layers.py`（10 项）并在 `tests/conftest.py` 里为每个用例隔离
  用户目录，避免开发机上的真实 key 混进断言。全部 **357 passed**。

## [0.4.6] — 2026-11-23

### Added

- **关于（About）里能看到 GitHub 地址**：打包应用的 About 是系统面板——macOS 只显示一行静态版权、
  Windows 根本没有，两者都放不了可点的链接。现在三层都有：`Info.plist` 的
  `NSHumanReadableCopyright` 写成 `MIT License · https://github.com/a2012s/CodeErratum`（系统
  About 面板底部那行）；审查页顶栏的版本徽标改成按钮（`v0.4.6`），点开是带版本、简介、地址的
  对话框，可复制、可「在 GitHub 打开」；外壳顶栏加 `GitHub` 入口（桌面版打开的就是外壳）。
  地址只在 `codeerratum/__init__.py` 的 `HOMEPAGE` 定义一次，`/api/meta` 下发，spec 从源码正则读。
  打开动作走 `POST /api/open/homepage`——URL 是常量、不接受请求参数，避免变成"打开任意链接"的
  通用原语；webview 里的页面本来也没法自己调起系统浏览器。新增 4 项测试。
- **实时发现现在两条路都能看到**：SSE 只属于打开它的那个请求，刷新页面、开第二个标签页、或在别处
  启动的运行都走 `/api/review/status` 轮询——那条路过去只有进度文字，右侧一无所有。现在每条发现
  同时写进状态快照（`WebApp._note_finding`，按 `id + model` 去重、上限 500 条），轮询端用同一套
  卡片渲染并去重；运行结束时跟踪端会自动 `openRun` 打开报告。顺带修了一个会让"所有发现都看不见"的
  接线错误：回调里 `self` 是 HTTP handler 而非 `WebApp`，`_note_finding` 抛 `AttributeError` 又被
  上层的"展示失败不能中断付费运行"吞掉。现在该分支会把异常打到 stderr，同类接线错误不再静默。
  新增 3 项测试（状态里有发现、换轮次清空、按模型去重）。
- **发现逐条实时出现在右侧**：过去要等 `done` 帧才有结果，中途只有「单元 12/53」在动，看不出
  到底有没有发现问题。现在每个单元解析完就发一条 `finding` SSE 帧
  （`server._stream_findings`，只带 severity/file/line/claim/model，完整记录仍随运行归档），
  右侧换成实时列表、一条一张卡片；运行结束后正式报告替换它。界面明说这些发现**尚未聚类、
  未证伪**。回调在 `orchestrator._review_with` 里按单元触发，异常一律吞掉——展示层不该让跑了
  一半的付费审查失败。新增 2 项测试（`finding` 帧必须早于 `done`；回调按单元触发，报告仍来自
  完整集合）。
- **Windows 打包**：`packaging/` 早有 win32 分支，缺的是两件只会在 Windows 上暴露的东西：
  `webview` 包内自带 `WebView2Loader.dll` / `Microsoft.Web.WebView2.Core.dll`，由
  `collect_data_files("webview")` 收集（漏了的表现是"图标闪一下、没有窗口"）；`--selftest`
  增加 `webview.platforms.edgechromium` 导入检查——`import webview` 成功不代表后端可用，
  真缺 pythonnet 时是开窗那一刻才炸。窗口版没有 stdout，selftest 结果写入
  `~/.codeerratum/desktop.log`，构建脚本回显末尾几行。新增 `.github/workflows/build.yml`
  （windows-latest + macos-latest，手动触发或推 `v*` 标签出 zip 并挂到 Release 草稿）。

### Fixed

- **`调用失败：ReadTimeout` 既没说在哪一步超时，也说不清为什么**：唯一的 `timeout=120` 同时管
  连接和读取，而 8192 的生成上限在实测 ~50 token/s 下需要 ~165s——长答案天然跑不完，表现就是一批
  单元同时「读取超时」。重试虽然覆盖了 `TransportError`，但读取超时也吃满 3 次 ×120s（近 6 分钟），
  每次都重发输入、重新计费。现在：连接 10s / 读取 240s（`CODEERRATUM_READ_TIMEOUT` 可调，推理型
  模型建议 420）/ 写 30s / 连接池 10s；读取超时只重试 1 次（对端已安静一整窗），429/5xx/连接错误
  仍是 3 次且尊重 `Retry-After`（上限 30s）。失败文案改成会解释原因的中文/英文：
  `读取超时（240s 内没有返回任何数据；已重试 1 次）`、`鉴权失败（HTTP 401），检查 API key 与 base_url`、
  `被限流（HTTP 429，已按 Retry-After 退避重试）`；运行结束的 notice 追加去重后的失败原因列表。
  新增 5 项测试（窗口取值、读取超时只重试一次、Retry-After 上限、失败文案）。
- **两个语言切换按钮**：壳（`projects.html`）顶部有语言选择，标签页里的 `index.html` 头部
  又有一个，相隔 40px。现在内页在 iframe 里时用 `html.embedded #langSel { display: none }`
  隐藏自己的那个，判断放在 `<head>` 的内联脚本里（`window.self !== window.top`），首帧就不闪；
  语言切换仍由壳 `postMessage({type:"ce:lang"})` 驱动，直接打开 `/p/<slug>/` 时内页那个照常可用。
- **「不是 git 仓库」不再是死路**：这句报错只说了不行，没说怎么办——把一份拷来的项目
  文件夹选进界面，用户只会看到「不是 git 仓库：<路径>」。现在拒绝的理由和补救一起给出：
  目录里没有 `.git` 就没有 diff 可读，在该目录 `git init && git add -A`（暂存即可，
  不必 commit）；要审已有的全部代码，把审查范围填成空树
  `4b825dc642cb6eb9a060e54bf8d69288fbee4904`。空树哈希抽成 `ingest.EMPTY_TREE_SHA1`，
  Web、CLI、`pipeline` 三处校验共用；弹窗 `#modalMsg` 加 `white-space: pre-line`
  让多行提示不再挤成一句，审查范围下方也补了这条说明。新增 2 项测试
  （空树能审出单元、报错必须带上两条补救）。
- **英文界面侧栏横向溢出 23px**：`.row2` 首列 `1fr` 会被下拉框的 min-content 撑住，
  英文选项 `committee · three models` 把整行推出 292px 侧栏（中文不触发）。改为
  `minmax(0, 1fr)`；实测中英两语 `asideOverflowX` 均为 0。
- **多项目视图里每个项目只渲染出 150px 高**：外壳把项目放在 `<div id="frames">` 里，而该容器
  没有高度，`iframe.project { height: 100% }` 无从解析，浏览器退回 iframe 默认的 150px ——
  标签栏下方只有一条界面、其余全黑。现在 `#frames` 有 `height: 100%`，并在总览页 `hidden`
  （两个 100% 子元素并存会把外壳撑出一屏滚动）。打包产物同样受影响，已重新构建。
- **审查页高度硬减 51px**：`.layout { height: calc(100vh - 51px) }` 假设 header 恒为 51px，
  实际中文/英文 57px、窄窗口换行后 78px，多出的部分让页面出现滚动条。改为 body 纵向
  flex + `.layout { flex: 1; min-height: 0 }` + `grid-template-rows: minmax(0, 1fr)`；
  header 实测高度写入 `--header-h`，右侧抽屉的 `top` 跟随它（原先固定 51px，英文下会压住
  header 底部）。
- **英文界面下外壳白屏**：`<p data-en="…">` 的英文值丢了 `<b id="maxParallel">`，`applyLang()`
  用 `innerHTML` 覆盖后元素消失，`refresh()` 读到 null 抛 `TypeError`，标签、卡片、iframe
  全部不渲染。文案拆成三段让数字元素不被覆盖，该行也改成可选访问。新增
  `tests/test_layout.py`（5 项）把这三条成因锁进测试。
- **关闭/刷新页面不再把审查整轮判失败**（本次「审查失败」的真实原因）：SSE 写帧遇到
  `BrokenPipeError: [Errno 32] Broken pipe` 时会从 `progress` 回调一路抛回 `run_review`，
  被当成 `internal error`，已跑完的评审与证伪结果全部丢在 `status=error`。
  实测 traceback 确认：标签页关闭后下一行 `say(...)` 写帧失败即中止。
  现在 `_write_sse()` 捕获 `OSError` 并返回 False，处理函数置 `_client_gone` 后停止写帧，
  **审查继续在服务端跑完**，其他标签页或刷新后的页面通过 `/api/review/status` 接着看；
  `_send()` 同样吞掉断连写失败，不再为每个已关闭的请求打印 traceback。新增 2 项测试。
- **失败原因不再丢失**：`GET /api/review/status` 过去只保留 running/status/进度，异常一句话发进 SSE 就被丢掉；
  其他标签页看到「审查失败」四个字却无从判断。现在失败原因写入状态并回显（含其他标签页的轮询路径），
  服务端打印完整 traceback；下发前把仓库根/家目录替换成 `<repo>` / `~`，不泄露本地路径。
  同时修正 `finished_at` 把 `time.monotonic()` 当墙上时间下发的问题。新增 3 项测试。
- **证伪进度截断**：SSE 里的 `证伪 …` 行过去硬切 `claim[:40]`，英文文案断在半个单词上。
  现在输出完整 claim（Finding 本身限制 ≤60 字符），只把换行折成空格以保持单帧。新增 1 项测试。

### Changed

- **单元上限不再静默截断**：界面里把「上限」填成 200，过去服务端直接按 50 跑，界面一个字都不说。
  现在两侧都明说：输入框下方即时显示琥珀色提示（「超出 1–50，服务端会按 50 执行，并在进度里提示」），
  服务端夹取后把原因写进进度日志首行并着色为警告——`上限 200 超出范围（最多 50 个审查单元），已按 50 执行`，
  同时随 `notices` 字段进 `/api/review` 的 JSON 汇总和 `/api/review/status` 的 `params`，其他标签页也看得到。
  夹取边界抽成 `MAX_UNITS_CAP`；顺带修掉前端 `Number(v) || 10` 会把 0 悄悄变成 10 的问题。新增 5 项测试。
- **进度日志按重要程度着色**：阶段行（启动、Split diff、证伪轮）蓝色；警告/降级/证据不足/被证伪（refuted）琥珀色；
  通过（confirmed）、整体完成绿色；失败、调用失败、超时、异常红色并加粗；逐单元例行日志保持灰色。
  明暗主题分别定义 `--warn` / `--ok` / `--bad`，避免暗色下红绿字看不清。显式级别优先于自动推断。新增 1 项测试。

- **审查中重复点击的反馈**：过去在审查进行中再点「运行审查」，报英文 `a review is already running in this process`，
  按钮也没进入审查态。现在 WebApp 维护全进程唯一的审查状态（running / status / 进度消息 / 范围 / 计时），
  新增 `GET /api/review/status`；各标签页每 1.5s 轮询——审查在进行中时「运行审查」显示「审查中…」并禁用，
  进度日志跟着刷新（含其他标签页或断线连接启动的审查）。抢跑 409 不再弹错误框，转为跟踪已有审查；
  进度回调统一写状态，锁冲突文案改中文。新增 2 项测试。

### Changed

- **可拖动左右分栏 + 进度面板常驻**：`.layout` 从固定 `292px | 1fr` 改为 `sidebar | 拖拽柄 | content` 三列，
  拖动竖直分割线实时改变两侧占比（220px ~ 视口-420px），双击复位 292px，宽度记在 localStorage。
  进度面板在侧栏内改为 `position: sticky; top: 0`，滚动历史运行不再被顶出视野；日志默认 300px 高，
  底部新增拖拽手柄调高度（双击复位），同样持久化。拖动用 Pointer Events + window 级 move/up，
  不依赖 setPointerCapture 成功与否；`flex-shrink:0` 防止 sticky 面板被 flex 容器压塌。
  移动端（<860px）自动回退单列、隐藏分割线。新增 1 项静态断言测试，CDP 实测拖动/sticky/持久化通过。
- **异构判据从厂商改为模型版本**：过去 dual/committee 按 `family`（厂商）去重，同厂两个版本会被拒
  （`mode 'committee' needs 3 heterogeneous models but only 2 distinct families are available`）。
  现在按锁定的 `model_id` 判定：`qwen3.8-flash` 与 `qwen4-max-preview` 算两个独立评审者，版本侧重点不同
  正好互补；相同 `model_id` 重复配置仍被拒。`family` 退为展示/报告标签（留空自动推断），不影响同席。
  证伪轮优先用未参与审查的空闲模型，没有则从提出发现之外的评审者中挑一个交叉证伪。
  更新 registry / orchestrator 注释、配置抽屉说明、中英文 README。新增 4 项测试。

### Added

- **可双击安装的桌面应用打包**：新增 `packaging/`（`desktop_entry.py` 入口、`CodeErratum.spec`、`build.py`），
  `uv run python packaging/build.py` 产出 `dist/CodeErratum.app`（macOS）/ `dist/CodeErratum/`（Win、Linux）。
  用 onedir 而非 onefile——onefile 每次启动都要解包到临时目录，冷启动慢且易被当释放器。实测 67 MB，
  主体是 Python 运行时。spec 把 `codeerratum/web/static` 与 `codeerratum/prompts` 都放进包，并按平台
  补 pywebview 的后端 hiddenimport（`webview.platforms.cocoa` 等，它用 importlib 导入，静态分析跟不到）。
  打包脚本额外做了 ad-hoc 签名（`codesign -s -`），够在本机运行；分发到别的机器仍需 Developer ID + 公证。
- **`--selftest`：防「启动正常、一花钱就炸」**：`prompts/*.md` 只在真发起审查时读取，漏打包不影响启动。
  构建脚本因此执行冻结后的 `CodeErratum --selftest`，逐个检查 `static/`、`prompts/`、`webview` 是否在包内，
  缺一个就让构建失败。实测冻结包内路径落在 `Contents/Frameworks/codeerratum/{web/static,prompts}`，
  正是 `Path(__file__)` 与 `_locate_static()` 的解析位置，源码逻辑无需改。
- **窗口版没有终端，所以有日志和弹窗**：入口脚本在冻结模式下把 stdout/stderr 重定向到
  `~/.codeerratum/desktop.log`（>1 MB 轮转一份，`COLUMNS=120` 避免 rich 在日志里断词），
  未捕获异常除了写 traceback 还会弹一个原生提示框（osascript / MessageBoxW / zenity）。
- **双击启动强制 `--no-repo`**：Finder 拉起时 cwd 是 `/`，按 cwd 播种会以 `not a git repository` 在窗口出现前退出。
  入口脚本因此默认加 `--no-repo`，只有显式传 `--repo` 才不加。隔离 HOME 实测：空总览、
  `/api/projects` 为 `[]`、`/api/meta` 返回「还没有添加项目」、SIGTERM 干净退出并释放端口。
- **桌面窗口（pywebview，可选依赖）**：新增 `codeerratum desktop`，起同一个服务、同一份界面，把回环地址交给
  系统 WebView（WKWebView / WebView2 / WebKitGTK）显示，不打包 Chromium。窗口必须直接加载 `http://127.0.0.1:<port>/`，
  不能用 `file://` 或自定义协议——服务端的同源校验要求 `Origin` 等于回环 `Host`，换来源会让所有 POST 403、
  审查按钮静默失效。`text_select` 显式打开（pywebview 默认禁止选文字，报告页要能复制行号），
  `private_mode=False` + 固定 `storage_path` 让 localStorage 里的语言与分栏宽度跨重启保留。
  关窗即停服务、释放端口。新增 `pyproject.toml` 的 `desktop` extra、`web/desktop.py`、`web/lifecycle.py`、
  `web/bootstrap.py`，19 项测试；真实窗口实测：页面渲染、`evaluate_js` 读到标题与按钮、SIGTERM 关窗后端口释放。
- **服务端给启动器留的四个口子**：`ui` / `desktop` 都支持 `--no-repo`（空项目列表启动，不要求 cwd 是 git 仓库；
  桌面应用从启动器拉起时 cwd 通常是 `/`，原来会直接报 `not a git repository` 退出）、`--port 0`（由系统分配端口，
  `server_address[1]` 读回真实值）、`--print-port`（绑定后往 stdout 打一行 `PORT=<n>`，给外壳解析）、
  `--parent-pid`（看门狗，见下）。空项目时根路径 `/api/*` 返回 404 并说明「还没有添加项目」，不再是 500。
  `make_server()` 的 `app` 改为可空（必须有 hub），`make_handler()` 同步。
- **进程看门狗与优雅退出**：`--parent-pid` 每 2 秒探一次父进程，消失即停服务——崩溃或强杀留下的孤儿会抱着
  一次付费审查空转。POSIX 用「被 reparent 则直接父进程已死」+ `kill(pid, 0)` 兜底（纯 `kill` 会把僵尸当活的，
  正好是崩溃场景）；Windows 的 `getppid()` 在解释器启动后就固定、`os.kill(pid, 0)` 更会直接终止进程，
  改用 `OpenProcess` + `WaitForSingleObject(0)`，并显式声明 `HANDLE` 返回类型避免 64 位截断。
  SIGTERM / SIGINT 从 `signal.signal` 改为阻塞信号 + 专用线程 `sigwait`：桌面窗口的主线程卡在 Cocoa 事件循环里时，
  Python 的处理器根本没机会执行（实测 SIGTERM 十秒关不掉窗口），换线程取信号后才生效。
  顺带把 `ui` 的中文硬编码提示（未配置模型、Ctrl+C）并入消息目录。
- **冻结打包的静态资源查找**：`STATIC_DIR` 不再写死 `Path(__file__).parent / "static"`，
  改为探测 `sys._MEIPASS` 下三种常见 `--add-data` 布局再回落到源码目录——否则 PyInstaller/py2app 冻出来的
  程序首页直接白屏或 500。找不到就回落到源码路径，让错误表现成「模板 404」而不是导入期崩溃。
- **中英双语界面**：顶栏新增语言下拉（中文 / English），默认中文，选择存 localStorage。切换会同时改界面文案与
  服务端消息——页面按请求带 `X-Lang`，SSE 进度行、错误提示、状态文案都由 `src/codeerratum/i18n.py` 的消息目录
  按当前语言渲染，语言存在 `ContextVar` 里，多标签页 / 多项目各自独立。项目外壳页切换语言时通过 `postMessage`
  通知 iframe 立即重绘，不重载页面（重载会丢进度日志）。静态标记用 `data-en` / `data-en-ph` / `data-en-title`，
  动态文案用 `T()`；英文补了单复数（`1 finding` / `2 findings`）。CLI 与 MCP 用 `CODEERRATUM_LANG=en` 获得
  英文消息。报告骨架与历史归档 notice 不随界面回改。新增 13 项目录测试 + 4 项 HTTP 测试。

- **多项目同时审查（便签页）**：`/` 改为项目外壳，一个项目一个便签，便签内嵌原来的完整界面，
  另有「总览」把状态/进度/token 摊成卡片。后端新增 `ProjectHub`（`web/hub.py`）：每个项目一个
  `WebApp`，审查锁、进度状态、配置与 `feedback.db` 全部按仓库隔离，路由挂在 `/p/<slug>/`；
  根 `/api/*` 仍指向第一个项目，单仓库模式行为不变（未传 hub 时与旧版逐字节一致）。
  新增 `GET/POST /api/projects`、`/api/projects/pick|find`、`/api/projects/<slug>/remove|open`。
  项目列表持久化到 `~/.codeerratum/projects.json`，重启恢复并跳过失效路径。
  并发用 `threading.BoundedSemaphore` 限制（默认 2 个项目，`--max-parallel` 可调），
  超出进入 `queued` 状态、可取消排队；`BoundedSemaphore` 在 `execute_prepared` 里
  兜底 release，异常路径不会漏槽位。前端页面通过 `data-base` 前缀所有 API 请求，
  项目页里的「切换仓库」改为「项目总览」；拖拽落在项目标签页时用 `postMessage` 转给外壳。
  新增 `tests/test_hub.py` 10 项测试（隔离、404、增删、持久化、排队与取消排队）。
- **打开当前仓库文件夹**：仓库抽屉的当前仓库信息下方新增「打开当前仓库文件夹」按钮，
  调用 `POST /api/repo/open` 用系统文件管理器打开服务端已定位的 git 根（macOS `open` /
  Windows `explorer` / Linux `xdg-open` 或 `gio`）。目标不接受页面传入，避免被当成任意目录打开器；
  失败弹可复制对话框，与其他写接口一样要求同源 Origin / Host。新增 4 项测试。
- **审查中停止**：进度面板标题栏新增「停止审查」按钮，点一次即向服务端发 `POST /api/review/stop`。
  WebApp 用线程安全 Event 传递停止信号，编排层在 asyncio 循环内轮询并 cancel 所有在途模型调用
  （httpx 请求被真正中断，不会等几十秒才响应），SSE 推 `cancelled` 帧收尾，状态记为 `cancelled`
  而非失败——不弹错误框、不写半截报告。重复点击幂等，无审查可停时返回 409，跨标签页可停同一场审查。
  新增 4 项测试（含在途调用确实被取消的单元验证）。
- **token 消耗统计**：每次模型调用的 usage 本来就逐条采集，现在汇总进运行 summary 与 SSE `done` 帧：
  输入 / 输出 / 缓存 token 总量 + 按模型分解（调用次数与各自小计），进度面板完成后逐行显示。
  `GET /api/review/status` 也带上最后一次运行的 token 汇总，跨标签页跟踪时同样可见。新增 2 项测试。
- **审查进度实时推送（SSE）**：点「运行审查」后侧栏展开进度日志，逐条显示 diff 切分、模型/单元
  评审、证伪、报告落盘，不再只靠一个转圈干等。新增 `POST /api/review/stream` 走 `text/event-stream`，
  进度回调贯穿 pipeline → orchestrator（每模型每单元完成即上报）；`log` / `done` / `empty` / `error`
  四类帧，结束帧关闭连接让浏览器 reader 正常收尾。参数校验与审查锁在发流之前完成，非法输入仍是真
  400/409；错误帧走可复制对话框、不回显本地路径。原 `/api/review` 保留兼容 CLI/测试。新增 5 项测试。
- **模型连通性检测**：配置表格每行新增「检测」按钮 +「检测全部」。发一个最小 chat 请求
  （`max_tokens=1`、单次不重试、10s 超时）验证端点可达 / 鉴权通过 / model_id 有效；绿勾「✓ 可用」带延迟，
  红叉「✗ 不可用」的悬停提示给出 HTTP 状态或原因（401/403 鉴权、404 端点、429 限流、超时等）。
  改动任意字段后结果自动失效，避免拿旧结果判断新配置。`__KEEP__` 会解析已保存的 api_key 再测，
  不产生假红叉。检测不写配置、不回显密钥，与其他写接口一样要求同源 Origin / Host。新增 14 项测试。
- **原生文件夹选择器 + 拖拽定位仓库**：仓库抽屉新增「浏览本地…」，直接弹出系统文件夹对话框
  （macOS `osascript` / Windows PowerShell / Linux `zenity`），选中即定位 git 根并切换；取消不报错。
  文件夹可直接拖到窗口，按名字在最近仓库、家目录（限 5 层、跳过依赖与缓存目录）里定位，唯一命中自动切换、
  多命中列候选。浏览器沙箱不暴露目录绝对路径，故原生对话框跑在本地服务进程里。新增 6 项测试。
- **界面直接切换本地仓库**：顶栏「切换仓库」抽屉，粘贴任意本地绝对路径（支持 `~`）即可把审查目标
  指向该 git 仓库；填仓库内子目录会自动定位到 git 根。最近用过的 6 个仓库保存在浏览器 localStorage，
  一点即切。模型配置按仓库隔离——每个仓库读各自的 `.codeerratum/config.json`，切过去若有配置立即生效。
  切换走同源 Origin / Host 校验，且审查进行中拒绝切换（artifacts 按仓库路径落盘，中途切换会写错地方）。
  浏览器不允许网页读取文件夹选择器，因此用粘贴路径而非原生目录对话框。新增 8 项测试。
- **界面查看与修改配置**：顶栏「配置」抽屉展示当前模式、模型清单、端点与 api_key 脱敏提示，
  可直接增删改并保存。配置写入 `.codeerratum/config.json`（权限 0600），文件优先于环境变量，
  重启 UI 后仍生效，CLI 也读同一份。api_key 从不回显，输入框留空即沿用原值。
  配置写入和审查一样要求同源 loopback Origin / Host；配置文件损坏时直接报错，不静默退回环境变量。
- 界面 diff 输入框补提示：本地未提交改动填 `HEAD`（含已暂存），新文件需先 `git add`；README 同步。
- **默认写工作单 + 文件名改名**：界面「同时写 erratumReview.md 到仓库根」默认勾选；
  产品报告文件名由 `review.md` 改为 `erratumReview.md`（CLI `-o` 默认值与 README 同步）。
  每次运行的内部归档仍为 `.codeerratum/runs/<run_id>/review.md`，历史运行不受影响。
- **顶栏面板互斥**：切换仓库 / 配置 / 文档 / 统计同一时刻只显示一个，当前按钮高亮；
  再点当前按钮收起，点另一个直接切换，点击面板外空白处也关闭（拖拽定位也会切到仓库面板）。

### Fixed

- **模型检测的端点错配诊断**：控制台网页地址收到 POST 会返回 302，原先报含糊的 JSONDecodeError；
  现在直接指出 base_url 可能填成了网页地址并附上重定向目标。`sk-sp-` 密钥配通用 dashscope 端点
  返回 401 时，提示改用 Token Plan 专属端点，不再让用户以为是密钥失效。
- **错误提示改为模态对话框**：原先所有错误走 2~5 秒自动消失的 toast，看不清也复制不了。
  现在错误统一进入带遮罩的对话框，需点「关闭」（或 Esc / 点遮罩）才消失；错误文本框可选中，
  另有「复制错误」按钮。审查失败附带 diff 范围、路径与模式，模型检测失败列出每个端点原因。
- **单提交仓库默认 diff 报错**：UI 输入框默认 `HEAD~1`，仓库只有一个提交时 git 直接报
  `fatal: ambiguous argument 'HEAD~1'`。默认值改为 `HEAD`（本地未提交改动），
  后端遇到该情形给出中文提示并保留原始 git 错误供复制。新增 2 项测试。
- **模型画像重复计数**：`save_run` 与 `save_comparison` 各自写一遍 `model_profile`，
  dual/committee 下每次运行的 `n_assert` 被算两遍；且证伪轮的 `confirm` 仍记在证伪者头上，
  一个从不提交发现的模型会拿到 100% precision。现在 `update_profiles` 是唯一写入者，
  每个增量由 `verdict_events` 的 (run, finding, model, role) 去重，重复保存同一 collection 不再膨胀。
- **`--diff` 可被当作 git 选项**：`get_diff` 把 revspec 直接放在 `git diff` 选项之后，
  `--output=/path` 会被 git 接受，把 diff 写到任意文件并返回空 stdout —— 与“没有变更”无法区分。
  现在拒绝以 `-` 开头的 revspec，并加 `--end-of-options` 让 git 自身兜底。
- **风险标签按子串匹配**：`author_name` 被判成 auth、`blocking_call` 被判成锁，单元预算被导向
  无关代码。改为标识符词匹配并拆分 camelCase，`author` 不再命中 `auth`。
- **误报历史从未回流**：`rank_findings` 的 `rejected_category_counts` 参数一直没人传。
  新增 `ReviewStore.rejected_category_counts()`，执行时读 `feedback.db` 并传给排序。
- **工单渲染能被模型输出破坏**：`claim` 里的换行会注入伪章节，源码/docstring 里的 ``` 会提前
  闭合围栏。claim/detail/说明折叠为单行，围栏长度按内容中最长反引号动态加长。
- **跨版本 diff 会审错区域**：审 `main...feature` 等未 checkout 的版本时，hunk 行号被用来切
  工作区源码，模型看到错位代码，而锚点哈希校验的正是同一份错位源码，所以仍会通过。
  现在 `git diff --raw` 取出新侧 blob，切分、摘录、上下文与锚点校验都用 diff 版本的内容
  （`new_side_sources`）；blob 取不到时回退工作区并逐行核对，不一致就跳过并写明。
- **测试临时目录固定在仓库内**：pytest 的 `current` 符号链接使下一次会话的 `rm_rf` 以
  ENOTEMPTY 失败，收集阶段就中断。移除固定的 `--basetemp=.pytest_tmp`，改用 pytest 默认临时目录。

### Tests

新增 9 条回归测试（画像幂等与归属、选项注入、pathspec 兼容、风险标签、误报回流、工单渲染
防注入、跨版本 blob 切分、跨版本锚点校验、pipeline 传递 blob）；配置界面新增 11 条（脱敏、
往返与权限、保留密钥、坏输入、同源校验、配置驱动真实运行、损坏文件不静默回退）。合计 180 项通过。

## [0.4.2] — 路径过滤

### Added

- `--path` / UI「路径过滤」：把 diff 限定到指定目录（如 `src/api`），不必先 `git add`
  再借 `--cached` 绕路。支持空格分隔的多个路径；报告「覆盖说明」会写明审查了哪些路径。
- 路径校验：拒绝绝对路径与 `..`（含 Windows 式 `..\`），在Web 层即以 400 返回。

### Tests

新增 `tests/test_pathspec.py` 5 项。合计 161 项通过。

## [0.4.1] — 审查回归与安全加固

### Fixed

- **`独有区永久为空`**：`agreement()` 把证伪者的 `confirm` 当成第二个独立审查者，
  也把同一模型跨审查单元的重复断言算成共识。现在按「独立断言的模型数」判定，
  证伪裁决只决定发现是否保留，不再参与一致性计算。实测：同一份 mock 输入从
  `unique=0 / consensus=2` 变为 `unique=2 / consensus=0`。
- **模型画像计错对象**：证伪裁决过去累加到裁决者（专职证伪者）头上，导致一个从不
  提交发现的模型拿到 100% precision 并被优先选为审查者。现在裁决归给被裁决发现的
  作者，且一个发现对每个作者只算一个精度样本。
- **Web UI 跨站 POST / DNS rebinding**：过去「没有 Origin 就放行」使任意网页能用
  text/plain 表单提交 JSON body 来烧 token；Host 也未校验，evil.com 指向 127.0.0.1
  同样能读到本地状态。现在 POST 必须带与 loopback Host 完全一致的 Origin，
  所有请求一律拒绝非 loopback Host。
- **Web UI 输出路径越界**：`write_markdown` 的 `out` 曾接受绝对路径与 `..`，
  本地页面可借审查请求写仓库外文件；现在只接受简单文件名。
- **Web UI 错误回显**：未捕获异常的文本（可能含用户磁盘绝对路径）不再返回浏览器，
  只写入服务端日志。
- `codeerratum docs --check` 的漂移表格被打印两遍。

### Tests

新增 4 条回归测试，覆盖 5 种情形（证伪者 confirm 不算共识、同模型重复不算共识、
无 Origin 的 POST 被拒、非 loopback Host 被拒、越界 out 被拒）。合计 156 项通过。

## [0.4.0] — 四、五、六期

第四期把结果送到 PR 上，第五期补文档与漂移，第六期接 MCP 与多语言。同时清掉三处
「看起来是数字、其实是常量」的设计债。

### Added

- `codeerratum pr [--post]`：读 GitHub event，跑审查，行内评论上限 5 条 + 1 条摘要。
  默认 dry-run 并把待发内容写入 `pr-comments.json`。
- `action.yml` 与 `docs/github-action.yml` 示例；fork PR 拿不到 secrets 的坑已注明。
- `codeerratum docs [--check] [--strict]`：从 AST 生成模块文档与签名基线，
  漂移检测只看 API 形状变化，不看行号移动。
- `codeerratum mcp`：stdio MCP server，四个工具 `review_diff` / `list_runs` /
  `get_finding` / `check_docs_drift`。
- Web UI 新增「文档」抽屉：模块树、公开符号数、签名漂移（`GET /api/docs`）。
- `detect_language()`：按扩展名给单元打语言标签，非 Python 文件按文件级审查。
- `spec.py`：规格等级真实探测（L4 契约 / L3 外部规格 / L2 自述 / L1 仅代码），
  报告头同时给出判定依据。
- `abstain_warning()`：证伪轮 `insufficient_context` 超过 30% 时进入 notices。
- 模型画像回流：`save_run` 累计 `model_profile`（断言/独有/确认/反驳），
  下次运行注入 `ModelSpec.measured_*`；`apply_measured_profiles()` 负责这一步。

### Changed

- `split_diff` 按文件扩展名推断语言，`language=` 参数变成强制覆盖。
- prompt 的代码块标记改用单元的真实语言，而不是写死 `python`。
- 报告头「规格等级」由 `L1` 变成 `L1（无规格来源，仅凭代码判断）` 这类可辩驳的表述。

### Fixed

- `annotation_coverage` 曾用绝对路径判断跳过目录，任何位于 dot 目录下的仓库
  （临时目录、CI workspace）都会被整体跳过，覆盖率恒为 0。
- `Store.model_profile_map()` 与既有 `model_profiles()` 重名，改为独立名称。

### Security

- PR 评论按 hunk 过滤锚点：GitHub 会因一行不在 diff 而拒掉整批，降级为摘要而非全丢。
- 同一 commit 重跑不重复评论（隐藏 marker + 先查后发）。
- MCP server 只读本地仓库，工具参数中的路径不接受 URL；stdout 只输出协议消息。

## [0.3.0] — 项目规范

让审查知道「本项目的规矩」，降低噪音。形态是**追加**而非替换：把 system prompt 交给用户整段覆盖，会把静默失败引进来（锚点校验全挂 → 空报告 → 看起来像「没有 bug」）。

### Added

- `codeerratum review --rules <file>`；不传则自动发现 `<repo>/.codeerratum/rules.md`。
- 规范包在 `<house_rules>` 块内，位置在硬规则之后、`# Output` 之前；块头显式声明「与硬规则冲突时硬规则优先」。
- UI 侧栏折叠区可直接编辑规范；`/api/meta` 回传自动发现状态（路径 / 字数 / sha），空白 textarea 的语义因此不再含糊。
- `FindingCollection` 新增 `house_rules_sha` / `house_rules_chars` / `house_rules_source`，`review.md` 头部同步显示。同一 diff 两次结果不同时，先看这里。
- `tests/test_house_rules.py` 14 项，含「规则确实进了发给模型的 messages」这条端到端断言。

### Changed

- prompt 模板新增 `{{HOUSE_RULES}}` 占位符；加载器找不到占位符时**抛错**，而不是静默跳过——否则以后改 prompt 时规则会无声消失。
- 证伪轮不接收项目规范（有意为之）：吸收了项目规矩的证伪者会退化成第二个审查者。

### Security

- 用户文本里的 `<house_rules>` / `</house_rules>` 先剥离，防止提前闭合标签越界成指令。
- 规范上限 4000 字，超出截断并声明；防止用户文本把硬规则挤出上下文窗口。
- UI 只接受规范**文本**，不接受路径，本地服务不具备读任意文件的能力。

## [0.2.0] — 图形界面

`codeerratum ui`：本地 Web UI，标准库实现，零新增依赖。目的不是好看，是把终端里读不出来的卖点（三区分流、证据等级、证伪记录）变成一屏能看懂的东西。

### Added

- `codeerratum ui [--port] [--host] [--no-browser] [--repo]`：起一个本地服务并打开浏览器。
- 界面内发起审查：diff 范围 / 模式 / 单元上限 / **作者模型排除** / 可选同时写 `review.md`。
- 三区分流页签（独有 / 冲突 / 共识），quick 模式退化为「全部发现」。
- 逐条卡片：严重度、`文件:行` + 带行号代码片段、证据与强度、修改建议（diff 上色）、验收命令一键复制、证伪轮各方意见。
- 采纳 / 驳回（强制填理由）/ 重置，直接回写 `feedback.db`；状态按 finding 内容 id 关联，重跑不丢裁决。
- 被定位校验丢弃的发现可展开查看——防幻觉这条设计底线在界面上可见。
- 历史运行列表 + 统计抽屉（运行数、发现数、采纳率、按分类）。

### Changed

- 抽出 `pipeline.execute_review()` 作为 CLI 与 UI 的唯一执行路径。此前逻辑内嵌在 `review` 命令里，UI 若另写一份必然漂移（不同的上限、不同的落盘、不同的错误处理）。
- 每次运行固定归档 `review.md` 到 `.codeerratum/runs/<run_id>/`；`-o/--out` 只是额外副本，不再要求写进仓库根。
- CLI 提示文案统一为中文（数值与结构不变）；单元超限提示由英文 `Capped at N units` 改为中文说明。

### Security

- 服务默认只绑 `127.0.0.1`；POST 校验 `Origin`/`Host`，跨站请求返回 403（否则任意网页都能替你发起付费调用）。
- run_id / finding_id 走正则校验，阻断路径穿越；请求体上限 2MB。
- 进程内互斥锁：一次只跑一个审查，第二个请求返回 409。

## [Unreleased]

一次针对已完成代码的审计修复，重点是"让输出可信"与"让数据可积累"。

### Fixed — 会导致功能失效或数据错误

- **严格 JSON Schema 不合规**：`strict: true` 的 schema 缺 `additionalProperties: false`、`required` 未覆盖全部字段、`str | None` 仍是 `anyOf`，真实 OpenAI 端点会直接 400。现在发送前统一改写为 strict 子集；端点仍拒绝时自动降级为 `json_object` 并记住该能力。
- **单条非法 finding 摧毁整块结果**：`claim` 超 60 字符会抛异常，导致同一次调用里其他有效结论一起丢失，并被误报成 provider 错误。改为逐条校验，非法项进入 `rejected`（`invalid_finding`）。
- **跨文件锚点被接受**：模型写 `file: other.py`、片段却来自被审查文件，校验仍通过，报告会指向一个不存在的定位。现在文件名不匹配一律拒绝（`anchor_file_mismatch`）。
- **删除文件被当作可审查单元**：源文件不存在，读取时抛 `FileNotFoundError`。删除文件与工作区不存在的文件现在跳过，并在报告《覆盖说明》中列出。
- **parse 成功率统计口径错误**：重试成功的调用被记为 `success=0 / failed=1`。改为按调用计数（`calls / first_try / repaired / failed`），新增 `success_rate`。
- **重复发现重置人工裁决**：`INSERT OR REPLACE` 把 `accepted` 改回 `open`，采纳率被自己抹掉。改为 upsert 只更新内容字段，保留状态与首次时间。

### Fixed — 质量、成本、一致性

- **调用方检索恒为空**：jedi 在 hunk 首行第 0 列查询，永远拿不到引用。改为定位符号名 token，并按仓库作用域创建 `jedi.Project`。
- **每次发送整个文件**：一行改动会把整文件（外加重复的函数体）塞进 prompt。改为按 hunk ±6 行 + 包围符号生成带绝对行号的摘录，超长时截断并标注省略行。
- **多文件 diff 丢失末个 hunk**：跨文件边界前没有回填 body，除最后一个文件外每个文件的最后一个 hunk 内容为空，导致 risk tag 与符号归属失效。
- **报告自相矛盾**：正文渲染 13 条、摘要写 10 条、执行结果表 10 行。现在三处口径统一，并在头部与摘要显式披露"共 N 条，渲染前 M 条"。
- **排序被丢弃**：聚合器算出的证据/一致性权重被 `(severity, id)` 重排覆盖。新增 `ranked_findings()`：严重度 → 硬证据 → 一致性。
- **lens 被硬编码**：模型给的安全类视角被丢弃，报告统一显示 `correctness`。
- **静态工具重复执行**：同一文件每个模型各跑一次 ruff/mypy。按 (path, mtime, size) 缓存；超时/缺失降级为空结果而不中断审查。
- **无效 diff 抛原始堆栈**：新增 `DiffError`，CLI 给出中文提示与替代命令。

### Changed

- 运行产物与反馈库统一落在仓库根的 `.codeerratum/`（`runs/<run_id>/` + `feedback.db`），不再随 cwd 漂移，也不污染工作区。
- `review` 新增 `--author-model`：作者模型不得进入审查席位（此前只有路由层约束，CLI 从未传入）。
- 单元预算按"风险标签 → 是否公开符号 → 改动行数"排序后再截断。
- 报告新增《本次覆盖说明》，把跳过的文件、失败的调用、被截断的单元数显式写出。

### Added

- `LICENSE`（MIT）、GitHub Actions CI（3.11/3.12/3.13：lint + format + test）。
- 版本号单一来源（`pyproject.toml` 从 `src/codeerratum/__init__.py` 读取）。
- `tests/test_regressions.py`：16 条回归测试，每条对应上面一个已修缺陷。
- 中英双语 README：英文 `README.md` 作默认首页（PyPI 描述同源），中文 `README.zh-CN.md`，两版顶部互链。
