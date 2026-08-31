import json
import logging
from pathlib import Path

from ..config import config as app_config

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "token_plan.json"
COLLECTIONS_DIR = Path(__file__).resolve().parent / "config" / "collections"

REQUIRED_FIELDS = [
    ("mimo", "api_key"), ("mimo", "base_url"), ("mimo", "model"),
    ("pdf", "dir"), ("pdf", "chunk_size"), ("pdf", "chunk_overlap"),
]


def _get_nested(cfg: dict, *keys):
    current = cfg
    for k in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(k)
    return current


def load_config() -> dict:
    """加载并校验全局配置文件。"""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"找不到配置文件：{CONFIG_PATH}")

    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    if not isinstance(cfg, dict):
        raise ValueError(f"配置文件顶层必须是 JSON 对象")

    cfg = _fill_env(cfg)

    # 必填字段校验
    missing = []
    for *parents, field in REQUIRED_FIELDS:
        val = _get_nested(cfg, *parents, field)
        if val is None or (isinstance(val, str) and not val.strip()):
            missing.append(".".join((*parents, field)))
    if missing:
        raise ValueError(f"配置文件缺少必填字段：\n  " + "\n  ".join(missing))

    # siliconflow key 继承
    sf_key = (cfg.get("siliconflow") or {}).get("api_key", "").strip()
    if sf_key:
        for sec in ("reranker", "eval_llm"):
            section = cfg.get(sec)
            if isinstance(section, dict) and not section.get("api_key", "").strip():
                section["api_key"] = sf_key

    return cfg


def _fill_env(cfg: dict) -> dict:
    """密钥统一从 dataagent 的 .env 注入（仓库内配置只留占位，防泄露）。

    - mimo：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL
    - siliconflow：SILICONFLOW_API_KEY（未配置时关闭 API rerank，检索回退 RRF）
    - elasticsearch：hosts 以 dataagent 的 ES_HOST / ES_PORT 为准（单源）
    """
    mimo = cfg.setdefault("mimo", {})
    # 单源配置：LLM 的 key/地址/模型一律以 dataagent 的 .env 为准（避免 .env 更换后旧值残留）
    mimo["api_key"] = app_config.LLM_API_KEY
    mimo["base_url"] = app_config.LLM_BASE_URL
    mimo["model"] = app_config.LLM_MODEL

    sf = cfg.setdefault("siliconflow", {})
    if app_config.SILICONFLOW_API_KEY:
        sf["api_key"] = app_config.SILICONFLOW_API_KEY
    elif not str(sf.get("api_key", "")).strip():
        cfg.setdefault("reranker", {}).setdefault("enabled", False)

    es = cfg.setdefault("elasticsearch", {})
    host = app_config.ES_CONFIG.get("host", "localhost")
    if host and not str(host).startswith(("http://", "https://")):
        host = f"http://{host}"
    es["hosts"] = [f"{host}:{app_config.ES_CONFIG.get('port', 9200)}"]
    return cfg


def load_collection(name: str, cfg: dict = None) -> dict:
    """加载指定 collection 配置并合并到全局配置。

    合并规则：collection 的字段覆盖全局配置中对应的键。
    返回合并后的配置副本。
    """
    if cfg is None:
        cfg = load_config()

    cfg = json.loads(json.dumps(cfg))  # deep copy

    fp = COLLECTIONS_DIR / f"{name}.json"
    if not fp.exists():
        available = [f.stem for f in COLLECTIONS_DIR.glob("*.json")] if COLLECTIONS_DIR.is_dir() else []
        raise FileNotFoundError(
            f"找不到 collection 配置：{fp}\n可用的 collection：{available or '无'}"
        )

    with open(fp, "r", encoding="utf-8") as f:
        col = json.load(f)

    # 合并：索引名
    if "index_name" in col:
        cfg.setdefault("elasticsearch", {})["index_name"] = col["index_name"]

    # 合并：语料路径覆盖
    if "pdf_dir" in col:
        cfg.setdefault("pdf", {})["dir"] = col["pdf_dir"]
    if "corpus_dir" in col:
        cfg.setdefault("corpus", {})["dir"] = col["corpus_dir"]
    if "chunk_size" in col:
        cfg.setdefault("pdf", {})["chunk_size"] = col["chunk_size"]
    if "chunk_overlap" in col:
        cfg.setdefault("pdf", {})["chunk_overlap"] = col["chunk_overlap"]
    if "text_field" in col:
        cfg.setdefault("corpus", {})["text_field"] = col["text_field"]

    # system_prompt 挂到顶层
    if "system_prompt" in col:
        cfg["system_prompt"] = col["system_prompt"]

    # 合并：lifecycle 生命周期配置
    if "lifecycle" in col:
        cfg["lifecycle"] = col["lifecycle"]

    # 合并：indexing 索引配置（语义去重开关/阈值等）
    if "indexing" in col:
        cfg["indexing"] = col["indexing"]

    # 合并：recall 召回配置（use_vector 等）
    if "recall" in col:
        cfg.setdefault("recall", {}).update(col["recall"])

    # 保留 collection 元信息
    cfg["_collection"] = {
        "name": col.get("name", name),
        "display_name": col.get("display_name", name),
    }

    logger.info("已加载 collection: %s (%s)", name, col.get("display_name", name))
    return cfg




