import json
import asyncio
import os
import threading
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional, Tuple, List
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api.provider import LLMResponse
from astrbot.api import logger, AstrBotConfig
from astrbot.core.utils.session_waiter import (
    session_waiter,
    SessionController,
    SessionFilter,
)
try:
    from astrbot.api.message_components import At, Plain, MessageChain
except ImportError:  # 兼容旧版本
    from astrbot.api.message_components import At, Plain

    MessageChain = None

# 引用（回复）消息组件：AstrBot v4 标准做法使用 astrbot.core.message.components.Reply。
# 旧版本若 import 失败则 Reply 置为 None，运行时降级为纯文本回复（不引用）。
try:
    from astrbot.core.message.components import Reply
except ImportError:  # 极少数旧版本兼容：尝试从 api 路径导入
    try:
        from astrbot.api.message_components import Reply
    except ImportError:
        Reply = None


# 线程安全的题库管理基类
class ThreadSafeStoryStorage:
    """线程安全的题库管理基类，支持持久化使用记录"""

    def __init__(self, storage_name: str, data_path=None):
        self.storage_name = storage_name
        self.data_path = data_path
        self.used_indexes: set[int] = set()
        self.lock = threading.Lock()  # 线程锁
        self.usage_file = (
            self.data_path / f"{storage_name}_usage.json" if self.data_path else None
        )
        self.load_usage_record()

    def load_usage_record(self):
        """从文件加载使用记录"""
        if not self.usage_file:
            self.used_indexes = set()
            return

        try:
            if self.usage_file.exists():
                with open(self.usage_file, "r", encoding="utf-8") as f:
                    self.used_indexes = set(json.load(f))
                logger.info(
                    f"从 {self.usage_file} 加载了 {len(self.used_indexes)} 个使用记录"
                )
            else:
                self.used_indexes = set()
                logger.info(
                    f"使用记录文件不存在，创建新的记录: {self.usage_file}"
                )
        except Exception as e:
            logger.error(f"加载使用记录失败: {e}")
            self.used_indexes = set()

    def save_usage_record(self):
        """保存使用记录到文件"""
        if not self.usage_file:
            return

        try:
            self.usage_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.usage_file, "w", encoding="utf-8") as f:
                json.dump(list(self.used_indexes), f, ensure_ascii=False, indent=2)
            logger.info(
                f"保存了 {len(self.used_indexes)} 个使用记录到 {self.usage_file}"
            )
        except Exception as e:
            logger.error(f"保存使用记录失败: {e}")

    def reset_usage(self):
        """重置使用记录"""
        with self.lock:
            self.used_indexes.clear()
            self.save_usage_record()
            logger.info(f"{self.storage_name} 使用记录已重置")

    def get_usage_info(self) -> Dict:
        """获取使用记录信息"""
        with self.lock:
            return {
                "used": len(self.used_indexes),
                "used_indexes": list(self.used_indexes),
            }


# 游戏状态管理
class GameState:
    def __init__(self):
        self.active_games: Dict[str, Dict] = {}  # 群聊ID -> 游戏状态

    def start_game(self, group_id: str, puzzle: str, answer: str, **extra) -> bool:
        """开始游戏，返回是否成功"""
        if group_id in self.active_games:
            return False
        game_data = {
            "puzzle": puzzle,
            "answer": answer,
            "is_active": True,
            "qa_history": [],
            "hint_history": [],
        }
        game_data.update(extra)
        self.active_games[group_id] = game_data
        return True

    def end_game(self, group_id: str) -> bool:
        """结束游戏"""
        if group_id in self.active_games:
            del self.active_games[group_id]
            return True
        return False

    def get_game(self, group_id: str) -> Optional[Dict]:
        """获取游戏状态"""
        return self.active_games.get(group_id)

    def is_game_active(self, group_id: str) -> bool:
        """检查是否有活跃游戏"""
        return group_id in self.active_games


# 网络海龟汤管理
class NetworkSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, network_file: str, data_path=None):
        # 初始化基类
        super().__init__("network_soupai", data_path)
        self.network_file = network_file
        self.stories: List[Dict] = []
        self.load_stories()

    def load_stories(self):
        """从文件加载网络海龟汤故事"""
        try:
            if os.path.exists(self.network_file):
                with open(self.network_file, "r", encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(
                    f"从 {self.network_file} 加载了 {len(self.stories)} 个网络海龟汤故事"
                )
            else:
                self.stories = []
                logger.warning(f"网络海龟汤文件不存在: {self.network_file}")
        except Exception as e:
            logger.error(f"加载网络海龟汤失败: {e}")
            self.stories = []

    def get_story(self) -> Optional[Tuple[str, str]]:
        """从网络题库获取一个故事，避免重复（线程安全）"""
        if not self.stories:
            return None

        with self.lock:
            # 获取所有可用的索引（排除已使用的）
            available_indexes = [
                i for i in range(len(self.stories)) if i not in self.used_indexes
            ]

            # 如果没有可用题目，清空已用记录，重新开始一轮
            if not available_indexes:
                logger.info("网络题库已全部使用完毕，清空记录重新开始")
                self.used_indexes.clear()
                available_indexes = list(range(len(self.stories)))
                # 立即保存重置后的状态
                self.save_usage_record()

            # 从可用索引中随机选择一个
            import random

            selected = random.choice(available_indexes)
            self.used_indexes.add(selected)

            # 保存使用记录
            self.save_usage_record()

            story = self.stories[selected]
            logger.info(
                f"从网络题库获取故事，索引: {selected}, 已使用: {len(self.used_indexes)}/{len(self.stories)}"
            )
            return story["puzzle"], story["answer"]

    def get_storage_info(self) -> Dict:
        """获取网络题库信息"""
        usage_info = self.get_usage_info()
        return {
            "total": len(self.stories),
            "available": len(self.stories) - usage_info["used"],
            "used": usage_info["used"],
        }


# 存储库管理
class LocalSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, storage_file: str, max_size: int = 50, data_path=None):
        # 初始化基类
        super().__init__("storage_soupai", data_path)
        self.storage_file = storage_file
        self.max_size = max_size
        self.stories: List[Dict] = []
        self.load_stories()

    def load_stories(self):
        """从文件加载故事"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            if os.path.exists(storage_path):
                with open(storage_path, "r", encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(f"从 {storage_path} 加载了 {len(self.stories)} 个故事")
            else:
                self.stories = []
                logger.info("存储库文件不存在，创建新的存储库")
        except Exception as e:
            logger.error(f"加载故事失败: {e}")
            self.stories = []

    def save_stories(self):
        """保存故事到文件"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            # 确保目录存在
            os.makedirs(os.path.dirname(storage_path), exist_ok=True)
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(self.stories, f, ensure_ascii=False, indent=2)
            logger.info(f"保存了 {len(self.stories)} 个故事到 {storage_path}")
        except Exception as e:
            logger.error(f"保存故事失败: {e}")

    def add_story(self, puzzle: str, answer: str) -> bool:
        """添加故事到存储库"""
        with self.lock:
            if len(self.stories) >= self.max_size:
                # 移除最旧的故事
                self.stories.pop(0)
                logger.info("存储库已满，移除最旧的故事")

            story = {
                "puzzle": puzzle,
                "answer": answer,
                "created_at": datetime.now().isoformat(),
            }
            self.stories.append(story)
            self.save_stories()
            logger.info(f"添加新故事到存储库，当前存储库大小: {len(self.stories)}")
            return True

    def get_story(self) -> Optional[Tuple[str, str]]:
        """从存储库获取一个故事，避免重复（线程安全）"""
        if not self.stories:
            return None

        with self.lock:
            # 获取所有可用的索引（排除已使用的）
            available_indexes = [
                i for i in range(len(self.stories)) if i not in self.used_indexes
            ]

            # 如果没有可用题目，清空已用记录，重新开始一轮
            if not available_indexes:
                logger.info("本地存储库已全部使用完毕，清空记录重新开始")
                self.used_indexes.clear()
                available_indexes = list(range(len(self.stories)))
                # 立即保存重置后的状态
                self.save_usage_record()

            # 从可用索引中随机选择一个
            import random

            selected = random.choice(available_indexes)
            self.used_indexes.add(selected)

            # 保存使用记录
            self.save_usage_record()

            story = self.stories[selected]
            logger.info(
                f"从本地存储库获取故事，索引: {selected}, 已使用: {len(self.used_indexes)}/{len(self.stories)}"
            )
            return story["puzzle"], story["answer"]

    def get_storage_info(self) -> Dict:
        """获取存储库信息"""
        usage_info = self.get_usage_info()
        return {
            "total": len(self.stories),
            "max_size": self.max_size,
            "available": self.max_size - len(self.stories),
            "used": usage_info["used"],
            "remaining": len(self.stories) - usage_info["used"],
        }


