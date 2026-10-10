"""仪表盘认证测试(TD-05): Bearer Token 中间件

威胁模型
--------
仪表盘暴露模拟盘资金操作(POST /api/paper/reset 清空账户!)与策略信号。
默认部署在 127.0.0.1/局域网; 一旦映射到公网, 任何人可清空账户。

契约(全部锁定):
  - 未配置 STOCK_API_TOKEN  -> 完全不启用(本地/内网体验不变, 向后兼容)
  - 配置后 /api/* 全部要求 Authorization: Bearer <token>
  - /api/health **豁免**(容器探针/负载均衡不能要求凭据)
  - 静态资源与首页豁免(无敏感数据; 前端登录页超出本次范围, 文档建议反代加头)
  - 错误: 401 + WWW-Authenticate: Bearer
  - 比较: secrets.compare_digest(时序安全)
全部离线(TestClient), 不依赖真实网络。
"""

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from stock_model.web.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[1]


def _reload_settings(monkeypatch, token):
    """重载 settings 模块并注入 STOCK_API_TOKEN(单例在 import 时创建)"""
    import stock_model.config.settings as settings_mod

    monkeypatch.setenv("STOCK_API_TOKEN", token)
    if token:
        monkeypatch.setattr(settings_mod, "_settings", None)
        return settings_mod.get_settings()
    monkeypatch.setattr(settings_mod, "_settings", None)
    return settings_mod.get_settings()


@pytest.fixture
def no_auth(monkeypatch):
    monkeypatch.delenv("STOCK_API_TOKEN", raising=False)
    import stock_model.config.settings as sm

    monkeypatch.setattr(sm, "_settings", None)
    app = create_app()
    with TestClient(app) as c:
        yield c


@pytest.fixture
def with_auth(monkeypatch):
    _reload_settings(monkeypatch, "secret-token-123")
    app = create_app()
    with TestClient(app) as c:
        yield c
    import stock_model.config.settings as sm

    monkeypatch.setattr(sm, "_settings", None)


class TestAuthDisabled:
    """未配置 token = 完全不启用(向后兼容, 本地体验不变)"""

    def test_api_accessible_without_token(self, no_auth):
        assert no_auth.get("/api/health").status_code == 200

    def test_paper_account_accessible(self, no_auth):
        r = no_auth.get("/api/paper/account")
        assert r.status_code == 200, r.text


class TestAuthEnabled:
    def test_api_without_token_is_401(self, with_auth):
        r = with_auth.get("/api/paper/account")
        assert r.status_code == 401
        assert r.headers["www-authenticate"] == "Bearer"

    def test_api_with_wrong_token_is_401(self, with_auth):
        r = with_auth.get("/api/paper/account", headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

    def test_api_with_correct_token_is_ok(self, with_auth):
        r = with_auth.get(
            "/api/paper/account", headers={"Authorization": "Bearer secret-token-123"}
        )
        assert r.status_code == 200, r.text

    def test_health_is_exempt(self, with_auth):
        """容器探针/负载均衡不能要求凭据"""
        assert with_auth.get("/api/health").status_code == 200

    def test_static_is_exempt(self, with_auth):
        """前端静态资源豁免(无敏感数据; 登录页方案见 OPERATIONS)"""
        assert with_auth.get("/").status_code == 200

    def test_reset_is_protected_too(self, with_auth):
        """**最敏感的端点**: 清空账户必须同样被保护"""
        r = with_auth.post("/api/paper/reset", json={"account_id": "default"})
        assert r.status_code == 401

    def test_scheme_must_be_bearer(self, with_auth):
        r = with_auth.get("/api/paper/account", headers={"Authorization": "Basic secret-token-123"})
        assert r.status_code == 401


class TestCompareDigest:
    """时序安全比较必须**真的在比较处被调用**(而不是文件里出现过)

    第一版是字符串存在性检查, 变异(把 compare_digest 换成 ==, 字样仍在)没红 —— 假锁。
    改用 AST: 找到认证中间件函数, 断言其体内的比较表达式确实调用了 compare_digest。
    """

    @staticmethod
    def _middleware_fn():
        import ast

        src = (REPO_ROOT / "src" / "stock_model" / "web" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "_bearer_auth"
            ):
                return node
        pytest.fail("未找到 _bearer_auth 中间件(可能被重命名, 请同步本测试)")

    def test_compare_digest_called_in_middleware(self):
        import ast

        fn = self._middleware_fn()
        calls = [
            n
            for n in ast.walk(fn)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "compare_digest"
        ]
        assert calls, "认证中间件必须调用 secrets.compare_digest(时序安全)"

    def test_middleware_does_not_use_plain_eq_for_token(self):
        """中间件体内不得出现 `credential == <token>` 这类裸比较"""
        import ast

        fn = self._middleware_fn()
        for node in ast.walk(fn):
            if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.Eq):
                left = ast.unparse(node.left)
                assert "credential" not in left, (
                    f"发现裸比较: {ast.unparse(node)[:60]} —— 请改用 compare_digest"
                )


class TestSettingsWiring:
    def test_token_field_exists_in_settings(self):
        p = REPO_ROOT / "src" / "stock_model" / "config" / "settings.py"
        src = p.read_text(encoding="utf-8")
        assert "api_token" in src, "Settings 需要新增 api_token 字段"
        assert "STOCK_API_TOKEN" in src or "env_prefix" in src
