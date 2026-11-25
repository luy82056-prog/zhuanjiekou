import queue
import unittest
from types import SimpleNamespace

from app import AudioXhl, XiaoeLoginRequired


URL = (
    "https://appexample0123456.h5.xet.pomoho.com/p/course/video/"
    "v_000000000000000000000000?product_id=course_demo"
)


class FakeAuthFlow:
    _xiaoe_shop_prefix = staticmethod(AudioXhl._xiaoe_shop_prefix)
    _has_xiaoe_login_cookie = staticmethod(AudioXhl._has_xiaoe_login_cookie)
    _one_click_cookie_worker = AudioXhl._one_click_cookie_worker

    def __init__(self, results, preferred=""):
        self.events = queue.Queue()
        self.results = results
        self.store = SimpleNamespace(data={
            "xiaoe_source_by_shop": {"appexample0123456": preferred} if preferred else {}
        })

    def _xiaoe_cookie_header(self, _url, source, **_kwargs):
        return "ko_token=valid-session-token; source=" + source

    def _resolve_xiaoe_with_cookie(self, _url, cookie):
        source = cookie.split("source=", 1)[1]
        result = self.results.get(source)
        if isinstance(result, Exception):
            raise result
        if result is None:
            raise XiaoeLoginRequired("该登录来源没有当前章节权限")
        return result


class XiaoeAuthFlowTests(unittest.TestCase):
    def test_stale_first_profile_falls_through_to_valid_browser(self):
        info = {"title": "课程", "extractor": "小鹅通视频音轨", "count": 1}
        flow = FakeAuthFlow({
            "专用 Edge 登录窗口": XiaoeLoginRequired("旧登录已过期"),
            "Edge 登录状态": info,
        }, preferred="专用 Edge 登录窗口")

        flow._one_click_cookie_worker(URL, "专用 Edge 登录窗口", "", "")

        event = flow.events.get_nowait()
        self.assertEqual(event[0], "xiaoe_authorized")
        self.assertEqual(event[3], "Edge 登录状态")
        self.assertIs(event[4], info)

    def test_all_invalid_profiles_request_one_login_after_checks(self):
        flow = FakeAuthFlow({})

        flow._one_click_cookie_worker(URL, "专用 Edge 登录窗口", "", "")

        event = flow.events.get_nowait()
        self.assertEqual(event[0], "xiaoe_cookie_login_required")
        self.assertIn("没有任何登录状态通过", event[1])
        self.assertIn("专用 Edge 登录窗口", event[1])


if __name__ == "__main__":
    unittest.main()
