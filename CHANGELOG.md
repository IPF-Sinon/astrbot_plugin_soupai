# 更新日志 (CHANGELOG)

本文件记录 AstrBot 海龟汤插件 (`astrbot_plugin_soupai`) 的所有版本变更。

---

## [v1.4.6] - 2026-07-19

### 修复
- **修复 `'SoupaiPlugin' object has no attribute 'data_path'` 启动报错**: `data_path` 初始化移到 `_load_difficulty()` 之前执行，确保加载难度持久化文件时目录已就绪

## [v1.4.5] - 2026-07-19

### 修复
- **难度系统全面修复**:
  - `/汤难度` 未设置时不再固定为"普通"，改为默认"简单"
  - 支持数字 order 设置难度，例如 `/汤难度 2` 即可切换到简单模式
  - 难度设置现在会持久化到 `difficulty.json`，重启插件不丢失
  - 当配置中不存在默认难度时，自动回退到 order 最小的难度
- **简单模式提问次数调整**: 默认从无限改为 90 次

## [v1.4.4] - 2026-07-19

### 优化
- **异步化**: 提问和生成提示改为后台异步任务，AI回复不再阻塞游戏会话控制
- **提问并发优化**: 同一游戏会话中可同时触发多条 LLM 提问请求

### 修复
- 移除 `network_soupai.json` 中的无效/低质量谜题（婴儿眼睛汤等）



---

## [v1.4.3] - 2026-07-18

### 🐛 Bug 修复

- **修复 f-string 语法错误导致插件加载失败**：`main.py` 第 1630 行存在 `f-string: unmatched ')'` 语法错误，根源为上一轮修复中遗留的字符串嵌套问题。现已修正为普通字符串。



---

## [v1.4.2] - 2026-07-18

### 🐛 Bug 修复

- **修复 `check_game_status` 调用时 NameError**：补全缺失的 `question_info` / `hint_info` 变量定义，/汤状态 指令不再报错。
- **修复验证次数「耗尽前/耗尽后」切换逻辑**：改为独立计数器 `verification_before_attempts` / `verification_after_attempts`，互不干扰；提问耗尽后不会占用耗尽前计数。配置「耗尽前 2 次、耗尽后 3 次」现在正确工作：耗尽前用完进入耗尽后完整 3 次。
- **修复验证次数提示硬编码为 `2`**：所有剩余次数提示改为读取当前难度的实际配置值；`verification_after_limit = -1`（无限）时显示 `∞`，不结束游戏。
- **修复删除「普通」难度后无法开始游戏**：添加 `_get_fallback_difficulty()` 方法，当配置中不存在「普通」时自动回退到 `order` 最小的第一个难度。
- **修复 `config=None` 兼容写法错误**：删除 `= None` 默认值与 `AstrBotConfig({})` 死分支，`__init__` 签名改为 `def __init__(self, context: Context, config: AstrBotConfig):`，严格遵守框架契约。



---

## [v1.4.1] - 2026-07-18

### 🐛 Bug 修复

- **修复 README 版本号显示为 v1.0.6 的问题**：Release v1.4.0 的 zip 包中 README 版本号未正确更新，现已重新打包为 v1.4.1。




---

## [v1.3.1] - 2026-07-17

### 🐛 Bug 修复

- **修复「无限模式下提问计数不生效」**：`/汤状态` 一直显示 `提问：0/∞`，实际已提问多次却不计数的问题。
  - 根因：提问计数自增逻辑被 `if question_limit is not None` 守卫卡住。无限模式（如「简单」难度 `limit: -1`）下 `question_limit` 被解析为 `None`，整段自增代码被跳过，`question_count` 始终为初始值 `0`。
  - 现改为**无条件自增**计数（无限模式同样累计），仅当 `question_limit is not None 且 question_count >= question_limit` 时才提示「提问次数已用完，进入验证环节」。
  - 状态查询（`/汤状态`、会话内查询）读取的即真实计数，现在无限模式会正确显示如 `15/∞`。
  - 附带修正 `merge` 模式在无限模式下不再硬拼 `（N/None）`，统一改为仅输出判断结果。