# 自定义海龟汤存储
class CustomSoupaiStorage(ThreadSafeStoryStorage):
    def __init__(self, storage_file: str, data_path=None):
        # 初始化基类
        super().__init__("custom_soupai", data_path)
        self.storage_file = storage_file
        self.stories: List[Dict] = []
        self.load_stories()

    def load_stories(self):
        """从文件加载自定义故事"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            if os.path.exists(storage_path):
                with open(storage_path, "r", encoding="utf-8") as f:
                    self.stories = json.load(f)
                logger.info(f"从 {storage_path} 加载了 {len(self.stories)} 个自定义海龟汤故事")
            else:
                self.stories = []
                logger.info("自定义海龟汤文件不存在，创建新的存储库")
        except Exception as e:
            logger.error(f"加载自定义海龟汤失败: {e}")
            self.stories = []

    def save_stories(self):
        """保存自定义故事到文件"""
        try:
            storage_path = (
                self.storage_file
                if isinstance(self.storage_file, str)
                else str(self.storage_file)
            )
            # 确保目录存在
            os.makedirs(os.path.dirname(storage_path), exist_ok=True)
            with open(storage_path, "w", encoding="utf-8") as f:
                json.dump(self.stories, f, ensure_ascii=False, indent=2)
            logger.info(f"保存了 {len(self.stories)} 个自定义海龟汤故事到 {storage_path}")
        except Exception as e:
            logger.error(f"保存自定义海龟汤失败: {e}")

    def add_story(self, puzzle: str, answer: str) -> bool:
        """添加自定义故事到存储库"""
        with self.lock:
            story = {
                "puzzle": puzzle,
                "answer": answer,
                "created_at": datetime.now().isoformat(),
            }
            self.stories.append(story)
            self.save_stories()
            logger.info(f"添加新自定义海龟汤故事，当前存储库大小: {len(self.stories)}")
            return True

    def get_story(self) -> Optional[Tuple[str, str]]:
        """从自定义存储库获取一个故事，避免重复（线程安全）"""
        if not self.stories:
            return None

        with self.lock:
            # 获取所有可用的索引（排除已使用的）
            available_indexes = [
                i for i in range(len(self.stories)) if i not in self.used_indexes
            ]

            # 如果没有可用题目，清空已用记录，重新开始一轮
            if not available_indexes:
                logger.info("自定义存储库已全部使用完毕，清空记录重新开始")
                self.used_indexes.clear()
                available_indexes = list(range(len(self.stories)))
                # 立即保存重置后的状态
                self.save_usage_record()

            # 从可用索引中随机选择一个
            import random

            selected = random.choice(available_indexes)
            self.used_indexes.add(selected)

            # 保存使用记录
            self.save_usage_record()

            story = self.stories[selected]
            logger.info(
                f"从自定义存储库获取故事，索引: {selected}, 已使用: {len(self.used_indexes)}/{len(self.stories)}"
            )
            return story["puzzle"], story["answer"]

    def get_storage_info(self) -> Dict:
        """获取自定义存储库信息"""
        usage_info = self.get_usage_info()
        return {
            "total": len(self.stories),
            "used": usage_info["used"],
            "remaining": len(self.stories) - usage_info["used"],
        }


# 验证结果类
class VerificationResult:
    """验证结果类"""

    def __init__(self, level: str, comment: str, is_correct: bool = False):
        self.level = level
        self.comment = comment
        self.is_correct = is_correct

    def to_dict(self) -> Dict:
        return {
            "level": self.level,
            "comment": self.comment,
            "is_correct": self.is_correct,
        }


# 自定义会话过滤器 - 以群为单位进行会话控制
class GroupSessionFilter(SessionFilter):
    """会话过滤器，确保每个群的会话独立"""

    def __init__(self, group_id: str):
        # 为每个会话保存其所属群 ID
        self.group_id = group_id

    def filter(self, event: AstrMessageEvent) -> str:
        current_group_id = (
            event.get_group_id() if event.get_group_id() else event.unified_msg_origin
        )
        # 仅当事件来自该群时才返回有效的会话 ID，否则返回空串避免误触发
        return self.group_id if current_group_id == self.group_id else ""


@register(
    "astrbot_plugin_soupai",
    "KONpiGG",
    "AI 海龟汤推理游戏插件，支持自动生成谜题、智能判断、验证系统、智能提示、存储库管理等功能。网络题库包含超过300道海龟汤，还在持续更新中。",
    "1.4.5",
    "https://github.com/KONpiGG/astrbot_plugin_soupai",
)
class SoupaiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.game_state = GameState()

        # 获取配置值
        self.generate_llm_provider_id = self.config.get("generate_llm_provider", "")
        self.judge_llm_provider_id = self.config.get("judge_llm_provider", "")
        self.game_timeout = self.config.get("game_timeout", 300)
        self.storage_max_size = self.config.get("storage_max_size", 50)
        self.auto_generate_start = self.config.get("auto_generate_start", 3)
        self.auto_generate_end = self.config.get("auto_generate_end", 6)
        self.puzzle_source_strategy = self.config.get(
            "puzzle_source_strategy", "network_first"
        )
        # TODO: 别名兼容处理，建议若干版本后删除
        if self.puzzle_source_strategy == "ai_first":
            self.puzzle_source_strategy = "local_first"

        # 回复方式配置：quote=引用回复 / merge=合并回复 / direct=直接回复
        self.reply_mode = self.config.get("reply_mode", "quote")
        if self.reply_mode not in ("quote", "merge", "direct"):
            self.reply_mode = "quote"

        # 解析难度组配置
        self.difficulty_groups = self._parse_difficulty_groups()
        self.group_difficulty: Dict[str, str] = self._load_difficulty()

        # 数据存储路径: 使用框架提供的工具获取插件数据目录
        self.data_path = StarTools.get_data_dir()
        self.data_path.mkdir(parents=True, exist_ok=True)

        # 存储库初始化延迟到 init 方法中
        self.local_story_storage = None
        self.online_story_storage = None
        self.custom_story_storage = None

        # 防止重复调用的状态
        self.generating_games = set()  # 正在生成谜题的群聊ID集合

        # 自动生成状态
        self.auto_generating = False
        self.auto_generate_task = None

    def _send_reply(self, event: AstrMessageEvent, text: str):
        """统一的回复发送入口，按 ``reply_mode`` 配置选择三种回复方式之一：

        - ``quote`` （引用回复，默认）：在消息链首部插入 ``Reply(id=消息ID)``，
          引用提问者原消息后再输出内容（AstrBot v4 标准做法）。
        - ``merge`` （合并回复）：不引用，但将「提问内容 + 回答 + 计数」合并到一条消息里。
        - ``direct``（直接回复）：不引用、不合并，仅输出判断结果（是/否等）。

        若当前 AstrBot 版本不支持 ``Reply`` 组件，则自动降级为 ``direct`` 形式发送，
        不影响游戏逻辑。
        """
        mode = getattr(self, "reply_mode", "quote")
        if mode == "quote":
            try:
                if Reply is not None:
                    msg_id = getattr(event.message_obj, "message_id", None)
                    if msg_id:
                        chain = [Reply(id=msg_id), Plain(text)]
                        return event.send(event.chain_result(chain))
            except Exception as e:
                logger.warning(f"生成引用消息失败，降级为纯文本: {e}")
            # 引用不可用或无 message_id 时降级
            return event.send(event.plain_result(text))
        # merge / direct 均为纯文本发送；区别在于上层传入的 text 内容（是否合并）。
        return event.send(event.plain_result(text))

    def _parse_difficulty_groups(self) -> Dict[str, Dict]:
        """解析难度组配置（适配 template_list 格式）"""
        # 默认配置（作为后备）
        # 注意：配置文件中使用 `limit` 字段表示提问次数，`-1` 表示无限。
        # 内部统一转换为 `question_limit` 字段名，并将 `-1` 转为 `None` 表示无限，
        # 以避免 `0 >= -1` 恒为真导致"无限"被误判为"立即用完"。
        default_groups = {
            "娱乐": {
                "order": 1,
                "question_limit": None,
                "accept_levels": ["完全还原", "核心推理正确", "部分正确"],
                "hint_limit": 15,
                "verification_before_limit": 0,
                "verification_after_limit": -1,
            },
            "简单": {
                "order": 2,
                "question_limit": 90,
                "accept_levels": ["完全还原", "核心推理正确"],
                "hint_limit": 10,
                "verification_before_limit": 0,
                "verification_after_limit": 8,
            },
            "普通": {
                "order": 3,
                "question_limit": 35,
                "accept_levels": ["完全还原"],
                "hint_limit": 5,
                "verification_before_limit": 0,
                "verification_after_limit": 4,
            },
            "困难": {
                "order": 4,
                "question_limit": 15,
                "accept_levels": ["完全还原"],
                "hint_limit": 1,
                "verification_before_limit": 0,
                "verification_after_limit": 2,
            },
            "666开挂了": {
                "order": 5,
                "question_limit": 5,
                "accept_levels": ["完全还原"],
                "hint_limit": 0,
                "verification_before_limit": 0,
                "verification_after_limit": 2,
            }
        }
        
        # 从配置中读取难度组（格式为 template_list）
        result = {}
        difficulty_groups_config = self.config.get("difficulty_groups", [])
        
        # 确保 difficulty_groups_config 是列表格式
        if not isinstance(difficulty_groups_config, list):
            difficulty_groups_config = []
        
        for group in difficulty_groups_config:
            if not isinstance(group, dict):
                continue
                
            name = group.get("name", "")
            if not name:
                continue
            
            # 读取配置中的 limit 字段（-1 表示无限），统一转为内部使用的
            # question_limit 字段，并将 -1 转为 None 表示无限。
            raw_limit = group.get("limit", 30)
            question_limit = None if raw_limit == -1 else raw_limit
            
            # 提示次数同样支持 -1 表示无限
            raw_hint_limit = group.get("hint_limit", 5)
            hint_limit = None if raw_hint_limit == -1 else raw_hint_limit
            
            # 使用配置中的值，如果不存在则使用默认值
            result[name] = {
                "order": group.get("order", 10),
                "question_limit": question_limit,
                "accept_levels": group.get("accept_levels", ["完全还原"]),
                "hint_limit": hint_limit,
                "verification_before_limit": group.get("verification_before_limit", 0),
                "verification_after_limit": group.get("verification_after_limit", 2),
            }
        
        # 如果没有配置任何难度组，使用默认配置
        if not result:
            result = default_groups.copy()
        
        return result

    def _get_fallback_difficulty(self) -> str:
        """当配置中不存在 '普通' 时，返回 order 最小的第一个难度"""
        sorted_items = sorted(
            self.difficulty_groups.items(),
            key=lambda x: x[1].get("order", 999)
        )
        return sorted_items[0][0] if sorted_items else "普通"

    def _load_difficulty(self) -> Dict[str, str]:
        """从 JSON 文件加载持久化的群难度设置"""
        path = self.data_path / "difficulty.json"
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"加载难度设置失败: {e}")
        return {}

    def _save_difficulty(self):
        """将群难度设置持久化到 JSON 文件"""
        path = self.data_path / "difficulty.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.group_difficulty, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存难度设置失败: {e}")

    def _ensure_story_storages(self) -> None:
        """确保题库存储被初始化。

        在某些环境下, 插件的 ``init`` 方法可能未被调用或异常退出,
        导致存储对象仍为 ``None``。为避免后续调用出现
        ``'NoneType' object has no attribute 'get_story'`` 的错误, 这里
        提供一次性惰性初始化。
        """

        if self.local_story_storage is None:
            storage_file = self.data_path / "storage_soupai.json"
            self.local_story_storage = LocalSoupaiStorage(
                storage_file, self.storage_max_size, self.data_path
            )

        if self.online_story_storage is None:
            plugin_dir = Path(__file__).resolve().parent
            network_file = plugin_dir / "network_soupai.json"
            self.online_story_storage = NetworkSoupaiStorage(
                str(network_file), self.data_path
            )

        if self.custom_story_storage is None:
            custom_file = self.data_path / "custom_soupai.json"
            self.custom_story_storage = CustomSoupaiStorage(
                custom_file, self.data_path
            )

    async def init(self, context: Context):
        """插件初始化，此时 self.data_path 可用"""
        await super().init(context)

        # 初始化存储对象
        self._ensure_story_storages()

        # 启动自动生成任务
        asyncio.create_task(self._start_auto_generate())

        online_info = self.online_story_storage.get_storage_info()
        logger.info(
            f"海龟汤插件已加载，配置: 生成LLM提供商={self.generate_llm_provider_id}, 判断LLM提供商={self.judge_llm_provider_id}, 超时时间={self.game_timeout}秒, 网络题库={online_info['total']}个谜题, 本地存储库大小={self.storage_max_size}, 谜题来源策略={self.puzzle_source_strategy}"
        )

    async def terminate(self):
        """插件卸载时清理资源"""
        # 停止自动生成
        self.auto_generating = False
        if self.auto_generate_task:
            self.auto_generate_task.cancel()
        logger.info("海龟汤插件已卸载呜呜呜呜呜")

    async def _start_auto_generate(self):
        """启动自动生成任务"""
        while True:
            try:
                now = datetime.now()
                current_hour = now.hour

                # 检查是否在自动生成时间范围内
                if self.auto_generate_start <= current_hour < self.auto_generate_end:
                    if not self.auto_generating:
                        # 检查存储库是否已满，如果已满则不启动自动生成
                        self._ensure_story_storages()
                        storage_info = self.local_story_storage.get_storage_info()
                        if storage_info["available"] <= 0:
                            logger.info(
                                f"本地存储库已满，跳过自动生成，时间: {current_hour}:00"
                            )
                            # 等待1小时后再次检查
                            await asyncio.sleep(3600)  # 1小时
                            continue

                        logger.info(f"开始自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = True
                        asyncio.create_task(self._auto_generate_loop())
                else:
                    if self.auto_generating:
                        logger.info(f"停止自动生成故事，时间: {current_hour}:00")
                        self.auto_generating = False

                # 等待1小时后再次检查
                await asyncio.sleep(3600)  # 1小时
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成任务错误: {e}")
                await asyncio.sleep(3600)  # 出错后等待1小时再试

    async def _auto_generate_loop(self):
        """自动生成循环"""
        # 确保在运行循环前题库已初始化
        self._ensure_story_storages()
        while self.auto_generating:
            try:
                # 检查本地存储库是否已满
                storage_info = self.local_story_storage.get_storage_info()
                if storage_info["available"] <= 0:
                    logger.info("本地存储库已满，停止自动生成")
                    self.auto_generating = False
                    break

                # 生成一个故事
                puzzle, answer = await self.generate_story_with_llm()
                if puzzle and answer and not puzzle.startswith("（"):
                    self.local_story_storage.add_story(puzzle, answer)
                    logger.info("自动生成故事成功")
                else:
                    logger.warning("自动生成故事失败")

                # 等待5分钟再生成下一个
                await asyncio.sleep(300)  # 5分钟
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"自动生成故事错误: {e}")
                await asyncio.sleep(300)  # 出错后等待5分钟再试

    # ✅ 生成谜题和答案
    async def generate_story_with_llm(self) -> Tuple[str, str]:
        """使用 LLM 生成海龟汤谜题"""

        # 根据配置获取指定的生成 LLM 提供商
        if self.generate_llm_provider_id:
            provider = self.context.get_provider_by_id(self.generate_llm_provider_id)
            if provider is None:
                logger.error(
                    f"未找到指定的生成 LLM 提供商: {self.generate_llm_provider_id}"
                )
                return "（无法生成题面，指定的生成 LLM 提供商不存在）", "（无）"
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                logger.error("未配置 LLM 服务商")
                return "（无法生成题面，请先配置大语言模型）", "（无）"

        prompt = self._build_puzzle_prompt()

        try:
            logger.info("开始调用 LLM 生成谜题...")
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt="你是一个专业的反转推理谜题创作者，专门为海龟汤游戏设计谜题。你需要创作简洁、具象、有逻辑反转的谜题，让玩家能够通过是/否提问逐步还原真相。每次创作都必须全新、原创，不能重复已有故事。",
            )

            text = llm_resp.completion_text.strip()
            logger.info(f"LLM 返回内容: {text}")

            # 尝试多种格式解析
            puzzle = None
            answer = None

            # 格式1: "题面：xxx 答案：xxx"
            if "题面：" in text and "答案：" in text:
                puzzle = text.split("题面：")[1].split("答案：")[0].strip()
                answer = text.split("答案：")[1].strip()

            # 格式2: "**题面**：xxx **答案**：xxx" (Markdown格式)
            elif "**题面**" in text and "**答案**" in text:
                puzzle = text.split("**题面**")[1].split("**答案**")[0].strip()
                if puzzle.startswith("：") or puzzle.startswith(":"):
                    puzzle = puzzle[1:].strip()
                answer = text.split("**答案**")[1].strip()
                if answer.startswith("：") or answer.startswith(":"):
                    answer = answer[1:].strip()

            # 格式3: "题面：xxx\n答案：xxx"
            elif "题面：" in text and "\n答案：" in text:
                puzzle = text.split("题面：")[1].split("\n答案：")[0].strip()
                answer = text.split("\n答案：")[1].strip()

            # 格式4: 尝试从文本中提取题面和答案
            else:
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue

                    # 寻找题面
                    if not puzzle and ("题面" in line or "**题面**" in line):
                        puzzle = line
                        if "：" in line:
                            puzzle = line.split("：", 1)[1].strip()
                        elif ":" in line:
                            puzzle = line.split(":", 1)[1].strip()
                        # 移除可能的Markdown标记
                        puzzle = puzzle.replace("**", "").replace("*", "").strip()

                    # 寻找答案
                    elif not answer and ("答案" in line or "**答案**" in line):
                        answer = line
                        if "：" in line:
                            answer = line.split("：", 1)[1].strip()
                        elif ":" in line:
                            answer = line.split(":", 1)[1].strip()
                        # 移除可能的Markdown标记
                        answer = answer.replace("**", "").replace("*", "").strip()

                    # 如果找到了题面但还没找到答案，继续寻找
                    elif puzzle and not answer and len(line) > 20:
                        # 可能是答案的开始
                        answer = line

            if puzzle and answer:
                # 清理答案中的多余内容
                if "----" in answer:
                    answer = answer.split("----")[0].strip()
                if "---" in answer:
                    answer = answer.split("---")[0].strip()

                logger.info(f"成功解析谜题: 题面='{puzzle}', 答案='{answer}'")
                return puzzle, answer

            logger.error(f"LLM 返回内容格式错误: {text}")
            return "生成失败", "无法解析 LLM 返回的内容"
        except Exception as e:
            logger.error(f"生成谜题失败: {e}")
            return "生成失败", f"LLM 调用出错: {e}"

    def _build_puzzle_prompt(self) -> str:
        """构建谜题生成的提示词"""
        import random

        # 丰富的主题列表，增加多样性
        themes = [
            # 🔍 人类行为与误导
            "误解他人行为的代价",
            "看似反常实则合理的选择",
            "主动伪装带来的反转",
            "隐瞒真相与道德困境",
            "他人为主角设下的圈套",
            "故意失败的计划",
            "真实动机被遮蔽",
            "道德与规则的冲突",
            # 🧠 心理博弈与控制
            "陷害与自保之间的抉择",
            "信息不对称引发的误判",
            "操控他人感知的行为",
            "主观偏见导致的误解",
            "冷静外表下的激烈动机",
            "以退为进的心理策略",
            # 🧪 现实逻辑与错觉
            "空间结构引发的错觉",
            "物品使用的误导性",
            "因果顺序的错配",
            "隐藏在日常中的意外用途",
            "非典型证据的误导",
            "时间线的巧妙安排",
            # 📍 社会环境与冲突
            "职场中的暗中博弈",
            "公众场合下的隐秘行为",
            "权力结构下的自我保护",
            "日常制度漏洞的利用",
            "面对规则边缘的选择",
            "技术被滥用的后果",
            "资源争夺下的灰色行为",
            # 🧩 特定身份与角色
            "保安不是最了解监控的人",
            "程序员的删除并非错误",
            "清洁工的观察比谁都细致",
            "老师的行为引发质疑",
            "医生做出的不寻常选择",
            "司机的路线似乎有问题",
            "演员的自毁是否另有用意",
            # 🕯 情感错位与人性
            "好意引发的巨大误会",
            "爱被误解为恶意",
            "习惯性行为暴露了真相",
            "为了他人不得不说谎",
            "逃避责任的精心设计",
            "牺牲某人换取整体安全",
        ]

        selected_theme = random.choice(themes)

        prompt = (
            f"你是一个逻辑推理谜题设计师，正在创作一个用于【海龟汤游戏】的原创谜题。\n\n"
            "【目标】：生成一个结构清晰、信息复杂、具备反差感的逻辑谜题，玩家可以通过是/否提问逐步还原真相。答案中解释的所有行为和结果，必须都在题面中有所体现或留有暗示，禁止引入题面未提及的核心行为或结果。谜题在满足以上要求的前提下，应尽可能风格多样、身份多样、行为设定独特、反转机制不重复，避免模板化创作。\n\n"
            "【题面】要求：\n"
            "1~2句话，控制在30字以内，但不能过短或单一；\n"
            "必须包含具体人物 + 至少两个具体细节或行为（如行为+环境、行为+结果、两个动作等）；\n"
            "行为必须具象明确，严禁使用抽象词、形容词、心理或情绪描述；\n"
            "必须包含异常或矛盾要素，能引发为什么？的思考；\n"
            "允许黑暗元素，如陷害、伤害、诱导、自残、掩盖证据等冷峻现实情节；\n"
            "不得使用幻想、梦境、魔法、精神病等设定；\n"
            "使用陈述句，不得使用疑问句或解释语气。\n\n"
            "【答案】要求：\n"
            "不超过200字；\n"
            "真实可实现，具有完整因果逻辑；\n"
            "至少包含两个推理层次或误导点（例如动机误导+情境误导）；\n"
            "不得出现反转在于、真相是、实际上之类的总结或解释语；\n"
            "不要使用说明性句子或教学语气；\n"
            "整体氛围可偏冷峻，但必须具备可还原性，逻辑自洽。\n"
            "答案仅用于解释题面中已有行为与结果，禁止引入题面未包含的额外关键事件或角色。\n\n"
            "参考例子：\n"
            "题面：女演员在试镜前剪断了自己的裙子，却最终被录取。\n"
            "答案：这名女演员事先得知试镜剧本中有一幕裙子被撕裂的情节。她故意提前剪开裙子并精心处理切口，使在表演时裙子自然裂开看起来逼真震撼。评审认为她的表演最具冲击力，毫不犹豫录取了她。她的破坏行为反而让她脱颖而出。\n\n"
            "【输出格式】：\n"
            "题面：XXX\n"
            "答案：XXX\n\n"
            f"请基于「{selected_theme}」主题生成一个完全原创的反转推理谜题。"
        )

        return prompt

    async def _generate_for_storage(self) -> bool:
        """为存储库生成故事"""
        try:
            puzzle, answer = await self.generate_story_with_llm()
            if puzzle and answer and not puzzle.startswith("（"):
                self.local_story_storage.add_story(puzzle, answer)
                logger.info("为存储库生成故事成功")
                return True
            else:
                logger.warning("为存储库生成故事失败")
                return False
        except Exception as e:
            logger.error(f"为存储库生成故事错误: {e}")
            return False

    # ✅ 验证用户推理
    async def verify_user_guess(
            self, user_guess: str, true_answer: str
    ) -> VerificationResult:
        """
        验证用户推理

        Args:
            user_guess: 用户的推理内容
            true_answer: 标准答案

        Returns:
            VerificationResult: 验证结果
        """
        # 获取判断 LLM 提供商
        if self.judge_llm_provider_id:
            provider = self.context.get_provider_by_id(self.judge_llm_provider_id)
            if provider is None:
                logger.error(
                    f"未找到指定的判断 LLM 提供商: {self.judge_llm_provider_id}"
                )
                return VerificationResult("验证失败", "未配置判断 LLM，无法验证")
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                return VerificationResult("验证失败", "未配置 LLM，无法验证")

        # 构建验证提示词
        system_prompt = self._build_verification_system_prompt()
        user_prompt = self._build_verification_user_prompt(user_guess, true_answer)

        try:
            logger.info(f"开始验证用户推理: '{user_guess[:50]}...'")

            llm_resp: LLMResponse = await provider.text_chat(
                prompt=user_prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt=system_prompt,
            )

            text = llm_resp.completion_text.strip()
            logger.info(f"验证 LLM 返回内容: {text}")

            # 解析验证结果
            result = self._parse_verification_result(text)
            return result

        except Exception as e:
            logger.error(f"验证用户推理失败: {e}")
            return VerificationResult("验证失败", f"验证过程中发生错误: {e}")

    def _build_verification_system_prompt(self) -> str:
        """构建验证系统提示词"""
        return """你是一个推理游戏的裁判。玩家需要还原一个隐藏的完整故事，你的任务是根据玩家的陈述与标准答案对比，判断其相似程度。

