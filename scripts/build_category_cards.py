#!/usr/bin/env python3
"""Build the fixed 128-leaf current-product Category Card artifact."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

CardSpec = tuple[str, int, int, int]

_CARD_SPECS: tuple[CardSpec, ...] = (
    ("electronics.laptop", 1500, 5500, 35000),
    ("electronics.desktop", 1200, 4800, 50000),
    ("electronics.tablet", 500, 2600, 16000),
    ("electronics.phone", 300, 3200, 18000),
    ("electronics.monitor", 300, 1800, 18000),
    ("electronics.television", 500, 3200, 60000),
    ("electronics.camera", 500, 5000, 80000),
    ("electronics.headphone", 20, 500, 8000),
    ("electronics.speaker", 30, 800, 30000),
    ("electronics.keyboard", 20, 300, 4000),
    ("electronics.mouse", 10, 180, 2500),
    ("electronics.printer", 300, 1600, 30000),
    ("electronics.storage", 30, 500, 12000),
    ("electronics.networking", 30, 500, 15000),
    ("electronics.wearable", 80, 800, 10000),
    ("electronics.gaming-console", 500, 2800, 10000),
    ("electronics.laptop-accessory", 10, 180, 2000),
    ("electronics.laptop-part", 20, 500, 8000),
    ("home.drinkware.cup", 5, 60, 800),
    ("home.drinkware.bottle", 8, 90, 1000),
    ("kitchen.cookware", 20, 300, 5000),
    ("kitchen.bakeware", 10, 150, 2500),
    ("kitchen.tableware", 5, 120, 3000),
    ("kitchen.utensil", 3, 60, 1200),
    ("kitchen.food-storage", 5, 100, 1800),
    ("kitchen.small-appliance", 50, 500, 8000),
    ("appliances.major-appliance", 500, 3500, 30000),
    ("home.bedding", 20, 300, 8000),
    ("home.bath", 5, 120, 3000),
    ("home.decor", 5, 180, 10000),
    ("home.lighting", 10, 250, 12000),
    ("home.cleaning", 5, 100, 5000),
    ("home.storage", 10, 180, 6000),
    ("home.rug", 20, 350, 15000),
    ("home.window-treatment", 20, 300, 8000),
    ("home.accessory", 3, 80, 2000),
    ("furniture.chair", 80, 700, 20000),
    ("furniture.sofa", 500, 4000, 60000),
    ("furniture.bed", 500, 3500, 50000),
    ("furniture.table", 150, 1500, 30000),
    ("furniture.desk", 150, 1200, 20000),
    ("furniture.cabinet", 150, 1800, 30000),
    ("furniture.shelf", 50, 500, 10000),
    ("furniture.outdoor", 100, 1200, 30000),
    ("furniture.mattress", 300, 2500, 30000),
    ("furniture.accessory", 10, 180, 3000),
    ("clothing.mens-top", 20, 180, 5000),
    ("clothing.mens-bottom", 30, 220, 5000),
    ("clothing.womens-top", 20, 180, 8000),
    ("clothing.womens-bottom", 30, 220, 8000),
    ("clothing.dress", 40, 300, 20000),
    ("clothing.outerwear", 80, 600, 30000),
    ("clothing.underwear", 10, 100, 1500),
    ("clothing.sleepwear", 20, 180, 2500),
    ("clothing.activewear", 20, 220, 5000),
    ("clothing.swimwear", 20, 180, 3000),
    ("clothing.uniform", 30, 250, 3000),
    ("clothing.accessory", 5, 100, 5000),
    ("shoes.athletic", 50, 400, 8000),
    ("shoes.casual", 40, 300, 6000),
    ("shoes.formal", 80, 500, 12000),
    ("shoes.boot", 80, 550, 12000),
    ("shoes.sandal", 20, 180, 3000),
    ("shoes.accessory", 3, 50, 1000),
    ("beauty.skincare", 10, 180, 5000),
    ("beauty.makeup", 5, 120, 3000),
    ("beauty.haircare", 10, 100, 2000),
    ("beauty.fragrance", 30, 400, 8000),
    ("beauty.personal-care", 5, 80, 1500),
    ("health.vitamin", 10, 120, 2000),
    ("health.medical-supply", 5, 120, 10000),
    ("health.wellness-device", 30, 500, 20000),
    ("health.oral-care", 5, 80, 2000),
    ("health.vision-care", 10, 150, 8000),
    ("sports.fitness", 10, 300, 20000),
    ("sports.team-sport", 10, 180, 6000),
    ("sports.cycling", 20, 800, 50000),
    ("sports.racket-sport", 20, 300, 10000),
    ("sports.water-sport", 30, 800, 50000),
    ("outdoor.camping", 20, 500, 20000),
    ("outdoor.hiking", 20, 350, 10000),
    ("outdoor.fishing", 10, 300, 20000),
    ("outdoor.hunting", 20, 500, 30000),
    ("outdoor.accessory", 5, 100, 3000),
    ("automotive.part", 20, 500, 30000),
    ("automotive.accessory", 5, 150, 8000),
    ("automotive.tire", 100, 600, 8000),
    ("automotive.fluid", 10, 100, 1000),
    ("tools.hand-tool", 5, 100, 5000),
    ("tools.power-tool", 80, 600, 20000),
    ("tools.accessory", 3, 80, 3000),
    ("industrial.safety-supply", 5, 150, 10000),
    ("industrial.material-handling", 50, 1000, 80000),
    ("industrial.component", 5, 200, 30000),
    ("books.book", 5, 60, 1000),
    ("media.music-recording", 5, 80, 3000),
    ("media.movie-video", 5, 60, 1500),
    ("media.video-game", 20, 300, 1200),
    ("office.stationery", 2, 30, 800),
    ("office.electronics", 20, 400, 10000),
    ("office.art-supply", 3, 80, 3000),
    ("office.school-supply", 2, 50, 1500),
    ("toys.figure-doll", 5, 100, 5000),
    ("toys.building-set", 10, 200, 8000),
    ("toys.game-puzzle", 5, 80, 1500),
    ("toys.outdoor-toy", 10, 180, 5000),
    ("baby.diaper", 20, 120, 1000),
    ("baby.feeding", 5, 100, 3000),
    ("baby.gear", 30, 800, 15000),
    ("pets.food", 5, 100, 2000),
    ("pets.litter-supply", 5, 80, 1000),
    ("pets.accessory", 3, 80, 3000),
    ("grocery.snack", 1, 25, 300),
    ("grocery.beverage", 1, 20, 500),
    ("grocery.pantry", 1, 30, 800),
    ("grocery.fresh", 1, 40, 1000),
    ("grocery.frozen", 5, 60, 1000),
    ("grocery.household-consumable", 3, 50, 800),
    ("jewelry.fine", 100, 3000, 200000),
    ("jewelry.fashion", 5, 100, 5000),
    ("jewelry.watch", 30, 800, 300000),
    ("luggage.suitcase", 80, 600, 12000),
    ("luggage.bag", 20, 300, 30000),
    ("luggage.travel-accessory", 5, 80, 2000),
    ("musical-instruments.string", 100, 1500, 100000),
    ("musical-instruments.keyboard", 200, 2000, 80000),
    ("musical-instruments.percussion", 30, 800, 100000),
    ("general.general-merchandise", 1, 100, 5000),
)

_KEYWORD_OVERRIDES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "electronics.laptop": (("laptop", "notebook", "macbook"), ("case", "sleeve", "charger")),
    "electronics.laptop-accessory": (("laptop case", "laptop sleeve", "laptop charger"), ()),
    "electronics.laptop-part": (("laptop ram", "laptop battery", "laptop replacement"), ("case",)),
    "electronics.phone": (("smartphone", "mobile phone", "iphone"), ("case", "cover")),
    "home.drinkware.cup": (("cup", "mug", "tumbler"), ("holder", "sticker")),
}

_PROTOTYPE_OVERRIDES: dict[str, str] = {
    "electronics.phone": (
        "smartphone mobile phone handset iPhone Android phone complete device "
        "including gaming models; exclude cases covers stands and replacement parts"
    ),
}

_ZH_ALIASES: dict[str, tuple[str, ...]] = {
    "electronics.laptop": ("笔记本电脑", "轻薄本", "游戏本"),
    "electronics.desktop": ("台式电脑", "台式机", "电脑主机"),
    "electronics.tablet": ("平板电脑", "平板"),
    "electronics.phone": ("手机", "智能手机"),
    "electronics.monitor": ("显示器", "电脑屏幕"),
    "electronics.television": ("电视机", "智能电视"),
    "electronics.camera": ("相机", "数码相机", "微单相机"),
    "electronics.headphone": ("耳机", "头戴式耳机"),
    "electronics.speaker": ("音箱", "蓝牙音箱"),
    "electronics.keyboard": ("键盘", "机械键盘"),
    "electronics.mouse": ("鼠标", "无线鼠标"),
    "electronics.printer": ("打印机",),
    "electronics.storage": ("存储设备", "移动硬盘"),
    "electronics.networking": ("网络设备", "路由器"),
    "electronics.wearable": ("智能穿戴", "智能手表"),
    "electronics.gaming-console": ("游戏机", "游戏主机"),
    "electronics.laptop-accessory": ("笔记本配件", "电脑包"),
    "electronics.laptop-part": ("笔记本零件", "电脑维修配件"),
    "home.drinkware.cup": ("杯子", "咖啡杯", "马克杯"),
    "home.drinkware.bottle": ("水杯", "水瓶", "保温杯"),
    "kitchen.cookware": ("炊具", "锅具"),
    "kitchen.bakeware": ("烘焙模具", "烤盘"),
    "kitchen.tableware": ("餐具", "碗碟"),
    "kitchen.utensil": ("厨具", "厨房用具"),
    "kitchen.food-storage": ("食品收纳", "保鲜盒"),
    "kitchen.small-appliance": ("厨房小家电",),
    "appliances.major-appliance": ("大家电", "大型家电"),
    "home.bedding": ("床上用品", "寝具"),
    "home.bath": ("卫浴用品", "浴室用品"),
    "home.decor": ("家居装饰", "家装摆件"),
    "home.lighting": ("灯具", "家居照明"),
    "home.cleaning": ("清洁用品",),
    "home.storage": ("家居收纳", "收纳用品"),
    "home.rug": ("地毯", "地垫"),
    "home.window-treatment": ("窗帘", "百叶窗"),
    "home.accessory": ("家居配件",),
    "furniture.chair": ("椅子", "座椅"),
    "furniture.sofa": ("沙发",),
    "furniture.bed": ("床架", "床"),
    "furniture.table": ("桌子", "餐桌"),
    "furniture.desk": ("书桌", "办公桌"),
    "furniture.cabinet": ("柜子", "橱柜"),
    "furniture.shelf": ("置物架", "书架"),
    "furniture.outdoor": ("户外家具",),
    "furniture.mattress": ("床垫",),
    "furniture.accessory": ("家具配件",),
    "clothing.mens-top": ("男士上衣", "男装上衣"),
    "clothing.mens-bottom": ("男士裤装", "男裤"),
    "clothing.womens-top": ("女士上衣", "女装上衣"),
    "clothing.womens-bottom": ("女士裤装", "女裤"),
    "clothing.dress": ("连衣裙", "裙装"),
    "clothing.outerwear": ("外套", "大衣"),
    "clothing.underwear": ("内衣",),
    "clothing.sleepwear": ("睡衣", "家居服"),
    "clothing.activewear": ("运动服", "健身服"),
    "clothing.swimwear": ("泳装", "泳衣"),
    "clothing.uniform": ("制服", "工作服"),
    "clothing.accessory": ("服饰配件",),
    "shoes.athletic": ("运动鞋", "跑鞋"),
    "shoes.casual": ("休闲鞋",),
    "shoes.formal": ("正装鞋", "皮鞋"),
    "shoes.boot": ("靴子", "短靴"),
    "shoes.sandal": ("凉鞋", "拖鞋"),
    "shoes.accessory": ("鞋类配件", "鞋垫"),
    "beauty.skincare": ("护肤品", "护肤"),
    "beauty.makeup": ("彩妆", "化妆品"),
    "beauty.haircare": ("护发", "美发用品"),
    "beauty.fragrance": ("香水", "香氛"),
    "beauty.personal-care": ("个人护理",),
    "health.vitamin": ("维生素", "营养补充剂"),
    "health.medical-supply": ("医疗用品", "医用耗材"),
    "health.wellness-device": ("健康设备", "理疗仪"),
    "health.oral-care": ("口腔护理", "牙齿护理"),
    "health.vision-care": ("视力护理", "眼部护理"),
    "sports.fitness": ("健身器材", "健身用品"),
    "sports.team-sport": ("团队运动用品", "球类用品"),
    "sports.cycling": ("骑行用品", "自行车用品"),
    "sports.racket-sport": ("球拍运动", "网球用品"),
    "sports.water-sport": ("水上运动用品",),
    "outdoor.camping": ("露营用品", "露营装备"),
    "outdoor.hiking": ("徒步用品", "徒步装备"),
    "outdoor.fishing": ("钓鱼用品", "渔具"),
    "outdoor.hunting": ("狩猎用品",),
    "outdoor.accessory": ("户外配件",),
    "automotive.part": ("汽车零件", "汽配零件"),
    "automotive.accessory": ("汽车用品", "汽车配件"),
    "automotive.tire": ("轮胎", "汽车轮胎"),
    "automotive.fluid": ("汽车油液", "机油"),
    "tools.hand-tool": ("手动工具",),
    "tools.power-tool": ("电动工具",),
    "tools.accessory": ("工具配件",),
    "industrial.safety-supply": ("工业安全用品", "劳保用品"),
    "industrial.material-handling": ("物料搬运设备",),
    "industrial.component": ("工业零部件",),
    "books.book": ("图书", "书籍"),
    "media.music-recording": ("音乐唱片", "音乐录音"),
    "media.movie-video": ("电影影碟", "影视视频"),
    "media.video-game": ("电子游戏", "游戏软件"),
    "office.stationery": ("文具", "办公文具"),
    "office.electronics": ("办公电子设备",),
    "office.art-supply": ("美术用品", "画材"),
    "office.school-supply": ("学习用品", "学校用品"),
    "toys.figure-doll": ("玩偶", "公仔"),
    "toys.building-set": ("积木", "拼装玩具"),
    "toys.game-puzzle": ("桌游拼图", "益智游戏"),
    "toys.outdoor-toy": ("户外玩具",),
    "baby.diaper": ("纸尿裤", "尿不湿"),
    "baby.feeding": ("婴儿喂养用品",),
    "baby.gear": ("婴儿用品", "母婴装备"),
    "pets.food": ("宠物食品", "宠物粮"),
    "pets.litter-supply": ("猫砂用品", "宠物清洁用品"),
    "pets.accessory": ("宠物用品", "宠物配件"),
    "grocery.snack": ("零食", "休闲食品"),
    "grocery.beverage": ("饮料", "饮品"),
    "grocery.pantry": ("厨房食品", "粮油调味"),
    "grocery.fresh": ("生鲜", "新鲜食品"),
    "grocery.frozen": ("冷冻食品", "速冻食品"),
    "grocery.household-consumable": ("家庭消耗品", "日用消耗品"),
    "jewelry.fine": ("贵重珠宝", "高级珠宝"),
    "jewelry.fashion": ("时尚首饰", "饰品"),
    "jewelry.watch": ("手表", "腕表"),
    "luggage.suitcase": ("行李箱", "旅行箱"),
    "luggage.bag": ("箱包", "背包"),
    "luggage.travel-accessory": ("旅行配件", "旅行收纳"),
    "musical-instruments.string": ("弦乐器", "吉他"),
    "musical-instruments.keyboard": ("键盘乐器", "电子琴"),
    "musical-instruments.percussion": ("打击乐器", "架子鼓"),
    "general.general-merchandise": ("一般商品", "综合百货"),
}


def build_cards() -> dict[str, object]:
    cards = []
    for card_id, minimum, p50, maximum in _CARD_SPECS:
        parent_id, leaf = card_id.split(".", 1)
        default_keywords = tuple(part for part in leaf.replace(".", "-").split("-") if part)
        positive, negative = _KEYWORD_OVERRIDES.get(card_id, (default_keywords, ()))
        entity_kind = "PRIMARY_PRODUCT"
        if card_id.endswith("accessory"):
            entity_kind = "ACCESSORY"
        elif card_id.endswith(("part", "component")):
            entity_kind = "REPLACEMENT_PART"
        elif card_id == "home.decor":
            entity_kind = "DECORATION"
        shipping_maximum, eta_max = _shipping_profile(card_id, maximum)
        in_stock = "0.95" if card_id in {"jewelry.fine", "jewelry.watch"} else "0.98"
        out_of_stock = "0.05" if in_stock == "0.95" else "0.02"
        cards.append(
            {
                "card_id": card_id,
                "parent_id": parent_id,
                "name": leaf.replace(".", " ").replace("-", " ").title(),
                "entity_kind": entity_kind,
                "category_aliases": [
                    card_id.replace(".", " / ").replace("-", " "),
                    *_ZH_ALIASES.get(card_id, ()),
                ],
                "positive_keywords": list(positive),
                "negative_keywords": list(negative),
                "prototype_text": _PROTOTYPE_OVERRIDES.get(
                    card_id,
                    card_id.replace(".", " ").replace("-", " "),
                ),
                "pricing": {
                    "hard_min": str(minimum),
                    "p50": str(p50),
                    "hard_max": str(maximum),
                },
                "inventory": {
                    "in_stock_probability": in_stock,
                    "out_of_stock_probability": out_of_stock,
                },
                "shipping": {
                    "minimum": "0",
                    "maximum": str(shipping_maximum),
                    "free_probability": "0.45",
                    "maximum_price_ratio": "0.50",
                    "delivery_days_min": 2,
                    "delivery_days_max": eta_max,
                },
            }
        )
    if len(cards) != 128 or len({card["card_id"] for card in cards}) != 128:
        raise RuntimeError("the fixed taxonomy must contain 128 unique leaves")
    return {
        "schema_version": "glodex.category-cards.v2",
        "ruleset_version": "semantic-category-clean-v4",
        "cards": cards,
    }


def _shipping_profile(card_id: str, maximum_price: int) -> tuple[int, int]:
    if card_id.startswith(("furniture.", "appliances.")):
        return min(1200, max(120, maximum_price // 20)), 15
    if card_id.startswith(("grocery.", "beauty.", "health.")):
        return min(60, max(10, maximum_price // 20)), 7
    return min(300, max(20, maximum_price // 30)), 10


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(build_cards(), ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
