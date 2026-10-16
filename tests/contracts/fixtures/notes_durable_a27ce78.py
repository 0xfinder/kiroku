# The durable function exactly as it was at commit a27ce78 (branch 7-notes), pinned as a
# fixture for the notes-durable-cancellation contract case. Never edit the code below.
async def durable[T](operation: Coroutine[Any, Any, T]) -> T:
    """Let a returned paid outcome reach storage on the first shutdown cancellation."""
    task = asyncio.create_task(operation)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(task, WRITE_GRACE_S)
        except TimeoutError:
            log.error("Notes storage unavailable during shutdown; pending span resumes on restart")
        finally:
            if not task.done():
                task.cancel()
        raise