你的任务是对这两个内容进行比较，判断它们在"核心因果逻辑、关键行为动机、事件结果解释"方面是否一致。

请根据相似程度将玩家推理划分为以下四个等级之一：

1. 完全还原：核心逻辑、动机、因果链、关键行为全部准确复原，无明显偏差；
2. 核心推理正确：主干因果逻辑清晰、关键转折已被识别，但部分细节错误或过程含混；
3. 部分正确：推理中包含部分正确线索或行为判断，但整体逻辑不完整或动机解释偏离；
4. 基本不符：推理内容与真相不符，逻辑错误严重，无法解释题面设定。

请输出以下格式：
等级：{等级}
评价：{一句简评}

注意：
- 当等级为"完全还原"或"核心推理正确"时，表示玩家基本猜中了故事真相。
- 评价应中立简洁，仅反映玩家推理的整体完成度、偏离程度或结构性问题。  
- 严禁直接或间接泄露正确答案中的信息，包括行为动机、情节真相、因果反转等。  
- 不得使用带有暗示性的语句，如"其实…"、"你忽略了…"、"正确是…"等。
- 只输出等级和评价，不要添加其他内容。"""

    def _build_verification_user_prompt(self, user_guess: str, true_answer: str) -> str:
        """构建验证用户提示词"""
        return f"""标准答案是：
{true_answer}