---

## [v1.3.0] - 2026-07-17

### ✨ 新功能 / 配置重构

- **回复方式改为三选一下拉配置 `reply_mode`**（替换原 `quote_in_group` / `quote_in_private` 两个布尔开关）：
  - `quote`（引用回复，默认）：引用提问者原消息后再输出内容，群聊/私聊统一生效，参考 astrbot_plugin_smart_quote。
  - `merge`（合并回复）：不引用，但把「问题 + 回答 + 计数」合并到一条消息（即旧版困难模式那种 `❓…💬…` 格式）。
  - `direct`（直接回复）：不引用、不合并，仅输出判断结果（是/否等），即旧版娱乐模式那种格式。
- 旧配置 `quote_in_group` / `quote_in_private` 升级后自动失效（代码仅读取 `reply_mode`，非法值回退为 `quote`）。

### 🐛 Bug 修复

- **修复「群聊提问不引用、且不同难度格式不一致」**：根因为问答判断路径（提问回答）此前**完全没走**引用/统一回复逻辑，而是直接 `event.send(event.plain_result(...))`；同时文本格式被硬编码为「有限次数→合并样式、无限次数→裸结果」，导致娱乐与困难看起来不一样、且都不引用。
  - 现已将该路径统一接入新的 `_send_reply(event, text)` 发送入口，提问回答、验证结果、提示等**所有 bot 回复**都按 `reply_mode` 一致处理。
  - 引用能力从此覆盖**提问回答**（你反馈的「验证不通过能正常引用、但提问不能」问题已解决）。

### 📝 配置变更说明

- `_conf_schema.json`：删除 `quote_in_group`、`quote_in_private`；新增字符串型 `reply_mode`（默认 `quote`，可选 `quote` / `merge` / `direct`）。
- 升级后请在插件配置页确认 `reply_mode` 取值符合预期（默认 `quote`）。

### 🔧 技术改进

- 新增统一回复入口方法 `_send_reply(event, text)`：根据 `reply_mode` 选择引用（`Reply(id=message_id)` + `chain_result`）或纯文本发送；`Reply` 不可用时自动降级为纯文本，不影响游戏逻辑。
- 删除旧 `_should_quote()` / `_quote_result()`，所有调用点（验证流程、提示、问答流程等）统一改为 `self._send_reply(...)`。



---

## [v1.2.0] - 2026-07-17

### ✨ 新功能

