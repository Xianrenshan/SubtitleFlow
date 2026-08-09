import contextlib
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select, update, delete, func, text
from backend.models import Base, TaskDB, CropRecord
from backend.config import backend_config

DATABASE_URL = "sqlite+aiosqlite:///./tasks.db"
engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        # 迁移：为已有数据库添加 file_size 列（列已存在则跳过）
        try:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN file_size INTEGER"))
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[migrate] 添加 file_size 失败: {e}")

        # 迁移：添加 token_usage 列
        try:
            await conn.execute(text("ALTER TABLE tasks ADD COLUMN token_usage JSON"))
        except Exception as e:
            if "duplicate column" not in str(e).lower():
                print(f"[migrate] 添加 token_usage 失败: {e}")

        # 迁移：将旧状态 pending 改为 uploaded（兼容旧数据）
        try:
            await conn.execute(
                text("UPDATE tasks SET status = 'uploaded' WHERE status = 'pending'")
            )
        except Exception:
            pass


async def get_task(task_id: str) -> TaskDB | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TaskDB).where(TaskDB.task_id == task_id))
        return result.scalar_one_or_none()


async def create_task(
    task_id: str,
    input_path: str,
    original_filename: str = None,
    file_size: int = None,
) -> None:
    """创建任务记录，默认状态为 uploaded（已上传未入队）"""
    async with AsyncSessionLocal() as session:
        task = TaskDB(
            task_id=task_id,
            status="uploaded",
            input_video_path=str(input_path),
            original_filename=original_filename,
            file_size=file_size,
            progress=0,
            current_step="已上传",
        )
        session.add(task)
        await session.commit()


async def update_task(task_id: str, expected_step: str = None, **kwargs) -> None:
    """更新任务，可选乐观锁（expected_step 校验当前步骤）"""
    async with AsyncSessionLocal() as session:
        if expected_step:
            stmt = (
                update(TaskDB)
                .where(
                    TaskDB.task_id == task_id,
                    TaskDB.current_step == expected_step,
                )
                .values(**kwargs)
            )
        else:
            stmt = update(TaskDB).where(TaskDB.task_id == task_id).values(**kwargs)
        await session.execute(stmt)
        await session.commit()


async def delete_old_tasks(hours: int) -> int:
    """删除超过指定小时数的已完成/失败任务"""
    from datetime import datetime, timedelta

    async with AsyncSessionLocal() as session:
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        stmt = delete(TaskDB).where(
            TaskDB.updated_at < cutoff,
            TaskDB.status.in_(["success", "failed", "interrupted"]),
        )
        result = await session.execute(stmt)
        await session.commit()
        return result.rowcount


async def get_all_tasks(
    status: str = None,
    page: int = 1,
    page_size: int = 20,
    search: str = None,
):
    """分页查询任务列表，支持逗号分隔的多状态筛选"""
    async with AsyncSessionLocal() as session:
        stmt = select(TaskDB).order_by(TaskDB.created_at.desc())

        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if statuses:
                stmt = stmt.where(TaskDB.status.in_(statuses))

        if search:
            stmt = stmt.where(TaskDB.original_filename.like(f"%{search}%"))

        count_stmt = select(func.count()).select_from(TaskDB)
        if stmt.whereclause is not None:
            count_stmt = count_stmt.where(stmt.whereclause)

        total_res = await session.execute(count_stmt)
        total = total_res.scalar()

        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        result = await session.execute(stmt)
        tasks = result.scalars().all()

        return tasks, total


async def get_tasks_by_status(status: str, limit: int = 1):
    """按状态查询任务（按创建时间升序），供调度器使用"""
    async with AsyncSessionLocal() as session:
        stmt = (
            select(TaskDB)
            .where(TaskDB.status == status)
            .order_by(TaskDB.created_at.asc())
            .limit(limit)
        )
        result = await session.execute(stmt)
        return result.scalars().all()


async def delete_task_by_id(task_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id == task_id))
        await session.commit()


async def delete_tasks_by_ids(task_ids: list):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id.in_(task_ids)))
        await session.commit()


# ==================== 裁剪记录 CRUD ====================

async def get_crops(task_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CropRecord).where(CropRecord.task_id == task_id)
        )
        return result.scalars().all()


async def get_crop(crop_id: str):
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CropRecord).where(CropRecord.crop_id == crop_id)
        )
        return result.scalar_one_or_none()


async def create_crop(crop_id: str, task_id: str, segments: list) -> None:
    async with AsyncSessionLocal() as session:
        crop = CropRecord(
            crop_id=crop_id,
            task_id=task_id,
            segments=segments,
            status="processing",
        )
        session.add(crop)
        await session.commit()


async def update_crop(crop_id: str, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        stmt = (
            update(CropRecord)
            .where(CropRecord.crop_id == crop_id)
            .values(**kwargs)
        )
        await session.execute(stmt)
        await session.commit()


async def delete_crop(crop_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(CropRecord).where(CropRecord.crop_id == crop_id))
        await session.commit()