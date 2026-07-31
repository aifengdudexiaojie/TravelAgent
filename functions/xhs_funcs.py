from xiaohongshu_mcp_client import _batch_call


def search_notes(keyword: str, limit: int):
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

    # for i, f in enumerate(feeds[:limit], 1):
    #     card = f.get("noteCard", {})
    #     title = card.get("displayTitle", "") or ""
    #     user = card.get("user", {})
    #     author = user.get("nickname", "未知")
    #     interact = card.get("interactInfo", {})
    #     likes = interact.get("likedCount", 0)
    #     nid = f.get("id", "")
    #
    #     print(f"  [{i}] {title}" if title else f"  [{i}] (无标题)")
    #     print(f"      likes: {likes}  author: {author}")
    #     if nid:
    #         print(f"      https://www.xiaohongshu.com/explore/{nid}")
    #     print()

    return feeds[:limit]


def get_note_detail(note_id: str, xsec_token: str, load_comments: bool = False):
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
        arguments["limit"] = 5

    result = _batch_call("tools/call", {
        "name": "get_feed_detail",
        "arguments": arguments,
    })

    # if isinstance(result, dict):
    #     # 实际返回结构: result.data.note.{...}
    #     note = result.get("data", {}).get("note", result)
    #
    #     title = note.get("title", "") or result.get("title", "")
    #     desc = note.get("desc", "") or result.get("desc", "") or ""
    #     user = note.get("user", {})
    #     interact = note.get("interactInfo", {})
    #     images = note.get("imageList", [])
    #     comments = note.get("comments", [])
    #     print(desc)
    #     print(f"  标题: {title or '(无标题)'}")
    #     print(f"  正文: {desc}...")
    #     print(f"  作者: {user.get('nickname', '?')}")
    #     print(f"  ❤️ {interact.get('likedCount', 0)}  💬 {interact.get('commentCount', 0)} 条评论")
    #     if images:
    #         print(f"  🖼️ {len(images)} 张图片")
    #     if comments:
    #         print(f"\n  评论预览:")
    #         for c in comments[:3]:
    #             ui = c.get("userInfo", {})
    #             nick = ui.get("nickname", "匿名")
    #             content = c.get("content", "")[:80]
    #             print(f"    👤 {nick}: {content}")

    return result


def show_status():
    """检查登录状态"""
    print("\n--- 登录状态 ---")
    result = _batch_call("tools/call", {"name": "check_login_status"})
    if isinstance(result, dict):
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        print(f"  {result}")
    print()


def get_notes_by_query(query: str):
    show_status()