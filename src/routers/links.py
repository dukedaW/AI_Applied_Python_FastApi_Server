import datetime as dt
import string
import random
import typing as tp
from fastapi import HTTPException, APIRouter, Query, Depends, status, Body
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
import jwt
import hashlib
from pydantic import HttpUrl
from sqlalchemy.ext.asyncio import AsyncSession
import sqlalchemy as sa
from src.db_sqlite.engine import get_async_session
from src.redis_.engine import get_redis_client

ALIAS_LENGTH = 10
SECRET_KEY = "SECRET_KEY"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 10
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/token")

router = APIRouter(prefix='/links', tags=['Links'])


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(plain_password: str, hashed: str) -> bool:
    return hash_password(plain_password) == hashed

def create_access_token(data: dict, expires_delta: dt.timedelta | None = None) -> str:
    to_encode = data.copy()
    expire = dt.datetime.now() + (expires_delta or dt.timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    to_encode["exp"] = expire
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)



async def get_current_user(
    token: str = Depends(oauth2_scheme),
    session: AsyncSession = Depends(get_async_session)
) -> tp.Dict[str, str]:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_email: str = payload.get("sub")
        if user_email is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверные учетные данные")
    except jwt.PyJWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Неверные учетные данные")

    query = sa.text(
        """
        select * from users where email = :email
        """
    )
    result = await session.execute(query, params={'email': user_email})
    user = result.fetchone()
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь не найден")

    return {"id": user.id, "email": user.email}


@router.post("/register", summary="Регистрация нового пользователя")
async def register(
        email: str,
        password: str,
        session: AsyncSession = Depends(get_async_session)
):
    query = sa.text(
        """
        select * from users where email = :email
        """
    )
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


@router.post("/token", summary="Получение JWT токена")
async def login(
        form_data: OAuth2PasswordRequestForm = Depends(),
        session: AsyncSession = Depends(get_async_session)
):
    query = sa.text(
        """
        select * from users where email = :email
        """
    )
    result = await session.execute(query, params={'email': form_data.username})

    user = result.fetchone()
    if not user or not verify_password(form_data.password, user.password_hash):
        raise HTTPException(status_code=400, detail="Неверный email или пароль")

    token = create_access_token(data={"sub": user.email})
    return {"access_token": token, "token_type": "bearer"}

async def alias_exists(alias: str, session: AsyncSession) -> bool:
    result = await session.execute(
        sa.text("SELECT id FROM links WHERE custom_alias = :alias"),
        {"alias": alias}
    )
    return result.fetchone() is not None


@router.post("/shorten", description="Получить сокращенную ссылку")
async def shorten_link(
        url: HttpUrl,
        session=Depends(get_async_session),
        redis_client=Depends(get_redis_client),
        custom_alias: str | None = None,
        expires_at: dt.datetime | None = None,
        token: tp.Optional[str] = Depends(oauth2_scheme)
) -> tp.Dict[str, str]:

    user_id = None
    if token:
        try:
            current_user = await get_current_user(token, session)
            user_id = current_user["id"]
        except HTTPException:
            user_id = None

    if custom_alias is not None:
        if await alias_exists(custom_alias, session):
            raise HTTPException(status_code=400, detail="Алиас уже занят. Выберите другое значение.")
        alias = custom_alias
    else:
        while True:
            potential = "".join(random.choices(string.ascii_letters + string.digits, k=ALIAS_LENGTH))
            if not await alias_exists(potential, session):
                alias = potential
                break

    alias = alias.replace('http://', '').replace('https://', '')

    if expires_at is None:
        expires_at = dt.datetime.now() + dt.timedelta(minutes=3)

    try:
        await session.execute(
            sa.text("""
                    INSERT INTO links (original_url, custom_alias, expires_at, user_id)
                    VALUES (:original_url, :alias, :expires_at, :user_id)
                """),
            {"original_url": str(url),
             "alias": alias,
             "expires_at": expires_at,
             "user_id": user_id,}
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=500, detail="Ошибка сохранения данных в базе") from exc

    try:
        redis_client.set(alias, str(url))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {'original_url': str(url), "short_link": alias}


