"""导出的 JSON Schema 必须与 pydantic 模型同步——**靠测试，不靠纪律**。

`docs/cmdb-entities.schema.json` 是给将来的 CMDB 构建界面用的（表单生成 + 编辑器侧校验）。
运行时校验走 pydantic（错误信息更好，且能表达 JSON Schema 表达不了的规则：id 前缀与 type
一致、引用完整性、标签词表、端点类型相容）。两份表示并存就有漂移风险——这个测试就是防线。
"""
from __future__ import annotations

import json
from pathlib import Path

from aiops_datasource_mcp_server.backends.entity_graph import EntityDocument

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "docs" / "cmdb-entities.schema.json"

_REGENERATE = (
    "重新生成：cd servers/aiops-datasource-mcp-server && uv run python -c \""
    "import json,pathlib;"
    "from aiops_datasource_mcp_server.backends.entity_graph import EntityDocument;"
    "pathlib.Path('docs/cmdb-entities.schema.json').write_text("
    "json.dumps(EntityDocument.model_json_schema(), indent=2, sort_keys=True, "
    "ensure_ascii=False)+chr(10), encoding='utf-8')\""
)


def _generated() -> str:
    return (
        json.dumps(
            EntityDocument.model_json_schema(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
    )


def test_schema_file_exists() -> None:
    assert SCHEMA_PATH.is_file(), f"缺少 {SCHEMA_PATH}"


def test_checked_in_schema_matches_model() -> None:
    """模型改了但没重新生成 schema → 这里失败。"""
    assert SCHEMA_PATH.read_text(encoding="utf-8") == _generated(), (
        f"docs/cmdb-entities.schema.json 与 EntityDocument 模型已漂移。\n{_REGENERATE}"
    )


def test_nodes_is_an_open_map_and_documents_why() -> None:
    """"12 个类型键齐全"这条约束**不在** schema 里，只能靠运行时校验。

    它是一个**跨字段**约束——`nodes` 的键必须恰好等于 `ontology.node_types` 里声明的
    类型。JSON Schema 表达不了这种"键集等于另一个字段所声明的集合"，所以 schema 里
    `nodes` 只能是开放的 string → Node[] 映射。

    别为了让 schema 好看去硬列 12 个键：那会造出第二份类型清单，与 ontology 漂移。
    真正的保证在 ``test_entity_graph_backend.py`` 的
    ``test_missing_node_type_key_rejected`` / ``test_undeclared_node_type_key_rejected``。
    """
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    nodes = schema["properties"]["nodes"]
    assert nodes["additionalProperties"]["items"]["$ref"] == "#/$defs/Node"
    assert "properties" not in nodes   # 刻意不是固定键的对象


def test_schema_forbids_extra_properties() -> None:
    """打错的键不能静默忽略——运行时不接受，schema 也要如实反映。"""
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["additionalProperties"] is False
    assert schema["$defs"]["Node"]["additionalProperties"] is False
