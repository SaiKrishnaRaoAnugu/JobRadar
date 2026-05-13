import os
import jwt
import httpx
from urllib.parse import urlencode
from datetime import datetime, timedelta
from fastapi import HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

JWT_SECRET    = os.getenv('JWT_SECRET', 'jobradar-change-this-in-production')
JWT_ALGORITHM = 'HS256'
JWT_EXPIRE_DAYS = 30

GOOGLE_CLIENT_ID     = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
GOOGLE_REDIRECT_URI  = os.getenv('GOOGLE_REDIRECT_URI', 'http://localhost:7000/auth/google/callback').strip()

GOOGLE_AUTH_URL     = 'https://accounts.google.com/o/oauth2/v2/auth'
GOOGLE_TOKEN_URL    = 'https://oauth2.googleapis.com/token'
GOOGLE_USERINFO_URL = 'https://www.googleapis.com/oauth2/v2/userinfo'

_bearer = HTTPBearer(auto_error=False)


def create_token(user_id: int, email: str) -> str:
    payload = {
        'sub':   str(user_id),
        'email': email,
        'exp':   datetime.utcnow() + timedelta(days=JWT_EXPIRE_DAYS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def _decode(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail='Session expired — please log in again')
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail='Invalid token')


def require_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer)) -> dict:
    if not credentials:
        raise HTTPException(status_code=401, detail='Not authenticated')
    payload = _decode(credentials.credentials)
    return {'user_id': int(payload['sub']), 'email': payload['email']}


def optional_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer)):
    if not credentials:
        return None
    try:
        payload = _decode(credentials.credentials)
        return {'user_id': int(payload['sub']), 'email': payload['email']}
    except HTTPException:
        return None


def google_auth_url(state: str = '') -> str:
    params = urlencode({
        'client_id':     GOOGLE_CLIENT_ID,
        'redirect_uri':  GOOGLE_REDIRECT_URI,
        'response_type': 'code',
        'scope':         'openid email profile',
        'access_type':   'offline',
        'prompt':        'select_account',
        'state':         state,
    })
    return f'{GOOGLE_AUTH_URL}?{params}'


async def exchange_code_for_user(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            'code':          code,
            'client_id':     GOOGLE_CLIENT_ID,
            'client_secret': GOOGLE_CLIENT_SECRET,
            'redirect_uri':  GOOGLE_REDIRECT_URI,
            'grant_type':    'authorization_code',
        })
        if token_resp.status_code != 200:
            raise HTTPException(status_code=400, detail='Google token exchange failed')
        tokens = token_resp.json()

        user_resp = await client.get(GOOGLE_USERINFO_URL, headers={
            'Authorization': f'Bearer {tokens["access_token"]}'
        })
        if user_resp.status_code != 200:
            raise HTTPException(status_code=400, detail='Failed to fetch Google user info')
        return user_resp.json()
