"""构建 DataX 官方文档双语语料。

从 DataX 官方仓库（GitHub / Gitee 镜像）读取插件 `doc/*.md` 与顶层文档，
清洗成「中文说明 + 英文参数/错误码关键词 + JSON 配置样例」的结构化条目，
输出 src/rag 可直接灌库的 JSONL 语料（每行 {source, heading, text}）。

双语策略（解决「中文 embedding vs 英文 JSON 配置」的匹配问题）：
  1. 保留文档中的英文参数名与 JSON 样例原文（直接参与向量化）；
  2. 每个段落追加“关键词 Keywords: ...”行，显式列出英文参数名/插件名；
  3. 每个插件生成一条“插件总览”双语条目（英文 plugin.json 描述 + 中文摘要）。

用法：
  python scripts/build_datax_corpus.py --repo <DataX仓库路径> --out <语料目录>
  python scripts/build_datax_corpus.py                # 使用默认路径
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

DEFAULT_REPO = (
    Path(__file__).resolve().parent.parent / "data" / "datax_docs" / "DataX"
)
DEFAULT_OUT = (
    Path(__file__).resolve().parent.parent / "data" / "datax_docs" / "corpus"
)

# 与本项目（数据集成 Agent）相关的核心插件；其余插件文档保持不动
PLUGINS = [
    "mysqlreader", "mysqlwriter",
    "mongodbreader", "mongodbwriter",
    "elasticsearchwriter",
    "starrockswriter",
    "dorisreader", "doriswriter",
    "selectdbwriter",
    "rdbmsreader", "rdbmswriter",
    "hdfsreader", "hdfswriter",
    "oraclereader", "oraclewriter",
    "sqlserverreader", "sqlserverwriter",
    "postgresqlreader", "postgresqlwriter",
]

# 顶层通用文档
GENERAL_DOCS = ["README.md", "introduction.md", "userGuid.md", "dataxPluginDev.md"]

# 附加的 JSON 配置样例（插件目录下 doc 之外的 json 样例）
EXTRA_SAMPLES = {
    "doriswriter": ["mysql2doris.json"],
    "selectdbwriter": ["stream2selectdb.json"],
}

# ---------------------------------------------------------------------------
# Markdown 清洗
# ---------------------------------------------------------------------------

_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_IMG_RE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def clean_inline(text: str) -> str:
    """去掉 markdown 链接/图片/HTML 残留，保留可读文本。"""
    text = _IMG_RE.sub(r"\1", text)
    text = _LINK_RE.sub(r"\1", text)
    text = _HTML_BR_RE.sub("；", text)
    text = _HTML_TAG_RE.sub("", text)
    text = text.replace("```", "").replace("**", "").replace("`", "")
    text = text.replace("___", "").replace("---", "")
    return text.strip()


def _is_heading_line(line: str) -> tuple[int, str] | None:
    m = re.match(r"^(#{1,4})\s+(.+?)\s*#*\s*$", line)
    if m:
        return len(m.group(1)), m.group(2).strip()
    return None


def iter_sections(text: str) -> list[dict]:
    """按标题层级切分 markdown，返回 [{level, title, body}]。"""
    lines = text.splitlines()
    sections: list[dict] = []
    cur: dict | None = None

    def flush():
        nonlocal cur
        if cur is not None:
            body = "\n".join(cur["lines"]).strip()
            if body:
                cur["body"] = body
                sections.append(cur)
        cur = None

    for line in lines:
        h = _is_heading_line(line)
        if h:
            flush()
            cur = {"level": h[0], "title": h[1], "lines": []}
        elif cur is not None:
            cur["lines"].append(line)
        else:
            # 文档开头的无标题段落（如 H1 之前的说明）
            if not sections and line.strip():
                cur = {"level": 0, "title": "概述", "lines": []}
                cur["lines"].append(line)
    flush()
    return sections


def extract_code_blocks(body: str) -> tuple[str, list[str]]:
    """提取 ``` 围栏代码块，返回 (去掉代码块后的 body, 代码块列表)。"""
    blocks: list[str] = []
    out_lines: list[str] = []
    in_block = False
    buf: list[str] = []
    for line in body.splitlines():
        if line.strip().startswith("```"):
            if in_block:
                blocks.append("\n".join(buf))
                buf = []
                in_block = False
            else:
                in_block = True
            continue
        if in_block:
            buf.append(line)
        else:
            out_lines.append(line)
    if buf:
        blocks.append("\n".join(buf))
    return "\n".join(out_lines), blocks


