"""
FastAPI 日志追踪演示服务

核心方案：每个请求通过 contextvars 绑定唯一 trace_id，
通过中间件自动注入到整个请求链路的所有日志中。

高并发场景下，只需在日志中搜索某个 trace_id，即可获取该请求的完整调用链。

启动方式：
    uv run uvicorn server:app --host 0.0.0.0 --port 8000
    # 或
    python main.py

压测验证（高并发）：
    uv run python -c "
    import asyncio, httpx
    async def main():
        async with httpx.AsyncClient() as c:
            tasks = [c.get('http://127.0.0.1:8000/orders/create') for _ in range(200)]
            await asyncio.gather(*tasks)
    asyncio.run(main())
    "
"""
import asyncio
import random

from fastapi                        import FastAPI, Request
from fastapi.responses              import JSONResponse

from logger_utils.trace             import set_trace_id, get_trace_id
from logger_utils.logger_manager    import logger

# ──────────────────────────────────────────────
# FastAPI 应用
# ──────────────────────────────────────────────
app = FastAPI(title="日志追踪演示服务")


# ──────────────────────────────────────────────
# Trace ID 中间件
# ──────────────────────────────────────────────
@app.middleware("http")
async def trace_middleware(request: Request, call_next):
    """
    每个请求进入时：
    1. 从 Header 中提取 trace_id（没有则自动生成）
    2. 写入 contextvars，后续所有日志自动带上
    3. 响应头中返回 trace_id，方便前端/调用方追踪
    """
    # 优先取上游传入的，没有则生成新的
    trace_id = request.headers.get("X-Trace-Id")
    if trace_id:
        set_trace_id(trace_id)
    else:
        trace_id = set_trace_id()

    response = await call_next(request)
    response.headers["X-Trace-Id"] = trace_id

    return response


# ──────────────────────────────────────────────
# 模拟复杂的业务处理
# ──────────────────────────────────────────────
async def simulate_db_query(label: str, delay: float = 0.05):
    """模拟数据库查询"""
    logger.debug(f"[DB] 查询 {label} ...")
    await asyncio.sleep(random.uniform(delay * 0.5, delay * 1.5))
    logger.debug(f"[DB] 查询 {label} 完成")


async def simulate_cache_op(label: str):
    """模拟缓存操作"""
    hit = random.choice([True, False])
    logger.debug(f"[CACHE] {label} 命中={hit}")
    return hit


async def simulate_rpc_call(service: str, delay: float = 0.1):
    """模拟 RPC 调用下游服务"""
    logger.info(f"[RPC] → 调用 {service} ...")
    await asyncio.sleep(random.uniform(delay * 0.5, delay * 1.5))
    success = random.random() > 0.1  # 90% 成功率
    if success:
        logger.info(f"[RPC] ← {service} 返回成功")
    else:
        logger.error(f"[RPC] ← {service} 返回失败！")
    return success


# ──────────────────────────────────────────────
# 业务接口
# ──────────────────────────────────────────────
@app.get("/health")
async def health():
    logger.info("健康检查")
    return {"status": "ok", "trace_id": get_trace_id()}


@app.get("/orders/create")
async def create_order():
    """
    模拟创建订单的完整链路（6 步），每步都有日志。
    高并发时，所有日志都带有该请求专属的 trace_id。
    """
    logger.info("━━━ 开始创建订单 ━━━")

    # Step 1: 查询用户
    logger.info("[1/6] 查询用户信息")
    await simulate_db_query("users")
    logger.info("[1/6] 用户信息查询完成")

    # Step 2: 查询库存
    logger.info("[2/6] 查询商品库存")
    await simulate_cache_op("stock:item_123")
    await simulate_db_query("inventory")
    logger.info("[2/6] 库存查询完成")

    # Step 3: 锁定库存（RPC 调用库存服务）
    logger.info("[3/6] 锁定库存")
    ok = await simulate_rpc_call("inventory-service")
    if not ok:
        logger.error("[3/6] 库存锁定失败，订单创建中止")
        return JSONResponse(
            {"error": "库存不足", "trace_id": get_trace_id()},
            status_code=500,
        )

    # Step 4: 创建订单
    logger.info("[4/6] 写入订单记录")
    await simulate_db_query("orders (INSERT)")
    logger.info("[4/6] 订单记录写入完成")

    # Step 5: 清理缓存
    logger.info("[5/6] 清理相关缓存")
    await simulate_db_query("cache (DELETE)")
    logger.info("[5/6] 缓存清理完成")

    # Step 6: 发送通知
    logger.info("[6/6] 发送订单通知")
    await simulate_rpc_call("notification-service")
    logger.info("[6/6] 通知发送完成")

    logger.info("━━━ 订单创建成功 ━━━")
    return {"status": "success", "trace_id": get_trace_id(), "steps": 6}


@app.get("/users/{user_id}")
async def get_user(user_id: int):
    """模拟查询用户"""
    logger.info(f"查询用户 ID={user_id}")
    await simulate_cache_op(f"user:{user_id}")
    await simulate_db_query(f"users WHERE id={user_id}")
    logger.info(f"用户 ID={user_id} 查询完成")
    return {"user_id": user_id, "name": f"用户_{user_id}", "trace_id": get_trace_id()}


@app.get("/orders/batch")
async def batch_orders():
    """模拟批量操作，内部并发处理多个子任务（子任务共享父 trace_id + 子序号）"""
    logger.info("开始批量处理订单")

    async def process_one(i: int):
        # 子任务使用父 trace_id 加后缀，既有继承又能区分
        parent_tid = get_trace_id()
        set_trace_id(f"{parent_tid}-{i}")
        logger.info(f"子任务 [{i}] 开始")
        await simulate_db_query(f"batch_item_{i}")
        logger.info(f"子任务 [{i}] 完成")

    tasks = [process_one(i) for i in range(5)]
    await asyncio.gather(*tasks)

    logger.info("批量处理完成")
    return {"status": "success", "batch_count": 5, "trace_id": get_trace_id()}
