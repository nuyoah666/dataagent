"""DataX 语料构建器测试：验证 Markdown 清洗、双语关键词、经验条目。"""

import json
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import build_datax_corpus as bdc  # noqa: E402


@pytest.fixture
def fake_repo(tmp_path):
    """构造迷你 DataX 仓库：三种参数文档格式 + 通用文档。"""
    repo = tmp_path / "DataX"
    repo.mkdir(parents=True)
    (repo / "userGuid.md").write_text(
        "# DataX\n\nDataX 是离线数据同步工具。\n\n## Quick Start\n\n下载 datax.tar.gz 解压后运行 `python datax.py job.json`。",
        encoding="utf-8",
    )
    (repo / "README.md").write_text(
        "# DataX\n\n支持异构数据源同步。\n\n## Features\n\n插件化 Reader/Writer 体系。",
        encoding="utf-8",
    )

    # mysqlreader：`* **param**` + 缩进子列表格式
    mr = repo / "mysqlreader" / "doc"
    mr.mkdir(parents=True)
    (mr / "mysqlreader.md").write_text(
        """# MysqlReader 插件文档

## 1 快速介绍

MysqlReader 通过 JDBC 从 MySQL 读取数据。

## 3.2 参数说明

* **username**

\t* 描述：数据源的用户名 <br />

\t* 必选：是 <br />

* **jdbcUrl**

\t* 描述：JDBC 连接信息，支持多个备库探测

\t* 必选：是

* **column**

\t* 描述：需要同步的列，["*"] 表示全部列
""",
        encoding="utf-8",
    )
    (repo / "mysqlreader" / "src" / "main" / "resources").mkdir(parents=True)
    (repo / "mysqlreader" / "src" / "main" / "resources" / "plugin.json").write_text(
        json.dumps({
            "name": "mysqlreader",
            "description": "useScene: prod. mechanism: Jdbc connection.",
        }),
        encoding="utf-8",
    )

    # mongodbwriter：`* name：描述` 单行格式 + 类型转换表格 + JSON 样例
    mw = repo / "mongodbwriter" / "doc"
    mw.mkdir(parents=True)
    (mw / "mongodbwriter.md").write_text(
        """### Datax MongoDBWriter
#### 4 参数说明

* address：MongoDB 数据地址，需以 Json 数组给出。【必填】
* userName：MongoDB 用户名。【选填】

#### 5 类型转换

| DataX 内部类型 | MongoDB 数据类型 |
| -------- | ----- |
| Long | int, Long |
| Double | double |

#### 6 配置样例

```json
{
  "writer": {
    "name": "mongodbwriter",
    "parameter": {
      "address": ["127.0.0.1:27017"],
      "writeMode": {"isReplace": "true", "replaceKey": "id"}
    }
  }
}
```
""",
        encoding="utf-8",
    )
    (repo / "mongodbwriter" / "src" / "main" / "resources").mkdir(parents=True)
    (repo / "mongodbwriter" / "src" / "main" / "resources" / "plugin.json").write_text(
        json.dumps({"name": "mongodbwriter", "description": "MongoDB writer."}),
        encoding="utf-8",
    )
    return repo


def _load_jsonl(fp: Path) -> list[dict]:
    return [json.loads(line) for line in fp.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_build_corpus_structure(fake_repo, tmp_path):
    out = tmp_path / "corpus"
    bdc.build_corpus(fake_repo, out)

    assert (out / "MANIFEST.json").exists()
    manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["entries"] > 0
    assert manifest["repo_commit"] == "unknown"  # 假仓库无 git

    general = _load_jsonl(out / "datax_general.jsonl")
    assert any("userGuid" in e["source"] for e in general)

    exp = _load_jsonl(out / "datax_experience.jsonl")
    assert len(exp) == len(bdc.EXPERIENCES)
    assert all(e["source"].startswith("datax_experience/") for e in exp)


def test_param_parsing_formats(fake_repo, tmp_path):
    out = tmp_path / "corpus"
    bdc.build_corpus(fake_repo, out)

    mr = _load_jsonl(out / "plugin_mysqlreader.jsonl")
    param = next(e for e in mr if "参数说明" in e["heading"])
    assert "参数 username：数据源的用户名" in param["text"]
    assert "必选：是" in param["text"]
    assert "参数 jdbcUrl：JDBC 连接信息" in param["text"]
    # 关键词行包含英文参数名
    assert "关键词 Keywords:" in param["text"]
    assert "username" in param["text"].split("关键词 Keywords:")[-1]

    mw = _load_jsonl(out / "plugin_mongodbwriter.jsonl")
    param_w = next(e for e in mw if "参数说明" in e["heading"])
    assert "参数 address：MongoDB 数据地址" in param_w["text"]
    # 类型转换表格转为文本
    type_tab = next(e for e in mw if "类型转换" in e["heading"])
    assert "Long → int, Long" in type_tab["text"]
    # JSON 配置样例保留且键名进关键词
    sample = next(e for e in mw if "配置样例" in e["heading"])
    assert '"address"' in sample["text"]
    assert "writeMode" in sample["text"].split("关键词 Keywords:")[-1]


def test_plugin_overview_bilingual(fake_repo, tmp_path):
    out = tmp_path / "corpus"
    bdc.build_corpus(fake_repo, out)

    mr = _load_jsonl(out / "plugin_mysqlreader.jsonl")
    overview = next(e for e in mr if "插件总览" in e["heading"])
    assert "Plugin Overview" in overview["text"]
    assert "英文描述" in overview["text"]
    assert "中文简介" in overview["text"]


def test_skip_empty_sections(fake_repo, tmp_path):
    out = tmp_path / "corpus"
    bdc.build_corpus(fake_repo, out)

    mr = _load_jsonl(out / "plugin_mysqlreader.jsonl")
    # 文档没有"性能报告"小节；经验条目源前缀正确
    assert all(not e["heading"].startswith("性能报告") for e in mr)
    assert all(e["source"] == "datax_docs/mysqlreader" for e in mr)
