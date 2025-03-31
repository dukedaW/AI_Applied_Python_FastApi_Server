import hashlib
import typing as tp

import sqlalchemy as sa
from fastapi import HTTPException, Depends, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.ext.asyncio import AsyncSession

from src.db_sqlite.engine import get_async_session


security = HTTPBasic()


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()


def verify_password(plain_password: str, hashed: str) -> bool:
    return hash_password(plain_password) == hashed


async def get_current_user(
    credentials: HTTPBasicCredentials = Depends(security),
    session: AsyncSession = Depends(get_async_session)
) -> tp.Dict[str, tp.Any]:

    query = sa.text("SELECT * FROM users WHERE email = :email")
    result = await session.execute(query, params={'email': credentials.username})
    user = result.fetchone()

    if not user or not verify_password(credentials.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверные учетные данные",
            headers={"WWW-Authenticate": "Basic"},
        )
    return {"id": user.id, "email": user.email}
