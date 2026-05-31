import asyncio
import aiohttp

async def test():
    url = "http://remarkable-joy.railway.internal:2333/version"
    headers = {"Authorization": "jarvisbot"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as r:
                print(f"Status: {r.status}")
                print(f"Response: {await r.text()}")
    except Exception as e:
        print(f"Failed: {e}")

asyncio.run(test())