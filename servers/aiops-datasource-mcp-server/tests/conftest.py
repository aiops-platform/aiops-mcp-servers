"""共享 fixtures：清配置缓存 + 隔离常用 env + 上游默认地址 + 冻结的 CMDB 基线。"""
from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest
from aiops_datasource_mcp_server.backends import entity_graph as eg
from aiops_datasource_mcp_server.config import get_settings

#: 包内那份**活文件**的真实路径。刻意不走 `resolve_cmdb_path()`——后者会被
#: `DATASOURCE_CMDB_PATH` 影响，而下面那道守卫要盯的正是"没人动过它"。
_LIVE_CMDB = Path(
    str(resources.files("aiops_datasource_mcp_server").joinpath("data", "cmdb-entities.json"))
)


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


@pytest.fixture(scope="session", autouse=True)
def _live_cmdb_must_stay_untouched():
    """**整轮测试不得改动包内那份活实体文件。**

    这是踩了两次之后加的防护网，两次都是"测试全绿、数据被悄悄改了"：

    1. `cmdb` 夹具里先调 `eg.resolve_cmdb_path()`（它**顺带把 `get_settings` 的
       lru_cache 暖热**），之后 `monkeypatch.setenv` 再也进不去——写操作直接落到真文件；
    2. 夹具参数名拼成 `cmbd`（少个 a），pytest 静默不注入夹具、参数取默认 `None`，
       于是测试拿不到 tmp 副本，原地改真数据。

    两次的症状一模一样：测试通过，没人发现数据变了。所以这里不靠人细心，
    靠会话前后各取一次哈希——变了就报，并指出该从哪里恢复。
    """
    before = _digest(_LIVE_CMDB)
    yield
    after = _digest(_LIVE_CMDB)
    if before != after:
        pytest.fail(
            f"测试改动了包内实体文件（活数据）：{_LIVE_CMDB}\n"
            f"  会话前 sha256: {before}\n"
            f"  会话后 sha256: {after}\n"
            f"这几乎总是某个夹具没生效（参数名拼错？settings 缓存没清？），"
            f"导致写操作落到了真文件上。请 `git diff` 该文件确认改了什么。",
            pytrace=False,
        )

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
