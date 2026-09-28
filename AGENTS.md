# AGENTS.md

本文件是本仓库的项目记忆与开发约定，面向所有在此仓库工作的 AI coding agent（CodeBuddy Code、Codex、Cursor 等）。
2026-09-23 起取代原 `CODEBUDDY.md`（已删除）；CodeBuddy Code 在 `CODEBUDDY.md` 不存在时会自动加载本文件。
本仓库的文档、注释与开发约定均为中文；本文件沿用中文书写。

**权威顺序（冲突时按此裁决）**：`docs/01-requirements.md`（验收依据，与任何文档冲突时以其为准）→
`docs/03-implementation-plan.md`（进度与门禁的唯一权威）→ `docs/02-architecture.md`（架构与实施记录）→
`README.md` 与本文件（导览；事实以代码与 docs 为准，两者都可能滞后）。

> `.runtime/astrbot/AGENTS.md` 是上游 AstrBot 自带的同名文件，只描述上游项目，与本插件约定无关；
> 不要引用它、不要修改它。

## 1. 项目概览与当前状态

面向 QQ 群的**文本**聊天机器人：AstrBot `==4.28.1` Star 插件 + NapCat/OneBot v11 + DeepSeek，
以《租借女友》水原千鹤为人设参考（人设核心原则：**水原千鹤是个好女孩**）。
**非官方、仅被 @ 时回复、不主动接话。**

**当前状态：未上线。** 真实 QQ/NapCat/DeepSeek 从未联调。阶段状态（细节见 docs/03 §4、§9 与附录 D—L）：

| 阶段 | 状态 |
|---|---|
| S0 技术可行性 | 离线核验完成并留证（附录 D）；**阶段门于 2026-09-17 由维护者决定有意跳过——不是通过**；在线待办 O-01—O-10 仍有效 |
| S1 基础连接与严格 @ | `S1-01`—`S1-15` 的**离线部分**【已完成】；`S1-16` 阶段门【阻塞·在线/凭据】，**未通过** |
| S2 群上下文 | `S2-01`—`S2-09` 的**离线部分**【已完成】；`S2-10` 门禁记录见附录 F，**结论为未通过** |
| S3 授权记忆 | `S3-01/02/05—12` 与 **`S3-03`/`S3-04`** 的**离线部分**【已完成】（批次 3a—3f，判据映射见附录 G 与附录 H）；`S3-13` 不排期 |
| S4 运行与验收 | **进行中（离线先行，批次 4a + 4b + 4c-离线）**：`S4-01`/`S4-02` 的**离线部分**【已完成】（固定提示接线 + `model_prices` 价目表来源 + 队列/期限/去重不变量；判据映射见附录 I）；`S4-03`/`S4-04` 的**离线部分**【已完成】（清理失败登记升 schema v3 并跨重启保留 + `check_audit.py` 结构审计；判据映射见附录 J）；**`S4-01` 的离线残差【已完成】**（401/402 粘滞门禁：置位后聊天与抽取都不再发起付费调用，解除只有进程重启/重载；判据映射见附录 K）与 **`S1-17`**【已完成·离线部分】（`帮助` 回执，文案见附录 C.6）；`S4-05`—`S4-07` 未开始/阻塞，`S4-08` 阶段门未通过 |

- **`S1-16` / `S2-10` / `S3-13` / `S4-08` 四个阶段门均未记录通过 → 不得宣称任何阶段完成**；
  也不得把“离线测试全绿”当作验收通过（docs/03 §10）。**S4 离线先行**的决定与边界见 docs/03 §9.7：
  S4 不得宣称完成，真实 401/402/429、QQ 离线、真实计费、真实删除与进程重启、日志/端口/宿主快照与端到端验收（O-01—O-21，执行件见附录 L）仍在线。
- **生产默认关闭**：采集需维护者在运行时配置里填入允许群与维护者，并完成一次两步开启；
  记忆需**成员本人两步确认**（`记忆 开启` → `记忆 确认开启`；2026-09-23 起 `set_authorized` 有了唯一的合法生产入口），
  且自动抽取还需先配置预算金额。**不得手工写库充当验收证据**（R24）。
- **下一步合法工作只有两类**：① 具备真实账号/测试群/DeepSeek 凭据后执行在线清单（附录 D.3 的 O-01—O-10 + 附录 F.3.2 的 O-11—O-15 + 附录 I.4 的 O-16—O-17 + 附录 J.3 的 O-18—O-20 + 附录 K.3 的 O-21）——**可勾选的执行件与留证骨架见附录 L**，**执行准备、待决策项（Q1—Q6）与 TODO 见附录 M**；
  ② 在线证据齐备后推阶段门与 S4 剩余卡（`S1-16` → `S2-10` → `S3-13` → `S4-05`—`S4-08`）。
  **S1/S2/S3 的离线面已全部收口，S4 的 4a（S4-01/S4-02）、4b（S4-03/S4-04）与 4c-离线（S4-01 粘滞门禁残差 + S1-17 `帮助` 回执）离线部分已完成；离线侧暂无已授权的下一批任务卡**——
  开工新批次（如 `S4-05` 人设评审，属【待审·在线】；该批次在 docs/03 §4.2 的计划里记作 4c，与已完成的离线批次「4c-离线」区分）需维护者再次授权与批次前决定。
- **明令禁止的“捷径”**：为让抽取跑起来而填入预算金额（docs/03 §9.6）；手工写授权行（R24）；
  把插件加载进混有其他插件的在线实例（架构 §14.4）；为推进度弱化验收判据。

### 文档

