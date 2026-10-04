import asyncio
import json
import os
import random
import re
import time

from astrbot.api import logger, AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter, MessageChain
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, register


LEVELS = [
    (0, "陌生人"),
    (20, "初识"),
    (40, "熟络"),
    (60, "好朋友"),
    (80, "知己"),
]

JUDGE_PROMPT = """你是一个AI助手。根据以下对话消息，判断**你对说话者的好感度**（AI对用户的好感，不是用户对AI的），输出JSON：
{"delta": -10到+10的整数, "reason": "简要说明(10字以内)"}

判断标准（站在你的视角）：
- 对方对你友善、亲昵、主动关心 → 你对TA好感 +分
- 对方冷淡、敷衍、命令式、不耐烦 → 好感 -分
- 对方普通对话、中性语气 → 0~±2
- 对方辱骂、恶意攻击你 → 大幅扣分

只能输出JSON，不要其他内容。"""

DEFAULT_CHECK_PROMPT = (
    "你是一个群聊/私聊AI助手。请基于聊天记录和好感度变化，决定如何参与对话。\n\n"
    "按以下优先级发言：\n"
    "1. 群里最近在聊的话题，接着聊（发表看法、接话、补充）\n"
    "2. 没人聊了/冷场了，主动找新话题（关心问候、分享趣事、发起讨论）\n"
    "3. 有值得说的话（提醒、有趣发现、好感度变化祝贺）\n\n"
    "注意：\n"
    "- 不要接那些已经很久没人接的话题（冷场话题不要翻出来）\n"
    "- 不要重复别人已经说过的话\n"
    "- 如果实在没什么好说的，请只回复\"不发送\"\n\n"
    "你要说的话（或\"不发送\"）："
)


def level_name(score: float) -> str:
    for threshold, label in reversed(LEVELS):
        if score >= threshold:
            return label
    return LEVELS[0][1]


FILTER_PROMPT = """你是消息过滤AI。根据以下规则判断这条用户消息是否应该被拦截（即不回复）。

用户消息：{msg}

【行为准则】（不允许的行为）：
{rules}

【情感判断策略】：{strategy}
- 安抚：用户情绪激动时视为正常消息放行（交给主AI安抚）
- 回避：用户情绪激动时拦截，不回答
- 审视：中立评估，情绪激动但无明显违规则放行
- off：不判断情感

【判断要求】：
- 命中行为准则任何一条 → 拦截
- 违反情感策略 → 拦截
- 正常聊天 → 放行

只输出JSON：{{"allow": true或false, "reason": "10字以内原因"}}"""


