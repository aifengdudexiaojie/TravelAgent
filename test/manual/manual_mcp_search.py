"""
小红书 MCP 搜索测试 Demo

启动方式:
  1. 启动 xiaohongshu-mcp: 双击 xiaohongshu-mcp-windows-amd64.exe
  2. 运行本脚本: python test_mcp_search.py
"""

import json
from xiaohongshu_mcp_client import _batch_call


def search_notes(keyword: str, limit: int = 6):
    """搜索小红书笔记"""
    print(f"\n{'='*50}")
    print(f"  搜索: {keyword}")
    print(f"{'='*50}")

    result = _batch_call("tools/call", {
        "name": "search_feeds",
        "arguments": {"keyword": keyword},
    })

    if isinstance(result, dict):
        feeds = result.get("feeds", [])
    elif isinstance(result, list):
        feeds = result
    else:
        feeds = []

    print(f"  共找到 {len(feeds)} 条结果\n")

    for i, f in enumerate(feeds[:limit], 1):
        card = f.get("noteCard", {})
        title = card.get("displayTitle", "") or ""
        user = card.get("user", {})
        author = user.get("nickname", "未知")
        interact = card.get("interactInfo", {})
        likes = interact.get("likedCount", 0)
        nid = f.get("id", "")

        print(f"  [{i}] {title}" if title else f"  [{i}] (无标题)")
        print(f"      likes: {likes}  author: {author}")
        if nid:
            print(f"      https://www.xiaohongshu.com/explore/{nid}")
        print()

    return feeds

def get_note_detail(note_id: str, xsec_token: str, load_comments: bool):
    """
    获取笔记详情（正文、图片、评论等）

    参数:
        note_id: 笔记 ID
        xsec_token: 访问令牌
        load_comments: True=加载全部评论

    返回:
        {
            "title": "...",
            "desc": "正文...",
            "author": { "nickname": "...", "avatar": "..." },
            "interact": { "likedCount": 424, "commentCount": "16", ... },
            "images": ["url1", ...],
            "comments": [{ "userInfo": {...}, "content": "..." }, ...]
        }
    """
    print(f"\n--- 获取笔记详情: {note_id} ---")

    arguments = {
        "feed_id": note_id,
        "xsec_token": xsec_token,
    }
    if load_comments:
        arguments["load_all_comments"] = True
        arguments["limit"] = 20

    result = _batch_call("tools/call", {
        "name": "get_feed_detail",
        "arguments": arguments,
    })

    if isinstance(result, dict):
        data = result.get("data", {})
        note = data.get("note", result)

        title = note.get("title", "") or ""
        desc = note.get("desc", "") or ""
        user = note.get("user", {})
        interact = note.get("interactInfo", {})
        images = note.get("imageList", [])

        # 评论在 data.comments.list 里
        comments_raw = data.get("comments", {})
        comments = comments_raw.get("list", []) if isinstance(comments_raw, dict) else []

        print(f"  标题: {title or '(无标题)'}")
        print(f"  标签: {desc[:100]}")
        print(f"  作者: {user.get('nickname', '?')}")
        print(f"  ❤️ {interact.get('likedCount', 0)}  💬 {len(comments)} 条评论")
        if images:
            print(f"  🖼️ {len(images)} 张图片")
        if comments:
            print(f"\n  评论预览:")
            print(len(comments))
            for c in comments[:5]:
                ui = c.get("userInfo", {})
                nick = ui.get("nickname", "匿名")
                content = c.get("content", "")
                likes = c.get("likeCount", 0)
                print(f"    [{likes}赞] {nick}: {content}")

    return result



def show_status():
    """检查登录状态"""
    print("\n--- 登录状态 ---")
    result = _batch_call("tools/call", {"name": "check_login_status"})
    if isinstance(result, dict):
        for k, v in result.items():
            print(f"  {k}: {v}")
        return True
    else:
        print(f"  {result}")
        return False


if __name__ == "__main__":
    print("=" * 50)
    print("  小红书 MCP 搜索 Demo")
    print("=" * 50)

    # 1. 检查登录
    show_status()

    # 2. 搜索
    feeds = search_notes("厦门旅游攻略")
    note = feeds[0]
    detail = get_note_detail(
        note_id=note["id"],
        xsec_token=note["xsecToken"],
        load_comments=True,  # 加载全部评论
    )

    # 3. 拿到内容和评论
    title = detail.get("title")
    content = detail.get("desc")
    comments = detail.get("comments", [])
    images = detail.get("imageList", [])

    print("\n测试完成！")
