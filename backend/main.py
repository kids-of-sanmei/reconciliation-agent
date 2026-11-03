"""保留现有启动入口，应用装配由 app.main 负责。"""

from app.main import app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="127.0.0.1", port=7546)
