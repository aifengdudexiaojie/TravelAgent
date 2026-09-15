"""把 RAG 里"归属错误"的攻略重新归属到真正的作者（一次性数据修复工具）
================================================================================

背景
----
早期从攻略页自动写入 RAG 时，前端没把当前用户身份传过来，后端就用了环境变量
默认归属（默认 `rag_test_user`）。结果：聊天「历史模式」现在按 user_id 过滤
（只检索自己的攻略，见 services/rag/retrieval.py 的 visibility=own），
这些"自己的老攻略"反而检索不到了。

做法
----
把 RAG(PG `guides`) 里"归属可疑"的攻略，到 ES `travel_guides`（也就是"我的攻略"
列表用的索引）里按 **目的地 + 天数** 找到真正的作者，然后把归属一起改到：
    · PG      guides.user_id / username / nickname
    · ES      guide_search_docs 的同名字段（词法过滤用）
    · Qdrant  各向量集合里该攻略所有分块的 payload（服务端过滤用）

"归属可疑" = 当前归属是默认归属，或者该 user_id 在 ES users 索引里已经不存在（孤儿）。
匹配不到唯一作者的一律不动，并在报告里列出原因。

用法
----
    python fix_guide_owner.py              # dry-run：只打印会改什么（默认）
    python fix_guide_owner.py --apply      # 真正执行
    python fix_guide_owner.py --apply --include-orphan-only   # 只修孤儿归属
"""

from __future__ import annotations

import argparse
import sys
from typing import Dict, List, Optional, Tuple

from services.env_bootstrap import setup_console
from services.rag.store import (
    DEFAULT_OWNER,
    GUIDE_INDEX as RAG_GUIDE_INDEX,
    VECTOR_COLLECTIONS,
    GuideStores,
    es_client as rag_es_client,
    qdrant_client as rag_qdrant_client,
)

setup_console()


def _travel_guides_index() -> str:
    from services.es_client import GUIDE_INDEX  # "travel_guides"（我的攻略）
    return GUIDE_INDEX


def main() -> None:
    parser = argparse.ArgumentParser(description="把 RAG 里归属错误的攻略改回真正的作者")
    parser.add_argument("--apply", action="store_true", help="真正写入（默认只 dry-run）")
    parser.add_argument("--include-orphan-only", action="store_true",
                        help="只处理孤儿归属（user_id 已不存在），不动默认归属的")
    args = parser.parse_args()

    default_owner = str(DEFAULT_OWNER.get("user_id") or "rag_test_user")
    print(f"默认归属（会被视为可疑）：{default_owner}")
    print(f"模式：{'APPLY（会写入）' if args.apply else 'DRY-RUN（只打印）'}\n")

    stores = GuideStores()
    es = stores.es
    qd = stores.qd
    travel_index = _travel_guides_index()

    # ---- 1) 收集"我的攻略"里的真实作者（按 目的地+天数 建索引） ----
    res = es.search(index=travel_index, size=500,
                    _source=["guide_id", "user_id", "username", "nickname",
                             "title", "destination", "days"])
    candidates: Dict[Tuple[str, Optional[int]], List[dict]] = {}
    for hit in res["hits"]["hits"]:
        src = hit["_source"]
        if not src.get("user_id") or src["user_id"] == default_owner:
            continue
        key = (str(src.get("destination") or "").strip(), src.get("days"))
        candidates.setdefault(key, []).append(src)
    print(f"「我的攻略」里可用的作者样本：{sum(len(v) for v in candidates.values())} 条")

    # ---- 2) 收集 ES users 里存在的用户（判断孤儿） ----
    try:
        users_res = es.search(index="users", size=1000, _source=["user_id"])
        known_users = {h["_source"].get("user_id") for h in users_res["hits"]["hits"]}
    except Exception as exc:
        print(f"⚠️ 读取 users 索引失败（孤儿判定将跳过）：{exc}")
        known_users = set()

    # ---- 3) 遍历 RAG 攻略，找出需要修的 ----
    rows = stores.pg.query(
        "SELECT id, user_id, username, nickname, title, destination, days FROM guides"
    )
    fixes: List[dict] = []
    for guide_id, user_id, username, nickname, title, destination, days in rows:
        guide_id = str(guide_id)
        is_default = str(user_id) == default_owner
        is_orphan = bool(known_users) and str(user_id) not in known_users
        if not (is_default or is_orphan):
            continue
        if args.include_orphan_only and not is_orphan:
            continue

        key = (str(destination or "").strip(), days)
        matched = candidates.get(key) or candidates.get((str(destination or "").strip(), None)) or []
        owners = {(m["user_id"], m.get("username"), m.get("nickname")) for m in matched}
        if len(owners) != 1:
            print(f"跳过 {title[:26]:28} 目的地={destination} 天数={days} → "
                  f"匹配到 {len(owners)} 个候选作者")
            continue
        new_user_id, new_username, new_nickname = next(iter(owners))
        fixes.append({
            "guide_id": guide_id, "title": title,
            "old_user_id": user_id, "old_username": username,
            "new_user_id": new_user_id, "new_username": new_username, "new_nickname": new_nickname,
            "reason": "孤儿归属" if is_orphan else "落到了默认归属",
        })

    if not fixes:
        print("\n没有需要修复的攻略。")
        stores.close()
        return

    print(f"\n待修复 {len(fixes)} 条：")
    for f in fixes:
        print(f"  · {f['title'][:30]:32} {f['reason']:12} "
              f"{str(f['old_user_id'])[:12]}… → {str(f['new_user_id'])[:12]}… ({f['new_username']})")

    if not args.apply:
        print("\n（dry-run 结束，加 --apply 才会真正写入）")
        stores.close()
        return

    # ---- 4) 执行修复 ----
    rag_es = rag_es_client()
    qd_raw = rag_qdrant_client()
    print("\n开始写入…")
    for f in fixes:
        # PG
        stores.pg.query(
            "UPDATE guides SET user_id = %s, username = %s, nickname = %s WHERE id = %s",
            (f["new_user_id"], f["new_username"], f["new_nickname"], f["guide_id"]),
        )
        # ES（词法检索的 user_id 过滤）
        try:
            rag_es.update(index=RAG_GUIDE_INDEX, id=f["guide_id"], doc={
                "user_id": f["new_user_id"],
                "username": f["new_username"],
                "nickname": f["new_nickname"],
            })
        except Exception as exc:
            print(f"    ⚠️ ES 更新失败 {f['guide_id']}: {exc}")
        # Qdrant（向量检索的服务端过滤）
        from qdrant_client import models
        for coll in VECTOR_COLLECTIONS:
            try:
                qd_raw.set_payload(
                    collection_name=coll,
                    payload={"user_id": f["new_user_id"], "username": f["new_username"],
                             "nickname": f["new_nickname"]},
                    points=models.Filter(must=[models.FieldCondition(
                        key="guide_id", match=models.MatchValue(value=f["guide_id"]))]),
                    wait=True,
                )
            except Exception as exc:
                print(f"    ⚠️ Qdrant 更新失败 {coll}/{f['guide_id']}: {exc}")
        print(f"  ✅ {f['title'][:30]} → {f['new_username']}")

    stores.close()
    print(f"\n完成：{len(fixes)} 条攻略的归属已更新（PG / ES / Qdrant 三处一致）")


if __name__ == "__main__":
    sys.exit(main())
