# CODEBUDDY.md

This file provides guidance to CodeBuddy Code when working with code in this repository.

本仓库的文档、注释与开发约定均为中文；本文件沿用中文书写。

## 项目概览

面向 QQ 群的**文本**聊天机器人：AstrBot `==4.28.1` Star 插件 + NapCat/OneBot v11 + DeepSeek，
以《租借女友》水原千鹤为人设参考。**非官方、仅被 @ 时回复、不主动接话**。

**当前状态：未上线。** S0 离线核验完成，但 **S0 阶段门于 2026-09-17 被维护者决定有意跳过（不是通过）**，
在线部分（真实 QQ/NapCat/DeepSeek）从未验证。S1 进行中：`S1-01/02/03/05/06/07/08/09` 已完成，
`S1-04`、`S1-10`—`S1-16` 未开始；S2/S3/S4 未开始。

| 文档 | 角色 |
|---|---|
| [`docs/01-requirements.md`](docs/01-requirements.md) | 需求编号 R-* 与验收用例 A01—A19；**验收依据，与本文档冲突时以其为准** |
| [`docs/02-architecture.md`](docs/02-architecture.md) | 信任边界、数据生命周期、并发与故障策略、环境基线、§14 实施记录 |
| [`docs/03-implementation-plan.md`](docs/03-implementation-plan.md) | 阶段门禁、任务卡、依赖图、风险登记 R1—R16、附录 D（S0 证据）；**进度唯一权威** |

## 常用命令

项目根目录执行。所有命令经 `.runtime/astrbot` 的受控环境运行，**不安装依赖、不联网**。

```sh
export UV_CACHE_DIR="$PWD/.cache/uv"
export ASTRBOT_BUILD_DASHBOARD=0   # 阻止上游构建钩子运行 npm
```

```sh
# 全量离线核验：6 个 S0 核验脚本 + 全部单元测试（当前共 145 项单元测试）
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

# 单个 S0 核验脚本（check_env / check_outbound / check_gating / check_collect / check_temp / check_history）
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

### 目标数据流（S1 装配尚未完成，S1-14 落地）

```
OneBot 事件 → main.py（唯一框架入口、薄适配，提取 MessageFacts）
  → policy.classify（触发形态）
  → commands.parse（指令 / 聊天分流）
  → control.authorize（确定性权限判定；只来自可信身份 + 显式配置）
  → dedup.begin（事件与动作去重）→ budget.reserve（先预留）→ scheduler.submit（准入）
  → llm.py（S1-10，未实现）→ send_gate.py（S1-12，未实现）→ 唯一出站路径
```

### 模块边界（每个模块"必须不做"与职责同等重要）

| 模块 | 职责 | 必须不做 |
|---|---|---|
| `main.py` | Star 生命周期、钩子注册、唯一装配点与出站路径 | 不写业务规则、不解析身份、不直接 `event.send` |
| `config.py` | fail-closed 配置模型：任一字段缺失或畸形 → 整体退回哨兵 `Settings()`（拒绝全部） | 不默认开启采集/记忆；密钥不进 `repr` |
| `policy.py` | 纯触发形态分类：`IGNORE` / `EMPTY_OR_UNSUPPORTED` / `UNSUPPORTED_ATTACHMENT` / `TEXT_CANDIDATE`；`is_trusted_scope` 是权限与触发**共用的唯一谓词** | 不判权限、不读 DB、不判采集资格（属 S2）、不做指令分流 |
| `keys.py` | `BotInstanceKey` / `GroupKey` / `MemberKey`、`RevisionSnapshot`；只由可信元数据构造，缺一段即构造失败 | 不用昵称/群名/正文构造键 |
| `commands.py` | 纯解析 `CommandIntent` + 所需权限档；空白归一化 | 不核权、不执行、不调模型；**调用前提是调用方已确认真实 @** |
| `control.py` | 确定性授权：可信身份 + 显式配置映射 | 不读事件对象、不导入框架、不调模型；QQ 群管理员 ≠ 系统管理员 ≠ 维护者 |
| `scheduler.py` | 有界队列、全局/同群并发、业务期限；聊天优先于抽取 | 不发送、不调模型、不读存储、不判权限、不重试；**不用 `asyncio.Semaphore`**（FIFO 无法表达聊天优先）；不持有后台任务 |
| `dedup.py` | 事件与动作去重：`IN_FLIGHT` / `DONE` / `UNCERTAIN`；有界（容量 + TTL） | 不保存任何正文；**窗口无默认值，必须由调用方注入**；`release()` 仅限确认无副作用时 |
| `budget.py` | 预留 / 按 usage 结算 / 门槛核算 | 不调模型、不硬编码价目；缺失 usage 记估算**而非零**；价格未知且已配金额时保守拒绝 |

**尚不存在**（属后续任务卡，不要 import）：`health.py`（S1-04）、`llm.py`（S1-10）、
`context_assembly.py`（S1-11）、`send_gate.py`（S1-12）、`redact.py`（S1-13）、`storage/`、`memory/`。
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

- K1：任一 handler 的 filter 通过即使事件 `is_wake = True`——宽 filter 有副作用，须用
  `should_call_llm(False)` + `stop_event()` 收口。
- K3：私聊默认唤醒；K4：`ProcessStage` 在 handler 之后还有**第二条默认 LLM 路径**（同一事件可能双次计费）。
- K6：`mark_as_temp()` 只作用于 part，整轮排除还须在 `on_agent_done` 对 user 与 assistant
  两条 `Message` 设 `_no_save`。
- K8：EventBus 为每个事件建独立 task，"同群并发 1"必须插件自行加锁。
- R15：保留内置插件不受 `disable_builtin_commands` 约束，其 `handle_empty_mention` 优先级高于本插件
  且会 `stop_event()`；唯一关闭手段是 `platform_settings.empty_mention_waiting`。
- R16：框架对会话与平台消息历史**无原生 TTL**，20 轮/24 小时与 90 天全部需插件自建清理。
- R11：原生流程在发送门控前就已写入历史（`STAGES_ORDER` 中 ProcessStage 早于 RespondStage）。

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
  其余 `test_*.py` 只导入 `astrbot_plugin_chizuru/` 下的纯逻辑模块。
- S0 核验脚本在独立进程运行，共享引导在 `scripts/s0/_harness.py`（`Harness` + `Checker`）；
  **每个进程只能建立一个 `Harness`**（audit hook 不可卸载）。新脚本照抄该模板即可获得
  隔离 `ASTRBOT_ROOT`、11 类事件门控矩阵所需的真假事件与出站/模型调用记录器。
- `scripts/s0/` 不属于产品插件包，不参与插件运行时；不要把它 import 进插件源码。
