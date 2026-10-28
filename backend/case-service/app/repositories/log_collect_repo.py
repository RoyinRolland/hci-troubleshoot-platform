"""日志收集任务数据访问层。"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.log_collect import LogCollectTask
from ..schemas.log_collect import ACTIVE_STATUSES


class LogCollectTaskRepository:
    """任务表 CRUD。

    事务边界由 DatabaseManager 的 session 生成器统一提交，本层只 flush。
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(self, task: LogCollectTask) -> LogCollectTask:
        self.session.add(task)
        await self.session.flush()
        await self.session.refresh(task)
        return task

    async def get_by_id(self, task_id) -> LogCollectTask | None:
        result = await self.session.execute(select(LogCollectTask).where(LogCollectTask.task_id == task_id))
        return result.scalar_one_or_none()

    async def get_active_by_case(self, case_id: str) -> LogCollectTask | None:
        """返回该工单的活跃任务（同一工单最多一个，见部分唯一索引）。"""
        result = await self.session.execute(
            select(LogCollectTask)
            .where(LogCollectTask.case_id == case_id)
            .where(LogCollectTask.status.in_(ACTIVE_STATUSES))
            .order_by(LogCollectTask.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_by_case(
        self,
        case_id: str,
        *,
        created_by: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[LogCollectTask], int]:
        """按工单查询任务。

        ``created_by`` 非空时只返回该身份创建的任务：同一工单号可能被多个客户端
        复用（日志收集入口允许跨客户端指定），任务详情按创建者隔离。
        """
        filters = [LogCollectTask.case_id == case_id]
        if created_by is not None:
            filters.append(LogCollectTask.created_by == created_by)

        total_result = await self.session.execute(select(func.count()).select_from(LogCollectTask).where(*filters))
        total = int(total_result.scalar_one() or 0)
        result = await self.session.execute(
            select(LogCollectTask)
            .where(*filters)
            .order_by(LogCollectTask.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all()), total

    async def flush(self) -> None:
        await self.session.flush()

    async def commit(self) -> None:
        """结束当前事务，把连接先还给连接池。

        存在的理由只有一个：**长文件 IO 绝不能跨在事务中间**。
        连接级参数 ``idle_in_transaction_session_timeout`` 只有 30s（见
        shared/database/postgres.py），而合并一个日志包是纯磁盘/CPU 工作，
        实测 3.6 GiB 约 50s、20 GiB 更久。事务若一直开着，PG 会在合并期间
        直接 terminate 该连接，随后的 flush 必然 ``connection is closed``，
        表现为 complete 返回 500、制品元数据永远落不了库。
        """
        await self.session.commit()

    async def refresh(self, task: LogCollectTask) -> None:
        await self.session.refresh(task)