def _parse_param_item(lines: list[str]) -> dict | None:
    """解析 `* **param**` 或 `* param` 开头的参数块。"""
    head = lines[0] if lines else ""
    m = re.match(r"^\s*\*\s+\*{0,2}([A-Za-z_][A-Za-z0-9_.]*)\*{0,2}\s*[:：]?\s*(.*)$", head)
    if not m:
        return None
    desc_parts = [m.group(2)] if m.group(2).strip() else []
    required = default = ""
    for line in lines[1:]:
        s = line.strip().lstrip("*").strip()
        if s.startswith("描述"):
            desc_parts.append(clean_inline(s.split("：", 1)[-1].split(":", 1)[-1]))
        elif s.startswith("必选"):
            required = clean_inline(s.split("：", 1)[-1].split(":", 1)[-1])
        elif s.startswith("默认值"):
            default = clean_inline(s.split("：", 1)[-1].split(":", 1)[-1])
        elif s and not s.startswith(("注意", "参考")):
            desc_parts.append(clean_inline(s))
    desc = "；".join(p for p in desc_parts if p).strip("；")
    info = f"参数 {m.group(1)}：{desc}"
    if required:
        info += f"；必选：{required.strip('；')}"
    if default:
        info += f"；默认值：{default.strip('；')}"
    return {"name": m.group(1), "text": info}


_PARAM_HEAD_RE = re.compile(
    r"^\s*\*\s+\*{0,2}([A-Za-z_][A-Za-z0-9_.]*)\*{0,2}\s*[:：]?\s*"
)
_PARAM_SUB_RE = re.compile(r"^\s*\*\s*(描述|必选|必须|默认值|说明|注意|参考)")
_PARAM_INLINE_RE = re.compile(
    r"^\s*\*\s+([A-Za-z_][A-Za-z0-9_.]*)\s*[:：]\s*(.+)$"
)


def parse_param_docs(body: str) -> tuple[str, list[str], str]:
    """把 `* **参数名**` 列表解析成可检索文本，返回 (文本, 参数名, 剩余正文)。

    兼容三种写法：
      A. `* **name**` + 缩进的 `* 描述/必选/默认值` 子列表（mysqlwriter）
      B. `* name` + 同层 `* 描述/必选/默认值`（elasticsearchwriter）
      C. 单行 `* name：描述`（mongodbwriter）
    """
    lines = body.splitlines()
    result: list[str] = []
    names: list[str] = []
    consumed: set[int] = set()
    buf: list[tuple[int, str]] = []

    def flush_buf():
        if not buf:
            return
        item = _parse_param_item([l for _, l in buf])
        if item:
            names.append(item["name"])
            result.append(item["text"])
            for idx, _ in buf:
                consumed.add(idx)
        buf.clear()

    for i, line in enumerate(lines):
        if _PARAM_HEAD_RE.match(line):
            flush_buf()
            buf = [(i, line)]
        elif line.strip().startswith("*") and buf and _PARAM_SUB_RE.match(line):
            # 参数块的续行（描述/必选/默认值/注意）
            buf.append((i, line))
        elif not line.strip():
            # 空行不打断参数块（官方文档参数块间有空行）
            continue
        elif _PARAM_INLINE_RE.match(line):
            flush_buf()
            m = _PARAM_INLINE_RE.match(line)
            names.append(m.group(1))
            result.append(f"参数 {m.group(1)}：{clean_inline(m.group(2))}")
            consumed.add(i)
        else:
            flush_buf()

    flush_buf()
    remaining = "\n".join(l for i, l in enumerate(lines) if i not in consumed)
    return "\n".join(result), names, remaining


def parse_tables(body: str) -> tuple[str, list[str]]:
    """把 markdown 表格转成 `col1 → col2` 或 `col1：col2` 文本。"""
    lines = body.splitlines()
    result: list[str] = []
    rows: list[list[str]] = []
    for line in lines:
        if line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            # 跳过分隔行 | --- | --- |
            if cells and all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                continue
            rows.append(cells)
        elif rows:
            for row in rows:
                row = [clean_inline(c) for c in row if c]
                if len(row) >= 2:
                    result.append(" → ".join(row))
                elif row:
                    result.append(row[0])
            rows = []
    if rows:
        for row in rows:
            row = [clean_inline(c) for c in row if c]
            if len(row) >= 2:
                result.append(" → ".join(row))
            elif row:
                result.append(row[0])
    return "\n".join(result), []


