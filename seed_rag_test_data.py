"""RAG 检索测试种子数据生成器
====================================================
依据 skills/travel-summarizer.md 的输出 schema，生成多份完整攻略
（成都 / 西安 / 杭州+乌镇 / 三亚），写入 Elasticsearch(travel_guides)，
并自动执行一组"聊天式"探针查询验证 RAG 能否命中对应攻略。

用法（先确保 ES 已启动、.env 已配置 embedding）：
    docker compose up -d elasticsearch
    python seed_rag_test_data.py            # 入库 + 默认探针验证
    python seed_rag_test_data.py --probe "去年去西安的兵马俑安排"   # 追加自定义探针

说明：
- 入库字段完全走正式路径 services.guide_service.create_guide_record，
  与线上"攻略生成"存储逻辑一致（含 summary 向量化）。
- 探针查询模拟真实聊天：带"上次/去年/以前"等触发词，
  可同时验证 should_use_rag 触发 + 混合检索命中。
- 不删除已有数据；重复运行会产生多份相同目的地攻略（可用于压力测试）。
"""

import argparse
import json
import sys

# ============================================================
# 测试账号（可按需改为你的真实 user_id）
# ============================================================
TEST_USER = "rag_test_user"
TEST_USERNAME = "rag_tester"
TEST_NICKNAME = "RAG测试用户"


# ============================================================
# 探针查询 -> (查询文本, 期望命中的 destination)
# ============================================================
DEFAULT_PROBES = [
    ("上次去成都玩，攻略里推荐的火锅和兔头是哪几家？", "成都"),
    ("去年国庆去西安，兵马俑和大雁塔那两天是怎么安排的？", "西安"),
    ("以前去杭州乌镇三天两夜，住宿在西栅还是东栅方便？", "杭州+乌镇"),
    ("上次带小孩去三亚，防晒和蜈支洲岛要注意什么？", "三亚"),
]


# ============================================================
# 攻略数据集（结构遵循 skills/travel-summarizer.md 输出 schema；
# 另附 itinerary/food/tags 便于 guide_service 自动提取关键词）
# ============================================================

def _spot(name, type_, location, lng, lat, summary, highlights=None,
          precautions=None, duration="2小时", cost="免费", rec_count=3,
          sources=("小红书实测帖",)):
    return {
        "name": name, "type": type_, "location": location,
        "coordinates": {"lng": lng, "lat": lat}, "summary": summary,
        "highlights": highlights or [], "precautions": precautions or [],
        "duration": duration, "cost": cost,
        "recommendation_count": rec_count, "sources": list(sources),
    }


def _food(name, location, dishes, summary, avg_cost, sources=("大众点评实测帖",)):
    return {
        "name": name, "category": "main_dish", "location": location,
        "recommended_dishes": dishes, "summary": summary,
        "avg_cost": avg_cost, "precautions": [], "sources": list(sources),
    }


def _extra(name, type_, location, lng, lat, summary, reason, duration="2小时",
           cost="免费", sources=("小红书实测帖",)):
    return {
        "name": name, "type": type_, "location": location,
        "coordinates": {"lng": lng, "lat": lat}, "summary": summary,
        "duration": duration, "cost": cost,
        "recommend_reason": reason, "sources": list(sources),
    }


