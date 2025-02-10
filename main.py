# main.py
import uvicorn
from fastapi import FastAPI
from routes import router as api_router
from database import init_db

app = FastAPI()
app.include_router(api_router)


@app.on_event("startup")
async def startup():
    init_db()

if __name__ == '__main__':
    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=True)
