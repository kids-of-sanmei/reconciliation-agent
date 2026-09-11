from fastapi import FastAPI
from app.api.router import router as api_router

app = FastAPI()
app.include_router(api_router)

@app.middleware("http")
async def add_process_time_header(request, call_next) -> None:
    print("开始")
    response = await call_next(request)
    print("结束")
    return response


@app.get("/") 
def read_root():
    return {"message": "Hello World"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=7546)
