"""智能分块器 — 句子边界重叠 + 元数据（dataagent 精简版）。

保留 JSONL 语料灌库链路（load_all_docs / split_docs / normalize_chunks），
裁剪 PDF 标题感知与语义分块（dataagent 的 datax_docs / ops_incident 语料
均为 JSONL，无 PDF 输入）。
"""
import os
import json
import logging
import re

logger = logging.getLogger(__name__)

# 中文句子结束符
_SENTENCE_ENDINGS = re.compile(r'[。！？；\n]')


# ============================================================
# 文本规范化
# ============================================================

# 状态标记映射表
STATUS_MAPPING = {
    'DONE': '已完成',
    'ING': '进行中',
    'TODO': '待处理',
    'WIP': '进行中',
    'BLOCKED': '受阻',
    'PENDING': '待处理',
    '已完成': '已完成',
    '进行中': '进行中',
    '待处理': '待处理',
}


def normalize_text(text: str) -> str:
    if not text:
        return text

    # 1. 统一状态标记
    for eng, chn in STATUS_MAPPING.items():
        pattern = r'(?<![a-zA-Z])' + re.escape(eng) + r'(?![a-zA-Z])'
        text = re.sub(pattern, chn, text, flags=re.IGNORECASE)

    # 2. 去除多余空白
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()

    return text


def normalize_chunks(chunks: list[dict]) -> list[dict]:
    for chunk in chunks:
        if 'text' in chunk:
            chunk['text'] = normalize_text(chunk['text'])
    return chunks


# ============================================================
# 句子边界感知的重叠切分
# ============================================================

def _split_at_sentence_boundary(text: str, max_len: int) -> list[str]:
    """在句子边界处切分文本，每段不超过 max_len。"""
    if len(text) <= max_len:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        if start + max_len >= len(text):
            chunks.append(text[start:])
            break

        # 在 max_len 范围内找最后一个句子结束符
        end = start + max_len
        search_zone = text[start:end]
        last_period = -1
        for m in _SENTENCE_ENDINGS.finditer(search_zone):
            last_period = m.end()

        if last_period > 0:
            chunks.append(text[start:start + last_period])
            # 重叠：从上一段末尾回退 overlap 个字符
            overlap_start = max(start, start + last_period - max_len // 5)
            start = overlap_start if overlap_start > start else start + last_period
        else:
            # 没找到句子边界，强制切
            chunks.append(text[start:end])
            start = end

    return [c.strip() for c in chunks if c.strip()]


# ============================================================
# 主入口：加载 + 分块
# ============================================================


def load_all_docs(pdf_dir: str = None, corpus_dir: str = None, text_field: str = "text") -> list[dict]:
    """加载所有文档（dataagent 语料为 JSONL，可选支持 .txt/.md）。"""
    docs = []
    if corpus_dir and os.path.isdir(corpus_dir):
        docs.extend(_load_text_files(corpus_dir))
        docs.extend(_load_json_docs(corpus_dir, text_field))
    return docs


def split_docs(docs: list[dict], chunk_size: int = 600, chunk_overlap: int = 120,
               pdf_dir: str = None) -> list[dict]:
    """句子边界分块 + 标题前缀。

    返回 [{"text": str, "source": str, "heading": str, "position": int}]
    """
    all_chunks = []
    position = 0

    for doc in docs:
        source = doc["source"]
        text = doc["text"]

        # 带标题前缀：提升检索相关性
        heading = doc.get("heading", "")
        prefixed_text = f"[{heading}] {text}" if heading else text

        # 句子边界切分
        chunks = _split_at_sentence_boundary(prefixed_text, chunk_size)

        for chunk_text in chunks:
            if not chunk_text.strip():
                continue
            position += 1
            all_chunks.append({
                "text": chunk_text,
                "source": source,
                "heading": heading,
                "position": position,
                "char_count": len(chunk_text),
                "meta": doc.get("meta") or {},
            })

    return all_chunks


# ============================================================
# 文件加载（内部）
# ============================================================

def _load_text_files(directory: str) -> list[dict]:
    results = []
    for root, _, files in os.walk(directory):
        for fname in sorted(files):
            if not fname.lower().endswith((".txt", ".md")):
                continue
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                if text.strip():
                    results.append({"text": text, "source": fname, "heading": ""})
            except Exception:
                pass
    return results


def _load_json_docs(directory: str, text_field: str = "text") -> list[dict]:
    results = []
    for root, _, files in os.walk(directory):
        for fname in sorted(files):
            if not fname.lower().endswith((".json", ".jsonl")):
                continue
            try:
                with open(os.path.join(root, fname), "r", encoding="utf-8") as f:
                    if fname.lower().endswith(".jsonl"):
                        items = [json.loads(line) for line in f if line.strip()]
                    else:
                        data = json.load(f)
                        items = data if isinstance(data, list) else data.get("data", data.get("docs", []))
                for obj in items:
                    if not isinstance(obj, dict):
                        continue
                    text = obj.get(text_field, "")
                    if isinstance(text, list):
                        text = "\n".join(str(x) for x in text)
                    text = str(text) if text else ""
                    if text.strip():
                        src = obj.get("source", obj.get("title", obj.get("id", fname)))
                        heading = obj.get("heading", obj.get("section", ""))
                        doc = {"text": text, "source": str(src), "heading": str(heading)}
                        if obj.get("meta") is not None:
                            doc["meta"] = obj["meta"]
                        results.append(doc)
            except Exception:
                pass
    return results
