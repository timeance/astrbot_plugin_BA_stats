"""Broken Arrow map names. See docs/API_NOTES.md for the source of IDs and labels."""

# BATrace MapId (when verified), scene name, English menu name, 简体中文菜单名
_MAPS = (
    ("3", "Baltiisk", "Baltiisk Peninsula", "波罗的斯克半岛"),
    ("4", "Coast", "Black Coast", "黑海岸"),
    ("5", "Airport", "Riga Airport", "里加机场"),
    ("6", "River", "Meandering River", "蜿蜒之河"),
    ("7", "Dam", "The Dam", "考纳斯水坝"),
    ("8", "Tallinn Harbour", "Cold Harbor", "寒水港"),
    ("9", "Airbase", "Chkalovsk Airbase", "契卡洛夫斯克空军基地"),
    ("10", "Frontiers", "Highway of Death", "死亡公路"),
    ("11", "Central Village", "Dead Center", "死亡中心"),
    ("12", "Oil refinery", "Oil Rush", "石油热潮"),
    ("13", "Suwalki", "Suwalki Gap", "苏瓦乌基走廊"),
    ("14", "Jelgava", "Jelgavas Pils", "叶尔加瓦"),
    ("16", "Klaipeda", "Klaipeda Rift", "克莱佩达裂谷"),
    ("17", "Ruda", "Ruda Forest", "鲁达森林"),
    ("20", "Parnu", "Bay of Winds", "风啸湾"),
    ("21", "Chernyakhovsk", "Chernyakhovsky District", "切尔尼亚霍夫斯克"),
    ("22", "Ignalina Powerplant", "Ignalina Powerplant", "伊格纳利纳核电站"),
    ("23", "Kaliningrad", "Kaliningrad", "加里宁格勒"),
    ("25", "Kadaga Military Base", "Kadaga Military Base", "卡达加军事基地"),
    ("27", "Daugavpils", "Daugavpils", "陶格夫匹尔斯"),
    ("28", "Coal Mountain", "Coal Mountain", "煤山"),
    (None, "Narva", "Snow Castle", "纳尔瓦冰雪城堡"),
    (None, "Vyborg", "Vyborg", "维堡"),
    (None, "Kronstadt", "Kronstadt", "喀琅施塔得"),
    (None, "Tallinn Fortress", "Tallinn Fortress", "塔林要塞"),
    (None, "Nuclear Lenin", "Nuclear Lenin", "核动力列宁号"),
)
_BY_ID = {mid: zh for mid, _, _, zh in _MAPS if mid is not None}
_BY_NAME = {name.casefold(): zh for _, scene, en, zh in _MAPS for name in (scene, en)}


def display_map_name(map_id: str | None, name: str | None = None) -> str | None:
    if map_id in _BY_ID:
        return _BY_ID[map_id]
    if name and name not in (f"#{map_id}", str(map_id)):
        return _BY_NAME.get(name.casefold(), name)
    return f"地图#{map_id}" if map_id is not None else None
