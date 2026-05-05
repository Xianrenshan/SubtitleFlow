import contextlib
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, update, delete, func
from backend.models import Base, TaskDB
from backend.config import backend_config

DATABASE_URL = "sqlite+aiosqlite:///./tasks.db"

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_task(task_id: str) -> TaskDB | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TaskDB).where(TaskDB.task_id == task_id))
        return result.scalar_one_or_none()

async def create_task(task_id: str, input_path: str, original_filename: str = None) -> None:
    async with AsyncSessionLocal() as session:
        task = TaskDB(task_id=task_id, input_video_path=input_path, original_filename=original_filename)
        session.add(task)
        await session.commit()

async def update_task(task_id: str, expected_step: str = None, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        stmt = update(TaskDB).where(TaskDB.task_id == task_id)
        if expected_step is not None:
            stmt = stmt.where(TaskDB.current_step == expected_step)
        stmt = stmt.values(**kwargs)
        await session.execute(stmt)
        await session.commit()

async def delete_old_tasks(hours: int) -> int:
    from datetime import datetime, timedelta
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            delete(TaskDB).where(TaskDB.created_at < cutoff, TaskDB.status.in_(["success", "failed"]))
        )
        await session.commit()
        return result.rowcount

# ========== 新增：历史查询 & 删除 ==========

async def get_all_tasks(status: str = None, page: int = 1, page_size: int = 20):
    """分页查询任务，支持状态筛选（逗号分隔多个状态）"""
    async with AsyncSessionLocal() as session:
        stmt = select(TaskDB).order_by(TaskDB.created_at.desc())

        # 如果传了 status，按逗号拆分后筛选
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if statuses:
                stmt = stmt.where(TaskDB.status.in_(statuses))

        # 计算总数
        count_stmt = select(func.count()).select_from(TaskDB)
        if stmt.whereclause is not None:
            count_stmt = count_stmt.where(stmt.whereclause)
        total_res = await session.execute(count_stmt)
        total = total_res.scalar()

        # 分页
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        result = await session.execute(stmt)
        tasks = result.scalars().all()
        return tasks, total

async def delete_task_by_id(task_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id == task_id))
        await session.commit()

async def delete_tasks_by_ids(task_ids: list):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id.in_(task_ids)))
        await session.commit()