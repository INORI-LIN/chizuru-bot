# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

本仓库的文档、注释与开发约定均为中文；本文件沿用中文书写。

## 项目概览

面向 QQ 群的**文本**聊天机器人：AstrBot `==4.28.1` Star 插件 + NapCat/OneBot v11 + DeepSeek，
以《租借女友》水原千鹤为人设参考。**非官方、仅被 @ 时回复、不主动接话**。

**当前状态：未上线。** S0 离线核验完成，但 **S0 阶段门于 2026-09-17 被维护者决定有意跳过（不是通过）**，
在线部分（真实 QQ/NapCat/DeepSeek）从未验证。S1 进行中：`S1-01`—`S1-15` 的**离线部分**均已完成
（含 `main.py` 装配与离线回归），`S1-16` 阶段门与 `S1-10` 的在线部分仍【阻塞·在线/凭据】。
**S2 于 2026-09-18 离线先行**（docs/03 §9.5）：`S2-01`—`S2-09` 的**离线部分**均已完成
（`storage/`、`context_buffer.py`、`context_assembly.py` 动态材料、`history.py`、`notice.py`
告知两步开启与维护者命令、清理覆盖与启动裁剪、`main.py` 接线）；**`S2-02`/`S2-05` 的前置已由维护者确认**
（2026-09-18：维护者清单与目标群已定，值只进运行时配置；附录 C.1 告知文案定稿），
`S2-10` 阶段门的**门禁记录已产出（docs/03 附录 F）且结论为未通过**（S1 门未过、无在线证据；G03 的记忆辅助整轮与 G04 的 A09/A10 属 S3），**S2 不得宣称完成、S3 入口仍不成立**。
**S3 于 2026-09-18 离线先行**（docs/03 §9.6，性质同 S0/S2 的"有意先行"）：批次 3a（`S3-01`/`S3-02`）、3b（`S3-05`/`S3-06`）、3c（`S3-07`/`S3-10`）与 3d（`S3-08`/`S3-09`）的**离线部分**已完成——
`storage/schema.py` 升到 `SCHEMA_VERSION=2` 并新增记忆授权、低敏事实、来源去重三张 STRICT 表，`storage/memories.py` 提供仓储
（上限与保留期由调用方注入），`members.bump_member_revision` 让记忆变更复用成员修订号；`memory/` 包落地类别白名单与候选校验、
独立抽取请求与结构化解析（**提示词为待审初稿**，`extract-1` + 指纹钉住）、抽取的准入与单事务写回（回复之后 inline 运行、
零额外出站、失败不影响聊天）、以及隔离检索与临时注入（**用了记忆的那一轮不落共享历史**，附录 F 的 G03-③ 由此闭合）；
计划批次仅剩 3e（S3-11/S3-12，见 docs/03 §4.2），`S3-03`/`S3-04` 因附录 C.2/C.3 未定稿保持【待审】、`S3-13` 不排期。
**S3 不得宣称完成**；S4 未开始。
**生产环境采集与记忆均默认关闭**：采集需维护者在运行时配置里填入允许群与维护者并完成一次两步开启；
记忆链路在 `S3-03` 落地前**没有任何合法写入路径**（不得手工写库充当验收证据）。

| 文档 | 角色 |
|---|---|
| [`docs/01-requirements.md`](docs/01-requirements.md) | 需求编号 R-* 与验收用例 A01—A19；**验收依据，与本文档冲突时以其为准** |
| [`docs/02-architecture.md`](docs/02-architecture.md) | 信任边界、数据生命周期、并发与故障策略、环境基线、§14 实施记录 |
| [`docs/03-implementation-plan.md`](docs/03-implementation-plan.md) | 阶段门禁、任务卡、依赖图、风险登记 R1—R25、附录 A—F（D 为 S0 证据、E 为 S2 判据映射、F 为 S2 门禁记录）；**进度唯一权威** |

## 常用命令

项目根目录执行。所有命令经 `.runtime/astrbot` 的受控环境运行，**不安装依赖、不联网**。

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export ASTRBOT_BUILD_DASHBOARD=0   # 阻止上游构建钩子运行 npm
```

```sh
# 全量离线核验：7 个核验脚本 + 全部单元测试（当前共 545 项单元测试）
bash scripts/s0/run_all.sh

