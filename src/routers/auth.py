import typing as tp

import sqlalchemy as sa
from fastapi import HTTPException, APIRouter, Depends, status
from fastapi.security import HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from src.db_sqlite.engine import get_async_session
from src.security.security import (
    verify_password,
    hash_password,
    security,
    get_current_user
)


router = APIRouter(prefix='/auth', tags=['Authentication'])


@router.post("/register", summary="Регистрация нового пользователя")
async def register(
        email: str,
        password: str,
        session: AsyncSession = Depends(get_async_session)
):

    query = sa.text("SELECT * FROM users WHERE email = :email")
    result = await session.execute(query, params={'email': email})

    if result.fetchone():
        raise HTTPException(status_code=400, detail="Пользователь с таким email уже зарегистрирован")

    stmt = sa.text("""
        INSERT INTO users (email, password_hash)
        VALUES (:email, :password_hash)
    """)
    await session.execute(stmt, params={'email': email, "password_hash": hash_password(password)})
    await session.commit()

    return {"msg": "Пользователь успешно зарегистрирован"}


@router.post("/login", summary="Аутентификация пользователя")
async def login(
    credentials: HTTPBasicCredentials = Depends(security),
    session: AsyncSession = Depends(get_async_session)
):
    query = sa.text("SELECT * FROM users WHERE email = :email")
    result = await session.execute(query, params={'email': credentials.username})
    user = result.fetchone()

    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный email или пароль",
            headers={"WWW-Authenticate": "Basic"},
        )

    return {"msg": "Успешная аутентификация", "user": {"id": user.id, "email": user.email}}


@router.get("/users/me", summary="Получение данных текущего пользователя")
async def read_current_user(
    current_user: tp.Dict[str, tp.Any] = Depends(get_current_user)
) -> tp.Dict[str, tp.Any]:
    """
    Текущий пользователь
    """
    return {"user": current_user}