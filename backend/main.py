"""后端启动入口，依赖组装和业务流程位于 app 包。"""

from app.factory import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=7546)
