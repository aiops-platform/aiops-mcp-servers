"""共享 fixtures：清配置缓存 + 隔离常用 env。

照 `aiops-datasource-mcp-server/tests/conftest.py` 的形状（`clear_settings_cache`
+ `env` 两个夹具）—— 配置是 `lru_cache` 的，**不清缓存则 monkeypatch.setenv 进不去**，
那正是隔壁那个夹具注释里记着的坑（"先访问了 settings，之后 setenv 再也进不去"）。
"""
from __future__ import annotations

import json

import pytest

from deploy_ops_mcp_server.config import get_settings

TARGETS = {
    "order-service": {"namespace": "order", "deployment": "order-service",
                      "container": "order-service"},
    "warranty-service": {"namespace": "order", "deployment": "warranty-service",
                         "container": "warranty-service"},
}


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def env(monkeypatch, clear_settings_cache):
    """隔离 Settings：固定目标表与三档超时，避免测试依赖本机 `.env`。

    ⚠️ 环境变量**优先于** `.env`（pydantic-settings 的优先级），所以这里 setenv 之后
    即使从仓库根跑（cwd 与 server 目录不同、找不到 `.env`）也稳定。
    """
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("AUTH_TOKEN", "")
    monkeypatch.setenv("DEPLOY_OPS_TARGETS", json.dumps(TARGETS))
    # 三条超时的关系必须是 subprocess > rollout（config 的 model_validator 会拦反例）
    monkeypatch.setenv("DEPLOY_OPS_ROLLOUT_TIMEOUT_SEC", "30")
    monkeypatch.setenv("DEPLOY_OPS_SUBPROCESS_TIMEOUT_SEC", "40")
    monkeypatch.setenv("DEPLOY_OPS_IMAGE_LOAD_TIMEOUT_SEC", "60")
    monkeypatch.setenv("DEPLOY_OPS_PROBE_TIMEOUT_SEC", "5")
    return get_settings()
