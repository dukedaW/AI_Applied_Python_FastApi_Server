from typing import AsyncGenerator
import redis


async def get_redis_client():
    redis_client = None
    try:
        redis_client = redis.Redis(host="localhost", port=6379, db=0, decode_responses=True)
        redis_client.ping()
        yield redis_client
    except Exception as e:
        print(str(e))
        if redis_client is not None:
            await redis_client.close()
