"""Tiện ích async."""
import asyncio
import functools


async def run_blocking(func, *args, **kwargs):
    """Chạy hàm đồng bộ trong thread pool (Python 3.8 không có asyncio.to_thread)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
