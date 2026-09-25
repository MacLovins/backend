from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt
from pwdlib import PasswordHash

from leadradar_auth.schemas import Principal
from leadradar_auth.settings import auth_settings

password_hash_helper = PasswordHash.recommended()
# Precalculated dummy Argon2 hash for timing attack mitigation on failed lookups
DUMMY_HASH = password_hash_helper.hash("LeadRadarDummyPasswordToPreventTimingAttacks!123")


def hash_password(password: str) -> str:
    return password_hash_helper.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return password_hash_helper.verify(plain_password, hashed_password)


def create_access_token(principal: Principal) -> str:
    now = datetime.now(UTC)
    expire = now + timedelta(minutes=auth_settings.ACCESS_TTL_MIN)
    payload = {
        "sub": str(principal.user_id),
        "org": str(principal.org_id),
        "role": principal.role,
        "email": principal.email,
        "name": principal.full_name,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": auth_settings.ISSUER,
        "aud": auth_settings.AUDIENCE,
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, auth_settings.JWT_SECRET, algorithm=auth_settings.JWT_ALG)


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        auth_settings.JWT_SECRET,
        algorithms=[auth_settings.JWT_ALG],
        issuer=auth_settings.ISSUER,
        audience=auth_settings.AUDIENCE,
    )
