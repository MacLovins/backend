from uuid import uuid4

from leadradar_auth.schemas import Principal
from leadradar_auth.security import (
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)


def test_password_hashing():
    raw = "MySecretPassword123!"
    hashed = hash_password(raw)
    assert hashed != raw
    assert verify_password(raw, hashed) is True
    assert verify_password("WrongPassword!", hashed) is False


def test_jwt_token_encode_decode():
    user_id = uuid4()
    org_id = uuid4()
    principal = Principal(
        user_id=user_id,
        org_id=org_id,
        email="sales@leadradar.ai",
        role="sales",
        full_name="Sales Rep",
    )

    token = create_access_token(principal)
    assert isinstance(token, str)

    payload = decode_access_token(token)
    assert payload["sub"] == str(user_id)
    assert payload["org"] == str(org_id)
    assert payload["email"] == "sales@leadradar.ai"
    assert payload["role"] == "sales"
    assert payload["name"] == "Sales Rep"


def test_insecure_jwt_secret_reason():
    from leadradar_auth import insecure_jwt_secret_reason
    from leadradar_auth.settings import DEV_JWT_SECRET

    assert insecure_jwt_secret_reason(DEV_JWT_SECRET) == "known default value"
    assert insecure_jwt_secret_reason("") == "empty"
    assert insecure_jwt_secret_reason("x" * 31) is not None
    assert insecure_jwt_secret_reason("a3f1" * 16) is None