@router.get("/{alias}", description="Перенаправление сокращенного URL")
async def redirect_link(
        alias: str,
        session: AsyncSession = Depends(get_async_session),
        redis_client=Depends(get_redis_client),
) -> RedirectResponse:
    original_url = redis_client.get(alias)
    if original_url:
        await session.execute(
            sa.text("UPDATE links SET clicks = clicks + 1 WHERE custom_alias = :alias"),
            {"alias": alias}
        )
        await session.commit()

    if isinstance(original_url, bytes):
        original_url = original_url.decode("utf-8")
        return RedirectResponse(url=original_url)

    query = sa.text("""
            SELECT original_url, expires_at, clicks
            FROM links
            WHERE custom_alias = :alias
        """)
    result = await session.execute(query, {"alias": alias})
    row = result.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Сокращенная ссылка не найдена.")

    original_url, expires_at, clicks = row

    if expires_at is not None:
        if isinstance(expires_at, str):
            expires_at_clean = expires_at.split('.')[0]
            expires_at = dt.datetime.strptime(expires_at_clean, "%Y-%m-%d %H:%M:%S")
        if dt.datetime.now() > expires_at:
            raise HTTPException(status_code=410, detail="Ссылка устарела.")

    await session.execute(
        sa.text("UPDATE links SET clicks = clicks + 1 WHERE custom_alias = :alias"),
        {"alias": alias}
    )
    await session.commit()

    try:
        redis_client.set(alias, original_url)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return RedirectResponse(url=original_url)

@router.delete("/{alias}", description="Удалить сокращенную ссылку")
async def delete_link(
    alias: str,
    session: AsyncSession = Depends(get_async_session),
    redis_client=Depends(get_redis_client),
    current_user: tp.Dict[str, str] = Depends(get_current_user)
) -> tp.Dict[str, str]:
    result = await session.execute(
        sa.text("SELECT id, user_id FROM links WHERE custom_alias = :alias"),
        {"alias": alias}
    )
    row = result.fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail="Ссылка с данным алиасом не найдена.")

    row_data = dict(row._mapping)
    if row_data['user_id'] != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Вы не можете удалять чужую ссылку")

    try:
        await session.execute(
            sa.text("DELETE FROM links WHERE custom_alias = :alias"),
            {"alias": alias}
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=500, detail="Ошибка удаления данных из базы") from exc

    try:
        redis_client.delete(alias)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {"detail": f"Сокращенная ссылка с алиасом '{alias}' успешно удалена."}


@router.put("/{alias}", description="Обновляет оригинальный URL для данного короткого адреса")
async def update_link(
    alias: str,
    update_link_url: HttpUrl,
    session: AsyncSession = Depends(get_async_session),
    redis_client=Depends(get_redis_client),
    current_user: tp.Dict[str, str] = Depends(get_current_user),
) -> tp.Dict[str, str]:

    query = sa.text("SELECT id FROM links WHERE custom_alias = :alias")
    result = await session.execute(query, {"alias": alias})
    row = result.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Ссылка с данным алиасом не найдена.")

    row_data = dict(row._mapping)
    if row_data['user_id'] != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Вы не можете изменять чужую ссылку")

    try:
        await session.execute(
            sa.text("UPDATE links SET original_url = :new_url WHERE custom_alias = :alias"),
            {"new_url": str(update_link_url), "alias": alias}
        )
        await session.commit()
    except Exception as exc:
        await session.rollback()
        raise HTTPException(status_code=500, detail="Ошибка сохранения данных в базе") from exc

    try:
        redis_client.set(alias, str(update_link_url))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"detail": f"URL для '{alias}' успешно обновлен.", "original_url": str(update_link_url)}


@router.get("/{alias}/stats", description="Статистика по ссылке: отображает оригинальный URL, дату создания, количество переходов и дату последнего использования")
async def get_link_stats(
    alias: str,
    session: AsyncSession = Depends(get_async_session),
) -> tp.Dict[str, tp.Any]:
    query = sa.text("""
        SELECT original_url, created_at, clicks, expires_at
        FROM links
        WHERE custom_alias = :alias
    """)
    result = await session.execute(query, {"alias": alias})
    row = result.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Сокращенная ссылка не найдена.")

    row_data = dict(row._mapping)
    return {
        "original_url": row_data["original_url"],
        "created_at": row_data["created_at"] if row_data["created_at"] else None,
        "clicks": row_data["clicks"],
        "expires_at": row_data["expires_at"] if row_data["expires_at"] else None,
    }


@router.get("/links/search", description="Поиск ссылки по оригинальному URL")
async def search_link(
    original_url: str,
    session: AsyncSession = Depends(get_async_session),
) -> tp.List[tp.Dict[str, tp.Any]]:
    query = sa.text("""
        SELECT custom_alias, original_url, created_at, clicks, expires_at 
        FROM links
        WHERE original_url = :original_url
    """)
    result = await session.execute(query, {"original_url": original_url})
    rows = result.fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail="Ссылка с указанным оригинальным URL не найдена.")

    links = []
    for row in rows:
        row_data = dict(row._mapping)
        links.append({
            "custom_alias": row_data["custom_alias"],
            "original_url": row_data["original_url"],
            "created_at": row_data["created_at"] if row_data["created_at"] else None,
            "clicks": row_data["clicks"],
            "expires_at": row_data["expires_at"] if row_data["expires_at"] else None,
        })
    return links