# ---------------- 1. 成都 3 天（美食 + 熊猫，relaxed） ----------------
CHENGDU = {
    "destination": "成都",
    "days": 3,
    "title": "3天成都慢游：熊猫基地+武侯祠锦里+火锅串串美食清单",
    "summary": (
        "成都3天慢游攻略：第一天熊猫繁育研究基地看大熊猫和小熊猫，中午洞子口张老二凉粉，"
        "下午武侯祠三国文化加锦里古街逛吃，晚上小龙坎火锅；第二天杜甫草堂、宽窄巷子喝盖碗茶、"
        "晚上串串香配冰粉；第三天人民公园鹤鸣茶社采耳体验老成都慢生活，返程前买麻辣兔头和"
        "夫妻肺片伴手礼。人均预算约1500元，整体节奏轻松不赶路。"
    ),
    "tags": ["成都", "美食", "熊猫", "慢游", "火锅", "串串", "宽窄巷子", "武侯祠"],
    "content": {
        "destination": "成都",
        "days": 3,
        "meta": {
            "destinations": ["成都"], "total_days": "3天",
            "date_range": {"start": None, "end": None},
            "budget": {"total": 1800, "estimated": 1520, "status": "within_budget"},
            "pace": "relaxed", "generated_at": None,
        },
        "trade_off_summary": {
            "total_spots_found": 12, "spots_selected": 8, "spots_excluded": 4,
            "exclusion_reasons": {
                "青城山": "距市区较远且时间紧张，3天排不下",
                "成都欢乐谷": "与用户放松型节奏不匹配",
            },
        },
        "daily_plan": {
            "第一天": {
                "date": None, "day_theme": "熊猫与三国文化",
                "periods": [
                    {"period": "早上", "content": "游览大熊猫繁育研究基地（约3小时）",
                     "type": "scenic_spot",
                     "detail": {"name": "成都大熊猫繁育研究基地", "summary": "看大熊猫幼崽与孔雀放养",
                                "duration": "3小时", "cost": "55元",
                                "coordinates": {"lng": 104.150, "lat": 30.750}}},
                    {"period": "中午", "content": "午餐：洞子口张老二凉粉（人均20元）",
                     "type": "food",
                     "detail": {"name": "洞子口张老二凉粉", "summary": "成都老字号凉粉",
                                "duration": "40分钟", "cost": "20元",
                                "coordinates": {"lng": 104.098, "lat": 30.681}}},
                    {"period": "下午", "content": "武侯祠博物馆 + 锦里古街",
                     "type": "scenic_spot",
                     "detail": {"name": "武侯祠·锦里", "summary": "三国文化遗迹与民俗街",
                                "duration": "4小时", "cost": "60元",
                                "coordinates": {"lng": 104.047, "lat": 30.645}}},
                    {"period": "晚上", "content": "晚餐：小龙坎老火锅，餐后回酒店休息",
                     "type": "food",
                     "detail": {"name": "小龙坎老火锅", "summary": "牛油麻辣锅底，推荐毛肚黄喉",
                                "duration": "1.5小时", "cost": "80元",
                                "coordinates": {"lng": 104.066, "lat": 30.658}}},
                ],
                "day_cost": 215, "tips": "熊猫基地建议早上8点前到，人少熊猫活跃",
            },
            "第二天": {
                "date": None, "day_theme": "诗圣草堂与老成都巷子",
                "periods": [
                    {"period": "早上", "content": "杜甫草堂博物馆",
                     "type": "scenic_spot",
                     "detail": {"name": "杜甫草堂", "summary": "唐代诗人杜甫故居园林",
                                "duration": "2.5小时", "cost": "50元",
                                "coordinates": {"lng": 104.029, "lat": 30.662}}},
                    {"period": "中午", "content": "午餐：陈麻婆豆腐总店",
                     "type": "food",
                     "detail": {"name": "陈麻婆豆腐", "summary": "麻婆豆腐发源店",
                                "duration": "1小时", "cost": "45元",
                                "coordinates": {"lng": 104.070, "lat": 30.668}}},
                    {"period": "下午", "content": "宽窄巷子喝茶采耳，闲逛买伴手礼",
                     "type": "scenic_spot",
                     "detail": {"name": "宽窄巷子", "summary": "清朝古街区改造的文创与茶馆",
                                "duration": "3小时", "cost": "免费",
                                "coordinates": {"lng": 104.056, "lat": 30.665}}},
                    {"period": "晚上", "content": "晚餐：钢管厂五区小郡肝串串香 + 冰粉",
                     "type": "food",
                     "detail": {"name": "小郡肝串串香", "summary": "麻辣串串配冰粉解辣",
                                "duration": "1.5小时", "cost": "60元",
                                "coordinates": {"lng": 104.061, "lat": 30.662}}},
                ],
                "day_cost": 155, "tips": "宽窄巷子小吃偏贵，吃饭可走到附近奎星楼街",
            },
            "第三天": {
                "date": None, "day_theme": "老成都慢生活",
                "periods": [
                    {"period": "早上", "content": "人民公园鹤鸣茶社盖碗茶 + 采耳",
                     "type": "activity",
                     "detail": {"name": "鹤鸣茶社", "summary": "百年老茶馆体验采耳",
                                "duration": "2小时", "cost": "50元",
                                "coordinates": {"lng": 104.060, "lat": 30.660}}},
                    {"period": "中午", "content": "午餐：双流老妈兔头，打包夫妻肺片",
                     "type": "food",
                     "detail": {"name": "双流老妈兔头", "summary": "成都特产麻辣兔头",
                                "duration": "1小时", "cost": "35元",
                                "coordinates": {"lng": 104.053, "lat": 30.655}}},
                    {"period": "下午", "content": "春熙路太古里逛街后返程",
                     "type": "activity",
                     "detail": {"name": "太古里", "summary": "潮流商圈，可买蜀锦等伴手礼",
                                "duration": "3小时", "cost": "免费",
                                "coordinates": {"lng": 104.081, "lat": 30.655}}},
                    {"period": "晚上", "content": "无安排（返程）", "type": "free",
                     "detail": {"name": "", "summary": "", "duration": "", "cost": "",
                                "coordinates": {"lng": None, "lat": None}}},
                ],
                "day_cost": 85, "tips": "兔头可真空打包，飞机高铁均可携带",
            },
        },
        "spots_catalog": [
            _spot("成都大熊猫繁育研究基地", "scenic_spot", "成都", 104.150, 30.750,
                  "全球最大熊猫人工繁育机构，早间可看到幼崽活动与孔雀开屏。",
                  highlights=["看熊猫幼崽", "园区植被好适合散步"], precautions=["8点前入园", "勿投喂"],
                  duration="3小时", cost="55元", rec_count=8),
            _spot("武侯祠·锦里", "scenic_spot", "成都", 104.047, 30.645,
                  "三国文化圣地与紧邻的锦里民俗街，红墙竹影适合拍照。",
                  highlights=["三国文物", "锦里夜游"], precautions=["节假日人多"],
                  duration="4小时", cost="60元", rec_count=6),
            _spot("杜甫草堂", "scenic_spot", "成都", 104.029, 30.662,
                  "诗圣杜甫流寓成都时的故居园林，环境清幽。",
                  highlights=["园林", "茅屋故居"], precautions=["夏季蚊子多"],
                  duration="2.5小时", cost="50元", rec_count=4),
        ],
        "food_catalog": [
            _food("小龙坎老火锅", "成都", ["牛油锅底", "毛肚", "黄喉"],
                  "成都现象级火锅，锅底香辣浓郁，食材新鲜。", "80元"),
            _food("洞子口张老二凉粉", "成都", ["凉粉", "甜水面"],
                  "开业几十年的老字号，红油凉粉酸辣开胃。", "20元"),
            _food("双流老妈兔头", "成都", ["麻辣兔头", "五香兔头"],
                  "成都伴手礼之王，可真空打包带走。", "35元"),
        ],
        "extra_recommendations": [
            _extra("青城山", "scenic_spot", "都江堰", 103.580, 30.900,
                   "道教名山，前山道观后山自然风光。",
                   "离市区较远需一天，本次3天行程排不下，适合下次专程前往。"),
            _extra("九眼桥酒吧街", "activity", "成都", 104.087, 30.642,
                   "锦江边夜生活聚集地，驻唱与精酿。",
                   "与'慢游放松'主题契合但预算有限，作为备选夜生活。"),
            _extra("钟水饺", "food", "成都", 104.070, 30.668,
                   "成都名小吃，红油水饺甜辣兼备。",
                   "春熙路附近顺路可尝，但避免与午餐正餐重复。"),
        ],
        "precautions_summary": {
            "tickets": ["熊猫基地、武侯祠均可现场或官方小程序购票，旺季提前1天"],
            "transport": ["市区地铁3/4号线覆盖主要景点，打车注意高峰期堵车"],
            "timing": ["熊猫基地清晨人少，下午熊猫大多睡觉"],
            "cost": ["宽窄巷子、锦里内消费偏高，正餐移步周边老街更实惠"],
            "other": ["夏天湿热，随身带伞和驱蚊水"],
        },
        "budget_breakdown": {
            "tickets": 165, "food": 480, "transport": 180,
            "accommodation": 600, "other": 95, "total": 1520, "remaining": 280,
        },
    },
}