def extract_json_keys(text: str) -> list[str]:
    """从 JSON 代码块中提取键名（英文关键词）。"""
    keys: list[str] = []
    for m in re.finditer(r'"([A-Za-z_][A-Za-z0-9_]*)":', text):
        k = m.group(1)
        if k not in keys:
            keys.append(k)
    return keys


def clean_body(body: str) -> str:
    """正文清洗：代码块保留、参数/表格结构化、去掉噪音行。"""
    body, code_blocks = extract_code_blocks(body)
    text_parts: list[str] = []

    # 参数说明
    params_text, names, remaining = parse_param_docs(body)
    if params_text:
        text_parts.append(params_text)

    # 表格
    tables_text, _ = parse_tables(body)
    if tables_text:
        text_parts.append(tables_text)

    # 剩余正文（去代码块、参数块、表格后）
    rest_lines = []
    for line in remaining.splitlines():
        s = line.strip()
        if not s or s in ("___", "---", "*", "**"):
            continue
        if s.startswith("|"):
            continue
        if _PARAM_HEAD_RE.match(line) or _PARAM_SUB_RE.match(line):
            continue
        rest_lines.append(clean_inline(line))
    rest = "\n".join(l for l in rest_lines if l).strip()
    if rest:
        text_parts.append(rest)

    for block in code_blocks:
        text_parts.append(f"配置样例 JSON:\n{block}")

    return "\n".join(p for p in text_parts if p).strip()


def build_keyword_line(*groups: list[str]) -> str:
    seen: list[str] = []
    for g in groups:
        for k in g:
            k = k.strip()
            if k and k not in seen:
                seen.append(k)
    if not seen:
        return ""
    return "关键词 Keywords: " + ", ".join(seen[:80])


# ---------------------------------------------------------------------------
# 插件处理
# ---------------------------------------------------------------------------


def _plugin_overview(repo: Path, plugin: str) -> dict | None:
    """插件总览：英文 plugin.json 描述 + 中文文档首段 + 参数名。"""
    pj = repo / plugin / "src" / "main" / "resources" / "plugin.json"
    en_desc = ""
    if pj.exists():
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
            en_desc = data.get("description", "").strip()
        except Exception:
            pass

    zh_desc = ""
    doc = repo / plugin / "doc"
    if doc.is_dir():
        for fp in sorted(doc.glob("*.md")):
            sections = iter_sections(fp.read_text(encoding="utf-8", errors="ignore"))
            intro = next((s for s in sections if "介绍" in s["title"] or s["level"] <= 1), None)
            if intro:
                zh_desc = clean_inline(intro["body"].split("\n")[0][:200])
            if zh_desc:
                break

    text = f"# {plugin} 插件总览（DataX {plugin} Plugin Overview）"
    if en_desc:
        text += f"\n英文描述: {en_desc}"
    if zh_desc:
        text += f"\n中文简介: {zh_desc}"
    text += (
        f"\n用途: DataX 数据源插件，用于 {plugin} 与其它数据源之间的同步。"
        "配置位置: job.content[].reader/writer.name = \"" + plugin + "\"。"
    )

    # 关键词：插件名 + 文档配置样例中的 JSON 键
    json_keys: list[str] = []
    doc_dir = repo / plugin / "doc"
    if doc_dir.is_dir():
        for fp in sorted(doc_dir.glob("*.md"))[:2]:
            json_keys.extend(extract_json_keys(fp.read_text(encoding="utf-8", errors="ignore")))
    kw = build_keyword_line([plugin], json_keys)
    if kw:
        text += "\n" + kw
    return {"source": f"datax_docs/{plugin}", "heading": f"{plugin} - 插件总览", "text": text}


