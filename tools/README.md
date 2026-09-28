# 小红书登录助手（给终端用户用）

## 它解决什么问题

服务器（云主机）直接登录小红书时，XHS 看到的是"**云 IP + 全新设备**"，
对部分账号会触发风控：要求短信验证码（短信有每日额度，试几次就发不出来），
甚至警告账号。

这个助手把登录挪到**用户自己的电脑**上完成 —— 那就是一次完全正常的登录，
不触发风控；登录成功后它把登录态同步给服务器，服务器只负责"用登录态抓数据"。

## 用户怎么用（三步）

1. 网页上点「登录小红书」→ 展开「登不上小红书？用登录助手」→ 点**生成配对码**（8 位，10 分钟、一次性）；
2. 运行助手：`xhs-login-helper.exe`（Windows）或 `python tools/xhs_login_helper.py`；
3. 助手会弹出一个浏览器窗口，**在里面正常登录小红书**（扫码或手机号都行）——
   登录完成后助手自动把登录态同步到服务器，网页状态灯随之变绿。

## 为什么必须用助手/扩展，而不能纯网页

注册凭据 `web_session` 是 **HttpOnly + Secure**：
浏览器从设计上禁止网页 JS 读取它（同源策略 + HttpOnly）。
所以能做这件事的只有三种：**浏览器扩展**（有 cookies API）、
**本地小程序**（自己驱动浏览器，能读全部 cookie）、用户手动从 DevTools 复制。
助手就是第二种：用 CDP（Chrome DevTools Protocol）连接它自己启动的浏览器。

## 打包成 exe（发给用户前做一次）

```powershell
cd D:\TravelAgent
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --onefile --name xhs-login-helper `
    --console tools\xhs_login_helper.py
# 产物：dist\xhs-login-helper.exe（约 10~15MB，可放到站点上供用户下载）
```

打包后无需用户装 Python（依赖只有标准库 + websockets，会一起打进去）。

## 其他

* 助手使用独立浏览器 profile（`%TEMP%\xhs-login-helper-profile`），下次运行会复用，
  设备身份稳定；不想复用就删掉该目录。
* 若提示"浏览器启动后立即退出"：多半是上一次的助手浏览器还开着，关掉再运行。
* 助手不会上传除 cookie 之外的任何东西；配对码一次性且 10 分钟过期。
