"""中文停用词（共用模块）。"""
import os
import logging

logger = logging.getLogger(__name__)

_FALLBACK_STOP_WORDS = frozenset({
    "的", "了", "着", "过", "地", "得", "在", "是", "和", "与", "及", "或", "等", "对", "为", "以", "于",
    "从", "到", "向", "由", "被", "把", "让", "使", "给", "跟", "同", "而", "并", "且", "但", "但是",
    "如果", "虽然", "因为", "所以", "我", "你", "他", "她", "它", "们", "这", "那", "这个", "那个",
    "这些", "那些", "什么", "怎么", "怎样", "如何", "为什么", "哪", "哪些", "谁", "哪里", "哪儿",
    "个", "些", "么", "不", "没", "没有", "无", "也", "都", "就", "还", "已", "已经", "将", "要",
    "会", "能", "可", "可以", "可能", "应该", "需要", "很", "非常", "太", "更", "最", "比较", "相对",
    "吗", "呢", "吧", "啊", "呀", "哦", "哈", "？", "！", "。", "，", "、", "：", "；", """, """,
    "'", "'", "（", "）", "(", ")", "【", "】", "[", "]", "《", "》", "<", ">", "…", "—", "·",
    "「", "」", "『", "』", " ", "\n", "\t",
})

_STOPWORDS_FILE = os.environ.get(
    "MYRAG_STOPWORDS_FILE",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "config", "hit_stopwords.txt"),
)

STOP_WORDS = _FALLBACK_STOP_WORDS


def load_stopwords() -> frozenset:
    global STOP_WORDS
    try:
        if os.path.isfile(_STOPWORDS_FILE):
            with open(_STOPWORDS_FILE, "r", encoding="utf-8-sig") as f:
                words = {ln.strip() for ln in f if ln.strip()}
            if words:
                logger.info("已加载哈工大停用词表: %d 词", len(words))
                STOP_WORDS = frozenset(words)
                return STOP_WORDS
    except Exception as e:
        logger.warning("读取停用词表失败: %s", e)
    STOP_WORDS = _FALLBACK_STOP_WORDS
    return STOP_WORDS


load_stopwords()