def _process_plugin(repo: Path, plugin: str) -> list[dict]:
    entries: list[dict] = []
    overview = _plugin_overview(repo, plugin)
    if overview:
        entries.append(overview)

    doc_dir = repo / plugin / "doc"
    if not doc_dir.is_dir():
        return entries

    for fp in sorted(doc_dir.glob("*.md")):
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        sections = iter_sections(raw)
        for sec in sections:
            body = clean_body(sec["body"])
            if not body:
                continue
            # 性能/测试报告等空壳小节跳过
            title = sec["title"]
            if re.search(r"(性能报告|测试报告|Performance|Test Report)", title) and len(body) < 80:
                continue
            names: list[str] = []
            _, names, _ = parse_param_docs(sec["body"])
            json_keys = extract_json_keys(body)
            kw = build_keyword_line([plugin], names, json_keys)
            text = body
            if kw:
                text += "\n" + kw
            entries.append({
                "source": f"datax_docs/{plugin}",
                "heading": f"{plugin} - {title}",
                "text": text,
            })

    # 附加 JSON 样例
    for sample in EXTRA_SAMPLES.get(plugin, []):
        fp = repo / plugin / sample
        if fp.exists():
            raw = fp.read_text(encoding="utf-8", errors="ignore")
            keys = extract_json_keys(raw)
            kw = build_keyword_line([plugin], keys)
            entries.append({
                "source": f"datax_docs/{plugin}",
                "heading": f"{plugin} - 配置样例 {sample}",
                "text": f"# DataX {plugin} 完整配置样例: {sample}\n{raw}\n{kw}",
            })
    return entries


def _process_general(repo: Path) -> list[dict]:
    entries: list[dict] = []
    for name in GENERAL_DOCS:
        fp = repo / name
        if not fp.exists():
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        doc_name = fp.stem
        sections = iter_sections(raw)
        for sec in sections:
            body = clean_body(sec["body"])
            if not body:
                continue
            entries.append({
                "source": f"datax_docs/{doc_name}",
                "heading": f"DataX 通用文档 - {doc_name} - {sec['title']}",
                "text": body,
            })
    return entries


# ---------------------------------------------------------------------------
# 踩坑经验（本项目的实战沉淀，与官方文档互补）
# ---------------------------------------------------------------------------

EXPERIENCES: list[tuple[str, str]] = [
    (
        "mongodb_address",
        """# 踩坑：MongoDB Reader/Writer 的 address 格式
现象: address 配成嵌套数组 [["127.0.0.1:27017"]] 或对象会报地址解析错误。
解决: address 必须是一层字符串数组，如 ["127.0.0.1:27017"]；集群多节点时 ["ip1:27017","ip2:27017"]。
补充: 无鉴权时 userName/userPassword 必须留空字符串（不是 null 也不是缺省）。""",
    ),
    (
        "mongodb_channel",
        """# 踩坑：MongoDB 源不支持分片，channel 必须为 1
现象: 用 mongodbreader 且 job.setting.speed.channel > 1 时，同一批数据被多通道重复读取，产生重复数据。
原因: MongoDB reader 未实现分片逻辑，多通道会各自全量读取。
解决: mongo 作为 reader 的 job 里 channel 固定为 1。""",
    ),
    (
        "mongodbwriter_column",
        """# 踩坑：mongodbwriter 的 column 字段键必须用 name
现象: column 写成 [{"key": "id", "type": "long"}] 会报字段找不到。
解决: MongoDB 插件 column 的键是 name（不是 key）: [{"name": "id", "type": "long"}]。
类型: int/long/double/string/date/bool/bytes/array，必须与源数据真实类型一致，声明错误会全部变成脏数据。""",
    ),
    (
        "mongodbwriter_writemode",
        """# 踩坑：mongodbwriter 的 writeMode 是 JSON 对象，不是字符串
现象: writeMode 写成 "upsert" 字符串会报参数类型错误。
解决: writeMode 必须是对象: {"isReplace": "true", "replaceKey": "id"}。
isReplace=true 表示按 replaceKey（业务主键）做更新；replaceKey 必须能唯一定位记录。""",
    ),
    (
        "mysqlwriter_jdbcurl",
        """# 踩坑：mysqlwriter 的 jdbcUrl 是字符串，mysqlreader 是数组
现象: 把 reader 的 jdbcUrl 数组写法直接复制给 writer 会报错。
解决: mysqlwriter 的 connection.jdbcUrl 是单个字符串，且一个 jdbcUrl 只能对应一个主库（不支持多主库负载）。
注意: 作业运行时 DataX 会自动追加 yearIsDateType=false&zeroDateTimeBehavior=convertToNull&rewriteBatchedStatements=true。
编码: 需要指定连接编码时在 jdbcUrl 末尾追加 useUnicode=true&characterEncoding=gbk。""",
    ),
    (
        "mysqlreader_splitpk",
        """# 经验：mysqlreader 提高并行度的 splitPk
说明: mysqlreader 不支持 FetchSize；数据量大时建议配置 splitPk（主键/自增列）让 DataX 按区间并行查询。
补充: querySql 模式不支持 splitPk；where 条件里不要写分号结尾。""",
    ),
    (
        "es_type_mapping",
        """# 经验：MySQL → Elasticsearch 字段类型映射
映射: bigint→long, int→integer, smallint/tinyint→short, float→float, double→double, decimal→double,
varchar/char→keyword（精确匹配）, text/longtext→text（全文搜索）, datetime/timestamp/date→date, boolean→boolean, json→object。
注意: keyword 用于精确匹配（term/agg），text 用于全文搜索（match）；选错会导致查询行为不符合预期。
工程建议: cleanup=false 避免覆盖已有索引；大数据量 batchSize 调到 5000-10000。""",
    ),
    (
        "starrocks_write",
        """# 经验：本机容器 StarRocks 写入两种方式
方式一（已验证）: 用 mysqlwriter 走 StarRocks FE 的 MySQL 协议（jdbc:mysql://127.0.0.1:9030/），writeMode 用 insert，
适合小数据量/个人环境，规避容器网络与 StreamLoad 端口映射问题。
方式二（官方）: starrockswriter 通过 StreamLoad 以 csv 格式导入，需配置 loadUrl（BE:8030）、selectedDatabase、column。
注意: 两种方式的 column 顺序必须与目标表列一致；preSql/postSql 可用于导入前后清理。""",
    ),
    (
        "incremental_sync",
        """# 经验：DataX 增量同步建议
方案: 用 mysqlreader 的 where 条件 + 游标字段（自增 id 或 update_time 时间戳）实现增量。
示例: "where": "id > ${last_max_id}" 或 "update_time >= '${last_max}'"。
注意: 时间字段要用与源库一致的格式；游标要落在索引列上避免全表扫描；多表批量时每张表单独维护游标。""",
    ),
    (
        "datax_error_troubleshooting",
        """# 经验：DataX 常见报错排查顺序
1. 连接失败: 先 telnet 目标端口，确认 jdbcUrl/账号密码/防火墙；
2. 脏数据: 看 job 输出 ERROR 行的脏数据明细，通常是类型不匹配（mongo/ES 插件最常见）；
3. 超时: 大数据量提高 channel 或 batchSize，ES 插件 batchSize 默认 1000；
4. 权限: StarRocks/MySQL 用户需要 INSERT/SELECT 权限；
5. 字符集: 中文乱码检查 jdbcUrl characterEncoding 与目标表 charset；
6. 通道重复: 检查 mongo 源 channel 是否为 1。""",
    ),
]