# ---------------- 2. 西安 2 天（历史文化，intense） ----------------
XIAN = {
    "destination": "西安",
    "days": 2,
    "title": "2天西安历史文化精华游：兵马俑+华清宫+城墙+回民街",
    "summary": (
        "西安2天历史文化精华游：第一天上午秦始皇兵马俑博物馆看一号坑军阵，中午临潼大盘鸡或"
        "肉夹馍，下午华清宫看唐代行宫与《长恨歌》故事，晚上回市区大雁塔北广场音乐喷泉；"
        "第二天登西安城墙骑行或漫步，参观钟鼓楼与回民街美食，尝羊肉泡馍、肉夹馍、凉皮和"
        "甑糕，傍晚永兴坊看非遗表演。行程紧凑，人均约900元，适合对历史感兴趣的朋友。"
    ),
    "tags": ["西安", "兵马俑", "华清宫", "古城墙", "回民街", "历史", "羊肉泡馍"],
    "content": {
        "destination": "西安",
        "days": 2,
        "meta": {
            "destinations": ["西安"], "total_days": "2天",
            "date_range": {"start": None, "end": None},
            "budget": {"total": 1200, "estimated": 930, "status": "within_budget"},
            "pace": "intense", "generated_at": None,
        },
        "trade_off_summary": {
            "total_spots_found": 10, "spots_selected": 7, "spots_excluded": 3,
            "exclusion_reasons": {
                "陕西历史博物馆": "免票但需提前3天预约，行程当天约不上",
                "大唐不夜城": "时间不够，仅远观大雁塔夜景",
            },
        },
        "daily_plan": {
            "第一天": {
                "date": None, "day_theme": "秦风唐韵：兵马俑与华清宫",
                "periods": [
                    {"period": "早上", "content": "乘地铁9号线转公交前往兵马俑，游一号坑军阵",
                     "type": "scenic_spot",
                     "detail": {"name": "秦始皇兵马俑博物馆", "summary": "世界第八大奇迹",
                                "duration": "3.5小时", "cost": "120元",
                                "coordinates": {"lng": 109.279, "lat": 34.383}}},
                    {"period": "中午", "content": "午餐：临潼当地肉夹馍与凉皮",
                     "type": "food",
                     "detail": {"name": "临潼肉夹馍", "summary": "腊汁肉夹馍香而不腻",
                                "duration": "1小时", "cost": "25元",
                                "coordinates": {"lng": 109.260, "lat": 34.370}}},
                    {"period": "下午", "content": "华清宫参观唐代御汤遗址与《长恨歌》场景",
                     "type": "scenic_spot",
                     "detail": {"name": "华清宫", "summary": "杨贵妃沐浴的唐代皇家温泉行宫",
                                "duration": "3小时", "cost": "120元",
                                "coordinates": {"lng": 109.211, "lat": 34.362}}},
                    {"period": "晚上", "content": "返回市区，大雁塔北广场看音乐喷泉",
                     "type": "scenic_spot",
                     "detail": {"name": "大雁塔", "summary": "玄奘译经之地，喷泉夜景震撼",
                                "duration": "1.5小时", "cost": "免费",
                                "coordinates": {"lng": 108.964, "lat": 34.219}}},
                ],
                "day_cost": 265, "tips": "兵马俑请官方讲解或电子导览，体验差别很大",
            },
            "第二天": {
                "date": None, "day_theme": "古城墙与市井美食",
                "periods": [
                    {"period": "早上", "content": "登西安城墙，骑行南门至东门段",
                     "type": "scenic_spot",
                     "detail": {"name": "西安城墙", "summary": "保存最完整的古代城垣",
                                "duration": "2.5小时", "cost": "54元",
                                "coordinates": {"lng": 108.946, "lat": 34.256}}},
                    {"period": "中午", "content": "午餐：回民街老孙家羊肉泡馍",
                     "type": "food",
                     "detail": {"name": "回民街", "summary": "西安小吃集大成之地",
                                "duration": "1.5小时", "cost": "50元",
                                "coordinates": {"lng": 108.940, "lat": 34.263}}},
                    {"period": "下午", "content": "钟鼓楼广场外观 + 逛书院门，买皮影剪纸",
                     "type": "scenic_spot",
                     "detail": {"name": "钟鼓楼·书院门", "summary": "明代钟楼地标与文房街区",
                                "duration": "2小时", "cost": "免费",
                                "coordinates": {"lng": 108.951, "lat": 34.261}}},
                    {"period": "晚上", "content": "永兴坊品尝甑糕、油泼面后返程",
                     "type": "food",
                     "detail": {"name": "永兴坊", "summary": "非遗美食街区",
                                "duration": "1.5小时", "cost": "40元",
                                "coordinates": {"lng": 108.966, "lat": 34.266}}},
                ],
                "day_cost": 144, "tips": "城墙租车骑行最省力，永兴坊傍晚有非遗演出",
            },
        },
        "spots_catalog": [
            _spot("秦始皇兵马俑博物馆", "scenic_spot", "西安", 109.279, 34.383,
                  "世界第八大奇迹，一号坑千人千面军阵最为震撼。",
                  highlights=["一号坑军阵", "青铜兵器"], precautions=["请正规讲解", "馆内禁闪光灯"],
                  duration="3.5小时", cost="120元", rec_count=9),
            _spot("华清宫", "scenic_spot", "西安", 109.211, 34.362,
                  "唐代皇家温泉行宫，骊山脚下的《长恨歌》实景故事发生地。",
                  highlights=["御汤遗址", "骊山"], precautions=["山上台阶多"],
                  duration="3小时", cost="120元", rec_count=6),
            _spot("西安城墙", "scenic_spot", "西安", 108.946, 34.256,
                  "国内保存最完整的古代城墙，骑行一圈约14公里。",
                  highlights=["城墙骑行", "日落夜景"], precautions=["租车需押金"],
                  duration="2.5小时", cost="54元", rec_count=7),
        ],
        "food_catalog": [
            _food("老孙家羊肉泡馍", "西安", ["羊肉泡馍", "小炒泡馍"],
                  "百年老店，自己掰馍再煮，汤浓肉烂。", "50元"),
            _food("回民街贾三灌汤包", "西安", ["灌汤包", "八宝粥"],
                  "皮薄汤足，西安名点之一。", "30元"),
            _food("永兴坊甑糕", "西安", ["甑糕", "油泼面"],
                  "非遗小吃，糯米红枣蒸制甜香。", "15元"),
        ],
        "extra_recommendations": [
            _extra("陕西历史博物馆", "scenic_spot", "西安", 108.953, 34.224,
                   "免费但馆藏顶级，需提前3天公众号预约。",
                   "本次未能约到票，若再访西安务必提前抢票。"),
            _extra("大唐不夜城", "scenic_spot", "西安", 108.962, 34.211,
                   "盛唐主题步行街，夜晚灯光秀与不倒翁小姐姐。",
                   "行程已满仅路过，适合喜欢夜游的用户替代城墙夜景。"),
            _extra("回坊麻酱凉皮", "food", "西安", 108.941, 34.265,
                   "回民街特色麻酱凉皮，口感爽滑。",
                   "若吃不惯泡馍可作主餐备选。"),
        ],
        "precautions_summary": {
            "tickets": ["兵马俑120元、华清宫120元，均需身份证实名购票"],
            "transport": ["市区到兵马俑约1.5小时，地铁9号线+公交/打车最稳"],
            "timing": ["陕历博须提前3天预约，抢不到就换行程"],
            "cost": ["回民街主街偏游客价，多走两步进巷子更地道"],
            "other": ["西北干燥，注意补水；参观陵区注意防晒"],
        },
        "budget_breakdown": {
            "tickets": 294, "food": 260, "transport": 120,
            "accommodation": 240, "other": 16, "total": 930, "remaining": 270,
        },
    },
}


