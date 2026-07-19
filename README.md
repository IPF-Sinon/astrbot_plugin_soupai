# AstrBot 海龟汤推理游戏插件 (astrbot_plugin_soupai)

## 📋 项目概述

**项目名称**: astrbot_plugin_soupai  
**作者**: KONpiGG  
**版本**: v1.4.4  
**许可证**: MIT  
**仓库地址**: https://github.com/KONpiGG/astrbot_plugin_soupai  
**AstrBot版本要求**: >= v3.4.36

## 🎯 功能特性

### 核心功能
- ✅ **智能谜题生成**: 支持网络题库、本地存储库与LLM现场生成
- ✅ **智能问答系统**: 使用LLM判断用户提问并回复"是/否/是也不是/不重要"
- ✅ **验证系统**: 使用LLM验证用户提交的完整推理结果
- ✅ **提示系统**: 使用LLM根据问答历史生成方向性提示
- ✅ **题库管理**: 记录题目使用情况，支持重置和查看详情

### 游戏特性
- 🎮 **多种难度**: 娱乐/简单/普通/困难/666开挂了五种难度级别
- 👥 **会话控制**: 每个群一次仅允许一个游戏
- ⏱️ **超时机制**: 游戏超时自动结束
- 🔄 **自动生成**: 在指定时间段自动生成新故事
- 🌐 **网络题库**: 近300道高质量海龟汤题目

## 🏗️ 项目结构

```
workspace/
├── astrbot_plugin/                    # 插件核心目录
│   ├── main.py                        # 插件主代码 (2324行)
│   ├── metadata.yaml                  # 插件元数据
│   ├── _conf_schema.json             # 配置模式定义
│   └── network_soupai.json           # 网络题库数据 (177KB)
├── astrbot_plugin_soupai_analysis.md # 详细分析报告
└── README.md                         # 本文件
```

## 🔧 技术架构

### 存储系统
```python
ThreadSafeStoryStorage (线程安全基类)
├── NetworkSoupaiStorage (网络海龟汤管理)
├── LocalSoupaiStorage (本地存储库管理) 
└── CustomSoupaiStorage (自定义海龟汤存储)
```

### 核心组件
1. **GameState**: 游戏状态管理，支持多群并发
2. **GroupSessionFilter**: 自定义会话过滤器，确保每个群的会话独立
3. **VerificationResult**: 验证结果类，支持四级验证结果

## 📊 功能模块

| 模块 | 功能 | 复杂度 |
|------|------|--------|
| 谜题生成 | 三种来源，可配置优先级 | 中等 |
| 问答判断 | LLM智能判断，四种回复 | 中等 |
| 验证系统 | 四级验证，智能评估 | 高 |
| 提示系统 | 基于历史生成方向性提示 | 高 |
| 会话控制 | 群聊独立，支持多用户 | 中等 |
| 存储管理 | 线程安全，自动重置 | 中等 |

## 📝 指令系统

### 用户指令
- `/汤` - 开始海龟汤游戏
- `/查看` - 查看当前已提问的问题及回答
- `/提示` - 获取方向性提示
- `/验证 <推理内容>` - 验证用户对完整故事的推理（2次验证机会）
- `/揭晓` - 查看完整故事并结束游戏
- `/汤难度 <难度>` - 设置游戏难度
- `/汤状态` - 查看当前游戏状态

### 难度组系统

插件支持完全可配置的难度组系统，每个难度组包含独立的参数配置：

| 难度 | 提问限制 | 提示次数 | 验证次数 | 猜中条件 |
|------|----------|----------|----------|----------|
| 娱乐 | 无限 | 15次 | 无限次 | 完全还原/核心推理正确/部分正确 |
| 简单 | 无限 | 10次 | 8次总次数 | 完全还原/核心推理正确 |
| 普通 | 35次 | 5次 | 4次总次数 | 仅完全还原 |
| 困难 | 15次 | 1次 | 2次总次数 | 仅完全还原 |
| 666开挂了 | 5次 | 0次 | 2次总次数 | 仅完全还原 |

**验证规则**：
- 提问耗尽前/耗尽后使用独立的计数器，互不干扰
- `verification_before_limit` 为0时跳过耗尽前阶段，直接使用耗尽后计数
- `verification_after_limit` 为-1时表示无限次验证
- 提问耗尽后不会重置计数，两阶段各自独立累积
- 配置修改后重启插件生效

### 管理员指令
- `/强制结束` - 强制结束当前游戏
- `/备用开始` - 手动开始生成备用故事
- `/备用结束` - 停止生成备用故事
- `/重置题库` - 重置网络题库和本地存储库的使用记录
- `/题库详情` - 查看题库使用情况与已用索引

## ⚙️ 配置说明

### 基础配置
```json
{
  "generate_llm_provider": "生成谜题的LLM提供商ID",
  "judge_llm_provider": "判断问答的LLM提供商ID",
  "game_timeout": 1800,
  "storage_max_size": 50,
  "auto_generate_start": 3,
  "auto_generate_end": 6,
  "puzzle_source_strategy": "network_first"
}
```