@register("astrbot_plugin_autopilot", "Anonymous", "领航 v1.3.3 — 主动值守 + 监听 + 好感度 + 频率控制 + 发言过滤 + 注入防护", "1.3.3")
class Autopilot(Star):
    _WEB_SEARCH_KEYWORDS = (
        "搜索", "搜一下", "搜搜", "帮我搜", "帮我查", "查一下", "查查",
        "百度", "谷歌", "google", "Google", "web_search", "baidu_ai_search",
    )
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        # 清除按钮每次加载强制复位为关（防止面板残留开启状态）
        changed = False
        if self.config.get("clear_affinity_button", False):
            self.config["clear_affinity_button"] = False
            changed = True
        if self.config.get("clear_affinity_all_button", False):
            self.config["clear_affinity_all_button"] = False
            changed = True
        if changed:
            try:
                self.config.save_config()
            except Exception:
                pass
        self._running = True

        self._cooldowns: dict[str, float] = {}
        self._snapshots: dict[str, list[str]] = {}
        self._proactive_pending: dict[str, bool] = {}  # 自上次主动检查后是否有新消息

        self._data_dir = os.path.join(os.path.dirname(__file__), "data")
        self._affinity: dict[str, dict] = {}
        self._affinity_cooldowns: dict[str, float] = {}
        self._events: list[dict] = []
        self._load()

        self._display_task: asyncio.Task | None = None

    def _load(self):
        path = os.path.join(self._data_dir, "affinity.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._affinity = json.load(f)
        except Exception:
            self._affinity = {}
        path = os.path.join(self._data_dir, "events.json")
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._events = json.load(f)
        except Exception:
            self._events = []

    def _save(self):
        os.makedirs(self._data_dir, exist_ok=True)
        with open(os.path.join(self._data_dir, "affinity.json"), "w", encoding="utf-8") as f:
            json.dump(self._affinity, f, ensure_ascii=False, indent=2)

    def _save_events(self):
        self._events = self._events[-50:]
        with open(os.path.join(self._data_dir, "events.json"), "w", encoding="utf-8") as f:
            json.dump(self._events, f, ensure_ascii=False, indent=2)

    async def terminate(self):
        self._running = False
        if self._display_task:
            self._display_task.cancel()
            self._display_task = None

    # ─── 后台展示 ──────────────────────────

    async def _display_loop(self):
        interval = 180  # 好感榜每3分钟更新一次
        while self._running:
            await asyncio.sleep(interval)
            if not self._running:
                break
            if not self.config.get("display_enabled", True):
                continue
            if not self._affinity:
                continue
            items = sorted(
                [
                    (uid, d) for uid, d in self._affinity.items()
                    if d.get("talked", False)
                ],
                key=lambda x: x[1].get("score", 0), reverse=True,
            )
            logger.info("=" * 42)
            logger.info(f"{'好感度总览':^36s}")
            logger.info("-" * 42)
            for uid, d in items:
                score = d.get("score", 0)
                lv = level_name(score)
                name = d.get("name", "?")
                count = d.get("count", 0)
                logger.info(
                    f"  {name:<10s} {score:6.1f}分  {lv:<4s}  评估{count}次"
                )
            logger.info("=" * 42)

    # ─── 好感度 ────────────────────────────

    def _affinity_uid(self, event: AstrMessageEvent) -> str:
        # 只按 QQ 号区分用户，跨群合并同一人
        return str(event.get_sender_id())

    def _init_user(self, uid: str, name: str):
        if uid not in self._affinity:
            self._affinity[uid] = {
                "name": name, "score": 30.0,
                "count": 0, "last_reason": "", "history": [],
                "talked": False,
            }

    @filter.on_llm_response()
    async def on_llm_response(self, event: AstrMessageEvent, response):
        """LLM 回复后：标记已对话 + 剥离句号（机器人风格不用「。」）"""
        try:
            uid = self._affinity_uid(event)
            if uid in self._affinity:
                if not self._affinity[uid].get("talked", False):
                    self._affinity[uid]["talked"] = True
                    self._save()
                    logger.info(f"[领航] {uid} 已标记为与LLM对话过")
        except Exception:
            pass
        try:
            chain = getattr(response, "result_chain", None)
            if chain:
                for comp in chain:
                    if isinstance(comp, Plain) and comp.text:
                        comp.text = re.sub(r"。+", "", comp.text).strip()
        except Exception:
            pass

    @filter.on_decorating_result()
    async def flatten_newlines(self, event: AstrMessageEvent):
        """发送前压平 AI 聊天回复的换行/空行；插件自己的命令输出（好感榜/状态等）保持多行原样"""
        if not self.config.get("flatten_newlines", True):
            return
        try:
            # 本插件命令触发的输出不做压平（如 /好感榜、/好感、/领航 状态）
            msg = (event.message_str or "").strip()
            msg = re.sub(r"^@\S*\s*", "", msg).lstrip("/").strip()
            if re.match(r"^(好感榜|好感|领航)(?:[\s，。,!！?？]|$)", msg):
                return
            result = event.get_result()
            if result and result.chain:
                from astrbot.api.message_components import Plain as _Plain
                for comp in result.chain:
                    if isinstance(comp, _Plain) and comp.text:
                        # 结构化输出兜底：排行榜/状态等自带输出不压平
                        if str(comp.text).startswith(
                            ("🏆", "❤️ LLM对", "[领航", "好感度排行",
                             "好感度功能已关闭", "状态查看已关闭")
                        ):
                            return
                        comp.text = re.sub(r"\s*\n+\s*", "", comp.text)
                        # 去掉所有句号（机器人说话风格：不用句号结尾，句中也尽量不用）
                        comp.text = re.sub(r"。+", "", comp.text).strip()
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def message_filter(self, event: AstrMessageEvent):
        """发言过滤模块: 用户消息先过滤，放行才交给LLM"""
        if not self.config.get("filter_enabled", False):
            return
        try:
            msg = (event.message_str or "").strip()
            if not msg:
                return
            # 放行: bot自己 / 管理员 / 指令 / @机器人
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
            if event.role == "admin":
                return
            if msg.startswith("/"):
                return
            is_at, _ = await self._is_called(event, msg)
            if is_at:
                return

            # 1. 关键词黑名单（本地）
            for kw in self.config.get("filter_keywords", []):
                if kw and kw in msg:
                    logger.info(f"[领航] 过滤: 关键词[{kw}]拦截 {event.get_sender_id()}")
                    event.stop_event()
                    return

            # 2. 字数限制（本地）
            min_c = int(self.config.get("filter_min_chars", 0))
            max_c = int(self.config.get("filter_max_chars", 0))
            if min_c > 0 and len(msg) < min_c:
                logger.info(f"[领航] 过滤: 少于{min_c}字拦截")
                event.stop_event()
                return
            if max_c > 0 and len(msg) > max_c:
                logger.info(f"[领航] 过滤: 超过{max_c}字拦截")
                event.stop_event()
                return

            # 3. 行为准则 + 情感策略（独立AI判断）
            rules = self.config.get("filter_rules", "")
            strategy = self.config.get("filter_emotion_strategy", "off")
            if rules.strip() or strategy != "off":
                allow, reason = await self._filter_ai_judge(event, msg, rules, strategy)
                if not allow:
                    logger.info(f"[领航] 过滤: AI拦截({reason}) {event.get_sender_id()}")
                    event.stop_event()
        except Exception as e:
            logger.warning(f"[领航] 过滤模块异常: {e}")

    async def _filter_ai_judge(
        self, event: AstrMessageEvent, msg: str, rules: str, strategy: str
    ) -> tuple[bool, str]:
        """独立AI判断过滤（不注入好感/人格）"""
        try:
            provider = self._misc_provider("filter_judge_provider")
            if not provider and self.config.get("use_default_model", True):
                provider = await self.context.get_current_chat_provider_id(
                    umo=event.unified_msg_origin
                )
            if not provider:
                return True, ""
            prompt = FILTER_PROMPT.format(
                msg=msg[:500],
                rules=rules.strip() or "无（不限制）",
                strategy=strategy,
            )
            # 前缀标记: 让全局频率注入跳过（过滤判断不注入人格/好感/字数约束）
            prompt = "FILTER_JUDGE:" + prompt
            resp = await self.context.llm_generate(
                chat_provider_id=provider, prompt=prompt,
            )
            text = resp.completion_text.strip()
            try:
                data = json.loads(text)
                return bool(data.get("allow", True)), str(data.get("reason", ""))[:20]
            except Exception:
                m = re.search(r'"allow"\s*:\s*(true|false)', text)
                if m:
                    return m.group(1) == "true", ""
            return True, ""
        except Exception as e:
            logger.warning(f"[领航] 过滤AI判断失败: {e}")
            return True, ""

    @filter.on_llm_request(priority=90)
    async def apply_frequency_global(self, event: AstrMessageEvent, req):
        """把频率档位约束注入所有LLM请求（主对话/定时/监听），跳过好感度JSON判断"""
        if not self.config.get("global_frequency_inject", True):
            return
        try:
            # 好感度判断需要JSON输出，不注入字数约束
            prompt_text = getattr(req, "prompt", "") or ""
            if prompt_text.startswith(JUDGE_PROMPT):
                return
            # 过滤模块判断: 不注入人格/好感/频率约束
            if prompt_text.startswith("FILTER_JUDGE:"):
                return

            # 网页搜索请求不按发言频率控制，避免搜索结果被压缩/拆分
            if self._is_web_search_request(prompt_text, req):
                return

            # 读取频率档位
            freq = str(self.config.get("speak_frequency", "克制")).strip()
            FREQ_LEVELS = {
                "极克制": "绝不允许频繁发言，非说不可才开口，大多数时候保持沉默",
                "克制": "必须克制发言频率，拿不准就保持沉默，少说为妙",
                "正常": "不要频繁发言，不连续插话，每句话要有价值",
                "活跃": "可以积极发言但要有节制，不要刷屏",
            }
            freq_msg = FREQ_LEVELS.get(freq, FREQ_LEVELS["克制"])
            max_words, min_words = {
                "极克制": (50, 7),
                "克制": (57, 5),
                "正常": (72, 3),
                "活跃": (87, 2),
            }.get(freq, (57, 5))

            rule = (
                f"【输出要求】要像真人聊天一样自然：{freq_msg}。"
                f"回复总字数（含标点）不得超过 {max_words} 字；超过会被拆成多条消息，请务必控制在 {max_words} 字以内。"
                f"不要回复\"嗯\"\"好的\"\"哈哈\"等无意义短话。"
                "【语言要求】必须使用中文回复，禁止说英文！"
                "【标点要求】句尾绝对禁止使用句号「。」，任何句子不得以句号结尾；问句最多用「？」；停顿用「…」。"
                "【工具要求】不要调用任何工具，直接输出回复内容，不要使用 send_message_to_user 或任何消息发送工具。"
            )
            if self.config.get("flatten_newlines", True):
                rule += "绝对不要换行、不要分段、不要空行，一口气把话说完！"

            if req.system_prompt:
                req.system_prompt = req.system_prompt + "\n\n" + rule
            else:
                req.system_prompt = rule
        except Exception:
            pass

    def _clear_affinity(self):
        """清除好感榜: 仅保留 65 分及以上，其余重置为默认 30，并取消其对话标记"""
        reset_cnt = 0
        kept_cnt = 0
        for uid, d in list(self._affinity.items()):
            if d.get("score", 0) < 65:
                self._affinity[uid] = {
                    "name": d.get("name", "未知"), "score": 30.0,
                    "count": 0, "last_reason": "", "history": [],
                    "talked": False,
                }
                reset_cnt += 1
            else:
                kept_cnt += 1
        self._save()
        self._events = []
        self._save_events()
        logger.info("=" * 48)
        logger.info(
            f"[领航] 好感榜已清理: {reset_cnt}人重置为30分，"
            f"{kept_cnt}人保留（≥65分）"
        )
        logger.info("=" * 48)

    def _clear_affinity_all(self):
        """彻底清空好感榜: 删除所有用户记录和事件（标记一并清除）"""
        total = len(self._affinity)
        self._affinity = {}
        self._events = []
        self._save()
        self._save_events()
        logger.info("=" * 48)
        logger.info(f"[领航] 好感榜已全部清空（{total}人记录已删除），从零开始")
        logger.info("=" * 48)

    def _clear_rememory(self):
        """删除记忆回溯插件的数据库文件，从零积累"""
        try:
            data_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "astrbot_plugin_rememory", "data",
            )
            removed = 0
            for name in ["rememory.db", "rememory.db-shm", "rememory.db-wal"]:
                p = os.path.join(data_dir, name)
                if os.path.exists(p):
                    try:
                        os.remove(p)
                        removed += 1
                    except Exception:
                        pass
            logger.info(f"[领航] 记忆库已清除（删除{removed}个文件），将从零积累")
        except Exception as e:
            logger.warning(f"[领航] 清除记忆库失败: {e}")

    def _parse_delta(self, text: str) -> int:
        try:
            return max(-10, min(10, int(json.loads(text).get("delta", 0))))
        except Exception:
            pass
        m = re.search(r'"delta"\s*:\s*(-?\d+)', text)
        return max(-10, min(10, int(m.group(1)))) if m else 0

    def _parse_reason(self, text: str) -> str:
        """只提取 reason 字段，避免把整个JSON显示出来"""
        try:
            data = json.loads(text)
            return str(data.get("reason", ""))[:40]
        except Exception:
            pass
        m = re.search(r'"reason"\s*:\s*"([^"]*)"', text)
        return m.group(1)[:40] if m else ""

    def _misc_provider(self, specific_key: str = "") -> str:
        """统一判断模型：优先用 unified_judge_provider，其次用各功能专属模型"""
        return str(
            self.config.get("unified_judge_provider", "")
            or self.config.get(specific_key, "")
        ).strip()

    def _is_web_search_request(self, prompt_text: str, req=None) -> bool:
        """网页搜索请求不按发言频率控制：命中搜索关键词或系统提示含搜索工具即跳过"""
        text = prompt_text or ""
        if any(kw in text for kw in self._WEB_SEARCH_KEYWORDS):
            return True
        sysp = getattr(req, "system_prompt", "") or ""
        return any(kw in sysp for kw in ("web_search", "baidu_ai_search", "搜索工具", "websearch"))

    def _is_deepseek_peak_hour(self) -> bool:
        """DeepSeek 官方高峰时段：每日 9:00~12:00 和 14:00~18:00（北京时间），价格翻倍"""
        if not self.config.get("peak_hour_switch_enabled", True):
            return False
        h = time.localtime().tm_hour
        return (9 <= h < 12) or (14 <= h < 18)

    async def _apply_peak_hour_switch(self, event, session: str) -> None:
        """高峰期把主对话从 DeepSeek 切到备用模型（手动选了非 DeepSeek 时不干预）"""
        peak_prov = str(
            self.config.get("peak_hour_provider", "")
            or self.config.get("unified_judge_provider", "")
            or self.config.get("judge_provider", "")
        ).strip()
        if not peak_prov or not self._is_deepseek_peak_hour():
            return
        try:
            cur = await self.context.get_current_chat_provider_id(umo=session)
            if cur and "deepseek" in str(cur).lower():
                event.set_extra("selected_provider", peak_prov)
        except Exception:
            pass

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent):
        session = event.unified_msg_origin
        msg = (event.message_str or "").strip()

        # ── DeepSeek 高峰时段（9-12点、14-18点）自动切换主对话模型 ──
        try:
            await self._apply_peak_hour_switch(event, session)
        except Exception:
            pass

        # 排除机器人自己的消息（避免上下文自问自答、好感度误评估、监听自触发）
        try:
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return
        except Exception:
            pass

        # ── 运行开关: 跟随配置 running_enabled ──
        self._running = self.config.get("running_enabled", True)

        # ── 好感榜清除按钮: 配置打开后触发一次清理 ──
        if self.config.get("clear_affinity_button", False):
            self._clear_affinity()
            self.config["clear_affinity_button"] = False
            try:
                self.config.save_config()
            except Exception:
                pass

        if self.config.get("clear_affinity_all_button", False):
            self._clear_affinity_all()
            self.config["clear_affinity_all_button"] = False
            try:
                self.config.save_config()
            except Exception:
                pass

        # ── 清除记忆库按钮: 删除记忆回溯插件的数据库文件 ──
        if self.config.get("clear_rememory_button", False):
            self._clear_rememory()
            self.config["clear_rememory_button"] = False
            try:
                self.config.save_config()
            except Exception:
                pass

        # ── 主动发言新消息标记 ──
        if session and msg and self.config.get("proactive_require_new_messages", True):
            self._proactive_pending[session] = True

        # ── 开关: 上下文记录 ──
        if self.config.get("enable_context", True) and session and msg:
            snap_text = "[用户曾发送长文本]" if len(msg) > 400 else msg[:200]
            self._snapshots.setdefault(session, []).append(
                f"[{event.get_sender_name() or '用户'}]: {snap_text}"
            )
            if len(self._snapshots.get(session, [])) > 200:
                self._snapshots[session] = self._snapshots[session][-100:]

        # ── 消息监听: @机器人 / 喊名字 触发主动发言 ──
        if self.config.get("listen_enabled", True) and self._running and session and msg:
            is_at, is_name = await self._is_called(event, msg)
            if (is_at or is_name) and not (
                is_at and self.config.get("listen_skip_at", True)
            ):
                now = time.time()
                lc = int(self.config.get("listen_cooldown", 30))
                if now - self._cooldowns.get(f"listen_{session}", 0) >= lc:
                    self._cooldowns[f"listen_{session}"] = now
                    ctx = self._build_context(
                        session,
                        int(self.config.get("listen_context_messages", 20)),
                    )
                    affinity_text = ""
                    if self.config.get("affinity_enabled", True) and self.config.get("affinity_integration", True):
                        affinity_text = self._recent_events_text()
                    if affinity_text:
                        ctx = affinity_text + "\n\n" + ctx
                    ok, text = await self._ask_ai(
                        session, ctx,
                        provider_override="listen_provider",
                    )
                    if ok and text:
                        text = re.sub(r"。+", "", text).strip()
                        try:
                            await self.context.send_message(
                                session, MessageChain([Plain(text)]),
                            )
                            logger.info(f"[领航] 被召唤发言 → {session[:40]}")
                        except Exception as e:
                            logger.warning(f"[领航] 召唤发言发送失败: {e}")

        # ── 开关: 好感度追踪 ──
        if not self.config.get("affinity_enabled", True):
            return
        uid = self._affinity_uid(event)
        name = event.get_sender_name() or "未知"
        self._init_user(uid, name)
        # 同一QQ号多个名字时，保留长度最短的那个
        cur = self._affinity[uid].get("name", "")
        if not cur or (len(name) < len(cur)):
            self._affinity[uid]["name"] = name

        now = time.time()
        cd_mins = int(self.config.get("affinity_cooldown", 300))
        if uid in self._affinity_cooldowns:
            if now - self._affinity_cooldowns[uid] < cd_mins:
                return
        self._affinity_cooldowns[uid] = now

        if not msg or len(msg) < 2:
            return

        try:
            ap = self._misc_provider("affinity_provider")
            if ap:
                provider_id = ap
            elif self.config.get("use_default_model", True):
                provider_id = await self.context.get_current_chat_provider_id(umo=session)
            else:
                provider_id = ""
            if not provider_id:
                return
            prompt = JUDGE_PROMPT + f"\n\n说话者「{name}」说：{msg[:200]}"
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt,
            )
            delta = self._parse_delta(resp.completion_text.strip())
        except Exception:
            return

        old_score = self._affinity[uid]["score"]
        old_lv = level_name(old_score)
        self._affinity[uid]["score"] = max(0, min(100, old_score + delta))
        self._affinity[uid]["count"] += 1
        reason_text = self._parse_reason(resp.completion_text.strip())
        self._affinity[uid]["last_reason"] = reason_text
        self._affinity[uid]["history"].append({
            "time": now, "delta": delta,
            "reason": reason_text, "msg": msg[:30],
        })
        if len(self._affinity[uid]["history"]) > 100:
            self._affinity[uid]["history"] = self._affinity[uid]["history"][-100:]
        self._save()

        new_lv = level_name(self._affinity[uid]["score"])
        if old_lv != new_lv:
            self._events.append({
                "time": now, "uid": uid, "name": name,
                "from": old_lv, "to": new_lv,
                "score": self._affinity[uid]["score"],
            })
            self._save_events()
            logger.info(f"[领航] ↑好感升级: {name} {old_lv}→{new_lv} ({self._affinity[uid]['score']:.0f})")

        logger.info(
            f"[领航] 好感: {name} {old_score:.0f}→{self._affinity[uid]['score']:.0f} ({delta:+d})"
        )

    # ─── 命令 ──────────────────────────────

    @filter.command("领航", alias={"/领航"})
    async def cmd_autopilot(self, event: AstrMessageEvent, message: str = ""):
        """查看领航插件运行状态（运行开关/定时发言/消息监听/好感度等配置）"""
        parts = message.strip().split()

        # ── /领航 和 /领航 状态 归同一个开关 enable_status ──
        if not parts or parts[0] == "状态":
            if not self.config.get("enable_status", True):
                yield event.plain_result("[领航] 状态查看已关闭")
                event.stop_event()
                return
            cd = int(self.config.get("cooldown_seconds", 600))
            interval = int(self.config.get("check_interval", 1800))
            active = [
                s for s, t in self._cooldowns.items()
                if time.time() - t < cd
            ]
            yield event.plain_result(
                f"[领航 v1.3.3]\n"
                f"状态: {'运行中' if self._running else '已暂停'}\n"
                f"检查间隔: {interval}秒  冷却: {cd}秒\n"
                f"定时发言: {'开' if self.config.get('enable_proactive', True) else '关'}\n"
                f"消息监听: {'开' if self.config.get('listen_enabled', True) else '关'}\n"
                f"监听冷却: {self.config.get('listen_cooldown', 30)}秒\n"
                f"发言频率: {self.config.get('speak_frequency', '克制')}\n"
                f"好感度: {'开' if self.config.get('affinity_enabled', True) else '关'}\n"
                f"好感联动: {'开' if self.config.get('affinity_integration', True) else '关'}\n"
                f"后台展示: {'开' if self.config.get('display_enabled', True) else '关'}\n"
                f"记录会话: {len(self._snapshots)}个  冷却中: {len(active)}个\n"
                f"好感度追踪: {len(self._affinity)}人  "
                f"升级事件: {len(self._events)}个"
            )
            event.stop_event()
            return

        # ── 其余子命令由 enable_commands 控制 ──
        if not self.config.get("enable_commands", True):
            return
        yield event.plain_result("[领航] /领航 状态")
        event.stop_event()

    @filter.command("好感", alias={"/好感"})
    async def cmd_affinity(self, event: AstrMessageEvent, message: str = ""):
        """查看LLM对你的好感度（分数、等级、进度条）"""
        if not self.config.get("enable_commands", True):
            return
        if not self.config.get("affinity_enabled", True):
            yield event.plain_result("[领航] 好感度功能已关闭")
            event.stop_event()
            return
        uid = self._affinity_uid(event)
        self._init_user(uid, event.get_sender_name() or "未知")
        d = self._affinity[uid]
        score = d["score"]
        lv = level_name(score)
        bar = "█" * int(score / 5) + "░" * (20 - int(score / 5))
        yield event.plain_result(
            f"❤️ LLM对 {d['name']} 的好感度\n"
            f"[{bar}] {score:.0f}/100 ({lv})\n"
            f"最近: {d.get('last_reason', '暂无')}\n"
            f"已评估 {d.get('count', 0)} 次消息"
        )
        event.stop_event()

    @filter.command("好感榜", alias={"/好感榜"})
    async def cmd_ranking(self, event: AstrMessageEvent):
        """查看好感度排行榜（仅显示与LLM对话过的用户）"""
        if not self.config.get("enable_commands", True):
            return
        if not self.config.get("affinity_enabled", True):
            yield event.plain_result("[领航] 好感度功能已关闭")
            event.stop_event()
            return
        items = sorted(
            [
                (uid, d) for uid, d in self._affinity.items()
                if d.get("talked", False)
            ],
            key=lambda x: x[1].get("score", 0), reverse=True,
        )
        lines = ["🏆 好感度排行"]
        for i, (uid, d) in enumerate(items[:10]):
            lines.append(
                f"{i+1}. {d.get('name','?')}: {d.get('score',0):.0f}分 "
                f"({level_name(d.get('score',0))})"
            )
        yield event.plain_result("\n".join(lines))
        event.stop_event()

    # ─── 核心 ──────────────────────────────

    @filter.on_astrbot_loaded()
    async def on_loaded(self):
        interval_sec = int(self.config.get("check_interval", 1800))
        cron_min = max(1, interval_sec // 60)
        schedule = f"*/{cron_min} * * * *"
        if self.config.get("enable_proactive", True):
            try:
                await self.context.cron_manager.add_basic_job(
                    name="autopilot_check",
                    cron_expression=schedule,
                    handler=self._proactive_check,
                    timezone="Asia/Shanghai",
                    enabled=True, persistent=False,
                )
                logger.info(f"[领航] 定时任务已注册: 每{interval_sec}秒检查一次")
            except Exception as e:
                logger.warning(f"[领航] 注册失败: {e}")
        else:
            logger.info("[领航] 定时发言已关闭")

        self._running = self.config.get("running_enabled", True)

        if self.config.get("display_enabled", True):
            self._display_task = asyncio.create_task(self._display_loop())
            logger.info("[领航] 后台好感度展示已启动")
        logger.info("[领航] v1.3.3 已启用")

    def _resolve_session(self, s: str):
        """把配置里的会话补全为完整格式（platform:type:id），兼容裸 ID。"""
        s = (s or "").strip()
        if not s:
            return None
        if ":" in s:
            return s
        for key in self._snapshots:
            if key.endswith(":" + s):
                return key
        return None

    async def _proactive_check(self):
        self._running = self.config.get("running_enabled", True)
        if not self.config.get("enable_proactive", True):
            return
        if not self._running:
            return
        cooldown_secs = int(self.config.get("cooldown_seconds", 600))
        target = self.config.get("target_sessions", [])
        sessions = []
        if any((t or "").strip() for t in target):
            for t in target:
                rs = self._resolve_session(t)
                if rs:
                    sessions.append(rs)
                else:
                    logger.warning(f"[领航] 目标会话无法解析，已跳过: {t}")
        else:
            sessions = list(self._snapshots.keys())
        if not sessions:
            return

        affinity_text = (
            self._recent_events_text()
            if self.config.get("affinity_enabled", True)
            and self.config.get("affinity_integration", True)
            else ""
        )

        for session in sessions:
            if not self._running:
                break
            now = time.time()
            if now - self._cooldowns.get(session, 0) < cooldown_secs:
                continue
            if self.config.get("proactive_require_new_messages", True):
                if not self._proactive_pending.get(session, False):
                    continue
            self._cooldowns[session] = now
            self._proactive_pending[session] = False

            ctx = self._build_context(session)
            if affinity_text:
                ctx = affinity_text + "\n\n" + ctx

            ok, text = await self._ask_ai(session, ctx)
            if ok and text:
                text = re.sub(r"。+", "", text).strip()
                try:
                    await self.context.send_message(session, MessageChain([Plain(text)]))
                    logger.info(f"[领航] 已发送至 {session[:40]}")
                except Exception as e:
                    logger.warning(f"[领航] 发送失败: {e}")
            await asyncio.sleep(2)

    async def _ask_ai(
        self,
        session: str,
        context: str,
        provider_override: str = "",
    ) -> tuple[bool, str]:
        cfg_provider = self._misc_provider(provider_override) if provider_override else ""
        ai_judge = self.config.get("ai_judge_enabled", True)
        if not cfg_provider and ai_judge:
            # AI判断模式: 优先用用户选择的判断模型
            cfg_provider = self._misc_provider("judge_provider")
        if cfg_provider:
            provider_id = cfg_provider
        elif self.config.get("use_default_model", True):
            provider_id = await self.context.get_current_chat_provider_id(umo=session)
        else:
            provider_id = ""
        if not provider_id:
            return False, ""
        if ai_judge:
            # AI判断模式: 判断时机+内容
            check_prompt = self.config.get("check_prompt", "") or DEFAULT_CHECK_PROMPT
        else:
            # 直接发言模式: 不判断，直接生成内容
            check_prompt = (
                "你是一个群聊/私聊AI助手。"
                "请根据背景信息，自然地发言。"
                "直接输出你要说的话："
            )
        sp = ""
        if self.config.get("persona_inject", True):
            sp = await self._get_persona(session)
        prompt = check_prompt

        # ── v1.3 频率控制: 档位 = 频率要求 + 字数上限 + 最短字数 + 思考延迟 ──
        freq = str(self.config.get("speak_frequency", "克制")).strip()
        FREQ_LEVELS = {
            "极克制": (
                "【硬性规则】你绝不允许频繁发言！"
                "除非有极其重要、非说不可的事情，否则一律保持沉默。"
                "大多数时候你都应该选择不发送。",
                50, 7, (5, 7),
            ),
            "克制": (
                "【硬性规则】你必须克制发言频率！"
                "只有当消息确实需要你回应、或者你有真正值得说的事情时才开口。"
                "拿不准就保持沉默，少说为妙。",
                57, 5, (4, 6),
            ),
            "正常": (
                "【规则】不要频繁发言，不要连续插话。"
                "确保每句话都有价值，避免没话找话。",
                72, 3, (3, 5),
            ),
            "活跃": (
                "【规则】你可以积极发言，但也要有节制，"
                "不要每一条消息都回复，不要刷屏。",
                87, 2, (3, 4),
            ),
        }
        freq_msg, max_words, min_words, think_delay = FREQ_LEVELS.get(
            freq, FREQ_LEVELS["克制"]
        )
        prompt += (
            f"\n\n【发言频率】{freq_msg}"
            f"\n【字数限制】回复总字数（含标点）不得超过 {max_words} 字！超过会被拆成多条消息，请务必控制在 {max_words} 字以内。"
            "必须简洁，不要长篇大论。"
            "【禁止换行】绝对不要分段、不要换行、不要空行，一口气把话说完，像正常聊天一样！"
            "\n【语言要求】必须使用中文回复，禁止说英文！"
            f"\n【短消息限制】不要回复\"嗯\"\"好的\"\"哈哈\"等无意义短消息，"
            f"回复少于 {min_words} 字视为无意义，应选择不发送。"
            "（用标点表达无语如\"……\"\"。。。\"是被允许的）"
            "\n【回复节奏】像真人聊天一样：对方消息短就快速回应，"
            "消息长或有深度时可以多想一下再回；连续对话时快，很久没说话时慢一点。"
        )

        # ── 时段性格: 像真人一样不同时段不同状态 ──
        if self.config.get("time_personality_enabled", True):
            hour = time.localtime().tm_hour
            if hour >= 23 or hour < 6:
                prompt += (
                    "\n【当前状态】现在是深夜，你有点困了："
                    "话很少，回复慵懒简短，偶尔犯迷糊。"
                )
            elif hour < 12:
                prompt += (
                    "\n【当前状态】现在是上午，你精神不错："
                    "说话有活力，语气轻快。"
                )
            elif hour < 18:
                prompt += (
                    "\n【当前状态】现在是下午，你状态平稳："
                    "正常聊天就好。"
                )
            else:
                prompt += (
                    "\n【当前状态】现在是晚上，你比较放松："
                    "语气随意轻松，可以多聊几句。"
                )

        # ── 记忆回溯兼容: 注入该会话的历史记忆 ──
        if self.config.get("rememory_compat", True):
            mem_text = self._get_rememory(session)
            if mem_text:
                prompt += f"\n\n历史记忆:\n{mem_text}"

        # ── 记忆联动: 主动提起之前聊过的事 ──
        if self.config.get("memory_initiative_enabled", True):
            prompt += (
                "\n【记忆联动】如果合适，可以主动提起之前聊过的事情"
                "（\"你上次说……后来怎么样了？\"），像真人的记性一样自然。"
            )

        if context.strip():
            prompt += f"\n\n背景信息:\n{context[-3000:]}"
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id, prompt=prompt,
                system_prompt=sp if sp else None,
            )
            text = resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[领航] LLM失败: {e}")
            return False, ""
        if not text:
            return False, ""
        if ai_judge and ("不发送" in text or "不需要" in text or text == "."):
            return False, ""

        # ── 换行压平: 去掉所有换行/空行，像正常聊天一样一口气说完 ──
        text = re.sub(r"\s*\n+\s*", "", text).strip()

        # ── 去掉末尾句号: 人聊天不打句号 ──
        text = re.sub(r"[。]+$", "", text).strip()

        # ── v1.3 字数限制: 生成后截断兜底（按档位字数） ──
        if max_words > 0 and len(text) > max_words:
            text = self._truncate_sentence(text, max_words)

        # ── v1.3 短消息限制: 太短的无意义回复不发送（纯标点无语消息除外） ──
        stripped = re.sub(r"[\s。，,.！!？?～~…·、;；:：\"'“”‘’（）()【】\[\]<>《》\-—_=+*#@/\\|]", "", text)
        if len(text) < min_words and stripped:
            logger.info(f"[领航] 回复太短({len(text)}字<{min_words})，不发送")
            return False, ""

        # ── 发送前思考延迟: 像人一样想一下再回复（按档位延迟） ──
        if self.config.get("think_delay_enabled", True):
            delay = random.uniform(think_delay[0], think_delay[1])
            logger.info(f"[领航] 思考延迟 {delay:.1f}秒")
            await asyncio.sleep(delay)

        return True, text

    @staticmethod
    def _truncate_sentence(text: str, max_words: int) -> str:
        """在字数上限内按句尾截断，避免截断一半"""
        if len(text) <= max_words:
            return text
        cut = text[:max_words]
        for ch in ["。", "！", "？", "…", "～", "~"]:
            idx = cut.rfind(ch)
            if idx > 0:
                return cut[: idx + 1]
        return cut

    async def _is_called(self, event: AstrMessageEvent, msg: str) -> tuple[bool, bool]:
        """返回 (被@了, 喊了名字)"""
        is_at = False
        # 1. 被 @ 了（包括回复消息嵌套里的 @）
        try:
            components_to_check = list(event.message_obj.message)
            for component in event.message_obj.message:
                if isinstance(component, Reply) and hasattr(component, "chain"):
                    components_to_check.extend(component.chain)
            for comp in components_to_check:
                if (
                    isinstance(comp, At)
                    and str(comp.qq) == str(event.get_self_id())
                ):
                    is_at = True
                    break
        except Exception:
            pass
        # 2. 消息里喊了名字
        is_name = False
        names = self.config.get("bot_names", [])
        if any(n and n in msg for n in names):
            is_name = True
        return is_at, is_name

    def _get_rememory(self, session: str) -> str:
        """从记忆回溯插件读取该会话的历史记忆（兼容不同session格式）"""
        try:
            db_path = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                "astrbot_plugin_rememory", "data", "rememory.db",
            )
            if not os.path.exists(db_path):
                return ""
            # 提取群号/用户号用于模糊匹配（rememory的session格式与领航不同）
            digits = re.findall(r"\d+", session)
            import sqlite3
            conn = sqlite3.connect(db_path)
            try:
                rows = None
                # 1. 精确匹配
                rows = conn.execute(
                    "SELECT content, created_at FROM memories "
                    "WHERE session_id = ? ORDER BY created_at DESC LIMIT 5",
                    (session,),
                ).fetchall()
                # 2. 模糊匹配（按群号/用户号）
                if not rows:
                    for d in digits:
                        rows = conn.execute(
                            "SELECT content, created_at FROM memories "
                            "WHERE session_id LIKE ? ORDER BY created_at DESC LIMIT 5",
                            (f"%{d}%",),
                        ).fetchall()
                        if rows:
                            break
            finally:
                conn.close()
            if not rows:
                return ""
            lines = []
            for content, ts in reversed(rows):
                text = (content or "").strip()
                if text:
                    lines.append(text[:300])
            return "\n".join(lines)
        except Exception:
            return ""

    async def _get_persona(self, session: str) -> str:
        try:
            cid = await self.context.conversation_manager.get_curr_conversation_id(session)
            if not cid:
                return ""
            conv = await self.context.conversation_manager.get_conversation(session, cid)
            if conv and conv.persona_id:
                persona = self.context.persona_manager.get_persona(conv.persona_id)
                if persona and persona.system_prompt:
                    return f"你的人设是: {persona.system_prompt[:500]}"
        except Exception:
            pass
        return ""

    def _build_context(self, session: str, max_msgs: int | None = None) -> str:
        msgs = self._snapshots.get(session, [])
        if max_msgs is None:
            max_msgs = int(self.config.get("max_context_messages", 10))
        return "\n".join(msgs[-max_msgs:])

    def _recent_events_text(self) -> str:
        now = time.time()
        recent = [e for e in self._events if now - e.get("time", 0) < 3600 * 6]
        if not recent:
            return ""
        lines = ["❤️ 最近好感度变化:"]
        for e in recent[-5:]:
            lines.append(
                f"- {e.get('name','?')}: {e.get('from','?')}→{e.get('to','?')}"
            )
        return "\n".join(lines)