| 文档 | 角色 |
|---|---|
| [`docs/01-requirements.md`](docs/01-requirements.md) | 需求编号 R-* 与验收用例 A01—A19；**验收依据，与本文档冲突时以其为准** |
| [`docs/02-architecture.md`](docs/02-architecture.md) | 信任边界、数据生命周期、并发与故障策略、环境基线、§14 实施记录 |
| [`docs/03-implementation-plan.md`](docs/03-implementation-plan.md) | 阶段门禁、任务卡 S0-01—S4-08、依赖图、风险登记 R1—R30、附录 A—M（D 为 S0 证据、E 为 S2 判据映射、F 为 S2-10 门禁记录、G/H 为 S3 判据映射、I—K 为 S4 各批判据映射、**L 为在线执行 runbook**、**M 为在线执行准备与待决策项**）；**进度唯一权威** |

## 2. 常用命令

项目根目录执行。所有命令经 `.runtime/astrbot` 的受控环境运行，**不安装依赖、不联网**。

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export ASTRBOT_BUILD_DASHBOARD=0   # 阻止上游构建钩子运行 npm
```

```sh
# 全量离线核验：8 个核验脚本 + 全部单元测试（当前共 803 项单元测试）
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

# 单个核验脚本（check_env / check_outbound / check_gating / check_collect / check_temp / check_history / check_single_call / check_audit）
env -u VIRTUAL_ENV uv run --project "$PWD/.runtime/astrbot" --no-sync --offline \
    python -B scripts/s0/check_gating.py
```

`run_all.sh` 的顺序：`check_env`(S0-01) → `check_outbound`(S0-06) → `check_gating`(S0-02) →
`check_collect`(S0-03) → `check_temp`(S0-04) → `check_history`(S0-05) → `check_single_call`(S1-10) →
`check_audit`(S4-04) → `unittest discover`。**“全绿” = 8 个脚本各自 exit 0（内部打印 `N/M PASS`）+ 803 项单测全部通过**；任一失败即非全绿。

环境与锁一致性（S0-01 判据，改动环境后必须复核）：

```sh
cmp environment/uv.lock .runtime/astrbot/uv.lock          # 必须逐字节一致
git -C .runtime/astrbot rev-parse HEAD                    # 应为 ab42c0d9b726d82ad0f9563e04c53a4460c00d61
uv lock --check --offline --project "$PWD/.runtime/astrbot" --no-python-downloads
```

**没有配置 lint/format 工具，也没有 CI**：ruff 不在受控环境内（架构 §14.3 明示“未宣称完成格式检查”）。
不要声称跑过 lint 或 CI，也不要为此新增依赖。

## 3. 环境与依赖约束

- **全程 uv，禁止 pip**：`pip` / `pip3` / `python -m pip` 及一切隐式调用被禁；`uv pip` 不是替代方案。
  插件源码中不得出现 `requirements.txt`，也不得导入 pip。
- `.runtime/astrbot/` 是固定提交的上游 AstrBot 源码（已 gitignore）与**唯一虚拟环境**
  `.runtime/astrbot/.venv`。**不得修改上游源码、不得重新解析锁、不得另建/复制 venv**。
  插件通过软链接 `.runtime/astrbot/data/plugins/astrbot_plugin_chizuru → ../../../../astrbot_plugin_chizuru`
  接入，**不存在第二份源码**。
- 插件**零第三方运行依赖**，测试只用标准库 `unittest`。需要新依赖时必须先与维护者确认并走受控锁变更。
- 每次测试/核验都会经 `sys.addaudithook` 建立进程级守卫：**禁止子进程、网络连接/绑定、pip 导入**，
  SQLite 只允许落在 `.runtime/` 下的临时库（K12 / R8）。存储类任务需要放宽时只做**作用域内白名单**，
  **绝不能删除或整体停用守卫**。

## 4. 架构

### 4.1 目标数据流（S1 装配 + S2/S3 离线部分已完成）

```
OneBot 事件 → main.py（唯一框架入口、薄适配，提取 MessageFacts）
  → policy.classify（触发形态）
  → commands.parse（指令 / 聊天分流）
  → control.authorize（确定性权限判定；只来自可信身份 + 显式配置）
  → dedup.begin（事件与动作去重）→ _prepare_chat（修订快照 → 材料 + 历史 + 记忆块，超预算即拒绝）
  → budget.reserve（先预留）→ scheduler.submit（准入）
  → llm.py（直接调用原生提供商）→ send_gate.evaluate（含发送前修订重读）
  → 唯一出站路径 _deliver() → 仅送达时写回互动历史（history.py / 框架会话存储）
  → 回复之后 inline 运行记忆抽取（准入 → 预留 → 调度 → 解析 → 单事务写回；零出站、失败不影响聊天）