# ---------------- 3. 杭州+乌镇 3 天（双城多地点衔接） ----------------
HANGZHOU_WUZHEN = {
    "destination": "杭州+乌镇",
    "days": 3,
    "title": "3天杭州乌镇双城记：西湖灵隐+西栅夜景+东栅早市",
    "summary": (
        "杭州+乌镇3天双城攻略：第一天西湖断桥白堤环湖骑行，中午楼外楼西湖醋鱼，下午灵隐寺"
        "飞来峰；第二天上午湖滨坐船游三潭印月，中午前往乌镇西栅入住临水客栈，傍晚看西栅"
        "夜景与摇橹船；第三天早上去东栅早市感受原住民生活，尝定胜糕和三白酒，午后返程。"
        "两地相距约80公里，车程1.5小时，人均预算1800元，适合文艺慢节奏旅行。"
    ),
    "tags": ["杭州", "乌镇", "西湖", "灵隐寺", "西栅", "古镇", "双城", "夜景"],
    "content": {
        "destination": "杭州+乌镇",
        "days": 3,
        "meta": {
            "destinations": ["杭州", "乌镇"], "total_days": "3天",
            "date_range": {"start": None, "end": None},
            "budget": {"total": 2200, "estimated": 1850, "status": "within_budget"},
            "pace": "normal", "generated_at": None,
        },
        "trade_off_summary": {
            "total_spots_found": 14, "spots_selected": 9, "spots_excluded": 5,
            "exclusion_reasons": {
                "西溪湿地": "与西湖同质且时间不足",
                "宋城千古情": "演出门票贵且更适合同行有小孩的家庭",
            },
        },
        "daily_plan": {
            "第一天": {
                "date": None, "day_theme": "西湖山水",
                "periods": [
                    {"period": "早上", "content": "断桥残雪出发，白堤骑行至平湖秋月",
                     "type": "scenic_spot",
                     "detail": {"name": "西湖·白堤", "summary": "西湖十景精华段",
                                "duration": "2小时", "cost": "免费",
                                "coordinates": {"lng": 120.155, "lat": 30.258}}},
                    {"period": "中午", "content": "午餐：楼外楼西湖醋鱼、龙井虾仁",
                     "type": "food",
                     "detail": {"name": "楼外楼", "summary": "百年杭帮菜名店",
                                "duration": "1.5小时", "cost": "120元",
                                "coordinates": {"lng": 120.147, "lat": 30.253}}},
                    {"period": "下午", "content": "灵隐寺飞来峰，走永福寺禅意路线",
                     "type": "scenic_spot",
                     "detail": {"name": "灵隐寺·飞来峰", "summary": "千年古刹与石窟造像",
                                "duration": "3小时", "cost": "75元",
                                "coordinates": {"lng": 120.100, "lat": 30.240}}},
                    {"period": "晚上", "content": "河坊街逛吃，住西湖附近酒店",
                     "type": "food",
                     "detail": {"name": "河坊街", "summary": "杭州老城小吃街",
                                "duration": "2小时", "cost": "60元",
                                "coordinates": {"lng": 120.170, "lat": 30.242}}},
                ],
                "day_cost": 255, "tips": "西湖景区共享单车多，环湖骑行比打车更顺",
            },
            "第二天": {
                "date": None, "day_theme": "西湖泛舟转场乌镇西栅",
                "periods": [
                    {"period": "早上", "content": "湖滨码头乘船游三潭印月、雷峰塔远观",
                     "type": "scenic_spot",
                     "detail": {"name": "三潭印月", "summary": "一元纸币背面图案实景",
                                "duration": "2小时", "cost": "55元",
                                "coordinates": {"lng": 120.145, "lat": 30.240}}},
                    {"period": "中午", "content": "午餐后乘大巴/拼车赴乌镇（约1.5小时）",
                     "type": "travel",
                     "detail": {"name": "杭州→乌镇", "summary": "跨城交通衔接",
                                "duration": "1.5小时", "cost": "40元",
                                "coordinates": {"lng": 120.480, "lat": 30.740}}},
                    {"period": "下午", "content": "入住西栅临水客栈，逛昭明书院、染布坊",
                     "type": "scenic_spot",
                     "detail": {"name": "乌镇西栅", "summary": "完整保留的江南水乡古镇",
                                "duration": "4小时", "cost": "150元",
                                "coordinates": {"lng": 120.486, "lat": 30.745}}},
                    {"period": "晚上", "content": "西栅夜景摇橹船 + 水边酒吧听评弹",
                     "type": "activity",
                     "detail": {"name": "西栅夜景", "summary": "灯火阑珊的水乡夜游",
                                "duration": "2小时", "cost": "120元",
                                "coordinates": {"lng": 120.487, "lat": 30.744}}},
                ],
                "day_cost": 365, "tips": "西栅门票当日有效，住景区内客栈可多次进出",
            },
            "第三天": {
                "date": None, "day_theme": "东栅早市与原乡生活",
                "periods": [
                    {"period": "早上", "content": "东栅看茅盾故居与水上早市，尝定胜糕",
                     "type": "scenic_spot",
                     "detail": {"name": "乌镇东栅", "summary": "生活气息更浓的老镇区",
                                "duration": "3小时", "cost": "110元",
                                "coordinates": {"lng": 120.483, "lat": 30.742}}},
                    {"period": "中午", "content": "午餐：白水鱼、红烧羊肉面，饮三白酒",
                     "type": "food",
                     "detail": {"name": "乌镇水乡菜", "summary": "古镇特色白水鱼与羊肉面",
                                "duration": "1小时", "cost": "70元",
                                "coordinates": {"lng": 120.484, "lat": 30.743}}},
                    {"period": "下午", "content": "购买蓝印花布与姑嫂饼伴手礼后返程",
                     "type": "free",
                     "detail": {"name": "伴手礼采购", "summary": "蓝印花布、姑嫂饼",
                                "duration": "1.5小时", "cost": "100元",
                                "coordinates": {"lng": 120.486, "lat": 30.745}}},
                    {"period": "晚上", "content": "无安排（返程）", "type": "free",
                     "detail": {"name": "", "summary": "", "duration": "", "cost": "",
                                "coordinates": {"lng": None, "lat": None}}},
                ],
                "day_cost": 280, "tips": "东栅早8点前游客少，最出片",
            },
        },
        "spots_catalog": [
            _spot("西湖（白堤-三潭印月）", "scenic_spot", "杭州", 120.155, 30.250,
                  "世界文化遗产，白堤骑行与三潭印月游船是经典玩法。",
                  highlights=["环湖骑行", "三潭印月"], precautions=["节假日人流大"],
                  duration="半天", cost="免费(游船另付)", rec_count=8),
            _spot("灵隐寺·飞来峰", "scenic_spot", "杭州", 120.100, 30.240,
                  "杭州最负盛名的古刹，飞来峰石窟造像精美。",
                  highlights=["石窟造像", "永福寺"], precautions=["门票需含飞来峰"],
                  duration="3小时", cost="75元", rec_count=6),
            _spot("乌镇西栅", "scenic_spot", "乌镇", 120.486, 30.745,
                  "枕水人家的江南水乡范本，夜景冠绝江南。",
                  highlights=["夜景摇橹船", "临水客栈"], precautions=["住景区内免重复门票"],
                  duration="1天", cost="150元", rec_count=9),
            _spot("乌镇东栅", "scenic_spot", "乌镇", 120.483, 30.742,
                  "原住民生活气息更浓，茅盾故居所在。",
                  highlights=["水上早市", "茅盾故居"], precautions=["门票与西栅分开"],
                  duration="3小时", cost="110元", rec_count=5),
        ],
        "food_catalog": [
            _food("楼外楼", "杭州", ["西湖醋鱼", "龙井虾仁", "东坡肉"],
                  "西湖边百年名店，正统杭帮菜代表。", "120元"),
            _food("知味观", "杭州", ["小笼包", "片儿川"],
                  "杭州老字号点心，湖滨店排队短。", "40元"),
            _food("乌镇水乡菜", "乌镇", ["白水鱼", "红烧羊肉面"],
                  "西栅景区内本地食材，白水鱼鲜嫩。", "70元"),
        ],
        "extra_recommendations": [
            _extra("西溪湿地", "scenic_spot", "杭州", 120.075, 30.272,
                   "城市湿地公园，可乘摇橹船穿行芦苇荡。",
                   "与西湖山水重复度高且时间不足，适合多一天的行程。"),
            _extra("宋城千古情", "activity", "杭州", 120.105, 30.170,
                   "大型室内歌舞秀，讲述杭州历史传说。",
                   "票价较贵，若同游有老人小孩值得纳入。"),
            _extra("南栅老街", "scenic_spot", "乌镇", 120.479, 30.740,
                   "西栅东栅之外的免费原生态街区。",
                   "时间充裕可顺路一逛，感受未商业化的乌镇。"),
        ],
        "precautions_summary": {
            "tickets": ["灵隐寺门票不含飞来峰，实际为飞来峰45+灵隐寺30"],
            "transport": ["杭州东站有直达乌镇大巴；返程末班约18:00"],
            "timing": ["西栅夜景最佳时段为19:00-21:00，摇橹船越晚越贵"],
            "cost": ["景区内餐饮整体偏贵，可自带零食补充"],
            "other": ["梅雨季（6-7月）备伞，水乡蚊虫多备驱蚊液"],
        },
        "budget_breakdown": {
            "tickets": 390, "food": 480, "transport": 180,
            "accommodation": 700, "other": 100, "total": 1850, "remaining": 350,
        },
    },
}