玩家还原的推理是：
{user_guess}

请判断其等级和简评。"""

    def _parse_verification_result(self, text: str) -> VerificationResult:
        """解析验证结果"""
        try:
            # 提取等级和评价
            lines = text.strip().split("\n")
            level = ""
            comment = ""

            for line in lines:
                line = line.strip()
                if line.startswith("等级："):
                    level = line.replace("等级：", "").strip()
                elif line.startswith("评价："):
                    comment = line.replace("评价：", "").strip()

            # 判断是否猜中
            is_correct = level in ["完全还原", "核心推理正确"]

            if not level or not comment:
                # 如果解析失败，尝试从文本中提取信息
                if "完全还原" in text or "核心推理正确" in text:
                    level = "核心推理正确" if "核心推理正确" in text else "完全还原"
                    comment = "推理基本正确，但解析结果格式异常"
                    is_correct = True
                else:
                    level = "验证失败"
                    comment = "无法解析验证结果"
                    is_correct = False

            return VerificationResult(level, comment, is_correct)

        except Exception as e:
            logger.error(f"解析验证结果失败: {e}")
            return VerificationResult("验证失败", f"解析验证结果时发生错误: {e}")

    # ✅ 判断提问的回答方式
    async def judge_question(self, question: str, true_answer: str) -> str:
        """使用 LLM 判断用户提问的回答方式"""

        # 根据配置获取指定的判断 LLM 提供商
        if self.judge_llm_provider_id:
            provider = self.context.get_provider_by_id(self.judge_llm_provider_id)
            if provider is None:
                logger.error(
                    f"未找到指定的判断 LLM 提供商: {self.judge_llm_provider_id}"
                )
                return "（未配置判断 LLM，无法判断）"
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                return "（未配置 LLM，无法判断）"

        prompt = (
            f"海龟汤游戏规则：\n"
            f"1. 故事的完整真相是：{true_answer}\n"
            f'2. 玩家提问或陈述："{question}"\n'
            f"3. 你的任务是判断玩家的说法是否符合真相。\n"
            f"4. 只能回答：\"是\"、\"否\"、\"不重要\"或\"是也不是\"。\n\n"
            f"判定标准：\n"
            f"- \"是\"：\n"
            f"  玩家命中关键事实或行为，且该信息能直接帮助接近真相。缺少部分细节可以忽略，只要不影响推理方向，就判\"是\"。\n"
            f"- \"否\"：\n"
            f"  与真相完全不符，或包含明显错误，会使玩家推理走向错误方向。\n"
            f"- \"不重要\"：\n"
            f"  与故事真相无关，或该信息无法推动推理进展。\n"
            f"- \"是也不是\"：\n"
            f"  玩家命中部分事实，但：\n"
            f"    1) 因果关系不完整或存在偏差；\n"
            f"    2) 表述中包含可能让玩家推理错误的成分；\n"
            f"    3) 忽略了与当前描述直接相关的重要关键点。\n"
            f"  如果只是缺少背景信息，但不影响方向，优先判\"是\"而不是\"是也不是\"。\n\n"
            f"额外说明：\n"
            f"- 不要求玩家一次性说出全部真相。\n"
            f"- 允许玩家只描述真相的一部分，只要方向正确且不会误导，就判\"是\"。\n"
            f"- 对可能误导玩家的陈述要谨慎，宁可判\"是也不是\"。\n"
            f"- 判定时平衡游戏流畅性和推理挑战性。"
        )

        try:
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
                system_prompt='你是一个海龟汤推理游戏的助手。你必须严格按照游戏规则回答，只能回答"是"、"否"、"不重要"或"是也不是"，不能添加任何其他内容。',
            )

            valid_responses = {"是", "否", "是也不是", "不重要"}
            reply = llm_resp.completion_text.strip()
            if reply in valid_responses:
                return reply
            return "你给ai干宕机了或者有什么其他原因，反正他没好好回复，我也不知道为什么（我努力修过代码了）"

        except Exception as e:
            logger.error(f"判断问题失败: {e}")
            return "（判断失败，请重试）"

    # ✅ 生成方向性提示
    def build_allow_list(self, puzzle: str, qa_history: List[Dict[str, str]]) -> List[str]:
        """根据题面和历史问答构建允许在提示中出现的名词列表"""
        import re

        # 汇总文本：题面 + 所有问答
        parts = [puzzle] + [
            f"{item.get('question', '')}{item.get('answer', '')}" for item in qa_history
        ]
        text = "\n".join(parts)

        # 提取连续的中文、字母或数字片段作为候选名词
        tokens = re.findall(r"[A-Za-z0-9\u4e00-\u9fff]+", text)

        allow: List[str] = []
        for token in tokens:
            if not token:
                continue
            # 同义合并：例如“男人A”“嫌疑人A”只保留末尾的大写字母
            m = re.match(r".*([A-Z])$", token)
            if m:
                token = m.group(1)
            if token not in allow:
                allow.append(token)
        return allow

    async def generate_hint(
            self,
            puzzle: str,
            true_answer: str,
            qa_history: List[Dict[str, str]],
            hint_history: List[str],
            allow_list: List[str],
    ) -> str:
        """根据本局已记录的问答与提示生成新的方向性提示"""
        if self.judge_llm_provider_id:
            provider = self.context.get_provider_by_id(self.judge_llm_provider_id)
            if provider is None:
                logger.error(
                    f"未找到指定的判断 LLM 提供商: {self.judge_llm_provider_id}"
                )
                return "（未配置判断 LLM，无法提供提示）"
        else:
            provider = self.context.get_using_provider()
            if provider is None:
                return "（未配置 LLM，无法提供提示）"

        history_text = "\n".join(
            [f"问：{item['question']}\n答：{item['answer']}" for item in qa_history]
        )
        hint_text = "\n".join(hint_history) if hint_history else "（无）"
        allow_text = ", ".join(allow_list) if allow_list else "（无）"
        prompt = (
            "你是\"海龟汤\"提示生成器。你知道完整真相（仅供内部推理，严禁外泄）。\n"
            "材料：\n\n"
            f"* 题面：{puzzle}\n"
            f"* 完整真相（不可外泄）：{true_answer}\n"
            f"* 历史问答：{history_text}\n"
            f"* 历史提示：{hint_text}\n"
            f"* 允许名词 allow_list：{allow_text}（只能使用其中名词，不得创造新名词）\n\n"
            "在心中完成：\n\n"
            "1. 从历史问答归纳：已确认/已否定/不重要/部分正确的信息；\n"
            "2. 用以下维度整理：对象/身份、关系、动机、时间、地点、证据、步骤、先后、条件、规则、误解；\n"
            "3. 选择一个“未探索”或“partial 尚缺”的维度，且与历史提示不重复；\n"
            "4. 只使用 allow_list 中的名词与通用词，生成一句**动作化**的下一步提问方向；\n"
            "5. 禁止泄露真相细节，不得同义改写泄露；不得复述已确认内容。\n\n"
            "输出要求（只输出一句）：\n\n"
            "* 格式：关注【<维度>】：<动词 + allow_list名词/通用词>\n"
            "* 字数 ≤ 22（或 ≤ 24），不得添加解释。"
        )

        try:
            llm_resp: LLMResponse = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                func_tool=None,
                image_urls=[],
            )
            text = llm_resp.completion_text.strip()
            if text.startswith("提示："):
                text = text[len("提示："):]
            return text
        except Exception as e:
            logger.error(f"生成提示失败: {e}")
            return "（生成提示失败，请重试）"

    @filter.command("汤难度")
    async def set_difficulty(self, event: AstrMessageEvent, level: str = ""):
        """设置游戏难度（支持中文名或数字 order）"""
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return
        if self.game_state.is_game_active(group_id):
            yield event.plain_result("当前有活跃游戏，无法修改难度")
            return

        # 尝试按名称匹配
        matched_name = None
        if level in self.difficulty_groups:
            matched_name = level
        elif level.isdigit():
            # 数字 order 匹配
            order_num = int(level)
            for name, conf in self.difficulty_groups.items():
                if conf.get("order") == order_num:
                    matched_name = name
                    break

        if matched_name is None:
            # 按order排序获取难度列表
            sorted_difficulties = sorted(
                self.difficulty_groups.items(),
                key=lambda x: x[1].get("order", 999)
            )
            options = "/".join([f"{name}({conf.get('order')})" for name, conf in sorted_difficulties])
            current = self.group_difficulty.get(group_id, "简单")
            yield event.plain_result(f"可选难度：{options}\n当前难度：{current}")
            return

        self.group_difficulty[group_id] = matched_name
        self._save_difficulty()
        yield event.plain_result(f"难度已设置为 {matched_name}")

    # 🎮 开始游戏指令
    @filter.command("汤")
    async def start_soupai_game(self, event: AstrMessageEvent):
        """开始海龟汤游戏
        
        使用格式: /汤 [题库类型] [题号]
        
        参数说明:
        - 题库类型 (可选): network(网络题库), storage(本地存储库), custom(自定义题库)
        - 题号 (可选): 指定题库中的题目索引，从0开始
        
        示例:
        /汤                    # 使用配置的策略随机获取谜题
        /汤 network           # 从网络题库随机获取谜题
        /汤 storage 5         # 从本地存储库获取第5号谜题
        /汤 custom 2          # 从自定义题库获取第2号谜题
        """
        group_id = event.get_group_id()
        logger.info(f"收到开始游戏指令，群ID: {group_id}")

        if not group_id:
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否已有活跃游戏
        if self.game_state.is_game_active(group_id):
            logger.info(f"群 {group_id} 已有活跃游戏")
            yield event.plain_result(
                "当前群聊已有活跃的海龟汤游戏，请等待游戏结束或使用 /揭晓 结束当前游戏。"
            )
            return

        # 检查是否正在生成谜题
        if group_id in self.generating_games:
            logger.info(f"群 {group_id} 正在生成谜题，忽略重复请求")
            yield event.plain_result("当前有正在生成的谜题，请稍候...")
            return

        self._ensure_story_storages()

        try:
            # 标记正在生成谜题
            self.generating_games.add(group_id)
            logger.info(f"开始为群 {group_id} 生成谜题")

            # 解析命令参数
            message_content = event.message_str.strip()
            args = message_content.split()[1:]  # 去掉命令本身
            
            story = None
            source_type = None
            puzzle_index = None
            
            # 解析参数格式: /汤 <network|storage|custom> <题号>
            # 两个参数都是可选的
            if len(args) >= 1:
                first_arg = args[0].lower()
                
                # 检查第一个参数是否是题库类型
                if first_arg in ["network", "local", "custom"]:
                    source_type = first_arg
                    
                    # 检查是否有第二个参数（题号）
                    if len(args) >= 2:
                        try:
                            puzzle_index = int(args[1])
                        except ValueError:
                            yield event.plain_result("题号必须是数字")
                            self.generating_games.discard(group_id)
                            return
                else:
                    # 第一个参数不是题库类型，可能是题号
                    try:
                        puzzle_index = int(first_arg)
                        # 使用配置的策略作为默认题库类型
                        strategy = self.puzzle_source_strategy
                        if strategy == "network_first":
                            source_type = "network"
                        elif strategy == "local_first":
                            source_type = "local"
                        elif strategy == "custom_first":
                            source_type = "custom"
                        else:  # random
                            source_type = "current"
                    except ValueError:
                        # 第一个参数既不是题库类型也不是题号，使用默认策略随机获取
                        source_type = "current"
            else:
                # 没有参数，使用配置的策略随机获取
                source_type = "current"
            
            # 根据解析的参数获取故事
            if puzzle_index is not None:
                # 指定了题号，从特定题库获取
                story = await self.get_story_by_index(source_type, puzzle_index)
                if not story:
                    yield event.plain_result(f"{source_type}题库中没有第 {puzzle_index} 号题目")
                    self.generating_games.discard(group_id)
                    return
            else:
                # 没有指定题号，根据策略随机获取
                if source_type == "current":
                    # 使用配置的策略获取随机故事
                    strategy = self.puzzle_source_strategy
                    story = await self.get_story_by_strategy(strategy)
                else:
                    # 从指定题库获取随机故事
                    if source_type == "network":
                        story = self.online_story_storage.get_story()
                    elif source_type == "local":
                        story = self.local_story_storage.get_story()
                    elif source_type == "custom":
                        story = self.custom_story_storage.get_story()
                    else:
                        yield event.plain_result("题库类型参数错误，请使用 network/local/custom")
                        self.generating_games.discard(group_id)
                        return


            if not story:
                yield event.plain_result("获取谜题失败，请重试")
                self.generating_games.discard(group_id)
                return

            puzzle, answer = story

            # 检查LLM生成是否失败
            if puzzle == "（无法生成题面，请先配置大语言模型）":
                yield event.plain_result(f"生成谜题失败：{answer}")
                self.generating_games.discard(group_id)
                return


            difficulty = self.group_difficulty.get(group_id, "简单")
            diff_conf = self.difficulty_groups.get(
                difficulty, self.difficulty_groups.get(self._get_fallback_difficulty())
            )

            if self.game_state.start_game(
                    group_id,
                    puzzle,
                    answer,
                    difficulty=difficulty,
                    question_limit=diff_conf.get("question_limit"),
                    question_count=0,
                    verification_before_attempts=0,
                    verification_after_attempts=0,
                    accept_levels=diff_conf["accept_levels"],
                    hint_limit=diff_conf.get("hint_limit"),
                    hint_count=0,
                    verification_before_limit=diff_conf.get("verification_before_limit", 0),
                    verification_after_limit=diff_conf.get("verification_after_limit", 2),
            ):
                extra = ""
                if diff_conf.get("question_limit") is not None:
                    extra = f"\n模式：{difficulty}（{diff_conf['question_limit']} 次提问"
                else:
                    extra = f"\n模式：{difficulty}（无限提问"

                hint_limit = diff_conf.get("hint_limit")
                if hint_limit == 0:
                    extra += "，无提示）"
                elif hint_limit is not None:
                    extra += f"，{hint_limit} 次提示）"
                else:
                    extra += "，无限提示）"

                yield event.plain_result(
                    f"🎮 海龟汤游戏开始！{extra}\n\n📖 题面：{puzzle}\n\n💡 请直接提问或陈述，我会回答：是、否、是也不是\n💡 输入 /提示 可以获取方向性提示\n💡 输入 /验证 <答案> 可以验证答案是否正确\n💡 输入 /揭晓 可以查看完整故事"
                )

                # 启动会话控制
                await self._start_game_session(event, group_id, answer)
            else:
                yield event.plain_result("游戏启动失败，请重试")

            # 移除生成状态，因为故事已经准备完成
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 故事准备完成，移除生成状态")

        except Exception as e:
            logger.error(f"启动游戏失败: {e}")
            # 发生异常时也要移除生成状态
            self.generating_games.discard(group_id)
            logger.info(f"群 {group_id} 启动游戏异常，移除生成状态")
            yield event.plain_result(f"启动游戏时发生错误：{e}")

    # 🔍 揭晓指令
    @filter.command("揭晓")
    async def reveal_answer(self, event: AstrMessageEvent):
        """揭晓答案"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("海龟汤游戏只能在群聊中进行哦~")
            return

        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            # 阻止事件继续传播，避免被会话控制系统重复处理
            await event.block()
            return
        game = self.game_state.get_game(group_id)
        if not game:
            yield event.plain_result(
                "当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏。"
            )
            return

        answer = game["answer"]
        puzzle = game["puzzle"]

        # 发送完整的揭晓信息
        yield event.plain_result(
            f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！"
        )

        # 结束游戏
        self.game_state.end_game(group_id)
        logger.info(f"游戏已结束，群ID: {group_id}")


    # ⚡ 异步后台任务：LLM 问答判断（不阻塞会话控制）
    async def _llm_judge_task(
        self, event, group_id: str, command_part: str, current_answer: str,
        question_limit, controller
    ):
        """后台执行 LLM 问答判断，完成后自动发送结果"""
        game = self.game_state.get_game(group_id)
        try:
            reply = await self.judge_question(command_part, current_answer)

            # 重新获取 game（可能已被结束）
            game = self.game_state.get_game(group_id)
            if not game:
                return

            # 记录提问和回答
            history = game.setdefault("qa_history", [])
            history.append({"question": command_part, "answer": reply})

            # 更新问题计数
            game["question_count"] = game.get("question_count", 0) + 1

            # 组装回复
            if self.reply_mode == "merge" and question_limit is not None:
                body = (
                    f"❓ 问题：{command_part}\n"
                    f"💬 回答：{reply}（{game['question_count']}/{question_limit}）"
                )
            else:
                body = reply

            await self._send_reply(event, body)

            # 有限次数且用尽时提示进入验证
            if question_limit is not None and game["question_count"] >= question_limit:
                await self._send_reply(
                    event,
                    "❗️提问次数已用完，将进入验证环节。请使用 /验证 <推理内容> 进行验证。",
                )

            # 重置超时
            controller.keep(timeout=self.game_timeout, reset_timeout=True)
        except Exception as e:
            logger.error(f"后台问答判断失败: {e}")
        finally:
            pass

    # ⚡ 异步后台任务：LLM 提示生成（不阻塞会话控制）
    async def _llm_hint_task(self, event, group_id: str, controller):
        """后台执行提示生成，完成后自动发送结果"""
        try:
            if not group_id:
                return
            game = self.game_state.get_game(group_id)
            if not game:
                await self._send_reply(event, "当前没有活跃的海龟汤游戏")
                return

            hint_limit = game.get("hint_limit")
            hint_count = game.get("hint_count", 0)
            if hint_limit == 0:
                await self._send_reply(event, "当前难度不可使用提示")
                return
            if hint_limit is not None and hint_count >= hint_limit:
                await self._send_reply(event, "提示次数已用完")
                return

            qa_history = game.get("qa_history", [])
            if not qa_history:
                await self._send_reply(event, "请先进行提问后再请求提示")
                return

            hint_history = game.get("hint_history", [])
            allow_list = self.build_allow_list(game["puzzle"], qa_history)
            hint = await self.generate_hint(
                game["puzzle"], game["answer"], qa_history, hint_history, allow_list
            )
            game["hint_count"] = hint_count + 1
            game["hint_history"] = hint_history + [hint]
            suffix = ""
            if hint_limit is not None:
                suffix = f"（{game['hint_count']}/{hint_limit}）"
            await self._send_reply(event, f"提示：{hint}{suffix}")
            controller.keep(timeout=self.game_timeout, reset_timeout=True)
        except Exception as e:
            logger.error(f"后台提示生成失败: {e}")
            await self._send_reply(event, f"生成提示时发生错误：{e}")



    # 🎯 游戏会话控制
    async def _start_game_session(
            self, event: AstrMessageEvent, group_id: str, answer: str
    ):
        """启动游戏会话控制"""
        try:

            @session_waiter(timeout=self.game_timeout, record_history_chains=False)
            async def game_session_waiter(
                    controller: SessionController, event: AstrMessageEvent
            ):
                try:
                    # 从游戏状态获取答案，确保变量可用
                    game = self.game_state.get_game(group_id)
                    if not game:
                        return
                    current_answer = game["answer"]
                    user_input = event.message_str.strip()
                    logger.info(f"会话控制收到消息: '{user_input}'")

                    # 允许在会话中使用 /汤状态 和 /强制结束 指令
                    if user_input in ("/汤状态", "汤状态"):
                        await self._handle_game_status_in_session(event, group_id)
                        return

                    if user_input in ("/强制结束", "强制结束"):
                        await self._handle_force_end_in_session(event, group_id)
                        if not self.game_state.is_game_active(group_id):
                            controller.stop()
                        return

                    normalized_input = user_input.lstrip("/").strip()
                    if normalized_input == "查看":
                        await self._handle_view_history_in_session(event, group_id)
                        controller.keep(timeout=self.game_timeout, reset_timeout=True)
                        return
                    if user_input in ("/提示", "提示"):
                        asyncio.create_task(self._llm_hint_task(event, group_id, controller))
                        return
                    # 特殊处理 /验证 指令
                    if user_input.startswith("/验证"):
                        import re

                        match = re.match(r"^/验证\s*(.+)$", user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            # 手动调用验证函数
                            await self._handle_verification_in_session(
                                event, user_guess, current_answer
                            )
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                controller.stop()
                                return
                        else:
                            await event.send(
                                event.plain_result(
                                    "请输入要验证的内容，例如：/验证 他是她的父亲"
                                )
                            )
                        return
                    elif user_input.startswith("验证"):
                        import re

                        match = re.match(r"^验证\s*(.+)$", user_input)
                        if match:
                            user_guess = match.group(1).strip()
                            # 手动调用验证函数
                            await self._handle_verification_in_session(
                                event, user_guess, current_answer
                            )
                            # 检查游戏是否已结束（用户可能猜中了）
                            if not self.game_state.is_game_active(group_id):
                                controller.stop()
                                return
                        else:
                            await event.send(
                                event.plain_result(
                                    "请输入要验证的内容，例如：验证 他是她的父亲"
                                )
                            )
                        return
                    # 特殊处理 /揭晓 指令
                    if user_input == "揭晓":
                        # 获取游戏信息并发送答案
                        game = self.game_state.get_game(group_id)
                        if game:
                            answer = game["answer"]
                            puzzle = game["puzzle"]
                            await event.send(
                                event.plain_result(
                                    f"🎯 海龟汤游戏结束！\n\n📖 题面：{puzzle}\n📖 完整故事：{answer}\n\n感谢参与游戏！"
                                )
                            )
                            self.game_state.end_game(group_id)
                        controller.stop()
                        return
                    # Step 1: 检查是否是 /开头的命令，如果是则忽略，让指令处理器处理
                    if user_input.startswith("/"):
                        # 不处理指令，让事件继续传播到指令处理器
                        return
                    # Step 2: 检查是否 @了 bot，只有@bot的消息才触发问答判断
                    if not self._is_at_bot(event):
                        return
                    # Step 3: 是@bot的自然语言提问，异步后台处理（不阻塞会话控制）
                    game = self.game_state.get_game(group_id)
                    question_limit = game.get("question_limit") if game else None
                    question_count = game.get("question_count", 0) if game else 0
                    if question_limit is not None and question_count >= question_limit:
                        # 从游戏配置获取实际验证次数限制
                        v_before_limit = game.get("verification_before_limit", 0)
                        v_after_limit = game.get("verification_after_limit", 2)
                        q_limit = game.get("question_limit")
                        q_count = game.get("question_count", 0)
                        if q_limit is not None and q_count >= q_limit:
                            remaining = v_after_limit - game.get("verification_after_attempts", 0) if v_after_limit != -1 else None
                        elif v_before_limit > 0:
                            remaining = v_before_limit - game.get("verification_before_attempts", 0)
                        elif v_after_limit != -1:
                            remaining = v_after_limit - game.get("verification_after_attempts", 0)
                        else:
                            remaining = None
                        await event.send(
                            event.plain_result(
                                f"❗️提问次数已用完，请使用 /验证 进行猜测（剩余{remaining}次验证机会）"
                            )
                        )
                        return

                    # 处理游戏问答消息
                    command_part = user_input.strip()
                    logger.info(f"处理游戏问答消息: '{command_part}'")

                    # 后台执行 LLM 判断
                    asyncio.create_task(
                        self._llm_judge_task(
                            event, group_id, command_part, current_answer,
                            question_limit, controller
                        )
                    )

                    # 重置超时（后台任务完成后会再次重置）
                    controller.keep(timeout=self.game_timeout, reset_timeout=True)

                except Exception as e:
                    logger.error(f"会话控制内部错误: {e}")
                    await event.send(event.plain_result(f"游戏处理过程中发生错误：{e}"))
                    # 如果发生错误，结束游戏
                    self.game_state.end_game(group_id)
                    controller.stop()

            try:
                # 使用群 ID 限制会话范围，避免多个群并发时互相触发
                await game_session_waiter(event, session_filter=GroupSessionFilter(group_id))
            except TimeoutError:
                game = self.game_state.get_game(group_id)
                if game:
                    await event.send(
                        event.plain_result(
                            f"⏰ 游戏超时！\n\n📖 完整故事：{game['answer']}\n\n游戏结束！"
                        )
                    )
                    self.game_state.end_game(group_id)
            except Exception as e:
                logger.error(f"游戏会话错误: {e}")
                await event.send(event.plain_result(f"游戏过程中发生错误：{e}"))
                self.game_state.end_game(group_id)
        except Exception as e:
            logger.error(f"启动游戏会话失败: {e}")
            await event.send(event.plain_result(f"启动游戏会话失败：{e}"))

    def _is_at_bot(self, event: AstrMessageEvent) -> bool:
        """检查消息是否@了bot"""

        bot_id = str(event.get_self_id())
        for comp in event.message_obj.message:
            if isinstance(comp, At) and str(comp.qq) == bot_id:
                return True
        return False

    async def get_story_by_strategy(self, strategy: str) -> Optional[Tuple[str, str]]:
        """根据策略获取故事，返回 (puzzle, answer) 或 None"""
        import random

        self._ensure_story_storages()

        if strategy == "network_first":
            # 策略1：优先网络题库 -> 本地存储库 -> 自定义题库 -> LLM现场生成

            # 1. 检查网络题库
            story = self.online_story_storage.get_story()
            if story:
                return story

            # 2. 检查本地存储库
            story = self.local_story_storage.get_story()
            if story:
                return story

            # 3. 检查自定义题库
            story = self.custom_story_storage.get_story()
            if story:
                return story

            # 4. LLM现场生成
            return await self.generate_story_with_llm()

        elif strategy == "local_first":
            # 策略2：优先本地存储库 -> 网络题库 -> 自定义题库 -> LLM现场生成

            # 1. 检查本地存储库
            story = self.local_story_storage.get_story()
            if story:
                return story

            # 2. 检查网络题库
            story = self.online_story_storage.get_story()
            if story:
                return story

            # 3. 检查自定义题库
            story = self.custom_story_storage.get_story()
            if story:
                return story

            # 4. LLM现场生成
            return await self.generate_story_with_llm()

        elif strategy == "custom_first":
            # 策略3：优先自定义题库 -> 本地存储库 -> 网络题库 -> LLM现场生成

            # 1. 检查自定义题库
            story = self.custom_story_storage.get_story()
            if story:
                return story

            # 2. 检查本地存储库
            story = self.local_story_storage.get_story()
            if story:
                return story

            # 3. 检查网络题库
            story = self.online_story_storage.get_story()
            if story:
                return story

            # 4. LLM现场生成
            return await self.generate_story_with_llm()

        elif strategy == "random":
            # 策略3：随机选择网络题库、本地存储库或自定义题库，失败时使用LLM现场生成

            # 随机决定这次从哪个题库获取
            choice = random.choice(["network", "local", "custom"])
            if choice == "network":
                # 参考策略1的网络题库逻辑
                story = self.online_story_storage.get_story()
                if story:
                    return story

                story = self.local_story_storage.get_story()
                if story:
                    return story

                story = self.custom_story_storage.get_story()
                if story:
                    return story

                return await self.generate_story_with_llm()
            elif choice == "local":
                # 参考策略2的本地存储库逻辑
                story = self.local_story_storage.get_story()
                if story:
                    return story

                story = self.online_story_storage.get_story()
                if story:
                    return story

                story = self.custom_story_storage.get_story()
                if story:
                    return story

                return await self.generate_story_with_llm()
            else:  # custom
                # 优先自定义题库
                story = self.custom_story_storage.get_story()
                if story:
                    return story

                story = self.local_story_storage.get_story()
                if story:
                    return story

                story = self.online_story_storage.get_story()
                if story:
                    return story

                return await self.generate_story_with_llm()

        return None

    async def get_story_by_index(self, source_type: str, index: int) -> Optional[Tuple[str, str]]:
        """根据索引获取特定故事
        
        Args:
            source_type: "network" - 网络题库, "current" - 当前策略题库, "custom" - 自定义题库
            index: 题目索引（从0开始）
        
        Returns:
            (puzzle, answer) 或 None
        """
        self._ensure_story_storages()
        
        if source_type == "network":
            # 从网络题库获取指定索引的故事
            if index < 0 or index >= len(self.online_story_storage.stories):
                return None
            
            story = self.online_story_storage.stories[index]
            # 标记为已使用
            with self.online_story_storage.lock:
                self.online_story_storage.used_indexes.add(index)
                self.online_story_storage.save_usage_record()
            
            logger.info(f"从网络题库获取指定故事，索引: {index}")
            return story["puzzle"], story["answer"]
            
        elif source_type == "custom":
            # 从自定义题库获取指定索引的故事
            if index < 0 or index >= len(self.custom_story_storage.stories):
                return None
            
            story = self.custom_story_storage.stories[index]
            # 标记为已使用
            with self.custom_story_storage.lock:
                self.custom_story_storage.used_indexes.add(index)
                self.custom_story_storage.save_usage_record()
            
            logger.info(f"从自定义题库获取指定故事，索引: {index}")
            return story["puzzle"], story["answer"]
            
        elif source_type == "current":
            # 根据当前策略获取指定索引的故事
            strategy = self.puzzle_source_strategy
            
            if strategy == "network_first":
                # 优先检查网络题库
                if index < len(self.online_story_storage.stories):
                    story = self.online_story_storage.stories[index]
                    with self.online_story_storage.lock:
                        self.online_story_storage.used_indexes.add(index)
                        self.online_story_storage.save_usage_record()
                    logger.info(f"从网络题库获取指定故事，索引: {index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查本地存储库
                local_index = index - len(self.online_story_storage.stories)
                if local_index >= 0 and local_index < len(self.local_story_storage.stories):
                    story = self.local_story_storage.stories[local_index]
                    with self.local_story_storage.lock:
                        self.local_story_storage.used_indexes.add(local_index)
                        self.local_story_storage.save_usage_record()
                    logger.info(f"从本地存储库获取指定故事，索引: {local_index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查自定义题库
                custom_index = local_index - len(self.local_story_storage.stories)
                if custom_index >= 0 and custom_index < len(self.custom_story_storage.stories):
                    story = self.custom_story_storage.stories[custom_index]
                    with self.custom_story_storage.lock:
                        self.custom_story_storage.used_indexes.add(custom_index)
                        self.custom_story_storage.save_usage_record()
                    logger.info(f"从自定义题库获取指定故事，索引: {custom_index}")
                    return story["puzzle"], story["answer"]
                
                # 超出范围，返回None
                return None
                
            elif strategy == "local_first":
                # 优先检查本地存储库
                if index < len(self.local_story_storage.stories):
                    story = self.local_story_storage.stories[index]
                    with self.local_story_storage.lock:
                        self.local_story_storage.used_indexes.add(index)
                        self.local_story_storage.save_usage_record()
                    logger.info(f"从本地存储库获取指定故事，索引: {index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查网络题库
                network_index = index - len(self.local_story_storage.stories)
                if network_index >= 0 and network_index < len(self.online_story_storage.stories):
                    story = self.online_story_storage.stories[network_index]
                    with self.online_story_storage.lock:
                        self.online_story_storage.used_indexes.add(network_index)
                        self.online_story_storage.save_usage_record()
                    logger.info(f"从网络题库获取指定故事，索引: {network_index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查自定义题库
                custom_index = network_index - len(self.online_story_storage.stories)
                if custom_index >= 0 and custom_index < len(self.custom_story_storage.stories):
                    story = self.custom_story_storage.stories[custom_index]
                    with self.custom_story_storage.lock:
                        self.custom_story_storage.used_indexes.add(custom_index)
                        self.custom_story_storage.save_usage_record()
                    logger.info(f"从自定义题库获取指定故事，索引: {custom_index}")
                    return story["puzzle"], story["answer"]
                
                # 超出范围，返回None
                return None
                
            elif strategy == "custom_first":
                # 优先检查自定义题库
                if index < len(self.custom_story_storage.stories):
                    story = self.custom_story_storage.stories[index]
                    with self.custom_story_storage.lock:
                        self.custom_story_storage.used_indexes.add(index)
                        self.custom_story_storage.save_usage_record()
                    logger.info(f"从自定义题库获取指定故事，索引: {index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查本地存储库
                local_index = index - len(self.custom_story_storage.stories)
                if local_index >= 0 and local_index < len(self.local_story_storage.stories):
                    story = self.local_story_storage.stories[local_index]
                    with self.local_story_storage.lock:
                        self.local_story_storage.used_indexes.add(local_index)
                        self.local_story_storage.save_usage_record()
                    logger.info(f"从本地存储库获取指定故事，索引: {local_index}")
                    return story["puzzle"], story["answer"]
                
                # 然后检查网络题库
                network_index = local_index - len(self.local_story_storage.stories)
                if network_index >= 0 and network_index < len(self.online_story_storage.stories):
                    story = self.online_story_storage.stories[network_index]
                    with self.online_story_storage.lock:
                        self.online_story_storage.used_indexes.add(network_index)
                        self.online_story_storage.save_usage_record()
                    logger.info(f"从网络题库获取指定故事，索引: {network_index}")
                    return story["puzzle"], story["answer"]
                
                # 超出范围，返回None
                return None
                
            elif strategy == "random":
                # 对于随机策略，我们无法准确知道索引对应哪个题库
                # 这里我们按顺序检查：先网络题库，再本地存储库，最后自定义题库
                if index < len(self.online_story_storage.stories):
                    story = self.online_story_storage.stories[index]
                    with self.online_story_storage.lock:
                        self.online_story_storage.used_indexes.add(index)
                        self.online_story_storage.save_usage_record()
                    logger.info(f"从网络题库获取指定故事，索引: {index}")
                    return story["puzzle"], story["answer"]
                
                local_index = index - len(self.online_story_storage.stories)
                if local_index >= 0 and local_index < len(self.local_story_storage.stories):
                    story = self.local_story_storage.stories[local_index]
                    with self.local_story_storage.lock:
                        self.local_story_storage.used_indexes.add(local_index)
                        self.local_story_storage.save_usage_record()
                    logger.info(f"从本地存储库获取指定故事，索引: {local_index}")
                    return story["puzzle"], story["answer"]
                
                custom_index = local_index - len(self.local_story_storage.stories)
                if custom_index >= 0 and custom_index < len(self.custom_story_storage.stories):
                    story = self.custom_story_storage.stories[custom_index]
                    with self.custom_story_storage.lock:
                        self.custom_story_storage.used_indexes.add(custom_index)
                        self.custom_story_storage.save_usage_record()
                    logger.info(f"从自定义题库获取指定故事，索引: {custom_index}")
                    return story["puzzle"], story["answer"]
                
                # 超出范围，返回None
                return None
        
        return None

    async def _handle_game_status_in_session(
            self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理游戏状态查询逻辑"""
        try:

            if self.game_state.is_game_active(group_id):
                game = self.game_state.get_game(group_id)
                difficulty = game.get("difficulty", self._get_fallback_difficulty())
                question_count = game.get("question_count", 0)
                question_limit = game.get("question_limit")
                hint_count = game.get("hint_count", 0)
                hint_limit = game.get("hint_limit")

                question_info = f"{question_count}/{question_limit}" if question_limit is not None else f"{question_count}/∞"
                hint_info = f"{hint_count}/{hint_limit}" if hint_limit is not None else ("不可用" if hint_limit == 0 else "无限")

                await event.send(
                    event.plain_result(
                        f"🎮 当前有活跃的海龟汤游戏\n📖 题面：{game['puzzle']}\n🎯 难度：{difficulty}\n❓ 提问：{question_info}\n💡 提示：{hint_info}"
                    )
                )
            else:
                await event.send(
                    event.plain_result(
                        "🎮 当前没有活跃的海龟汤游戏\n💡 使用 /汤 开始新游戏"
                    )
                )

        except Exception as e:
            logger.error(f"会话游戏状态查询失败: {e}")
            await event.send(event.plain_result(f"查询游戏状态时发生错误：{e}"))

    async def _handle_force_end_in_session(
            self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理强制结束游戏逻辑"""
        try:

            if self.game_state.end_game(group_id):
                await event.send(event.plain_result("✅ 已强制结束当前海龟汤游戏"))
            else:
                await event.send(event.plain_result("❌ 当前没有活跃的游戏需要结束"))

        except Exception as e:
            logger.error(f"会话强制结束失败: {e}")
            await event.send(event.plain_result(f"强制结束游戏时发生错误：{e}"))

    async def _handle_view_history_in_session(
            self, event: AstrMessageEvent, group_id: str
    ):
        """在会话控制中处理查看历史记录逻辑"""
        try:


            game = self.game_state.get_game(group_id)
            if not game:
                await event.send(event.plain_result("无法获取游戏状态"))
                return

            history = game.get("qa_history", [])

            if not history:
                await event.send(event.plain_result("目前还没有人提问哦~"))
                return

            lines = ["📋 提问记录："]
            for idx, item in enumerate(history, 1):
                lines.append(f"{idx}. 问：{item['question']}\n   答：{item['answer']}")

            response = "\n".join(lines)
            await event.send(event.plain_result(response))

        except Exception as e:
            logger.error(f"会话查看历史失败: {e}")
            await event.send(event.plain_result(f"查看历史记录时发生错误：{e}"))

    async def _build_hint_result(
            self, event: AstrMessageEvent, group_id: str
    ) -> Optional[MessageEventResult]:
        """生成提示结果，供指令或会话控制调用"""
        if not group_id:
            return event.plain_result("提示功能只能在群聊中使用")

        game = self.game_state.get_game(group_id)
        if not game:
            return event.plain_result("当前没有活跃的海龟汤游戏")

        hint_limit = game.get("hint_limit")
        hint_count = game.get("hint_count", 0)
        if hint_limit == 0:
            return event.plain_result("当前难度不可使用提示")
        if hint_limit is not None and hint_count >= hint_limit:
            return event.plain_result("提示次数已用完")

        qa_history = game.get("qa_history", [])
        if not qa_history:
            return event.plain_result("请先进行提问后再请求提示")

        hint_history = game.get("hint_history", [])
        allow_list = self.build_allow_list(game["puzzle"], qa_history)

        hint = await self.generate_hint(
            game["puzzle"], game["answer"], qa_history, hint_history, allow_list
        )
        game["hint_count"] = hint_count + 1
        game["hint_history"] = hint_history + [hint]
        suffix = ""
        if hint_limit is not None:
            suffix = f"（{game['hint_count']}/{hint_limit}）"
        return event.plain_result(f"提示：{hint}{suffix}")

    async def _handle_verification_in_session(
            self, event: AstrMessageEvent, user_guess: str, answer: str
    ):
        """在会话控制中处理验证逻辑"""
        try:
            group_id = event.get_group_id()
            game = self.game_state.get_game(group_id) if group_id else None
            
            if not game:
                return
            
            # 获取难度配置
            difficulty = game.get("difficulty", self._get_fallback_difficulty())
            diff_conf = self.difficulty_groups.get(
                difficulty, self.difficulty_groups.get(self._get_fallback_difficulty())
            )
            
            # 获取验证次数配置
            verification_before_limit = diff_conf.get("verification_before_limit", 0)
            verification_after_limit = diff_conf.get("verification_after_limit", 2)
            
            # 获取当前状态（双计数器：耗尽前/耗尽后互不干扰）
            question_limit = game.get("question_limit")
            question_count = game.get("question_count", 0)
            verification_before_attempts = game.get("verification_before_attempts", 0)
            verification_after_attempts = game.get("verification_after_attempts", 0)
            
            # 判断是否在提问耗尽前
            is_before_limit = question_limit is None or question_count < question_limit
            
            # 检查验证次数限制
            if is_before_limit and verification_before_limit == 0:
                # 提问耗尽前验证次数为0，直接使用耗尽后总次数
                if verification_after_attempts >= verification_after_limit and verification_after_limit != -1:
                    await self._send_reply(
                        event,
                        f"❗️验证次数已用完（{verification_after_attempts}/{verification_after_limit}）"
                    )
                    return
            elif is_before_limit and verification_before_limit > 0:
                # 提问耗尽前，检查耗尽前次数（独立计数器，不重置）
                if verification_before_attempts >= verification_before_limit:
                    await self._send_reply(
                        event,
                        f"💡 提问耗尽前验证次数已用完（{verification_before_limit}次），提问耗尽后将进入耗尽后计数。"
                    )
                    return
            else:
                # 提问耗尽后，检查耗尽后次数（独立计数器）
                if verification_after_attempts >= verification_after_limit and verification_after_limit != -1:
                    await self._send_reply(
                        event,
                        f"❗️验证次数已用完（{verification_after_attempts}/{verification_after_limit}）"
                    )
                    return
            
            # 验证用户推理
            result = await self.verify_user_guess(user_guess, answer)
            
            accept_levels = game.get("accept_levels", ["完全还原", "核心推理正确"])
            is_correct = result.level in accept_levels

            # 返回验证结果
            response = f"等级：{result.level}\n评价：{result.comment}"
            await self._send_reply(event, response)

            if is_correct:
                await self._send_reply(
                    event,
                    f"🎉 恭喜！你猜中了！\n\n📖 完整故事：{answer}\n\n游戏结束！"
                )
                if group_id:
                    self.game_state.end_game(group_id)
                return

            # 验证未通过，更新对应阶段的计数器
            if is_before_limit:
                # 提问耗尽前：递增耗尽前计数器
                game["verification_before_attempts"] = verification_before_attempts + 1
                current_attempts = game["verification_before_attempts"]
                limit = verification_before_limit if verification_before_limit > 0 else verification_after_limit
            else:
                # 提问耗尽后：递增耗尽后计数器
                game["verification_after_attempts"] = verification_after_attempts + 1
                current_attempts = game["verification_after_attempts"]
                limit = verification_after_limit
            
            # 计算剩余次数（None 表示无限）
            remaining = None
            if limit != -1:
                remaining = limit - current_attempts
            
            if remaining is None:
                # 无限次数（-1）：继续游戏，不结束
                await self._send_reply(
                    event,
                    f"❌ 验证未通过，请继续尝试。"
                )
            elif remaining > 0:
                await self._send_reply(
                    event,
                    f"❌ 验证未通过，你还有 {remaining} 次机会。"
                )
            else:
                # remaining == 0：验证次数用尽才结束
                await self._send_reply(
                    event,
                    f"❌ 验证未通过。\n\n📖 完整故事：{answer}\n\n游戏结束！"
                )
                self.game_state.end_game(group_id)

        except Exception as e:
            logger.error(f"会话验证失败: {e}")
            await event.send(event.plain_result(f"验证过程中发生错误：{e}"))
    # 📊 游戏状态查询
    @filter.command("汤状态")
    async def check_game_status(self, event: AstrMessageEvent):
        """查看当前游戏状态"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.is_game_active(group_id):
            game = self.game_state.get_game(group_id)
            difficulty = game.get("difficulty", self._get_fallback_difficulty())
            question_count = game.get("question_count", 0)
            question_limit = game.get("question_limit")
            hint_count = game.get("hint_count", 0)
            hint_limit = game.get("hint_limit")

            question_info = f"{question_count}/{question_limit}" if question_limit is not None else f"{question_count}/∞"
            hint_info = f"{hint_count}/{hint_limit}" if hint_limit is not None else ("不可用" if hint_limit == 0 else "无限")

            # 获取验证次数信息
            verification_before_limit = game.get("verification_before_limit", 0)
            verification_after_limit = game.get("verification_after_limit", 2)
            verification_before_attempts = game.get("verification_before_attempts", 0)
            verification_after_attempts = game.get("verification_after_attempts", 0)
            
            # 判断是否在提问耗尽前
            is_before_limit = question_limit is None or question_count < question_limit
            
            # 计算验证次数显示（双计数器）
            if verification_before_limit == 0:
                # 总次数（耗尽后）模式
                if verification_after_limit == -1:
                    verification_info = f"{verification_after_attempts}/∞"
                else:
                    verification_info = f"{verification_after_attempts}/{verification_after_limit}"
            elif is_before_limit:
                # 提问耗尽前模式
                verification_info = f"{verification_before_attempts}/{verification_before_limit}"
            else:
                # 提问耗尽后模式
                if verification_after_limit == -1:
                    verification_info = f"{verification_after_attempts}/∞"
                else:
                    verification_info = f"{verification_after_attempts}/{verification_after_limit}"

            yield event.plain_result(
                f"🎮 当前有活跃的海龟汤游戏\n📖 题面：{game['puzzle']}\n🎯 难度：{difficulty}\n❓ 提问：{question_info}\n💡 提示：{hint_info}\n🔍 验证：{verification_info}"
            )
        else:
            yield event.plain_result(
                "🎮 当前没有活跃的海龟汤游戏\n💡 使用 /汤 开始新游戏"
            )

    @filter.command("查看")
    async def view_question_history(self, event: AstrMessageEvent):
        """查看当前已提问的问题及回答"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return
        if not self.game_state.is_game_active(group_id):
            yield event.plain_result("当前没有活跃的海龟汤游戏")
            return
        game = self.game_state.get_game(group_id)
        history = game.get("qa_history", []) if game else []
        if not history:
            yield event.plain_result("目前还没有人提问哦~")
            return
        lines = ["📋 提问记录："]
        for idx, item in enumerate(history, 1):
            lines.append(f"{idx}. 问：{item['question']}\n   答：{item['answer']}")
        yield event.plain_result("\n".join(lines))

    # 🆘 强制结束游戏（管理员功能）
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("强制结束")
    async def force_end_game(self, event: AstrMessageEvent):
        """强制结束当前游戏（仅管理员）"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("此功能只能在群聊中使用")
            return

        if self.game_state.end_game(group_id):
            yield event.plain_result("✅ 已强制结束当前海龟汤游戏")
        else:
            yield event.plain_result("❌ 当前没有活跃的游戏需要结束")

    # 📚 备用故事管理指令
    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用开始")
    async def start_backup_generation(self, event: AstrMessageEvent):
        """开始生成备用故事（仅管理员）"""

        if self.auto_generating:
            yield event.plain_result("⚠️ 备用故事生成已在运行中")
            return

        # 检查存储库是否已满
        self._ensure_story_storages()
        storage_info = self.local_story_storage.get_storage_info()
        if storage_info["available"] <= 0:
            yield event.plain_result("⚠️ 存储库已满，无法生成更多故事")
            return

        self.auto_generating = True
        asyncio.create_task(self._auto_generate_loop())
        yield event.plain_result(
            f"✅ 开始生成备用故事，存储库状态: {storage_info['total']}/{storage_info['max_size']}"
        )

    # 🔒 全局指令拦截器 - 当正在生成时提醒用户
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def global_command_interceptor(self, event: AstrMessageEvent):
        """全局指令拦截器，当正在生成备用故事时提醒用户"""
        # 检查是否有活跃游戏，如果有活跃游戏，不在这里处理
        group_id = event.get_group_id()
        if group_id and self.game_state.is_game_active(group_id):
            # 有活跃游戏，让会话控制处理
            return

        # 如果正在生成备用故事，且不是 /备用结束 指令，则提醒用户
        if self.auto_generating:
            user_input = event.message_str.strip()
            # 只拦截非本插件的指令，避免阻断自己的指令
            if (
                    user_input.startswith("/")
                    and not user_input.startswith("/备用结束")
                    and not user_input.startswith("/汤")
                    and not user_input.startswith("/揭晓")
                    and not user_input.startswith("/验证")
                    and not user_input.startswith("/汤状态")
                    and not user_input.startswith("/强制结束")
                    and not user_input.startswith("/备用开始")
                    and not user_input.startswith("/备用状态")
                    and not user_input.startswith("/汤配置")
                    and not user_input.startswith("/重置题库")
                    and not user_input.startswith("/题库详情")
                    and not user_input.startswith("/查看")
                    and not user_input.startswith("/提示")
            ):
                yield event.plain_result(
                    "⚠️ 系统正在生成备用故事，请稍后再试或使用 /备用结束 停止生成"
                )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("备用结束")
    async def stop_backup_generation(self, event: AstrMessageEvent):
        """停止生成备用故事（仅管理员）"""

        if not self.auto_generating:
            yield event.plain_result("⚠️ 备用故事生成未在运行")
            return

        self.auto_generating = False
        yield event.plain_result("✅ 已停止生成备用故事，正在完成当前生成...")

    @filter.command("备用状态")
    async def check_backup_status(self, event: AstrMessageEvent):
        """查看备用故事状态"""
        self._ensure_story_storages()
        storage_info = self.local_story_storage.get_storage_info()
        online_info = self.online_story_storage.get_storage_info()
        status = "🟢 运行中" if self.auto_generating else "🔴 已停止"


        # 检查存储库是否已满
        storage_full_warning = ""
        if storage_info["available"] <= 0:
            storage_full_warning = "\n⚠️ 本地存储库已满，自动生成已停止"

        message = (
            f"📚 备用故事状态：\n"
            f"• 生成状态：{status}\n"
            f"• 本地存储库：{storage_info['total']}/{storage_info['max_size']}\n"
            f"• 已使用题目：{storage_info['used']}\n"
            f"• 剩余题目：{storage_info['remaining']}\n"
            f"• 可用空间：{storage_info['available']}\n"
            f"• 网络题库：{online_info['total']} 个 (已用: {online_info['used']}, 剩余: {online_info['available']})\n"
            f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00{storage_full_warning}"
        )

        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置题库")
    async def reset_story_storage(self, event: AstrMessageEvent):
        """重置题库使用记录（仅管理员）"""

        self._ensure_story_storages()

        # 重置网络题库使用记录
        self.online_story_storage.reset_usage()
        online_info = self.online_story_storage.get_storage_info()

        # 重置本地存储库使用记录
        self.local_story_storage.reset_usage()
        local_info = self.local_story_storage.get_storage_info()


        message = (
            f"✅ 题库使用记录已重置！\n"
            f"• 网络题库：{online_info['total']} 个谜题 (已重置)\n"
            f"• 本地存储库：{local_info['total']} 个谜题 (已重置)\n"
            f"• 所有题目现在都可以重新使用"
        )

        yield event.plain_result(message)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("题库详情")
    async def show_storage_details(self, event: AstrMessageEvent):
        """查看题库详细使用记录（仅管理员）"""

        # 确保题库已初始化
        self._ensure_story_storages()

        # 获取网络题库详细信息
        online_info = self.online_story_storage.get_storage_info()
        online_usage = self.online_story_storage.get_usage_info()

        # 获取本地存储库详细信息
        local_info = self.local_story_storage.get_storage_info()
        local_usage = self.local_story_storage.get_usage_info()


        # 安全计算使用率，避免除零错误
        online_usage_rate = (
            (online_info["used"] / online_info["total"] * 100)
            if online_info["total"] > 0
            else 0.0
        )
        local_usage_rate = (
            (local_info["used"] / local_info["total"] * 100)
            if local_info["total"] > 0
            else 0.0
        )

        message = (
            f"📊 题库详细使用记录：\n\n"
            f"🌐 网络题库：\n"
            f"• 总数：{online_info['total']} 个谜题\n"
            f"• 已使用：{online_info['used']} 个\n"
            f"• 剩余：{online_info['available']} 个\n"
            f"• 使用率：{online_usage_rate:.1f}%\n"
            f"• 已用索引：{online_usage['used_indexes'][:10]}{'...' if len(online_usage['used_indexes']) > 10 else ''}\n\n"
            f"💾 本地存储库：\n"
            f"• 总数：{local_info['total']} 个谜题\n"
            f"• 已使用：{local_info['used']} 个\n"
            f"• 剩余：{local_info['remaining']} 个\n"
            f"• 使用率：{local_usage_rate:.1f}%\n"
            f"• 已用索引：{local_usage['used_indexes'][:10]}{'...' if len(local_usage['used_indexes']) > 10 else ''}"
        )

        yield event.plain_result(message)

    @filter.command("提示")
    async def hint_command(self, event: AstrMessageEvent):
        """根据当前所有提问记录提供方向性提示"""
        result = await self._build_hint_result(event, event.get_group_id())
        if result:
            yield result

    # 🔍 验证指令（仅在非游戏会话时处理）
    @filter.command("验证")
    async def verify_user_guess_command(self, event: AstrMessageEvent, user_guess: str):
        """验证用户推理（仅在非游戏会话时处理）"""
        group_id = event.get_group_id()

        if not group_id:
            yield event.plain_result("验证功能只能在群聊中使用")
            return

        # 检查是否有活跃游戏，如果有活跃游戏，说明在会话控制中，不在这里处理
        if self.game_state.is_game_active(group_id):
            # 阻止事件继续传播，避免被会话控制系统重复处理
            await event.block()
            return
        # 只有在没有活跃游戏时才在这里处理（用于游戏外的验证）
        yield event.plain_result("当前没有活跃的海龟汤游戏，请使用 /汤 开始新游戏")

    # ⚙️ 查看当前配置
    @filter.command("汤配置")
    async def show_config(self, event: AstrMessageEvent):
        """查看当前插件配置"""

        # 确保题库已初始化
        self._ensure_story_storages()

        local_info = self.local_story_storage.get_storage_info()
        online_info = self.online_story_storage.get_storage_info()

        # 获取策略的中文描述
        strategy_names = {
            "network_first": "优先网络题库→本地存储库→LLM生成",
            "local_first": "优先本地存储库→网络题库→LLM生成",
            "custom_first": "优先自定义题库→本地存储库→LLM生成",
            "random": "随机从网络、本地或自定义题库中选择",
        }
        strategy_name = strategy_names.get(
            self.puzzle_source_strategy, self.puzzle_source_strategy
        )

        # 检查存储库是否已满
        storage_full_warning = ""
        if local_info["available"] <= 0:
            storage_full_warning = "\n⚠️ 本地存储库已满，自动生成已停止"

        # 获取难度组信息
        difficulty_info = []
        for name, config in self.difficulty_groups.items():
            question_limit = config.get("question_limit")
            question_info = f"{question_limit}次" if question_limit is not None else "无限"
            
            hint_limit = config.get("hint_limit")
            hint_info = f"{hint_limit}次" if hint_limit and hint_limit > 0 else ("无限" if hint_limit is None else "无")
            
            verification_before = config.get("verification_before_limit", 0)
            verification_after = config.get("verification_after_limit", 2)
            
            if verification_before == 0:
                verification_info = f"总次数：{verification_after if verification_after != -1 else '无限'}"
            else:
                verification_info = f"耗尽前：{verification_before}次，耗尽后：{verification_after}次"
            
            difficulty_info.append(
                f"• {name}：提问{question_info}，提示{hint_info}，验证{verification_info}"
            )

        config_info = (
            f"⚙️ 海龟汤插件配置：\n"
            f"• 生成谜题 LLM：{self.generate_llm_provider_id or '默认'}\n"
            f"• 判断问答 LLM：{self.judge_llm_provider_id or '默认'}\n"
            f"• 游戏超时：{self.game_timeout} 秒\n"
            f"• 网络题库：{online_info['total']} 个谜题 (已用: {online_info['used']}, 剩余: {online_info['available']})\n"
            f"• 本地存储库：{local_info['total']}/{local_info['max_size']} (已用: {local_info['used']}, 剩余: {local_info['remaining']})\n"
            f"• 自动生成时间：{self.auto_generate_start}:00-{self.auto_generate_end}:00\n"
            f"• 谜题来源策略：{strategy_name}{storage_full_warning}\n\n"
            f"🎮 难度组配置：\n" + "\n".join(difficulty_info) + "\n\n"
            f"💡 配置说明：\n"
            f"• 提问耗尽前验证次数为0时，以提问耗尽后验证次数为总次数\n"
            f"• 提问耗尽后验证次数为-1时表示无限次\n"
            f"• 配置修改后重启插件生效"
        )
        yield event.plain_result(config_info)

    # ➕ 添加自定义海龟汤
    @filter.command("添加海龟汤")
    async def add_custom_soupai(self, event: AstrMessageEvent, content: str):
        """添加自定义海龟汤故事，格式: /添加海龟汤 <汤面>|<汤底>"""
        
        # 确保自定义存储库已初始化
        self._ensure_story_storages()
        
        # 解析内容格式: 汤面|汤底
        if "|" not in content:
            yield event.plain_result("❌ 格式错误！请使用格式: /添加海龟汤 <汤面>|<汤底>")
            return
        
        puzzle, answer = content.split("|", 1)
        puzzle = puzzle.strip()
        answer = answer.strip()
        
        if not puzzle or not answer:
            yield event.plain_result("❌ 汤面和汤底都不能为空！")
            return
        
        # 添加故事到自定义存储库
        success = self.custom_story_storage.add_story(puzzle, answer)
        
        if success:
            # 获取添加后的故事索引
            story_index = len(self.custom_story_storage.stories) - 1
            yield event.plain_result(
                f"✅ 添加成功！海龟汤编号: {story_index}\n\n"
                f"📖 汤面: {puzzle}\n"
                f"📖 汤底: {answer}"
            )
        else:
            yield event.plain_result("❌ 添加失败，请重试")