```

非 @ 的普通群聊只走采集支路：`policy.classify → IGNORE/… → main._maybe_collect`
（受信范围 ∧ 形状 TEXT_ONLY ∧ 策略仓储 `is_collection_open` ∧ 成员未退出）→ `context_buffer.ingest`；
**不产生任何出站或模型调用**。生产环境因策略仓储无行而默认关闭。

记忆注入（S3-08）：`memory.retrieve` 只接受可信元数据构造的成员键，渲染成第二个临时 part；
**用了记忆的那一轮不落共享历史**（`history.should_record(memory_assisted=…)`）。

### 4.2 模块边界（每个模块“必须不做”与职责同等重要）

| 模块 | 职责 | 必须不做 |
|---|---|---|
| `main.py` | Star 生命周期、钩子注册（**只注册 `on_message`**）、唯一装配点与出站路径 `_deliver()`；普通群聊采集分支与 `上下文 退出/加入`；告知两步开启与维护者命令；**记忆授权两步、记忆命令面**；**`帮助` 回执与 401/402 粘滞门禁**（置位后聊天与抽取都不再发起付费调用，解除只有重启/重载）；聊天装配、修订号复核与历史写回 | 不写业务规则、不解析身份、不直接 `event.send`（出站必经 `send_gate`）；采集不落盘、不发消息、不调模型；历史写失败不置持久降级、不影响已送达回复；群开关读策略失败按未暂停放行（R21） |
| `config.py` | fail-closed 配置模型：任一字段缺失或畸形 → 整体退回哨兵 `Settings()`（拒绝全部） | 不默认开启采集/记忆；密钥不进 `repr` |
| `policy.py` | 纯触发形态分类：`IGNORE` / `EMPTY_OR_UNSUPPORTED` / `UNSUPPORTED_ATTACHMENT` / `TEXT_CANDIDATE`；`is_trusted_scope` 是权限与触发**共用的唯一谓词** | 不判权限、不读 DB、不判采集资格、不做指令分流；`UNSUPPORTED_ATTACHMENT` 的固定提示**装配层尚未接线** |
| `context_assembly.py` | 人格分层与静态规则 + 动态材料与记忆块装配：`STATIC_RULES`/`PERSONA_VERSION`/`build_chat_plan()`；`render_materials()`、`trim_to_budget()`（8192 预算；先丢群聊材料、再丢记忆行、最后丢历史；无法裁剪即返回 `None` 拒绝）；固定规则只进 system | 不组装上下文材料以外的东西、不调模型、不接受事件对象（因而读不到 `message_str`）、不导入框架（临时标记由 `main.py` 施加） |
| `history.py` | `@` 互动历史：`_at` 私有键、成对解析（不合格条目保守丢弃）、20 轮/24 小时取严、`flatten()` 剥离私有键、`storage_entries()` 写回形状、`should_record()`（送达 ∧ 非记忆辅助轮）、`trim_stored()`（启动裁剪只动 `_at` 成对条目） | 不导入框架、不做 IO、不判权限、不复制全历史（存储归框架会话存储）；裁剪不误删框架条目 |
| `notice.py` | 群告知文案与两步确认窗口：`NOTICE_TEXT`/`NOTICE_VERSION`（附录 C.1 逐字 + 指纹）、每群单槽 5 分钟窗口（换人即作废、有界、惰性淘汰）、`describe_policy()` 状态行 | 不导入框架/sqlite/asyncio、不做 IO、不调模型、不发消息、不判权限、不读配置（版本由调用方传入） |
| `fixed_notice.py` | 固定提示文案（S4-01）：`FIXED_NOTICE_VERSION`（附录 C.5 逐字 + 规范串指纹）、六条常量、`text_for(code)` 映射 | 不导入框架/sqlite/asyncio/`llm`、不做 IO、不调模型、不发消息、不判权限、不读配置、不拼装用户数据；未知错误码没有文案 |
| `help_notice.py` | `帮助` 回执文案（S1-17）：`HELP_NOTICE_VERSION`（附录 C.6 逐字 + SHA-256 指纹）、`HELP_NOTICE_TEXT`；测试同时钉住"与 `commands.COMMAND_TEXTS` 逐字对齐" | 不导入框架/sqlite/asyncio/`llm`/`logging`、不做 IO、不调模型、不发消息、不判权限、不读配置、不拼装用户数据 |
| `keys.py` | `BotInstanceKey` / `GroupKey` / `MemberKey`、`RevisionSnapshot`；只由可信元数据构造，缺一段即构造失败 | 不用昵称/群名/正文构造键 |
| `commands.py` | 纯解析 `CommandIntent` + 所需权限档；空白归一化 | 不核权、不执行、不调模型；**调用前提是调用方已确认真实 @**；未知文本返回 `None` |
| `control.py` | 确定性授权：可信身份 + 显式配置映射 | 不读事件对象、不导入框架、不调模型；QQ 群管理员 ≠ 系统管理员 ≠ 维护者 |
| `scheduler.py` | 有界队列、全局/同群并发、业务期限；聊天优先于抽取 | 不发送、不调模型、不读存储、不判权限、不重试；**不用 `asyncio.Semaphore`**（FIFO 无法表达聊天优先）；不持有后台任务 |
| `dedup.py` | 事件与动作去重：`IN_FLIGHT` / `DONE` / `UNCERTAIN`；有界（容量 + TTL） | 不保存任何正文；**窗口无默认值，必须由调用方注入**；`release()` 仅限确认无副作用时 |
| `budget.py` | 预留 / 按 usage 结算 / 门槛核算 | 不调模型、不硬编码价目；缺失 usage 记估算**而非零**；价格未知且已配金额时保守拒绝 |
| `health.py` | 平台状态归一化（只认 `Platform.status` 四个取值）、持久降级标志、抽取失败计数、维护者查询快照与文本 | 不导入框架、不探测、不发送；**不表示 QQ 登录态**（固定输出“未知”）；不读 `instance.config`（含 token）与任何错误文本/堆栈 |
| `send_gate.py` | 出站前复核：源范围 → 源触发形态 → 目标群一致 → 群开关 → 修订号 → 发送不确定；不一致即丢弃 | 不发送、不调模型、不做补救；**群开关与修订号由调用方注入**（无默认值）；不提供任何回执/送达 API；固定提示构造时即禁止模型调用 |
| `redact.py` | 类别化日志记录（`AuditRecord` 无自由文本字段）、错误码闭集、每进程加盐的关联标识、保留期与大小策略、AstrBot/NapCat 日志审计清单 | 不做日志 IO、不轮转文件（`logging` 都不导入）；不记正文/请求体/密钥/推理文本；关联摘要不可还原、跨重启不可关联（刻意取舍） |
| `context_buffer.py` | 普通群聊内存环形缓冲：30 条/10 分钟取严、惰性淘汰、无后台任务；形状/命令/敏感/自身/重复全部拒绝；`clear_group`/`clear_member`；条目附昵称（**仅展示**） | 不落盘、不请求模型、不发消息、不导入框架；**条数与 TTL 由调用方注入**；不采集未告知群；昵称不进键、不参与判定 |
| `storage/*` | 插件 SQLite：`db`（延迟建库、短事务、失败不恢复）、`schema`（**六张** STRICT 表 + `user_version`，版本 3）、`groups`（群策略仓储，无行即关闭）、`members`（退出/加入 + 成员修订号）、`memories`（记忆授权、低敏事实、来源去重）、`maintenance`（清理失败的待维护登记，S4-03） | 不在事务内等网络；不存全量群聊；**两张 S2 表不存记忆授权**（授权只在 `memory_state`）；**上限与保留期不写默认值、由调用方注入**；路径由调用方注入（无默认值）；登记表只有计数与时间 |
| `memory/*` | 授权记忆的候选层、管线与检索：`types.py`（四类白名单 `address`/`reply_length`/`interest`/`activity`、`prepare_source` 三道判定、`build_candidate` 校验）、`extract.py`（**待审**提示词 + 指纹、`parse_candidates` 只读两个键且不合格整批放弃）、`pipeline.py`（`admit` 五道准入与 `write_back` 单事务修订重核 + 来源去重）、`retrieve.py`（**只接受可信成员键**、注入块渲染、`category_label`）、`consent.py`（附录 C.2/C.3 定稿文案与版本常量、成员级两步确认窗口、版本谓词与群内回执/列表渲染） | 不导入框架/asyncio、不发送、不做预算预留与调度（属装配层）；**不含人设与群历史**；不递归触发聊天；**不读取模型给出的身份/授权字段**；所有准入拒绝都静默；`retrieve` 没有可传入他人 ID 的入口；`consent` 不落盘、不读配置、窗口键只能是 `MemberKey` |
| `llm.py` | 请求成形（`LLMRequestPlan`，键集固定、无工具参数）、回复解释（只读 `role`/`completion_text`）、错误分类、≤1 次重试判定（共用同一 `Deadline`）、usage 映射、`FollowUp` 类别 | 不发送、不导入框架、不建网关/客户端；不组装人格与上下文；不读写历史与存储；**不读取推理字段、不追加工具**；不含任何群内文案；**在线部分未验证** |

**已实现但要注意**：记忆授权与命令面（`S3-03`/`S3-04`，2026-09-23 批次 3f）已接线，文案见附录 C.2/C.3 与 `memory/consent.py`；
`帮助` 回执（`S1-17`，2026-09-28 批次 4c-离线）与 401/402 粘滞门禁（`S4-01` 离线残差，同批）已接线，文案见附录 C.6 与 `help_notice.py`——
**命令面已无"只记审计不执行"的指令**。
docs/03 §3.2 的目录树是**拟议结构 + 【已实现】标注**的混合；判断某文件是否存在请直接看仓库。

### 4.3 框架装配事实（已核验）

- **唯一 handler**：`main.py:459-461`，`@filter.platform_adapter_type(AIOCQHTTP)` + `@filter.event_message_type(ALL, priority=1000)`；
  收口三件套 `stop_event() + clear_result() + should_call_llm(True)` 在 `main.py:466-468`（K1/K5）。
- **唯一出站**：`_deliver()`（`main.py:2255`），出站前必过 `send_gate.evaluate`，`event.send` 抛异常即 `SEND_UNCERTAIN` 且**不重发**（唯一 `event.send` 调用在 `main.py:2299`）。
  三个包装出口：`_deliver_notice`（`main.py:1602`，告知全文）、`_deliver_memory`（`main.py:2039`，记忆类回执）、
  `_deliver_fixed`（`main.py:2064`，**固定提示与 `帮助` 回执**，S4-01/S1-17，固定 `FIXED_NOTICE + llm_invoked=False + revision=None`）。
- **群内用户可见输出穷举**：① `千鹤 状态`（既有报告 + 一行群上下文状态 + 金额已配置时的价目表行）；② `群上下文 开启` 与确认失败时的重发全文（附录 C.1 逐字）；
  ③ `记忆 开启` 与确认失败时的重发全文（附录 C.2 逐字）、`记忆 查看` 的提示（附录 C.3 逐字）与确认后的列表、其余记忆命令的回执；
  ④ **`帮助` 的逐字使用说明**（附录 C.6，S1-17）；⑤ `@` 后的模型聊天回复；⑥ **固定提示**（附录 C.5 逐字）：附件能力、超预算、失败、空回复、忙碌、暂不可用。
  **静默无回执**：`上下文 退出/加入`、`群上下文 关闭`、`上下文 清空`、`千鹤 暂停/恢复`（执行状态变更）；
  未分类的故障与修订变化导致的在途丢弃也静默。**没有"只记审计不执行"的命令**（`帮助` 于 2026-09-28 落地）。
- **401/402 粘滞门禁**（S4-01 离线残差，2026-09-28）：`main._payment_block` 在聊天装配前、抽取准入前拦下付费调用；
  首次 `AUTH_FAILED`/`BALANCE_INSUFFICIENT` 置 `PROVIDER_DISABLED`/`BUDGET_EXHAUSTED`（首次写一条 `EventCategory.DEGRADATION` 审计），
  `千鹤 状态` 的降级行给出原因；**解除只有进程重启/插件重载**，`_acquire_provider` 的"取到即清除"已删除（行为分化见 docs/03 §5.2 D34）。
- **清理失败跨重启可见**（S4-03，R23）：群级删除失败的次数与时间落在 `storage.maintenance`（`cleanup_failure` 表，schema v3），
  启动读回计数与 `DELETION_FAILED`（记忆相关能力保持停用），`千鹤 状态` 的「清理失败：N 次」行因此不随重启消失；
  一次**成功**的重跑（`上下文 清空`/`群上下文 关闭`/`上下文 退出`）按该群登记数扣减，最后一条清除时才解除降级。
- **注入机制**：动态材料与记忆块经 `extra_user_content_parts`（逐个 `mark_as_temp()`）、历史经 `contexts`，
  两者都不进 system 且受同一 8192 预算裁剪；`_no_save` 另行处理整轮排除（K6）。
- **配置生效时机**：`limits`、`budget`（含**价目表 `model_prices`**，S4-01）与两个记忆确认窗口（取 `memory.auth_confirm_ttl_seconds`）在 `initialize()` 时固定
  （运行时改动需重载插件）；身份、允许群、维护者映射与金额开关**每个事件重读**。
- **测试注入点**（`main.py:300-310`）：`clock` / `datetime_clock` / `redactor_salt` / `storage_path` /
  `history_cleaner` / `history_store` / `price_table`——`price_table` 注入时优先于配置；生产由 `model_prices` 构造，未配置即空价目表（R20）。

### 4.4 不可违背的硬约束

- **模型不参与授权**：权限、成员归属、保存范围只由可信事件元数据与确定性代码决定。
  昵称、群名片、消息正文、LLM 输出一律不作为授权或数据库指令依据。
- **fail-closed 是默认**：配置空/畸形即拒绝全部；空允许群列表 = 全部关闭；未配置抽取预算金额时
  自动记忆抽取保持关闭（金额 0 表示“未配置”，**不是“不限额度”**）。
- **凭据**：QQ 登录态、OneBot Token、API Key 只走运行时受限配置，**不进入仓库、文档、模型上下文、
  普通日志**。维护者提供时只说明配置位置，不要求写入受版本管理的文件。
- **严格 @ 是框架级问题，不是插件局部问题**：`WakingCheckStage` 早于插件执行且自身可发消息（K1/K2/R6），
  框架**不存在全局出站门控**，`on_decorating_result` / `after_message_sent` 不是拦截点。
  严格 @ 只能靠“只启用本插件 + 关闭附录 D.2 的全部配置键 + 插件自身收口”共同保证。
  **在完成在线验证前，本插件不得加载到混有其他插件的在线实例。**
- 必关配置只能设在**全局** `<ASTRBOT_ROOT>/data/cmd_config.json`，插件的 `_conf_schema.json`
  **无法**设置这些键（清单见 docs/03 附录 D.2 与 `scripts/s0/_harness.py:342-358`）。
  注意 `wake_prefix` 有顶层 list 与 `provider_settings.wake_prefix` string 两个不同键。

## 5. 易错点（已核验的上游行为，逐条有源码位置，见 docs/03 §3.4）

- K1：任一 handler 的 filter 通过即使事件 `is_wake = True`——宽 filter 有副作用，须显式收口。
  收口默认 LLM 链路要用 **`should_call_llm(True)`**：`call_llm` 初值为 `False`，第二路径的门槛是
  `not event.call_llm`（K5 已于 2026-09-18 修正）；需要交给框架送出的回复不能 `stop_event()`，
  否则回复也不会发出。**本插件的模型调用不走 `event.request_llm`**：按附录 D.2 直调原生提供商（K14）。
- K3：私聊默认唤醒；K4：`ProcessStage` 在 handler 之后还有**第二条默认 LLM 路径**（同一事件可能双次计费）。
- K6：`mark_as_temp()` 只作用于 part，整轮排除还须在 `on_agent_done` 对 user 与 assistant
  两条 `Message` 设 `_no_save`。
- K8：EventBus 为每个事件建独立 task，“同群并发 1”必须插件自行加锁。
- R15：保留内置插件不受 `disable_builtin_commands` 约束，其 `handle_empty_mention` 优先级高于本插件
  且会 `stop_event()`；唯一关闭手段是 `platform_settings.empty_mention_waiting`。
- R16：框架对会话与平台消息历史**无原生 TTL**，20 轮/24 小时与 90 天全部需插件自建清理。
- R21：存储路径解析失败（`StarTools.get_data_dir()` 探测不到插件名、库损坏等）会**静默降级**为
  “无存储”：采集与 `上下文 退出/加入` 全部关闭，插件照常聊天，只有 `千鹤 状态` 的降级行可见。
- R11：原生流程在发送门控前就已写入历史（`STAGES_ORDER` 中 ProcessStage 早于 RespondStage）。
- K15：`PlatformMessageHistoryManager.delete` 的 **docstring 与实现相反**——删的是“最近 offset 秒内”
  （`created_at >= now - offset`），默认 `86400` 只清最近 24 小时；整体删除的唯一惯用法是
  `offset_sec=99999999`。另：QQ 群消息经 `event.send` **不写**这张表（只有内置群历史开关或
  `Context.send_message` 才写），所以清理它是幂等防御动作。
- K15 相关：`ConversationManager.get_conversations(platform_id=...)` **只在 platform_id 为真时过滤**，
  空串会枚举全实例会话——插件侧必须先挡掉空 `platform_id`。

## 6. 版本化文本与待审文案（不要自行发明）

### 6.1 钉住表（改字必须同时改版本字面量与测试内指纹）

| 常量 | 值 | 位置 | 指纹（测试内字面量） |
|---|---|---|---|
| `PERSONA_VERSION` / `STATIC_RULES` | `persona-1` / 四块拼装（需求 §3.1 §3.2 §5.3 + 附录 C.4） | `context_assembly.py:58,62-89` | SHA-256 `9e008e996ace7032bdab8876f76349269338ff2cc6104bb6c8c5ef68eeaf455c`（`tests/test_context_assembly.py`） |
| `NOTICE_VERSION` / `NOTICE_TEXT` | `notice-1` / 附录 C.1 逐字 | `notice.py:26,30-53` | SHA-256 `3e69dbd0d3d1d7a707aad55bd28be403a524be41bcc7dd819568dc27d3192ab1`（`tests/test_notice.py`） |
| `EXTRACT_VERSION` / `EXTRACTION_RULES` | `extract-1` / **待审初稿** | `memory/extract.py:36,40-68` | SHA-256 `ff26b8311d6c4d58a1ac66ed7edfa996aac77167335d82ebebd27cb10b868760`（`tests/test_memory_extract.py`） |
| `MEMORY_BLOCK_VERSION` / `MEMORY_BLOCK_TITLE` / `CATEGORY_LABELS` | `memory-block-1` / `【本人记忆·临时材料】` / 需求 §4.3 逐字 | `memory/retrieve.py:27-43` | 字面量钉住（`tests/test_memory_retrieve.py`） |
| `CONSENT_VERSION` / `CONSENT_TEXT` | `consent-1` / 附录 C.2 逐字（2026-09-23 定稿） | `memory/consent.py` | SHA-256 `c8262dee04ae00bde436cd7533b941e1a3446b3be33c6cb017dded2a8ce706ff`（`tests/test_memory_consent.py`） |
| `VIEW_NOTICE_VERSION` / `VIEW_NOTICE_TEXT` | `memory-view-1` / 附录 C.3 逐字（同上定稿） | `memory/consent.py` | SHA-256 `93bb8a55d1a45c0d26ce28b0ee8ddcb8c1347a62f3d60e7ed6756609dad2d189`（同上） |
| `FIXED_NOTICE_VERSION` / 六条固定提示 | `fixed-notice-1` / 附录 C.5 逐字（2026-09-23 定稿） | `fixed_notice.py` | 规范串（按常量名升序 `名称=文本` 以换行连接）SHA-256 `ca30978801c72731ef479b615f1ab2a85788a4337a08e3c63aa87756034af8e4`（`tests/test_fixed_notice.py`） |
| `HELP_NOTICE_VERSION` / `HELP_NOTICE_TEXT` | `help-notice-1` / 附录 C.6 逐字（2026-09-28 定稿） | `help_notice.py` | 规范串（`名称=文本`）SHA-256 `8d1b263efd38cc191a51ddc69cd075570acef8895b2833d7cbf0cf53c72aceb6`（`tests/test_help_notice.py`） |
| `SCHEMA_VERSION` | `3`（六张 STRICT 表） | `storage/schema.py:25` | 升版用例（`tests/test_storage_db.py`，含 v2 → v3） |

改动流程：① 递增版本字面量（如 `persona-2`）；② 同步测试内 SHA-256/字面量；③ 在 docs/03 记录理由——
三者缺一不可。附录 C.1/C.2/C.3/C.5/C.6 均已定稿：`NOTICE_TEXT` 不得再改字（改了必须重新告知）；
`CONSENT_TEXT` 递增会让已授权成员回到未授权、必须重新确认（**R27**）。

### 6.2 待审清单（写出前先问维护者；不要在代码或回复里替它定稿）

- **群内回执/提示文案**：附录 C.1/C.2/C.3/C.5/C.6 已定稿并逐字入库（C.5 固定提示随批次 4a、C.6 `帮助` 回执随批次 4c-离线定稿）；
  **不再有未定稿的群内回执文案**；`上下文 退出/加入`、`群上下文 关闭`、`上下文 清空`、`千鹤 暂停/恢复` 保持静默执行
  （执行状态变更、无回执文案——这是既定设计，不是待办）。
- **模型可见文本**：`EXTRACTION_RULES`（待审初稿）、`MEMORY_BLOCK_TITLE`（待评审）、
  `MATERIAL_TITLE`；类别名已随 3f 按需求 §4.3 原文定稿（群内列表与注入块共用同一份）。
- **维护者可见新文案**：`notice.describe_policy`（`notice.py:170`）、`main._describe_group`（`main.py:1130` 附近）、
  `health.py` 的清理失败行与**价目表片段**（`health.py` 的 `format_report`，随批次 4a 组装但**标记待评审**）。
  （`check_audit.py` 的 `AuditRecord` 字段闭集与 `AUDIT_CHECKLIST` 输出属**核验脚本**，不面向群成员。）
- **建议参数（值已写死但标注“待评审”，改动需在 docs/03 记录理由）**：去重窗口 600s / 容量 2048
  （绑仍未在线核验的 O-07）、昵称 24 字、行开销 8 token、相对时间 12h、缓冲 300 字 / 8 关键词、
  日志 20 MiB、busy timeout 5s、`EXTRACTION_STOP_RATIO=0.9`、中文 2 token 估算。
  记忆确认窗口不再写死：取配置 `auth_confirm_ttl_seconds`（默认 300，范围 30—3600），两个窗口实例共用。
- `metadata.yaml` 的 `display_name` 与 `desc`；`_conf_schema.json` 的 29 个键中
  `notice_version` 必须与 `notice.NOTICE_VERSION` 一致，否则告知流程静默失败（R22）；
  `model_prices` 的模型 ID 必须与提供商返回值**逐字一致**，形状不合格会让整份配置退回哨兵（R28）。

## 7. 开发工作流

按**任务卡批次**推进（docs/03 §5 的分组：S1 的 1a—1e、S3 的 3a—3e 等）：

1. 从 `docs/03-implementation-plan.md` 取下一批 2—3 张任务卡，读其“任务/依赖/交付物/R/A/完成判据”。
2. 先确认依赖已 `【已完成】`；带 `【阻塞】`/`【待审】`/`【在线】` 标记的任务未满足前置不得开工。
3. 实现模块 + 对应 `tests/test_*.py`。**既有测试必须继续通过，不得放宽或删除既有断言**；
   真正的行为分化需在 docs/03 记录理由。
4. 跑 `bash scripts/s0/run_all.sh` 全绿。
5. 把状态**就地回写** docs/03 的任务卡表（未开始/进行中/已完成/阻塞），不新建状态文档；
   风险登记表（§8）只更新“当前状态”列，**不得删除已登记行**；编号一旦分配不复用；
   范围变化时递增文档次版本并在文首注明。
6. 提交。提交信息用 Conventional Commits（`feat:` / `docs:` / `test:`），主题英文小写，
   任务卡号写在括号里，例如 `feat: inject isolated memories and drop mid-flight results (S3-08 + S3-09)`。

**不得做的事**：为推进度弱化验收判据；把“离线测试通过”当作“验收通过”；宣称某阶段完成；
把未核验的假设写成默认参数（例：`dedup.py` 的去重窗口必须由调用方注入，来源是仍待在线验证的 O-07）；
以任何方式绕过阻塞（手工写库、填入预算金额、加载进混合插件实例）。**只提交，不推送**——推送需维护者明确指示。

**在线前置仍阻塞**：目标群与维护者清单、真实 QQ 账号与测试群、运行时 DeepSeek 凭据（docs/03 §9.2）。
未满足时对应能力保持关闭，**不得由实施者擅自取默认值放行**。

## 8. 测试组织

- 测试用标准库 `unittest`，无 pytest/conftest。**必须在仓库根目录运行**：测试以顶层包名
  `astrbot_plugin_chizuru.*` 导入模块，靠 `python -m` 把 CWD 放进 `sys.path`。
- 规模：**30 个测试模块、803 项用例**；`tests/fakes.py` 不是测试模块（不匹配 `test*.py`），
  也不要 import `scripts/s0/`。
- `tests/test_plugin.py` 使用**真实** AstrBot 框架对象（导入 AstrBot、解析 `metadata.yaml`、
  注入 `_conf_schema.json`、经 `call_handler` 复刻 `star_request.py` 的处理器调用循环）；
  `tests/test_assembly.py` 同样用真实框架对象 + `tests/fakes.py`（假时钟/提供商/平台、
  事件工厂、send 记录器、K12 作用域守卫）。
- **装配级测试必须替换 `event.send`**：真实实现会 `asyncio.create_task(Metric.upload(...))`
  并联网，离线守卫会拒绝且破坏“terminate 后无新增任务”的不变量。
- 存储类测试（`tests/test_storage_*.py`）用 `tempfile` 在 `.runtime/` 下建临时库（K12 只放行
  该前缀）；装配级用例经 `plugin.storage_path` 注入库路径，并把 `plugin.history_cleaner`
  换成 `fakes.FakeHistoryCleaner`（真实实现会触框架会话库与数据库）。
- S0 核验脚本在独立进程运行，共享引导在 `scripts/s0/_harness.py`（`Harness` + `Checker`）；
  **每个进程只能建立一个 `Harness`**（audit hook 不可卸载）。新脚本照抄该模板即可获得
  隔离 `ASTRBOT_ROOT`、11 类事件门控矩阵所需的真假事件与出站/模型调用记录器。
- `scripts/s0/` 不属于产品插件包，不参与插件运行时；不要把它 import 进插件源码。
- 纯逻辑模块有 AST 纯度断言（禁止 import `astrbot`）：只有 `main.py` 可以 import 框架
  （另有 `tests/`、`scripts/s0/`）。

## 9. 评审发现（2026-09-23 全项目复核）

### 9.1 本批已修正

1. `README.md`：S3/S4 行→按现状拆分；测试数写回现状（原值是 S2-10 时点的旧快照）；目录树补 `memory/`、`storage/` 描述补低敏记忆；
   长期记忆条目注明“授权入口未落地、当前无开启路径”（3f 落地后已再次改写）。
2. `astrbot_plugin_chizuru/metadata.yaml`：`desc` 与 `display_name` 更新为现状（原“不回复、不采集、不调用模型”已过时）。
3. `docs/03-implementation-plan.md` 文首：版本 0.19 → 0.24（与版本记录末行对齐；当时无范围变化，未新增版本行）。
4. `docs/03-implementation-plan.md` §3 领句：删去“本轮不创建任何文件”的过时表述，与 §3.2 的【已实现】标注一致。
5. 本文件取代 `CODEBUDDY.md`（其文档表漏登记附录 G，已在本文件 §1 修正）。

### 9.2 已登记、未修改（读代码/注释时别被误导）

| 位置 | 与现状不符之处 |
|---|---|
| `scripts/s0/_harness.py:14` | 示例写 `Harness(allow_sqlite=False)`，该参数不存在；真实签名是 `Harness(*, with_plugin=True)`（`:106`） |
| `storage/groups.py:6-8` | 称 S2-02/S2-05“因清单未提供而阻塞、命令接线等它们”——`main.py` 早已接线 |
| `context_buffer.py:14-15` | 称“暂停/关闭随 S2-05 命令落地”——已落地 |
| `send_gate.py:22-23` | 称“群开关与修订号在 S1 没有存储层”——S2 起来自持久化策略仓储 |
| `policy.py:28` | ~~称附件档“可回复固定的文本能力提示”——装配层从不发~~ **2026-09-23（批次 4a）已接线**：`main._handle_attachment` 经 `_deliver_fixed` 回一次能力提示；此条不再是过时注释 |
| `main.py` 的 `_deliver` 内 `GateFacts.previous=None` | `DropReason.SEND_UNCERTAIN` 分支在生产装配中不可达——去重层已在入口拦截重放；**批次 4a 复核后确认不改**（开启它需要跨事件保存发送结论，属新的状态设计） |
| `llm.py:377-401` | ~~`FollowUp` / `follow_up()` 无任何生产调用~~ **2026-09-23（批次 4a）已接线**：`main._handle_chat` 用 `follow_up` 判定该不该给固定提示；`background=True 恒 NOTHING` 仍是单元级护栏 |
| `redact.py:75` | ~~`EventCategory.DEGRADATION` 全仓库零引用~~ **2026-09-28（批次 4c-离线）已转正**：`main._mark_payment_degraded` 在 401/402 首次置位时各写一条；`health.py` 的 `_DEGRADATION_TEXT` 仍是另一物 |
| `budget.py`、`redact.py`、`notice.py` | `settle(cost=…)`、`NoticeGate.stats` 等公开 API 未接线，供测试与包内复用；`AUDIT_CHECKLIST` 自批次 4b 起由 `scripts/s0/check_audit.py` 输出（框架侧人工审计清单），`RetentionPolicy` 仍只被测试消费 |

以下不一致**结论为误判，不要据此改代码**：曾怀疑 `main.py:37` 的裁剪顺序注释与 `context_assembly.trim_to_budget`
相反——复核后二者一致（`context_assembly.py:320-359` 先分配记忆块，语义正是“先丢材料、再丢记忆行、最后丢历史”）。

### 9.3 批次 3f 落地后的回写（2026-09-23）

1. 记忆授权与命令面（`S3-03`/`S3-04`）接线，附录 C.2/C.3 定稿入库（`memory/consent.py`）；
   `main.py` 行号随之整体下移，本文件已同步全量行号。
2. `docs/03` 升至 0.25：任务卡状态、§5.2 的 D13—D18、附录 C 状态、**附录 H**、R24 状态与 **R27**；
   `docs/02` 升至 0.12 并新增 §14.7（同时修正 §14.6 标题只写“批次 3a”的滞后）。
3. `commands.py` 的 `记忆 关闭`/`记忆 删除全部` 共用 `MEMORY_DISABLE` **不再是“未落地”**：
   3f 按需求 §4.4 与附录 C.2 收敛为等价语义（先撤权、再清空，见 D17）。
4. `scripts/s0/run_all.sh` 的汇总结论同步为“S3 离线面已全部收口”。

### 9.4 批次 4a 落地后的回写（2026-09-23）

1. 固定提示（`S4-01`）与价目表来源（R20）接线，附录 **C.5** 定稿入库（`fixed_notice.py`）；
   `main.py` 行号再次整体下移，本文件已同步（唯一 handler `:421-423`、收口 `:428-430`、
   `_deliver` `:2036`、`event.send` `:2080`、测试注入点 `:263-274`）。
2. `docs/03` 升至 0.26：任务卡状态（S4-01/S4-02 离线部分）、§5.2 的 D19—D26、§9.7（S4 离线先行决定）、
   附录 C.5、**附录 I**、R20 状态与 **R28**；`docs/02` 升至 0.13 并新增 §14.8。
3. `policy.py` 的附件能力提示**不再是“未接线能力”**（§9.2 已回写）；`llm.follow_up` 进入生产调用。
4. `scripts/s0/check_gating.py` 补了一句覆盖范围说明（该矩阵**不调用插件的 `initialize()`**，
   插件侧出站由 `tests/test_plugin.py` / `tests/test_assembly.py` 覆盖），**判据一字未改**；
   `scripts/s0/run_all.sh` 的汇总结论新增 S4 一行。

### 9.5 批次 4b 落地后的回写（2026-09-23）

1. `storage/schema.py` 升到 **SCHEMA_VERSION=3**（新增 `cleanup_failure`）并新增 `storage/maintenance.py`；
   装配层接线"失败写登记 / 启动恢复 / 成功重跑扣减"（S4-03，闭合 R23）；`main.py` 行号再次下移，本文件已同步
   （唯一 handler `:427-429`、收口 `:434-436`、`_deliver` `:2123`、`event.send` `:2167`、测试注入点 `:268-279`）。
2. 新增 `scripts/s0/check_audit.py`（S4-04 的 8 项离线结构审计）并接入 `run_all.sh`：**核验脚本 7 → 8**，
   本文件与 README 的"全绿"定义、脚本清单、测试规模（**29 模块 / 789 项**）同步。
3. `docs/03` 升至 0.27：任务卡状态（S4-03/S4-04 离线部分）、§5.2 的 D27—D32、§9.8（4b 授权决定）、
   §8 的 R23 状态与新增 **R29**、**附录 J**（含在线编号 **O-18—O-20**）；`docs/02` 升至 0.14 并新增 §14.9。
4. `AUDIT_CHECKLIST` 自本批起由 `check_audit.py` 输出（§9.2 已回写）；`RetentionPolicy`、`NoticeGate.stats` 等其余未接线 API 状态不变。

### 9.6 批次 4c-离线落地后的回写（2026-09-28）

1. `main.py` 新增 **401/402 粘滞门禁**（`_PAYMENT_ENTRIES` / `_payment_reason` / `_payment_block` / `_mark_payment_degraded`）与
   **`_handle_help`**；新增 `help_notice.py`（`help-notice-1` + 逐字全文，附录 C.6）；`main.py` 行号再次下移，本文件已同步
   （唯一 handler `:459-461`、收口 `:466-468`、`_deliver` `:2255`、`event.send` `:2299`、测试注入点 `:300-310`）。
2. `docs/03` 升至 0.28：任务卡（**新增 S1-17**、S4-01 补记 4c-离线）、§5.2 的 **D33—D39**、§9.9（本批授权决定）、
   §8 新增 **R30**、**附录 C.6** 与**附录 K**（含在线编号 **O-21**）；`docs/02` 升至 0.15 并新增 §14.10（§8.3 的 401/402 两行同步粘滞语义）。
3. 测试规模 **30 模块 / 803 项**（新增 `tests/test_help_notice.py` 6 项、`test_assembly.py` 的 `StickyPaymentGateTests` 4 与 `HelpCommandTests` 4，改写 2 项）；
   `run_all.sh` 仍是 8 脚本全绿（10/15/12/7/8/11/24/8）。
4. `EventCategory.DEGRADATION` 转正（§9.2 已回写）；`README.md` 的 S4 行、目录树、测试数、"`帮助` 仍不执行"与
   "清理失败计数不跨重启"（4b 遗留笔误）一并回改。
5. **行为分化 2 处**：`_acquire_provider` 的"取到即清除"删除（提供商"后配置好"需重载）、`帮助` 由静默改为逐字回执；
   既有用例的改写与理由见 docs/03 §5.2 **D38**，**未放宽任何既有断言**。