# ---------------- 4. 三亚 4 天（亲子海滩度假，relaxed） ----------------
SANYA = {
    "destination": "三亚",
    "days": 4,
    "title": "4天三亚亲子度假：亚龙湾+蜈支洲岛+热带天堂+海鲜市场",
    "summary": (
        "三亚4天亲子度假攻略：前两晚住亚龙湾亲子酒店，玩亚龙湾沙滩和热带天堂森林公园的"
        "过江龙索桥；第三天乘船去蜈支洲岛浮潜、环岛电瓶车看情人桥，回程顺路逛第一市场买"
        "海鲜加工；第四天三亚湾椰梦长廊散步、三亚免税城采购后返程。全程防晒是重点，给"
        "孩子备泳圈和防晒霜，人均约3500元，节奏舒缓适合带娃家庭。"
    ),
    "tags": ["三亚", "亚龙湾", "蜈支洲岛", "亲子", "海岛", "海鲜", "度假", "防晒"],
    "content": {
        "destination": "三亚",
        "days": 4,
        "meta": {
            "destinations": ["三亚"], "total_days": "4天",
            "date_range": {"start": None, "end": None},
            "budget": {"total": 4000, "estimated": 3650, "status": "within_budget"},
            "pace": "relaxed", "generated_at": None,
        },
        "trade_off_summary": {
            "total_spots_found": 11, "spots_selected": 7, "spots_excluded": 4,
            "exclusion_reasons": {
                "天涯海角": "石景单一且离湾区远，带娃性价比低",
                "南山文化旅游区": "佛教主题较重，本次亲子行程以海滩为主",
            },
        },
        "daily_plan": {
            "第一天": {
                "date": None, "day_theme": "抵达亚龙湾·沙滩初体验",
                "periods": [
                    {"period": "早上", "content": "抵达三亚，入住亚龙湾亲子度假酒店",
                     "type": "accommodation",
                     "detail": {"name": "亚龙湾亲子酒店", "summary": "自带泳池与儿童乐园",
                                "duration": "-", "cost": "800元/晚",
                                "coordinates": {"lng": 109.620, "lat": 18.220}}},
                    {"period": "中午", "content": "酒店内午餐，午休避正午烈日",
                     "type": "free",
                     "detail": {"name": "", "summary": "", "duration": "", "cost": "",
                                "coordinates": {"lng": None, "lat": None}}},
                    {"period": "下午", "content": "亚龙湾沙滩玩沙踏浪，傍晚温度适宜下水",
                     "type": "activity",
                     "detail": {"name": "亚龙湾沙滩", "summary": "三亚最优质海湾",
                                "duration": "3小时", "cost": "免费",
                                "coordinates": {"lng": 109.630, "lat": 18.225}}},
                    {"period": "晚上", "content": "海边自助晚餐，儿童泳池玩水",
                     "type": "accommodation",
                     "detail": {"name": "酒店泳池", "summary": "亲子泳池",
                                "duration": "1.5小时", "cost": "免费",
                                "coordinates": {"lng": 109.620, "lat": 18.220}}},
                ],
                "day_cost": 850, "tips": "到达首日不安排远景点，让娃适应海岛节奏",
            },
            "第二天": {
                "date": None, "day_theme": "热带天堂森林公园",
                "periods": [
                    {"period": "早上", "content": "热带天堂森林公园，乘游览车观鸟巢度假村",
                     "type": "scenic_spot",
                     "detail": {"name": "亚龙湾热带天堂森林公园", "summary": "《非诚勿扰》取景地",
                                "duration": "3.5小时", "cost": "158元",
                                "coordinates": {"lng": 109.645, "lat": 18.245}}},
                    {"period": "中午", "content": "景区内午餐（自带干粮更实惠）",
                     "type": "food",
                     "detail": {"name": "", "summary": "景区餐饮偏贵", "duration": "1小时",
                                "cost": "50元", "coordinates": {"lng": None, "lat": None}}},
                    {"period": "下午", "content": "过江龙索桥 + 沧海楼远眺，回酒店午休",
                     "type": "scenic_spot",
                     "detail": {"name": "过江龙索桥", "summary": "雨林吊桥",
                                "duration": "2小时", "cost": "已含门票",
                                "coordinates": {"lng": 109.648, "lat": 18.248}}},
                    {"period": "晚上", "content": "晚间沙滩抓螃蟹活动（酒店免费提供）",
                     "type": "activity",
                     "detail": {"name": "沙滩夜游", "summary": "亲子抓螃蟹",
                                "duration": "1小时", "cost": "免费",
                                "coordinates": {"lng": 109.630, "lat": 18.225}}},
                ],
                "day_cost": 208, "tips": "森林公园全程电瓶车分站，老人小孩不累",
            },
            "第三天": {
                "date": None, "day_theme": "蜈支洲岛浮潜",
                "periods": [
                    {"period": "早上", "content": "乘船赴蜈支洲岛，码头排队建议9点前到",
                     "type": "scenic_spot",
                     "detail": {"name": "蜈支洲岛", "summary": "三亚海水最清澈离岛",
                                "duration": "全天", "cost": "门票+船票144元",
                                "coordinates": {"lng": 109.760, "lat": 18.310}}},
                    {"period": "中午", "content": "岛上午餐，环岛电瓶车游情人桥、观日岩",
                     "type": "scenic_spot",
                     "detail": {"name": "情人桥", "summary": "观海栈桥",
                                "duration": "2小时", "cost": "电瓶车120元",
                                "coordinates": {"lng": 109.765, "lat": 18.315}}},
                    {"period": "下午", "content": "沙滩浮潜区亲子浮潜（教练陪同）",
                     "type": "activity",
                     "detail": {"name": "浮潜体验", "summary": "看热带鱼与珊瑚",
                                "duration": "2小时", "cost": "380元/人",
                                "coordinates": {"lng": 109.762, "lat": 18.312}}},
                    {"period": "晚上", "content": "返航后第一市场自购海鲜加工",
                     "type": "food",
                     "detail": {"name": "第一市场海鲜", "summary": "自选海鲜+加工店",
                                "duration": "2小时", "cost": "250元",
                                "coordinates": {"lng": 109.505, "lat": 18.242}}},
                ],
                "day_cost": 894, "tips": "蜈支洲岛务必提前订票，旺季当天常售罄",
            },
            "第四天": {
                "date": None, "day_theme": "椰梦长廊与免税采购",
                "periods": [
                    {"period": "早上", "content": "三亚湾椰梦长廊散步拍照，孩子捡贝壳",
                     "type": "scenic_spot",
                     "detail": {"name": "椰梦长廊", "summary": "三亚湾免费海岸绿道",
                                "duration": "2小时", "cost": "免费",
                                "coordinates": {"lng": 109.460, "lat": 18.240}}},
                    {"period": "中午", "content": "午餐后前往三亚国际免税城（离岛免税）",
                     "type": "activity",
                     "detail": {"name": "三亚国际免税城", "summary": "海棠湾免税购物",
                                "duration": "3小时", "cost": "视购物",
                                "coordinates": {"lng": 109.740, "lat": 18.405}}},
                    {"period": "下午", "content": "按离岛时间前往机场，返程",
                     "type": "free",
                     "detail": {"name": "", "summary": "", "duration": "", "cost": "",
                                "coordinates": {"lng": None, "lat": None}}},
                    {"period": "晚上", "content": "无安排（返程）", "type": "free",
                     "detail": {"name": "", "summary": "", "duration": "", "cost": "",
                                "coordinates": {"lng": None, "lat": None}}},
                ],
                "day_cost": 100, "tips": "免税购物需凭离岛机票/船票，预留取货时间",
            },
        },
        "spots_catalog": [
            _spot("蜈支洲岛", "scenic_spot", "三亚", 109.760, 18.310,
                  "三亚海水最清澈的离岛，浮潜看珊瑚与热带鱼，情人桥出片。",
                  highlights=["浮潜", "情人桥", "观日岩"], precautions=["晕船提前服药", "旺季抢票"],
                  duration="全天", cost="144元起", rec_count=9),
            _spot("亚龙湾热带天堂森林公园", "scenic_spot", "三亚", 109.645, 18.245,
                  "临海山地热带雨林，《非诚勿扰2》取景地。",
                  highlights=["过江龙索桥", "沧海楼"], precautions=["全程电瓶车"],
                  duration="半天", cost="158元", rec_count=6),
            _spot("亚龙湾沙滩", "activity", "三亚", 109.630, 18.225,
                  "沙质细白、坡度平缓，最适合亲子玩沙踏浪。",
                  highlights=["玩沙", "踏浪"], precautions=["正午暴晒少下水"],
                  duration="3小时", cost="免费", rec_count=7),
        ],
        "food_catalog": [
            _food("第一市场海鲜加工", "三亚", ["和乐蟹", "芒果螺", "基围虾"],
                  "自购鲜活海鲜到加工店现做，性价比高。", "250元"),
            _food("椰子鸡", "三亚", ["文昌鸡", "椰子水锅底"],
                  "椰青水涮鸡清甜鲜嫩，带娃友好不辣。", "120元"),
            _food("清补凉", "三亚", ["椰奶清补凉"],
                  "海南特色糖水，饭后解暑。", "15元"),
        ],
        "extra_recommendations": [
            _extra("天涯海角", "scenic_spot", "三亚", 109.320, 18.290,
                   "传统地标景点，'天涯''海角'石刻。",
                   "石景较单一且离湾区远，本次亲子行程已足够丰富。"),
            _extra("南山文化旅游区", "scenic_spot", "三亚", 109.210, 18.310,
                   "海上观音像高108米，佛教文化主题园区。",
                   "宗教主题偏重，若行程延长可安排半日。"),
            _extra("后海村冲浪", "activity", "三亚", 109.690, 18.280,
                   "新手友好冲浪海湾，教练一对一教学。",
                   "孩子大一些的家庭可加入，替代某天下午的沙滩自由活动。"),
        ],
        "precautions_summary": {
            "tickets": ["蜈支洲岛旺季务必提前2-3天订票，浮潜另购套餐"],
            "transport": ["各湾区打车距离远，建议租车或包车更自由"],
            "timing": ["10:00-15:00紫外线最强，户外活动避开正午"],
            "cost": ["海鲜加工先谈好加工费再下单；免税购物需预留机场提货时间"],
            "other": ["防晒霜SPF50+每2小时补涂，孩子配长袖泳衣与泳圈"],
        },
        "budget_breakdown": {
            "tickets": 662, "food": 900, "transport": 500,
            "accommodation": 1600, "other": -12, "total": 3650, "remaining": 350,
        },
    },
}


