"""用**本地模拟的同一套表单**验证短信验证码提交机制真的管用。

为什么需要它：我无法触发小红书真实的风控表单（要扫码），所以把"提交验证码"这段
逻辑对着一个仿造页面验证：页面结构与小红书一致的点是——
  · 弹窗里有一个自动聚焦的验证码输入框（placeholder 含"验证码"）
  · 一个「验证」按钮
  · 输错 → 页面文本出现"验证码错误"
  · 输对 → 输入框消失、出现登录后的元素
这样能确定：定位输入框、逐字输入、多种提交方式、结果判定 这几步是对的；
剩下只有"小红书页面属性不同"这一种风险，而那段逻辑已经做了多路兜底。

用法：python test/manual/manual_sms_form.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from services import xhs_login_browser as lb          # noqa: E402

PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>小红书 - 你的生活兴趣社区</title></head><body>
<div class="login-container">
  <div class="login-reason">登录后推荐更懂你的笔记</div>
  <div class="qrcode-img-wrap"><img class="qrcode-img" src="TRUNCATED_PNG"></div>
  <div>扫码成功 请在手机上确认 重新扫码</div>
  <div id="sms">短信验证码验证 验证码将发送至 +86 133******32
    <input id="code" placeholder="请输入验证码" maxlength="6" autofocus>
    <button id="verify" onclick="check()">验证</button>
    <span id="err"></span>
    <button onclick="void 0">获取验证码</button>
  </div>
</div>
<div class="main-container"><div class="user"><div class="link-wrapper"></div></div></div>
<script>
function check() {
  const v = document.getElementById('code').value;
  if (v === '246810') {                       // 正确验证码
    document.getElementById('sms').remove();
    document.querySelector('.link-wrapper').innerHTML = '<span class="channel">我</span>';
    document.title = '小红书 - 已登录';
  } else {
    document.getElementById('err').textContent = '验证码错误，请重新输入';
  }
}
document.getElementById('code').addEventListener('keydown', e => { if (e.key === 'Enter') check(); });
</script></body></html>"""

# 1x1 透明 PNG（合法），避免用截断的 base64 导致 load 事件迟迟不来
TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8AAAwAB/AGtE0mAAAAAAElFTkSuQmCC")


class _FakeCtx:
    def __init__(self, cookies):
        self._cookies = cookies

    async def cookies(self):
        return self._cookies


async def run() -> bool:
    from playwright.async_api import async_playwright

    session = lb._LiveSession("sms-form-test", ROOT / "_sms_test_workdir")
    exe = lb.browser_executable()
    kwargs = {"headless": True,
              "args": ["--no-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN"]}
    if exe:
        kwargs["executable_path"] = exe

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(**kwargs)
    ctx = await browser.new_context(locale="zh-CN", timezone_id="Asia/Shanghai")
    page = await ctx.new_page()
    # 落到本地文件再打开（比 set_content 更接近真实页面，也不会有 load 卡住的问题）
    page_file = ROOT / "_sms_test_form.html"
    page_file.write_text(PAGE.replace("TRUNCATED_PNG", TINY_PNG), encoding="utf-8")
    await page.goto(page_file.as_uri(), wait_until="domcontentloaded")
    await page.wait_for_timeout(500)
    session._page = page
    session._ctx = _FakeCtx([{"name": "web_session", "value": "OLD"}])
    session._session0 = "OLD"

    ok = True
    print("== 1. 状态识别：应判定为 verify_sms 并解析出手机号 ==")
    data = await session._snapshot()
    print(f"   state={data.get('state')} phone={data.get('phone')}")
    ok &= data.get("state") == "verify_sms"
    ok &= data.get("phone") == "+86 133******32"

    print("== 2. 提交错误验证码：应如实回报「验证码错误」 ==")
    bad = await session.submit_code("000000")
    text = await session._page_text()
    print(f"   message={bad.get('message')}")
    print(f"   页面里出现错误提示：{'验证码错误' in text}")
    ok &= "验证码错误" in text
    ok &= bad.get("state") == "verify_sms"

    print("== 3. 提交正确验证码：应判定登录成功（页面出现登录元素） ==")
    # 模拟登录成功同时换掉 web_session（与真实情况一致）
    session._ctx._cookies = [{"name": "web_session", "value": "NEW"}]
    good = await session.submit_code("246810")
    print(f"   state={good.get('state')} tried={good.get('tried')} by={good.get('submitted_by')}")
    ok &= good.get("state") == "logged_in"

    await browser.close()
    await pw.stop()
    import shutil
    (ROOT / "_sms_test_form.html").unlink(missing_ok=True)
    shutil.rmtree(ROOT / "_sms_test_workdir", ignore_errors=True)
    print("\n结果：", "✅ 机制可用" if ok else "❌ 有问题")
    return ok


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
