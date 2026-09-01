"""语义层目录加载与解析。

提供：
  - load_catalog(path) -> SemanticCatalog：解析 YAML 并建立 指标/维度 索引
  - get_catalog()：单例（可被测试 monkeypatch 覆盖）
  - 查询构建：query_sql() 由语义名确定性生成 SELECT SQL
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

_CATALOG_PATH = Path(__file__).resolve().parent / "catalog.yaml"
_catalog: Optional["SemanticCatalog"] = None

_SAFE_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_\u4e00-\u9fff %\-:]+$")

# 时间粒度 -> DATE_FORMAT 格式（StarRocks 兼容 MySQL）
_GRANULARITY_FORMATS = {
    "day": "%Y-%m-%d",
    "month": "%Y-%m",
    "year": "%Y",
}

# 聚合函数白名单（防注入）
_AGG_WHITELIST = {
    "count": "COUNT",
    "count_distinct": "COUNT(DISTINCT",
    "sum": "SUM",
    "avg": "AVG",
    "max": "MAX",
    "min": "MIN",
}


def _validate_ident(name: str, field: str = "名称") -> str:
    name = (name or "").strip()
    if not _SAFE_IDENT_RE.match(name):
        raise ValueError(f"非法{field}: {name!r}")
    return name


class SemanticTable:
    def __init__(self, raw: dict):
        self.table = _validate_ident(raw.get("table", ""), "表名")
        self.alias = raw.get("alias", "") or self.table
        self.description = raw.get("description", "") or ""
        self.metrics: Dict[str, dict] = {}
        self.dimensions: Dict[str, dict] = {}
        for m in raw.get("metrics", []):
            name = _validate_ident(m.get("name", ""), "指标名")
            agg = str(m.get("agg", "count")).lower()
            if agg not in _AGG_WHITELIST:
                raise ValueError(f"指标 {name} 的聚合方式不支持: {agg}")
            self.metrics[name] = {
                "name": name,
                "display": m.get("display", "") or name,
                "column": _validate_ident(m.get("column", ""), "指标列"),
                "agg": agg,
                "description": m.get("description", "") or "",
            }
        for d in raw.get("dimensions", []):
            name = _validate_ident(d.get("name", ""), "维度名")
            self.dimensions[name] = {
                "name": name,
                "display": d.get("display", "") or name,
                "column": _validate_ident(d.get("column", ""), "维度列"),
                "type": d.get("type", "string"),
            }

    def find_metric(self, name: str) -> Optional[dict]:
        name = (name or "").strip().lower()
        for key, m in self.metrics.items():
            if key.lower() == name or m["display"].lower() == name:
                return m
        return None

    def find_dimension(self, name: str) -> Optional[dict]:
        name = (name or "").strip().lower()
        for key, d in self.dimensions.items():
            if key.lower() == name or d["display"].lower() == name:
                return d
        return None

    def all_metric_names(self) -> List[str]:
        return [f"{m['name']}({m['display']})" for m in self.metrics.values()]

    def all_dimension_names(self) -> List[str]:
        return [f"{d['name']}({d['display']})" for d in self.dimensions.values()]


class SemanticCatalog:
    def __init__(self, tables: List[SemanticTable], default_database: str,
                 default_engine: str, version: int = 1):
        self.tables = tables
        self.default_database = default_database
        self.default_engine = default_engine
        # 语义契约版本：口径变更时递增，决策日志带版本戳，可复现/可审计
        self.version = int(version)

    def table_by_name(self, table_name: str) -> Optional[SemanticTable]:
        for t in self.tables:
            if t.table == table_name or t.alias == table_name:
                return t
        return None

    def pick_table(self, metrics: List[str], dimensions: List[str]) -> SemanticTable:
        """按指标/维度名挑选物理表：取覆盖最多请求字段的表。"""
        best, best_score = None, -1
        for t in self.tables:
            score = 0
            for m in metrics:
                if t.find_metric(m):
                    score += 2
            for d in dimensions:
                if t.find_dimension(d):
                    score += 1
            if score > best_score:
                best, best_score = t, score
        if best is None or best_score <= 0:
            raise ValueError("语义层未找到匹配的表（指标/维度未注册）")
        return best

    def resolve(self, metric_names, dimension_names) -> Tuple[SemanticTable, List[dict], List[dict]]:
        """解析语义名 -> (表, 指标定义列表, 维度定义列表)，含别名与错误提示。"""
        table = self.pick_table(metric_names or [], dimension_names or [])
        metrics, missing_m = [], []
        for name in metric_names or []:
            m = table.find_metric(name)
            if m:
                metrics.append(m)
            else:
                missing_m.append(name)
        dims, missing_d = [], []
        for name in dimension_names or []:
            d = table.find_dimension(name)
            if d:
                dims.append(d)
            else:
                missing_d.append(name)
        if missing_m or missing_d:
            hint = "、".join(table.all_metric_names()) or "（无）"
            dhint = "、".join(table.all_dimension_names()) or "（无）"
            raise ValueError(
                f"未注册的指标/维度: {', '.join(missing_m + missing_d)}。"
                f"表 {table.table} 可选指标: {hint}；可选维度: {dhint}"
            )
        return table, metrics, dims

    def query_sql(
        self,
        metric_names: List[str],
        dimension_names: List[str],
        filters: Optional[List[dict]] = None,
        granularity: str = "",
        limit: int = 1000,
        order_by: Optional[str] = None,
        order_desc: bool = True,
    ) -> str:
        """由语义查询确定性生成 SELECT SQL（只读）。"""
        table, metrics, dims = self.resolve(metric_names, dimension_names)
        dims = list(dims)

        # 时间粒度：对 date 类型维度做 DATE_FORMAT 折叠
        if granularity:
            fmt = _GRANULARITY_FORMATS.get(str(granularity).lower())
            if not fmt:
                raise ValueError(f"不支持的粒度: {granularity}（可选 day/month/year）")
            for i, d in enumerate(dims):
                if d["type"] == "date":
                    dims[i] = {**d, "column": f"DATE_FORMAT({d['column']}, '{fmt}')"}

        select_parts = []
        for d in dims:
            select_parts.append(f"{d['column']} AS `{d['name']}`")
        for m in metrics:
            agg = _AGG_WHITELIST[m["agg"]]
            expr = f"{agg}{m['column']})" if agg.endswith("(") else f"{agg}({m['column']})"
            select_parts.append(f"{expr} AS `{m['name']}`")

        where_parts = []
        for f in filters or []:
            dim = table.find_dimension(f.get("dimension", ""))
            if not dim:
                raise ValueError(
                    f"过滤维度未注册: {f.get('dimension')}（可选: {'、'.join(table.all_dimension_names())}）"
                )
            op = str(f.get("op", "=")).strip().upper()
            if op not in ("=", "!=", ">", ">=", "<", "<=", "LIKE", "IN"):
                raise ValueError(f"不支持的过滤操作: {op}")
            value = str(f.get("value", "")).strip()
            if not _SAFE_VALUE_RE.match(value):
                raise ValueError(f"过滤值含非法字符: {value!r}")
            if op == "IN":
                items = [v.strip() for v in value.split(",") if v.strip()]
                quoted = ", ".join(f"'{v}'" for v in items)
                where_parts.append(f"{dim['column']} IN ({quoted})")
            elif op == "LIKE":
                where_parts.append(f"{dim['column']} LIKE '%{value}%'")
            else:
                where_parts.append(f"{dim['column']} {op} '{value}'")

        sql = f"SELECT {', '.join(select_parts)} FROM {table.table}"
        if where_parts:
            sql += " WHERE " + " AND ".join(where_parts)
        if dims:
            sql += " GROUP BY " + ", ".join(d["column"] for d in dims)
        if order_by:
            dim = table.find_dimension(order_by) or table.find_metric(order_by)
            if dim:
                sql += f" ORDER BY `{dim['name']}` {'DESC' if order_desc else 'ASC'}"
        sql += f" LIMIT {int(limit) if 0 < int(limit) <= 5000 else 1000}"
        return sql

    def explain(
        self,
        metric_names: List[str],
        dimension_names: List[str],
        filters: Optional[List[dict]] = None,
        granularity: str = "",
    ) -> Dict[str, Any]:
        """口径说明（结果可解释性）：返回本次查询的数据来源、指标公式、维度/过滤/粒度。

        与 query_sql 同源（都走 resolve），保证"用户看到的口径"就是"实际执行的口径"，
        LLM 不参与、不可编造。
        """
        table, metrics, dims = self.resolve(metric_names, dimension_names)
        fmt = _GRANULARITY_FORMATS.get(str(granularity).lower())

        cal_metrics = [{
            "name": m["name"], "display": m["display"],
            "column": m["column"], "agg": m["agg"],
            "formula": _metric_formula(m),
        } for m in metrics]
        cal_dims = []
        for d in dims:
            expr = d["column"]
            if fmt and d["type"] == "date":
                expr = f"DATE_FORMAT({d['column']}, '{fmt}')"
            cal_dims.append({"name": d["name"], "display": d["display"],
                             "column": d["column"], "type": d["type"], "expr": expr})
        cal_filters = []
        for f in filters or []:
            dim = table.find_dimension(f.get("dimension", ""))
            cal_filters.append({
                "dimension": dim["display"] if dim else f.get("dimension", ""),
                "op": str(f.get("op", "=")).upper(),
                "value": f.get("value", ""),
            })

        lines = [f"数据来源：{table.alias}（物理表 {table.table}）"]
        lines.append("指标口径：" + "；".join(f"{m['display']} = {m['formula']}" for m in cal_metrics))
        if cal_dims:
            lines.append("分组维度：" + "、".join(d["display"] for d in cal_dims))
        if cal_filters:
            lines.append("过滤条件：" + "；".join(
                f"{f['dimension']} {f['op']} {f['value']}" for f in cal_filters))
        if granularity:
            lines.append("时间粒度：" + _GRANULARITY_LABEL.get(str(granularity).lower(), str(granularity)))
        lines.append(f"执行引擎：{self.default_engine} / 库 {self.default_database}（只读 SELECT）")

        return {
            "table": table.table, "table_alias": table.alias,
            "database": self.default_database, "engine": self.default_engine,
            "metrics": cal_metrics, "dimensions": cal_dims,
            "filters": cal_filters, "granularity": str(granularity or ""),
            "text": "\n".join(lines),
        }


# 时间粒度 -> 中文（口径说明用）
_GRANULARITY_LABEL = {"day": "按天", "month": "按月", "year": "按年"}


def _metric_formula(m: dict) -> str:
    """指标 -> 可读计算公式，如 COUNT(DISTINCT `name`)。"""
    if m["agg"] == "count_distinct":
        return f"COUNT(DISTINCT `{m['column']}`)"
    return f"{_AGG_WHITELIST[m['agg']]}(`{m['column']}`)"




def catalog_to_raw(catalog: "SemanticCatalog") -> Dict[str, Any]:
    """把内存 catalog 导出为可写 YAML / 可编辑 JSON 的原始结构。"""
    return {
        "version": getattr(catalog, "version", 1),
        "default_database": catalog.default_database,
        "default_engine": catalog.default_engine,
        "tables": [{
            "table": t.table,
            "alias": t.alias,
            "description": t.description,
            "metrics": [{
                "name": m["name"], "display": m["display"], "column": m["column"],
                "agg": m["agg"], "description": m["description"],
            } for m in t.metrics.values()],
            "dimensions": [{
                "name": d["name"], "display": d["display"],
                "column": d["column"], "type": d["type"],
            } for d in t.dimensions.values()],
        } for t in catalog.tables],
    }


def save_catalog(raw: Dict[str, Any], path: Optional[Path] = None) -> "SemanticCatalog":
    """校验并保存语义层：先用 SemanticTable 严格校验（标识符/聚合白名单），
    通过后写回 YAML（唯一事实源），再热重载单例。校验失败抛 ValueError。"""
    path = path or _CATALOG_PATH
    tables = [SemanticTable(t) for t in (raw.get("tables") or [])]
    catalog = SemanticCatalog(
        tables,
        default_database=str(raw.get("default_database", "datax_test")),
        default_engine=str(raw.get("default_engine", "starrocks")),
    )
    payload = catalog_to_raw(catalog)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    reset_catalog()
    return catalog


def load_catalog(path: Optional[Path] = None) -> SemanticCatalog:
    path = path or _CATALOG_PATH
    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    tables = [SemanticTable(t) for t in raw.get("tables", [])]
    return SemanticCatalog(
        tables,
        default_database=str(raw.get("default_database", "datax_test")),
        default_engine=str(raw.get("default_engine", "starrocks")),
        version=int(raw.get("version", 1)),
    )


def get_catalog() -> SemanticCatalog:
    global _catalog
    if _catalog is None:
        _catalog = load_catalog()
    return _catalog


def reset_catalog() -> None:
    global _catalog
    _catalog = None