# 只跑全部单元测试
env -u VIRTUAL_ENV uv run --project "$PWD/.runtime/astrbot" --no-sync --offline \
    python -B -m unittest discover -s "$PWD/tests"

# 单个测试模块
env -u VIRTUAL_ENV uv run --project "$PWD/.runtime/astrbot" --no-sync --offline \
    python -B -m unittest discover -s "$PWD/tests" -p "test_budget.py" -v

# 单个测试方法（-k 为子串匹配）
env -u VIRTUAL_ENV uv run --project "$PWD/.runtime/astrbot" --no-sync --offline \
    python -B -m unittest discover -s "$PWD/tests" -k test_extraction_stops_before_chat -v

# 单个核验脚本（check_env / check_outbound / check_gating / check_collect / check_temp / check_history / check_single_call）
env -u VIRTUAL_ENV uv run --project "$PWD/.runtime/astrbot" --no-sync --offline \
    python -B scripts/s0/check_gating.py
```

环境与锁一致性（S0-01 判据，改动环境后必须复核）：

```sh
cmp environment/uv.lock .runtime/astrbot/uv.lock          # 必须逐字节一致
git -C .runtime/astrbot rev-parse HEAD                    # 应为 ab42c0d9b726d82ad0f9563e04c53a4460c00d61
uv lock --check --offline --project "$PWD/.runtime/astrbot" --no-python-downloads
```

**没有配置 lint/format 工具**：ruff 不在受控环境内（架构 §14.3 明示"未宣称完成格式检查"）。
不要声称跑过 lint，也不要为此新增依赖。

## 环境与依赖约束

- **全程 uv，禁止 pip**：`pip` / `pip3` / `python -m pip` 及一切隐式调用被禁；`uv pip` 不是替代方案。
  插件源码中不得出现 `requirements.txt`，也不得导入 pip。
- `.runtime/astrbot/` 是固定提交的上游 AstrBot 源码（已 gitignore）与**唯一虚拟环境**
  `.runtime/astrbot/.venv`。**不得修改上游源码或重新解析锁**。插件通过软链接
  `.runtime/astrbot/data/plugins/astrbot_plugin_chizuru → ../../../../astrbot_plugin_chizuru` 接入，
  不存在第二份源码。上游自带 `.runtime/astrbot/AGENTS.md`，仅描述上游项目，与本插件约定无关。
- 插件**零第三方运行依赖**，测试只用标准库 `unittest`。需要新依赖时必须先与维护者确认并走受控锁变更。
- 每次测试都会经 `sys.addaudithook` 建立进程级守卫：**禁止子进程、网络连接/绑定、pip 导入**，
  SQLite 只允许落在 `.runtime/` 下的临时库（K12 / R8）。存储类任务需要放宽时只做**作用域内白名单**，
  绝不能删除或整体停用守卫。

## 架构

### 目标数据流（S1 装配 + S2 离线部分已完成）

```
OneBot 事件 → main.py（唯一框架入口、薄适配，提取 MessageFacts）
  → policy.classify（触发形态）
  → commands.parse（指令 / 聊天分流）
  → control.authorize（确定性权限判定；只来自可信身份 + 显式配置）
  → dedup.begin（事件与动作去重）→ _prepare_chat（材料 + 历史 + 修订快照，超预算即拒绝）
  → budget.reserve（先预留）→ scheduler.submit（准入）
  → llm.py（S1-10 离线部分，直接调用原生提供商）→ send_gate.evaluate（含发送前修订重读）
  → 唯一出站路径 → 仅送达时写回互动历史（history.py / 框架会话存储）