def _process_experiences() -> list[dict]:
    return [
        {
            "source": f"datax_experience/{name}",
            "heading": f"踩坑经验 - {name}",
            "text": text,
        }
        for name, text in EXPERIENCES
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def _git_head(repo: Path) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            return r.stdout.strip()[:12]
    except Exception:
        pass
    return "unknown"


def build_corpus(repo: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    all_entries: list[dict] = []

    for plugin in PLUGINS:
        entries = _process_plugin(repo, plugin)
        all_entries.extend(entries)
        fp = out / f"plugin_{plugin}.jsonl"
        fp.write_text(
            "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries),
            encoding="utf-8",
        )

    general = _process_general(repo)
    all_entries.extend(general)
    (out / "datax_general.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in general),
        encoding="utf-8",
    )

    exp = _process_experiences()
    all_entries.extend(exp)
    (out / "datax_experience.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in exp),
        encoding="utf-8",
    )

    # Manifest：来源 + 版本可追溯
    manifest = {
        "source": "DataX 官方仓库（alibaba/DataX）",
        "repo_commit": _git_head(repo),
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "entries": len(all_entries),
        "plugins": PLUGINS,
    }
    (out / "MANIFEST.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="构建 DataX 官方双语语料")
    parser.add_argument("--repo", type=Path, default=DEFAULT_REPO)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.repo.is_dir():
        raise SystemExit(f"仓库目录不存在: {args.repo}")
    if not (args.repo / "userGuid.md").exists():
        raise SystemExit(f"不是 DataX 仓库根目录（缺少 userGuid.md）: {args.repo}")

    manifest = build_corpus(args.repo, args.out)
    print(f"语料构建完成: {args.out}")
    print(f"  条目数: {manifest['entries']}")
    print(f"  来源: {manifest['source']} @ {manifest['repo_commit']}")


if __name__ == "__main__":
    main()
