import requests
from config.llmConfig import get_settings

def get_location(address: str, city: str) -> dict:

    url = "https://restapi.amap.com/v3/geocode/geo"
    settings = get_settings()

    params = {
        "address": address,
        "key": settings.GAODE_API_KEY,
        "city":city,
        "output":"json",
    }

    response = requests.get(
        url,
        params=params
    )

    data = response.json()

    lng = data["geocodes"][0]["location"]
    lat = data["geocodes"][1]["location"]

    return {
        "lng": float(lng),
        "lat": float(lat)
    }