### 难度组配置
```json
{
  "difficulty_groups": {
    "娱乐": {
      "order": 1,
      "question_limit": null,
      "hint_limit": 15,
      "verification_before_limit": 0,
      "verification_after_limit": -1,
      "accept_levels": ["完全还原", "核心推理正确", "部分正确"]
    },
    "简单": {
      "order": 2,
      "question_limit": null,
      "hint_limit": 10,
      "verification_before_limit": 0,
      "verification_after_limit": 8,
      "accept_levels": ["完全还原", "核心推理正确"]
    },
    "普通": {
      "order": 3,
      "question_limit": 35,
      "hint_limit": 5,
      "verification_before_limit": 0,
      "verification_after_limit": 4,
      "accept_levels": ["完全还原"]
    },
    "困难": {
      "order": 4,
      "question_limit": 15,
      "hint_limit": 1,
      "verification_before_limit": 0,
      "verification_after_limit": 2,
      "accept_levels": ["完全还原"]
    },
    "666开挂了": {
      "order": 5,
      "question_limit": 5,
      "hint_limit": 0,
      "verification_before_limit": 0,
      "verification_after_limit": 2,
      "accept_levels": ["完全还原"]
    }
  }
}
```

**配置字段说明**：
| 字段 | 类型 | 说明 | 默认值 |
|------|------|------|--------|
| `order` | int | 难度显示顺序 | 必填 |
| `question_limit` | int/null | 可提问次数，表示无限 | 必填 |
| `hint_limit` | int | 可提示次数 | 必填 |
| `verification_before_limit` | int | 提问耗尽前验证次数 | 0 |
| `verification_after_limit` | int | 提问耗尽后验证次数，-1表示无限 | 必填 |
| `accept_levels` | array | 接受的验证等级 | ["完全还原"] |

## 🚀 部署要求

- **AstrBot版本**: >= v3.4.36
- **LLM服务商**: 已配置LLM服务商
- **存储空间**: 需要数据目录存储状态

## 📈 设计亮点

### 1. 模块化架构
- 存储系统分层设计，易于扩展
- 配置与逻辑分离
- 状态管理独立

### 2. 用户体验优化
- 清晰的状态反馈
- 智能提示系统
- 完善的错误处理
- 多难度选择

### 3. 技术实现
- 线程安全设计
- 异步任务管理
- 数据持久化
- 自动生成机制

## 🔍 代码质量分析

### 优点
1. **注释完善**: 代码有详细的中文注释
2. **错误处理**: 完善的异常处理和边界检查
3. **日志记录**: 使用框架日志系统记录关键操作
4. **类型提示**: 使用Python类型提示提高可读性

### 改进建议
1. **配置验证**: 添加配置参数的验证逻辑
2. **性能优化**: 网络题库加载可优化
3. **测试覆盖**: 添加单元测试
4. **文档完善**: 补充API文档

## 📚 参考资料

- [AstrBot插件开发文档](https://docs.astrbot.app/dev/star/plugin-new.html)
- [AstrBot GitHub仓库](https://github.com/AstrBotDevs/AstrBot)
- [插件模板](https://github.com/AstrBotDevs/AstrBot)

## 📁 文件说明

- `main.py` - 插件核心代码，包含所有功能实现
- `metadata.yaml` - 插件元数据，定义插件基本信息
- `_conf_schema.json` - 配置模式定义，定义可配置参数
- `network_soupai.json` - 网络题库数据，包含近300道海龟汤题目
- `astrbot_plugin_soupai_analysis.md` - 详细分析报告

## 🎮 游戏流程

```
用户发送 /汤
    ↓
检查是否已有游戏 → 是 → 提示"已有游戏进行中"
    ↓
选择谜题来源 (网络/本地/LLM)
    ↓
生成并公布谜题
    ↓
启动会话控制
    ↓
用户交互循环:
├── @机器人 + 问题 → 判断并回复
├── /提示 → 生成方向性提示
├── /验证 → 验证推理
├── /查看 → 显示问答历史
└── /揭晓 → 结束游戏
    ↓
游戏结束条件:
├── 验证成功
├── 用户主动揭晓
├── 超时自动结束
└── 管理员强制结束
```

## 📊 网络题库示例

```json
{
  "puzzle": "一个男人推开门，看到眼前的景象后立即跳楼自杀了。为什么？",
  "answer": "这个男人是灯塔管理员，他发现灯塔的灯灭了，意识到因为自己的疏忽导致船只失事，愧疚之下选择了跳楼。"
}
```

## 🔧 扩展可能性

1. **新功能**: 添加排行榜、成就系统
2. **题库扩展**: 支持用户提交题目
3. **多语言**: 支持英文版本
4. **主题系统**: 不同风格的海龟汤
5. **数据分析**: 玩家行为分析

## 📝 开发说明

本插件基于AstrBot插件开发框架构建，使用了以下核心功能：
- `@register` 装饰器注册插件
- `@filter.command` 注册指令处理器
- `session_waiter` 实现会话控制
- `self.context.get_using_provider()` 调用默认LLM服务
- `self.context.get_provider_by_id()` 调用指定的LLM服务商

## 📞 联系方式

- QQ群: 1053727011
- GitHub Issues: https://github.com/KONpiGG/astrbot_plugin_soupai/issues
