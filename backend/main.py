from fastapi import FastAPI
from app.api.router import router as api_router
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.model.base_model import Base
from app.model.user import User
app = FastAPI()
app.include_router(api_router)

@app.middleware("http")
async def add_process_time_header(request, call_next) -> None:
    print("开始")
    response = await call_next(request)
    print("结束")
    return response

engine_url = "mysql+aiomysql://root:123456@127.0.0.1:3306/app?charset=utf8"
async_demo = create_async_engine(
    url= engine_url,
    echo=True, # 输出日志
    pool_size=10, #连接池活跃数量
    max_overflow=20 #最大允许额外连接数量
)

async def creat_tables():
    async with async_demo.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

@app.on_event("startup")
async def start():
    await creat_tables()
@app.get("/") 
def read_root():
    return {"message": "Hello World"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7546)
