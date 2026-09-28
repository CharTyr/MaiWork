"""日志记录能力模块（已移除）

正确的日志使用方式：

    # 新 API：通过 ctx.logger 获取标准 logging.Logger
    self.ctx.logger.info("插件已启动")
    self.ctx.logger.error("出错了", exc_info=True)

    # 或者用 stdlib logging 模块（Runner 进程内所有日志均会通过 IPC 自动传输）
    import logging
    logger = logging.getLogger(__name__)
    logger.info("有效的日志方式")

为什么移除异步 API：
    原来的 ``await ctx.logging.info(...)`` 需要在所有日志调用处加 await，
    无法捕获第三方库日志，且不能在同步函数中使用。
    现新的 IPC 日志桥（RunnerIPCLogHandler）在 Runner 进程全局拦截所有
    logging.root 上的日志，直接把标准 logging 作为唯一接口。
"""
