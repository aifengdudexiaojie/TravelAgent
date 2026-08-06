import httpx
from config.llmConfig import get_settings


def get_location(address: str, city: str = "") -> dict:
    """
    通过高德地图 API 获取地点的经纬度。

    Args:
        address: 景点名称或详细地址
        city: 所在城市（可选，提高匹配精度）

    Returns:
        {"lng": 经度(float), "lat": 纬度(float)}

    Raises:
        ValueError: 未找到地点或 API 返回错误
    """
    url = "https://restapi.amap.com/v3/geocode/geo"
    settings = get_settings()

    params = {
        "address": address,
        "key": settings.GAODE_API_KEY,
        "city": city,
        "output": "json",
    }

    try:
        resp = httpx.get(url, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        raise ValueError(f"高德 API 请求失败: {e}")

    # 检查 API 状态
    if data.get("status") != "1":
        raise ValueError(f"高德 API 返回错误: {data.get('info', '未知错误')}")

    geocodes = data.get("geocodes") or []
    if not geocodes:
        raise ValueError(f"未找到地点: {address}")

    # location 格式: "经度,纬度"
    location_str = geocodes[0].get("location", "")
    if not location_str:
        raise ValueError(f"地点无坐标信息: {address}")

    try:
        lng_str, lat_str = location_str.split(",")
        return {"lng": float(lng_str), "lat": float(lat_str)}
    except (ValueError, TypeError) as e:
        raise ValueError(f"解析坐标失败: {location_str} ({e})")
