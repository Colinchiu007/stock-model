"""模拟盘「定时运行」界面的契约测试（CI 可跑，不需要浏览器）

为什么是"契约"而不是"界面测试"
-------------------------------
界面在本地用**真实浏览器**验证过（`experiments/verify_paper_ui.py`，7 项断言全过，
含"非交易日如实显示未执行"）。但**项目没有把 Playwright 纳入依赖** ——
CI 里跑不了浏览器（下载 chromium 会让每个 job 多几分钟）。

所以 CI 这一层只能锁**契约**（下面 4 条）。看不到的那部分——布局、可读性、
交互时序——**这层确实测不到**，别把它当视觉回归门禁。

其中第 4 条是**跨层锁**：后端 status 改了字段名而前端没跟着改时，这条会红 ——
"界面读一个不存在的字段"在浏览器里只表现为空白，很容易漏过去。
"""

from __future__ import annotations

import pathlib

from stock_model.paper.scheduler import PaperScheduler, ScheduleState

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
STATIC = REPO_ROOT / "src" / "stock_model" / "web" / "static"
# 卡片的容器：只需要 HTML 有(样式走 class)，JS 不必引用
CONTAINER_IDS = ("pp-sched-card",)

# 按钮：必须有**点击处理器**才算接线 —— 只断言"id 在文件里出现过"太弱:
# 变异验证时实测过, 把某一个 addEventListener 改坏, 子串检查照样通过(假锁)。
BUTTON_IDS = ("pp-sched-start", "pp-sched-stop", "pp-sched-run")

# 展示型元素：HTML 与 JS 必须成对出现，少一个就是"接线接了一半"
DISPLAY_IDS = (
    "pp-sched-state",
    "pp-sched-plan",
    "pp-sched-last",
    "pp-sched-alert",
    "pp-sched-warns",
    "pp-sched-time",
    "pp-sched-msg",
)

SCHEDULE_ELEMENT_IDS = CONTAINER_IDS + BUTTON_IDS + DISPLAY_IDS

# 前端渲染要读的 status 字段
RENDERED_STATUS_FIELDS = (
    "running",
    "schedule",
    "next_run_time",
    "last_run_at",
    "last_status",
    "last_result",
    "consecutive_failures",
    "last_error",
    "alert_channel",
    "alert_count",
    "warnings",
)


def _read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


class TestScheduleCardElements:
    """HTML 与 JS 必须对同一批 id 达成一致"""

    def test_html_has_all_elements(self):
        html = _read("index.html")
        missing = [i for i in SCHEDULE_ELEMENT_IDS if f'id="{i}"' not in html]
        assert not missing, f"index.html 缺少定时卡片元素: {missing}"

    def test_js_references_display_elements(self):
        """光有 HTML 不算接线 —— 展示型元素必须被 JS 真的读/写

        容器(``pp-sched-card``)不在此列: 它只用来定位/样式, JS 不必碰它。
        """
        js = _read("dashboard.js")
        missing = [i for i in DISPLAY_IDS if i not in js]
        assert not missing, f"dashboard.js 没有引用这些元素(接线不完整): {missing}"

    def test_buttons_have_click_handlers(self):
        """按钮必须真的接上 click —— 这是"点了没反应"的唯一机械防线

        变异验证: 删掉某个 addEventListener, 本测试必红。
        （只用子串判断"id 出现过"是假锁: 同一个 id 在别处出现一次就够了。）
        """
        js = _read("dashboard.js")
        unwired = [b for b in BUTTON_IDS if f"$('{b}').addEventListener('click'" not in js]
        assert not unwired, f"这些按钮没有接 click 处理器(点了没反应): {unwired}"

    def test_css_covers_schedule_card(self):
        """样式必须跟上 —— 否则新卡片会挤在一起, 看起来像坏了"""
        css = _read("dashboard.css")
        for cls in (".schedule-card", ".sched-actions", ".badge.ok", ".badge.err"):
            assert cls in css, f"dashboard.css 缺少 {cls}"

    def test_uses_text_prefix_not_color_only(self):
        """状态用「文字前缀 + 颜色」双通道表达, 不单靠颜色

        色盲用户与灰度截图同样要能读懂"已开启/未开启/不可用"。
        """
        js = _read("dashboard.js")
        for prefix in ("● 已开启", "● 未开启", "● 不可用", "● 状态不明"):
            assert prefix in js, f"缺少状态文字前缀: {prefix}"


class TestScheduleCardEndpointContract:
    """JS 调用的端点与 HTTP 方法必须与后端一致"""

    def test_js_calls_schedule_endpoints(self):
        js = _read("dashboard.js")
        assert "/api/paper/schedule?account_id=default" in js, "查询/停止端点的调用缺失"
        assert "'/api/paper/schedule'" in js, "开启端点调用缺失"
        assert "/api/paper/schedule/run" in js, "立即执行端点调用缺失"

    def test_delete_is_used_for_stop(self):
        js = _read("dashboard.js")
        assert "method: 'DELETE'" in js, "停止定时必须用 DELETE(与后端一致)"

    # 端点是否真的注册了 GET/POST/DELETE 与 /run, 由
    # tests/test_paper_api_endpoints.py 用 TestClient 覆盖(需要 fastapi)。
    # 这里只锁前端调用侧的契约, 不重复造一套。


class TestScheduleStatusFieldContract:
    """**跨层锁**：前端渲染读的字段必须真的由后端 status 返回"""

    @staticmethod
    def _status(tmp_path) -> dict:
        sched = PaperScheduler(
            schedule_file=tmp_path / "schedule.json",
            holiday_file=tmp_path / "holidays.json",
        )
        sched.set_runner(lambda account_id, days: {"status": "ok", "steps": 1, "persisted": True})
        sched._states["default"] = ScheduleState(account_id="default")
        try:
            return sched.status("default")
        finally:
            sched.shutdown()

    def test_rendered_fields_exist_in_status(self, tmp_path):
        info = self._status(tmp_path)
        missing = [f for f in RENDERED_STATUS_FIELDS if f not in info]
        assert not missing, (
            f"后端 status 缺少前端渲染需要的字段: {missing} —— 浏览器里只会表现为空白, 很难发现"
        )

    def test_js_still_uses_those_fields(self):
        """反向: 前端不再用某字段时, 提醒同步本测试"""
        js = _read("dashboard.js")
        unused = [f for f in RENDERED_STATUS_FIELDS if f not in js]
        assert not unused, f"dashboard.js 已不再使用这些字段, 请更新本测试: {unused}"

    def test_alert_channel_is_never_empty(self, tmp_path):
        """告警通道字段必须给出一句话(哪怕"未接入"), 不能是空串 ——
        空串会让界面显示"告警通道：", 用户看不出到底有没有告警"""
        assert self._status(tmp_path)["alert_channel"]