- **回复引用提问者消息**：参考 [astrbot_plugin_smart_quote](https://github.com/NickWoluff/astrbot_plugin_smart_quote) 实现，验证结果、验证次数提示、提示结果等 bot 回复现在可以引用玩家发送的原消息，群聊对话更清晰。
  - 新增配置项 `quote_in_group`（群聊引用，默认 **开启**）
  - 新增配置项 `quote_in_private`（私聊引用，默认 **关闭**）
  - 通过 `_should_quote()` 按消息来源（群聊/私聊）判断是否引用，`_quote_result()` 统一构造 `MessageChain([Quote(...), Plain(...)])`。
  - 引用构造失败时自动降级为纯文本回复，不影响功能。

### 🐛 Bug 修复

- **修复「一验证不通过就自动结束游戏」**：旧逻辑中 `remaining` 初始值为 `0`，当难度组的 `verification_after_limit` 为 `-1`（无限验证）时，三个分支里的 `if ... != -1` 均不成立，`remaining` 始终保持 `0`，命中 `remaining == 0` 分支误判为「次数用尽」而提前结束游戏。
  - 现改为 `remaining` 初始值为 `None`：
    - `remaining is None` → 无限次数，提示「请继续尝试」并**继续游戏**（不结束）
    - `remaining > 0` → 提示剩余次数，继续游戏
    - `remaining == 0` → 真正用尽才揭晓答案并结束游戏
  - 验证成功、验证次数用尽、次数重置等提示同样统一走引用发送逻辑。

### 📝 配置变更说明

- `_conf_schema.json` 新增 `quote_in_group`（默认 `true`）、`quote_in_private`（默认 `false`）两个布尔配置项。
- 升级后该两项默认生效（群聊默认引用），如不需要可在插件配置页关闭。

### 🔧 技术改进

- 新增导入：`from astrbot.api.message_components import At, Plain, Quote, MessageChain`
- 新增辅助方法：`_should_quote(event)`、`_quote_result(event, text)`。
- `_handle_verification_in_session()` 内的所有 `event.send(event.plain_result(...))` 验证相关回复均改为 `self._quote_result(event, ...)`。



---

## [v1.1.0] - 2026-07-17

### 🐛 Bug 修复

- **修复无限提问次数不生效**：难度组配置中 `提问次数`（`limit`）设为 `-1` 表示「无限」，
  但旧代码直接保留 `-1` 作为限制值，导致状态检查 `question_count >= -1` 恒为真，
  游戏在第一问后就被判定「提问次数已用完」。现已将 `-1` 统一转换为 `None`（Python 语义上的无限），
  彻底修复该问题。

- **修复无限提示次数不生效**：同理，`提示次数`（`hint_limit`）设为 `-1` 时旧代码会判定
  `hint_count >= -1` 恒为真，导致提示功能被立即禁用。现已将 `-1` 转换为 `None`，
  `/提示` 指令可正常使用无限次提示。

- **修复配置字段名不一致**：配置模式（`_conf_schema.json`）与用户文档（`README.md`）中使用的
  字段名为 `question_limit`（表示提问次数），而插件内部代码此前使用的是 `limit`。
  现统一：配置读取阶段从 `limit` 读取并转换为内部的 `question_limit` 字段，消除命名歧义。

### 📝 配置变更说明

- 难度组模板字段 `limit`（提问次数）与 `hint_limit`（提示次数）的 `-1` 值现在**正确表示无限**。
- 旧版配置（使用 `limit` 字段、含 `-1` 值）在升级后**自动兼容**，无需手动修改。
- `verification_after_limit`（提问耗尽后验证次数）的 `-1` 语义保持不变（仍表示无限次验证）。

### 🔧 技术改进

- `_parse_difficulty_groups()`：将 `-1` 转为 `None` 表示无限，并统一内部字段名为 `question_limit` / `hint_limit`。
- 游戏开始提示：无限提问显示为「（无限提问」，无限提示显示为「，无限提示）」。
- `/汤状态` 与游戏内状态查询：提问次数显示为 `X/∞`；提示次数 `None`（无限）显示为「无限」，`0` 显示为「不可用」。
- 难度列表展示（插件信息指令）：`question_limit` 为 `None` 时显示「无限」，提示同理。

### ✅ 升级前需做

1. 在 AstrBot WebUI 中**卸载旧版本**插件（v1.0.x）。
2. 安装 `astrbot_plugin_soupai_v1.1.0.zip`。
3. 启动插件后，在配置页确认难度组参数；`-1` 即代表无限。



---

## [v1.0.8] - 2026-07-17

### 🐛 Bug 修复

- 修复 `_conf_schema.json` 第 55 行 `hint` 字段中中文弯引号导致 JSON 解析失败的问题
  （`Expecting ',' delimiter: line 55 column 25`）。
- 将原有 5 个固定难度组重构为 `template_list` 格式，支持用户在 WebUI 中自定义任意数量与参数的难度组。
- `_parse_difficulty_groups()` 方法适配 `template_list` 配置格式，支持动态解析自定义难度组。



---

## [v1.0.6] - 2026-07-17

### 🎮 基础功能

- 海龟汤推理游戏核心功能（谜题生成、问答判断、验证系统、提示系统）。
- 五种内置难度：娱乐 / 简单 / 普通 / 困难 / 666开挂了。
- 网络题库（近 300 道）、本地存储库、自定义题库三种谜题来源。
- 会话控制、超时机制、自动生成备用故事等。
