"""语义层目录查询：问数可用的指标/维度清单（只读）。"""
from fastapi import APIRouter

from src.semantic import get_catalog

router = APIRouter()


@router.get("/semantic/catalog")
async def semantic_catalog():
    """返回语义层注册表：表、指标、维度，供问数提示与 UI 展示。"""
    catalog = get_catalog()
    tables = []
    for t in catalog.tables:
        tables.append({
            "table": t.table,
            "alias": t.alias,
            "description": t.description,
            "metrics": [
                {"name": m["name"], "display": m["display"], "agg": m["agg"], "description": m["description"]}
                for m in t.metrics.values()
            ],
            "dimensions": [
                {"name": d["name"], "display": d["display"], "type": d["type"]}
                for d in t.dimensions.values()
            ],
        })
    return {
        "default_database": catalog.default_database,
        "default_engine": catalog.default_engine,
        "tables": tables,
    }
