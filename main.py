# -*- coding: utf-8 -*-
"""
@File: main.py
@Author: jigangwang
@Email: wjigang@grupotr.es
@Date: 2026-07-28
@Desc: 启动 FastAPI 日志追踪演示服务
"""

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