# 注册数据集（保持顺序：插入顺序即文档展示顺序）
SEED_GUIDES = [
    {"user_id": TEST_USER, "username": TEST_USERNAME, "nickname": TEST_NICKNAME, **CHENGDU},
    {"user_id": TEST_USER, "username": TEST_USERNAME, "nickname": TEST_NICKNAME, **XIAN},
    {"user_id": TEST_USER, "username": TEST_USERNAME, "nickname": TEST_NICKNAME, **HANGZHOU_WUZHEN},
    # 三亚用第二个测试账号，验证多用户数据也可被检索
    {"user_id": "rag_test_user2", "username": "rag_tester2", "nickname": "RAG测试用户2", **SANYA},
]


# ============================================================
# 入库与验证
# ============================================================

def seed_guides():
    """按正式存储路径 create_guide_record 写入 ES。"""
    from services.es_client import ensure_indices, es_health
    from services.guide_service import create_guide_record

    health = es_health()
    if health.get("status") != "ok":
        raise RuntimeError(
            f"Elasticsearch 不可用：{health}。请先执行 docker compose up -d elasticsearch"
        )
    ensure_indices()

    inserted = []
    for g in SEED_GUIDES:
        # content 同时携带 skill 风格完整字段 + itinerary/food/tags（供关键词自动提取）
        content = dict(g["content"])
        content["itinerary"] = [
            {
                "day": i,
                "activities": [
                    {"name": p["detail"]["name"]} for p in day.get("periods", [])
                    if p.get("detail", {}).get("name")
                ],
            }
            for i, day in enumerate(content.get("daily_plan", {}).values(), start=1)
        ]
        content["food"] = [f["name"] for f in content.get("food_catalog", [])]
        content["tags"] = g["tags"]

        doc = create_guide_record(
            user_id=g["user_id"],
            username=g["username"],
            nickname=g["nickname"],
            title=g["title"],
            content=content,
            destination=g["destination"],
            days=g["days"],
            summary=g["summary"],
        )
        inserted.append((doc.get("guide_id"), g["destination"], g["title"]))
        print(f"✔ 已入库 [{g['destination']}] {g['title']}")
        print(f"    guide_id={doc.get('guide_id')}  含向量={bool(doc.get('embedding'))}")

    print(f"\n共入库 {len(inserted)} 条攻略（destination: "
          f"{', '.join(d for _, d, _ in inserted)}）")
    return inserted


