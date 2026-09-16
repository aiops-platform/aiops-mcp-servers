"""共享 fixtures：清配置缓存 + 隔离常用 env + 上游默认地址 + 冻结的 CMDB 基线。"""
from __future__ import annotations

from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.config import get_settings

#: 冻结的迁移基线：`cmdb.py` 字面量 → JSON 实体文件那次迁移的**产物快照**。
#:
#: ⚠️ **它必须是一份冻结副本，不能是包内那份活文件。** CMDB 自 2026-09-16 起是
#: **可编辑**的（编辑页 + `/admin/cmdb/**`），活文件会随人工编辑不断变化。
#: 任何"断言数据具体长什么样"的测试只要读活文件，就会在**每次有人改数据时**变红
#: ——而改数据恰恰是这个功能的目的。行为测试（拓扑方向、图查询、派生索引）要的是
#: **稳定可控的数据**。
#:
#: 活文件本身由 `test_cmdb_entities_data.py` 里明确标注「活文件」的那几条负责，
#: 它们只断言**结构与不变式**，不钉住数量。
BASELINE_CMDB = Path(__file__).resolve().parent / "fixtures" / "cmdb-migration-baseline.json"


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def baseline_cmdb(monkeypatch, clear_settings_cache):
    """把 CMDB 指向冻结基线，返回它的路径。需要显式拿路径的测试用它。"""
    monkeypatch.setenv("DATASOURCE_CMDB_PATH", str(BASELINE_CMDB))
    get_settings.cache_clear()
    eg.clear_graph_cache()
    yield BASELINE_CMDB
    eg.clear_graph_cache()


@pytest.fixture
def env(monkeypatch, clear_settings_cache):
    """隔离 Settings 常用项，指向测试用的上游地址与**冻结的 CMDB 基线**。

    CMDB 那条见 `BASELINE_CMDB` 的说明——简言之：活文件是可编辑的用户数据，
    不能拿它当行为测试的输入。
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("DATASOURCE_ES_URL", "http://es.test")
    monkeypatch.setenv("DATASOURCE_ES_INDEX", "app-logs")
    monkeypatch.setenv("DATASOURCE_PROM_URL", "http://prom.test")
    monkeypatch.setenv("DATASOURCE_K8S_NAMESPACE", "order")
    monkeypatch.setenv("DATASOURCE_REQUEST_TIMEOUT_SEC", "5.0")
    monkeypatch.setenv("DATASOURCE_MAX_RESPONSE_BYTES", "4096")
    monkeypatch.setenv("DATASOURCE_MAX_RANGE_HOURS", "24")
    monkeypatch.setenv("DATASOURCE_CMDB_PATH", str(BASELINE_CMDB))
    eg.clear_graph_cache()
    return get_settings()


# 固定测试窗口（避免用 now() 造成断言不稳定）
START = "2026-09-10T06:00:00"
END = "2026-09-10T07:00:00"