```

非 @ 的普通群聊只走采集支路：`policy.classify → IGNORE/… → main._maybe_collect`
（受信范围 ∧ 形状 TEXT_ONLY ∧ 策略仓储 `is_collection_open` ∧ 成员未退出）→ `context_buffer.ingest`；
**不产生任何出站或模型调用**。生产环境因策略仓储无行而默认关闭（S2-02 未落地）。

`main.py` 已按此装配（S1-14 + S2-02/03/04/05/06/07）：唯一出站路径是 `_deliver()`；
群内出口有且只有两处——`千鹤 状态`（既有报告 + 一行群上下文状态）与 `群上下文 开启`
（含确认失败时的重发全文，逐字取自已定稿的附录 C.1）；`上下文 退出/加入`、`群上下文 关闭`、
`上下文 清空`、`千鹤 暂停/恢复` 执行状态变更但**静默无回执**（回执文案待审）；
动态材料经 `extra_user_content_parts`（`mark_as_temp()`）、历史经 `contexts`，
两者都不进 system 且受同一 8192 预算裁剪。

### 模块边界（每个模块"必须不做"与职责同等重要）

| 模块 | 职责 | 必须不做 |
|---|---|---|
| `main.py` | Star 生命周期、钩子注册（**只注册 `on_message`**）、唯一装配点与出站路径 `_deliver()`；普通群聊采集分支与 `上下文 退出/加入`（S2-03/04）；告知两步开启与维护者命令 `关闭/清空/暂停/恢复`（S2-02/05）；聊天装配、修订号复核与历史写回（S2-06/07） | 不写业务规则、不解析身份、不直接 `event.send`（出站必经 `send_gate`）；采集不落盘、不发消息、不调模型；历史写失败不置持久降级、不影响已送达回复；群开关读策略失败按未暂停放行（R21） |
| `config.py` | fail-closed 配置模型：任一字段缺失或畸形 → 整体退回哨兵 `Settings()`（拒绝全部） | 不默认开启采集/记忆；密钥不进 `repr` |
| `policy.py` | 纯触发形态分类：`IGNORE` / `EMPTY_OR_UNSUPPORTED` / `UNSUPPORTED_ATTACHMENT` / `TEXT_CANDIDATE`；`is_trusted_scope` 是权限与触发**共用的唯一谓词** | 不判权限、不读 DB、不判采集资格（属 S2）、不做指令分流 |
| `context_assembly.py` | 人格分层与静态规则 + 动态材料装配（S1-11 + S2-06）：`STATIC_RULES`/`PERSONA_VERSION`/`build_chat_plan()`；`render_materials()`（短标签、转义昵称、相对时间、同事件去重）、`trim_to_budget()`（8192 预算，先裁最旧材料再裁最旧历史，无法裁剪即返回 `None` 拒绝）；固定规则只进 system | 不组装上下文材料以外的东西、不调模型、不接受事件对象（因而读不到 `message_str`）、不导入框架（临时标记由 `main.py` 施加） |
| `history.py` | `@` 互动历史（S2-07/S2-08）：`_at` 私有键、成对解析（不合格条目保守丢弃）、20 轮/24 小时取严、`flatten()` 剥离私有键、`storage_entries()` 写回形状、`should_record()`（送达 ∧ 非记忆辅助轮）、`trim_stored()`（启动裁剪只动 `_at` 成对条目） | 不导入框架、不做 IO、不判权限、不复制全历史（存储归框架会话存储）；裁剪不误删框架条目 |
| `notice.py` | 群告知文案与两步确认窗口（S2-02）：`NOTICE_TEXT`/`NOTICE_VERSION`（附录 C.1 逐字 + 指纹）、每群单槽 5 分钟窗口（换人即作废、有界、惰性淘汰）、`describe_policy()` 状态行 | 不导入框架/sqlite/asyncio、不做 IO、不调模型、不发消息、不判权限、不读配置（版本由调用方传入） |
| `keys.py` | `BotInstanceKey` / `GroupKey` / `MemberKey`、`RevisionSnapshot`；只由可信元数据构造，缺一段即构造失败 | 不用昵称/群名/正文构造键 |
| `commands.py` | 纯解析 `CommandIntent` + 所需权限档；空白归一化 | 不核权、不执行、不调模型；**调用前提是调用方已确认真实 @** |
| `control.py` | 确定性授权：可信身份 + 显式配置映射 | 不读事件对象、不导入框架、不调模型；QQ 群管理员 ≠ 系统管理员 ≠ 维护者 |
| `scheduler.py` | 有界队列、全局/同群并发、业务期限；聊天优先于抽取 | 不发送、不调模型、不读存储、不判权限、不重试；**不用 `asyncio.Semaphore`**（FIFO 无法表达聊天优先）；不持有后台任务 |
| `dedup.py` | 事件与动作去重：`IN_FLIGHT` / `DONE` / `UNCERTAIN`；有界（容量 + TTL） | 不保存任何正文；**窗口无默认值，必须由调用方注入**；`release()` 仅限确认无副作用时 |
| `budget.py` | 预留 / 按 usage 结算 / 门槛核算 | 不调模型、不硬编码价目；缺失 usage 记估算**而非零**；价格未知且已配金额时保守拒绝 |
| `health.py` | 平台状态归一化（只认 `Platform.status` 四个取值，其余落 `UNKNOWN`）、持久降级标志、抽取失败计数、供维护者查询的快照与文本 | 不导入框架、不探测、不发送；**不表示 QQ 登录态**（报告固定输出"未知"）；不读 `instance.config`（含 token）与任何错误文本/堆栈 |
| `send_gate.py` | 出站前复核：源范围 → 源触发形态 → 目标群一致 → 群开关 → 修订号 → 发送不确定；不一致即丢弃 | 不发送、不调模型、不做补救；**群开关与修订号由调用方注入**（无默认值）；不提供任何回执/送达 API；固定提示构造时即禁止模型调用 |
| `redact.py` | 类别化日志记录（`AuditRecord` 无自由文本字段）、错误码闭集、每进程加盐的关联标识、保留期与大小策略、AstrBot/NapCat 日志审计清单 | 不做日志 IO、不轮转文件（`logging` 都不导入）；不记正文/请求体/密钥/推理文本；关联摘要不可还原、跨重启不可关联（刻意取舍） |
| `context_buffer.py` | 普通群聊内存环形缓冲（S2-03）：30 条/10 分钟取严、惰性淘汰、无后台任务；形状/命令/敏感/自身/重复全部拒绝；`clear_group`/`clear_member`；条目附带昵称（**仅展示**） | 不落盘、不请求模型、不发消息、不导入框架；**条数与 TTL 由调用方注入**；不采集未告知群；昵称不进键、不参与判定 |
| `storage/*` | 插件 SQLite（S2-01/S2-04/S2-05 + S3-01/S3-02）：`db`（延迟建库、短事务、失败不恢复）、`schema`（**五张** STRICT 表 + `user_version`，版本 2）、`groups`（群策略仓储，无行即关闭；`bump_revision` 供清空与无行群的暂停使用）、`members`（退出/加入 + 成员修订号，含 `bump_member_revision`）、`memories`（记忆授权、低敏事实、来源去重） | 不在事务内等网络（纯同步、只 import 标准库）；不存全量群聊；**两张 S2 表不存记忆授权**（授权只在 `memory_state`）；**上限与保留期不写默认值、由调用方注入**；路径由调用方注入（无默认值） |
| `memory/*` | 授权记忆的候选层、管线与检索（S3-05—S3-08、S3-10，**3b—3d 已实现离线部分**）：`types.py`（四类白名单 `address`/`reply_length`/`interest`/`activity`、`prepare_source` 三道判定、`build_candidate` 校验）、`extract.py`（`EXTRACTION_RULES` + `EXTRACT_VERSION` 指纹钉住的**待审**提示词、`build_request` 的 `contexts`/临时材料恒空、`parse_candidates` 只读两个键且不合格整批放弃、`ExtractionStatus`）、`pipeline.py`（`admit` 的五道准入与 `write_back` 的单事务修订重核 + 来源去重 + 写入）、`retrieve.py`（**只接受可信元数据构造的成员键**、注入块渲染：`MEMORY_BLOCK_TITLE` 与 `CATEGORY_LABELS` 逐字取自需求 §4.3，`memory-block-1` 待审） | 不导入框架/asyncio、不发送、不做预算预留与调度（属装配层）；**不含人设与群历史**；不递归触发聊天；**不读取模型给出的身份/授权字段**——归属只来自可信事件元数据；所有准入拒绝都静默；`retrieve` 没有可传入他人 ID 的入口 |
| `llm.py` | 请求成形（`LLMRequestPlan`，键集固定、无工具参数）、回复解释（只读 `role`/`completion_text`）、错误分类（状态码委托 `redact.ErrorCode`）、≤1 次重试判定（共用同一 `Deadline`）、usage 映射、`FollowUp` 类别 | 不发送、不导入框架、不建网关/客户端；不组装人格与上下文（S1-11）；不读写历史与存储；**不读取推理字段、不追加工具**；不含任何群内文案；**在线部分未验证**（真实 401/402/429/5xx 与 usage 仍待 S0-07） |

**尚不存在**（属后续任务卡，不要 import）：记忆授权确认状态机（落点待定，属 S3-03）；`storage/` 与 `memory/` 已按 3a—3d 落地。
docs/03 §3.2 的目录树是**拟议结构**，不是现状清单——判断某文件是否存在请直接看仓库。

### 不可违背的硬约束

- **模型不参与授权**：权限、成员归属、保存范围只由可信事件元数据与确定性代码决定。
  昵称、群名片、消息正文、LLM 输出一律不作为授权或数据库指令依据。
- **fail-closed 是默认**：配置空/畸形即拒绝全部；空允许群列表 = 全部关闭；未配置抽取预算金额时
  自动记忆抽取保持关闭（金额 0 表示"未配置"，**不是"不限额度"**）。
- **凭据**：QQ 登录态、OneBot Token、API Key 只走运行时受限配置，**不进入仓库、文档、模型上下文、
  普通日志**。维护者提供时只说明配置位置，不要求写入受版本管理的文件。
- **严格 @ 是框架级问题，不是插件局部问题**：`WakingCheckStage` 早于插件执行且自身可发消息（K1/K2/R6），
  框架**不存在全局出站门控**，`on_decorating_result` / `after_message_sent` 不是拦截点。
  严格 @ 只能靠"只启用本插件 + 关闭附录 D.2 的全部配置键 + 插件自身收口"共同保证。
  **在完成在线验证前，本插件不得加载到混有其他插件的在线实例。**
- 必关配置只能设在**全局** `<ASTRBOT_ROOT>/data/cmd_config.json`，插件的 `_conf_schema.json`
  **无法**设置这些键。注意 `wake_prefix` 有顶层 list 与 `provider_settings.wake_prefix` string 两个不同键。

### 易错点（已核验的上游行为，逐条有源码位置，见 docs/03 §3.4）

- K1：任一 handler 的 filter 通过即使事件 `is_wake = True`——宽 filter 有副作用，须显式收口。
  收口默认 LLM 链路要用 **`should_call_llm(True)`**：`call_llm` 初值为 `False`，第二路径的门槛是
  `not event.call_llm`（K5 已于 2026-09-18 修正，原表述写反）；需要交给框架送出的回复不能
  `stop_event()`，否则回复也不会发出。**本插件的模型调用不走 `event.request_llm`**：按附录 D.2
  直调原生提供商（K14）。
- K3：私聊默认唤醒；K4：`ProcessStage` 在 handler 之后还有**第二条默认 LLM 路径**（同一事件可能双次计费）。
- K6：`mark_as_temp()` 只作用于 part，整轮排除还须在 `on_agent_done` 对 user 与 assistant
  两条 `Message` 设 `_no_save`。
- K8：EventBus 为每个事件建独立 task，"同群并发 1"必须插件自行加锁。
- R15：保留内置插件不受 `disable_builtin_commands` 约束，其 `handle_empty_mention` 优先级高于本插件
  且会 `stop_event()`；唯一关闭手段是 `platform_settings.empty_mention_waiting`。
- R16：框架对会话与平台消息历史**无原生 TTL**，20 轮/24 小时与 90 天全部需插件自建清理。
- R21：存储路径解析失败（`StarTools.get_data_dir()` 探测不到插件名、库损坏等）会**静默降级**为
  "无存储"：采集与 `上下文 退出/加入` 全部关闭，插件照常聊天，只有 `千鹤 状态` 的降级行可见。
- R11：原生流程在发送门控前就已写入历史（`STAGES_ORDER` 中 ProcessStage 早于 RespondStage）。
- K15：`PlatformMessageHistoryManager.delete` 的 **docstring 与实现相反**——删的是"最近 offset 秒内"
  （`created_at >= now - offset`），默认 `86400` 只清最近 24 小时；整体删除的唯一惯用法是
  `offset_sec=99999999`。另：QQ 群消息经 `event.send` **不写**这张表（只有内置群历史开关或
  `Context.send_message` 才写），所以清理它是幂等防御动作。
- K15 相关：`ConversationManager.get_conversations(platform_id=...)` **只在 platform_id 为真时过滤**，
  空串会枚举全实例会话——插件侧必须先挡掉空 `platform_id`。

## 开发工作流

按**任务卡批次**推进（docs/03 §5 的分组：S1 的 1a—1e 等）：

1. 从 `docs/03-implementation-plan.md` 取下一批 2—3 张任务卡，读其"任务/依赖/交付物/R/A/完成判据"。
2. 先确认依赖已 `【已完成】`；带 `【阻塞】`/`【待审】`/`【在线】` 标记的任务未满足前置不得开工。
3. 实现模块 + 对应 `tests/test_*.py`。**既有测试必须继续通过，不得放宽或删除既有断言**；
   真正的行为分化（如新增分类档位）需在 docs/03 记录理由。
4. 跑 `bash scripts/s0/run_all.sh` 全绿。
5. 把状态**就地回写** docs/03 的任务卡表（未开始/进行中/已完成/阻塞），不新建状态文档；
   风险登记表（§8）只更新"当前状态"列，**不得删除已登记行**；范围变化时递增文档次版本并在文首注明。
6. 提交。

**不得做的事**：为推进度弱化验收判据；把"离线测试通过"当作"验收通过"；宣称某阶段完成
（`S1-16`/`S2-10`/`S3-13` 阶段门未记录通过即不得宣称）；把未核验的假设写成默认参数
（例：`dedup.py` 的去重窗口必须由调用方注入，来源是仍待在线验证的 O-07）。

**在线前置仍阻塞**：目标群与维护者清单、真实 QQ 账号与测试群、运行时 DeepSeek 凭据
（docs/03 §9.2）。未满足时对应能力保持关闭，不得由实施者擅自取默认值放行。

## 测试组织

- 测试用标准库 `unittest`，无 pytest/conftest。**必须在仓库根目录运行**：测试以顶层包名
  `astrbot_plugin_chizuru.*` 导入模块，靠 `python -m` 把 CWD 放进 `sys.path`。
- `tests/test_plugin.py` 使用**真实** AstrBot 框架对象（导入 AstrBot、解析 `metadata.yaml`、
  注入 `_conf_schema.json`、经 `call_handler` 复刻 `star_request.py` 的处理器调用循环）；
  `tests/test_assembly.py` 同样用真实框架对象 + `tests/fakes.py`（假时钟/提供商/平台、
  事件工厂、send 记录器、K12 作用域守卫）；其余 `test_*.py` 只导入 `astrbot_plugin_chizuru/`
  下的纯逻辑模块。
- **装配级测试必须替换 `event.send`**：真实实现会 `asyncio.create_task(Metric.upload(...))`
  并联网，离线守卫会拒绝且破坏"terminate 后无新增任务"的不变量。`tests/fakes.py` 不是
  测试模块（文件名不匹配 `test*.py`），也不要 import `scripts/s0/`。
- 存储类测试（`tests/test_storage_*.py`）用 `tempfile` 在 `.runtime/` 下建临时库（K12 只放行
  该前缀）；装配级用例经 `plugin.storage_path` 注入库路径，并把 `plugin.history_cleaner`
  换成 `fakes.FakeHistoryCleaner`（真实实现会触框架会话库与数据库）。
- S0 核验脚本在独立进程运行，共享引导在 `scripts/s0/_harness.py`（`Harness` + `Checker`）；
  **每个进程只能建立一个 `Harness`**（audit hook 不可卸载）。新脚本照抄该模板即可获得
  隔离 `ASTRBOT_ROOT`、11 类事件门控矩阵所需的真假事件与出站/模型调用记录器。
- `scripts/s0/` 不属于产品插件包，不参与插件运行时；不要把它 import 进插件源码。
