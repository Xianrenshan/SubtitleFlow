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

    # 🆕 迁移：为已有数据库添加 file_size 列
    try:
        await conn.execute(text("ALTER TABLE tasks ADD COLUMN file_size INTEGER"))
    except Exception:
        pass # 列已存在，忽略

async def get_task(task_id: str) -> TaskDB | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TaskDB).where(TaskDB.task_id == task_id))
        return result.scalar_one_or_none()

async def create_task(task_id: str, input_path: str, original_filename: str = None, file_size: int = None) -> None:
    async with AsyncSessionLocal() as session:
        task = TaskDB(task_id=task_id, input_video_path=input_path, original_filename=original_filename, file_size=file_size)
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

# ========== 历史查询 & 删除 ==========
async def get_all_tasks(status: str = None, page: int = 1, page_size: int = 20, search: str = None):
    """分页查询任务，支持状态筛选（逗号分隔多个状态）和文件名搜索"""
    async with AsyncSessionLocal() as session:
        stmt = select(TaskDB).order_by(TaskDB.created_at.desc())
        
        if status:
            statuses = [s.strip() for s in status.split(",") if s.strip()]
            if statuses:
                stmt = stmt.where(TaskDB.status.in_(statuses))
                
        # 🆕 新增：文件名模糊搜索
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

async def delete_task_by_id(task_id: str):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id == task_id))
        await session.commit()

async def delete_tasks_by_ids(task_ids: list):
    async with AsyncSessionLocal() as session:
        await session.execute(delete(TaskDB).where(TaskDB.task_id.in_(task_ids)))
        await session.commit()

# ==================== 裁剪记录数据库操作 ====================
async def get_crops(task_id: str):
    """获取某任务的所有裁剪版本（按创建时间倒序）"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(CropRecord)
            .where(CropRecord.task_id == task_id)
            .order_by(CropRecord.created_at.desc())
        )
        return result.scalars().all()

async def get_crop(crop_id: str) -> CropRecord | None:
    """获取单个裁剪记录"""
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(CropRecord).where(CropRecord.crop_id == crop_id))
        return result.scalar_one_or_none()

async def create_crop(crop_id: str, task_id: str, segments: list) -> None:
    async with AsyncSessionLocal() as session:
        crop = CropRecord(crop_id=crop_id, task_id=task_id, segments=segments, status="processing")
        session.add(crop)
        await session.commit()

async def update_crop(crop_id: str, **kwargs) -> None:
    async with AsyncSessionLocal() as session:
        stmt = update(CropRecord).where(CropRecord.crop_id == crop_id).values(**kwargs)
        await session.execute(stmt)
        await session.commit()

async def delete_crop(crop_id: str) -> None:
    async with AsyncSessionLocal() as session:
        await session.execute(delete(CropRecord).where(CropRecord.crop_id == crop_id))
        await session.commit()