def _safe_rag_trigger(query: str) -> bool:
    """容错调用 should_use_rag。

    注意：services/rag_service.should_use_rag 在未命中触发关键词时会调用一个
    已被注释掉的 extract_keywords_from_query()，触发 NameError —— 这里兜底，
    让探针脚本不受该既有 bug 影响。
    """
    from services.rag_service import should_use_rag
    try:
        return should_use_rag(query)[0]
    except Exception:
        return False


def _safe_search(query: str, top_k: int = 5) -> list:
    """容错调用 search_related_guides（embedding 失败会内部回退纯关键词）。"""
    from services.rag_service import search_related_guides
    try:
        return search_related_guides(query, top_k=top_k)
    except Exception as e:
        print(f"   ⚠ 检索异常（回退纯关键词仍失败）：{e}")
        return []


def main():
    parser = argparse.ArgumentParser(description="RAG 检索测试种子数据生成与验证")
    parser.add_argument("--skip-seed", action="store_true", help="只跑验证探针，不重新入库")
    parser.add_argument("--probe", action="append", metavar="查询", help="追加自定义探针查询")
    args = parser.parse_args()

    if not args.skip_seed:
        seed_guides()
    else:
        print("跳过入库（--skip-seed）")

    probes = list(DEFAULT_PROBES)
    if args.probe:
        # 自定义探针不预设目的地：只要 top5 有命中即算 PASS
        probes.extend((p, "") for p in args.probe)

    print("\n" + "=" * 72)
    print("RAG 检索验证探针（查询含 上次/去年/以前 等触发词）")
    print("=" * 72)
    passed = 0
    for query, expected_dest in probes:
        trigger = _safe_rag_trigger(query)
        hits = _safe_search(query, top_k=5)
        hit_dests = [h.get("destination", "") for h in hits]
        matched = expected_dest in hit_dests if expected_dest else len(hits) > 0
        ok = (trigger or not expected_dest) and matched

        print(f"\n▶ 查询：{query}")
        print(f"  触发RAG开关: {'✅' if trigger else '❌'}   期望命中: {expected_dest or '（任意）'}")
        if hits:
            for i, h in enumerate(hits, 1):
                flag = " ◀命中" if h.get("destination") == expected_dest else ""
                print(f"   #{i} [{h.get('destination')}] {h.get('title', '')[:36]} "
                      f"score={h.get('_score', 0):.2f}{flag}")
        else:
            print("   无命中")
        passed += 1 if ok else 0
        print(f"  结果: {'✅ PASS' if ok else '❌ FAIL'}")

    print("\n" + "-" * 72)
    print(f"RAG 验证汇总：{passed}/{len(probes)} 通过")
    return 0 if passed == len(probes) else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"\n❌ 执行出错：{e}", file=sys.stderr)
        sys.exit(2)
