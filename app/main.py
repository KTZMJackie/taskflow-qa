from __future__ import annotations
import hashlib
from typing import Optional
from fastapi import Depends, FastAPI, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from itsdangerous import BadSignature, URLSafeSerializer
from pathlib import Path
from pydantic import BaseModel, Field
from app.database import get_conn, init_db
import os

SECRET = os.environ.get("TASKFLOW_SECRET", "dev-only-fallback-change-in-prod")
serializer = URLSafeSerializer(SECRET, salt="auth")
VALID_PRIORITIES = {"low", "medium", "high"}

app = FastAPI(title="TaskFlow API", version="1.0.0")
bearer = HTTPBearer(auto_error=False)
STATIC_DIR = Path(__file__).parent / "static"

class Credentials(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=6, max_length=128)


class TodoIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    priority: str = Field(default="medium")
    completed: bool = False


class TodoUpdate(BaseModel):
    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    priority: Optional[str] = None
    completed: Optional[bool] = None


def _hash(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def current_user(creds: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> int:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")
    try:
        data = serializer.loads(creds.credentials)
    except BadSignature:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid token")
    return int(data["uid"])


def _validate_priority(priority: str) -> None:
    if priority not in VALID_PRIORITIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"priority must be one of {sorted(VALID_PRIORITIES)}",
        )


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "version": app.version}

@app.post("/api/auth/register", status_code=201)
def register(creds: Credentials) -> dict:
    with get_conn() as conn:
        exists = conn.execute(
            "SELECT 1 FROM users WHERE username = ?", (creds.username,)
        ).fetchone()
        if exists:
            raise HTTPException(status.HTTP_409_CONFLICT, "Username already taken")
        cur = conn.execute(
            "INSERT INTO users (username, password) VALUES (?, ?)",
            (creds.username, _hash(creds.password)),
        )
        return {"id": cur.lastrowid, "username": creds.username}


@app.post("/api/auth/login")
def login(creds: Credentials) -> dict:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id, password FROM users WHERE username = ?", (creds.username,)
        ).fetchone()
    if row is None or row["password"] != _hash(creds.password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid credentials")
    token = serializer.dumps({"uid": row["id"]})
    return {"access_token": token, "token_type": "bearer"}

def _row_to_todo(row) -> dict:
    return {
        "id": row["id"],
        "title": row["title"],
        "priority": row["priority"],
        "completed": bool(row["completed"]),
        "created_at": row["created_at"],
    }


@app.get("/api/todos")
def list_todos(
    uid: int = Depends(current_user),
    completed: Optional[bool] = Query(default=None),
    priority: Optional[str] = Query(default=None),
) -> list:
    sql = "SELECT * FROM todos WHERE user_id = ?"
    params: list = [uid]
    if completed is not None:
        sql += " AND completed = ?"
        params.append(1 if completed else 0)
    if priority is not None:
        _validate_priority(priority)
        sql += " AND priority = ?"
        params.append(priority)
    sql += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_todo(r) for r in rows]


@app.post("/api/todos", status_code=201)
def create_todo(todo: TodoIn, uid: int = Depends(current_user)) -> dict:
    _validate_priority(todo.priority)
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO todos (user_id, title, priority, completed) VALUES (?, ?, ?, ?)",
            (uid, todo.title.strip(), todo.priority, int(todo.completed)),
        )
        row = conn.execute("SELECT * FROM todos WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _row_to_todo(row)


def _owned_todo_or_404(conn, todo_id: int, uid: int):
    row = conn.execute(
        "SELECT * FROM todos WHERE id = ? AND user_id = ?", (todo_id, uid)
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Todo not found")
    return row


@app.get("/api/todos/{todo_id}")
def get_todo(todo_id: int, uid: int = Depends(current_user)) -> dict:
    with get_conn() as conn:
        return _row_to_todo(_owned_todo_or_404(conn, todo_id, uid))


@app.put("/api/todos/{todo_id}")
def update_todo(todo_id: int, patch: TodoUpdate, uid: int = Depends(current_user)) -> dict:
    with get_conn() as conn:
        row = _owned_todo_or_404(conn, todo_id, uid)
        title = patch.title.strip() if patch.title is not None else row["title"]
        priority = patch.priority if patch.priority is not None else row["priority"]
        _validate_priority(priority)
        completed = patch.completed if patch.completed is not None else bool(row["completed"])
        conn.execute(
            "UPDATE todos SET title = ?, priority = ?, completed = ? WHERE id = ?",
            (title, priority, int(completed), todo_id),
        )
        updated = conn.execute("SELECT * FROM todos WHERE id = ?", (todo_id,)).fetchone()
    return _row_to_todo(updated)


@app.delete("/api/todos/{todo_id}", status_code=204, response_class=Response)
def delete_todo(todo_id: int, uid: int = Depends(current_user)) -> Response:
    with get_conn() as conn:
        _owned_todo_or_404(conn, todo_id, uid)
        conn.execute("DELETE FROM todos WHERE id = ?", (todo_id,))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")