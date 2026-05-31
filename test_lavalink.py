import asyncio
import aiohttp

async def test():
    url = "http://remarkable-joy.railway.internal:2333/version"
    headers = {"Authorization": "jarvisbot"}
    print(f"Trying to connect to {url}...")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
                print(f"Status: {r.status}")
                print(f"Response: {await r.text()}")
    except Exception as e:
        print(f"Failed: {type(e).__name__}: {e}")
    print("Done!")

asyncio.run(test())