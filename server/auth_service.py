"""Server-side validation and MySQL-backed registration/login workflows."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

try:
    import bcrypt
except ModuleNotFoundError:
    bcrypt = None

from server.database import Database, DatabaseConfigurationError
from server.email_service import EmailDeliveryError, EmailService
from server.env_config import EnvConfigurationError, env_int, env_text


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
LOGIN_ID_PATTERN = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9]{4,20}$")
PASSWORD_PATTERN = re.compile(r"^[A-Za-z0-9!@#$]{6,20}$")
PHONE_PATTERN = re.compile(r"^[0-9-]{8,20}$")
EMPLOYEE_NUMBER_PATTERN = re.compile(r"^[0-9]{1,10}$")


class AuthError(RuntimeError):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class SlidingWindowLimiter:
    """Process-local request limiter; DB controls email delivery across workers."""

    def __init__(self) -> None:
        self._entries: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def ensure_allowed(self, key: str, limit: int, window_sec: int) -> None:
        now = time.monotonic()
        with self._lock:
            entries = [value for value in self._entries.get(key, []) if value > now - window_sec]
            if len(entries) >= limit:
                raise AuthError("요청 횟수가 너무 많습니다. 잠시 후 다시 시도해 주세요.", 429)
            entries.append(now)
            self._entries[key] = entries


@dataclass(frozen=True)
class AuthSettings:
    verification_ttl_sec: int
    resend_cooldown_sec: int
    max_verification_attempts: int
    max_verification_sends_per_hour: int
    login_limit: int
    login_window_sec: int
    verification_secret: str

    @classmethod
    def from_environment(cls) -> "AuthSettings":
        try:
            return cls(
                verification_ttl_sec=env_int("EMAIL_VERIFICATION_TTL_SEC", minimum=1),
                resend_cooldown_sec=env_int(
                    "EMAIL_VERIFICATION_RESEND_COOLDOWN_SEC", minimum=1
                ),
                max_verification_attempts=env_int(
                    "EMAIL_VERIFICATION_MAX_ATTEMPTS", minimum=1
                ),
                max_verification_sends_per_hour=env_int(
                    "EMAIL_VERIFICATION_MAX_SENDS", minimum=1
                ),
                login_limit=env_int("LOGIN_MAX_FAILURES", minimum=1),
                login_window_sec=env_int("LOGIN_LOCKOUT_SEC", minimum=1),
                verification_secret=env_text("EMAIL_VERIFICATION_SECRET"),
            )
        except EnvConfigurationError as exc:
            raise AuthError("인증 환경 설정값이 올바르지 않습니다.", 503) from exc


class AuthService:
    def __init__(
        self,
        database: Database | None = None,
        email_service: EmailService | None = None,
        settings: AuthSettings | None = None,
        limiter: SlidingWindowLimiter | None = None,
    ) -> None:
        self.database = database or Database()
        self.email_service = email_service or EmailService()
        self.settings = settings or AuthSettings.from_environment()
        self.limiter = limiter or SlidingWindowLimiter()

    @staticmethod
    def normalize_email(value: Any) -> str:
        email = str(value or "").strip().lower()
        if not EMAIL_PATTERN.fullmatch(email) or len(email) > 100:
            raise AuthError("올바른 이메일 주소를 입력해 주세요.")
        return email

    @staticmethod
    def normalize_login_id(value: Any) -> str:
        login_id = str(value or "").strip()
        if not LOGIN_ID_PATTERN.fullmatch(login_id):
            raise AuthError("로그인 ID는 영문을 포함한 4~20자의 영문·숫자만 사용할 수 있습니다.")
        return login_id

    @staticmethod
    def validate_password(value: Any) -> str:
        password = str(value or "")
        if not PASSWORD_PATTERN.fullmatch(password):
            raise AuthError("비밀번호는 6~20자의 영문, 숫자, ! @ # $만 사용할 수 있습니다.")
        return password

    @staticmethod
    def _normalize_name(value: Any) -> str:
        name = str(value or "").strip()
        if not 1 <= len(name) <= 20:
            raise AuthError("이름은 1~20자로 입력해 주세요.")
        return name

    @staticmethod
    def _normalize_phone(value: Any) -> str:
        phone = str(value or "").strip()
        if not PHONE_PATTERN.fullmatch(phone):
            raise AuthError("전화번호는 숫자와 하이픈으로 8~20자 입력해 주세요.")
        return phone

    @staticmethod
    def _normalize_employee_number(value: Any) -> int:
        raw = str(value or "").strip()
        if not EMPLOYEE_NUMBER_PATTERN.fullmatch(raw):
            raise AuthError("사번은 최대 10자리 숫자로 입력해 주세요.")
        number = int(raw)
        if number > 2_147_483_647:
            raise AuthError("사번 값이 너무 큽니다.")
        return number

    def check_availability(self, field: str, value: Any) -> dict[str, bool]:
        if field == "login_id":
            normalized, column = self.normalize_login_id(value), "login_id"
        elif field == "employee_number":
            normalized, column = self._normalize_employee_number(value), "employee_number"
        elif field == "email":
            normalized, column = self.normalize_email(value), "email"
        else:
            raise AuthError("지원하지 않는 중복 확인 항목입니다.")
        try:
            with self.database.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f"SELECT 1 FROM users WHERE {column} = %s LIMIT 1", (normalized,))
                    return {"available": cursor.fetchone() is None}
        except DatabaseConfigurationError as exc:
            raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc

    def send_email_verification(self, email_value: Any, client_key: str) -> dict[str, int]:
        email = self.normalize_email(email_value)
        self.limiter.ensure_allowed(f"email:{client_key}", 10, 3600)
        now = _utcnow()
        try:
            with self.database.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT 1 FROM users WHERE email = %s LIMIT 1", (email,))
                    if cursor.fetchone() is not None:
                        raise AuthError("이미 가입된 이메일입니다.", 409)
                    cursor.execute(
                        "SELECT created_at FROM email_verifications WHERE email = %s ORDER BY created_at DESC LIMIT 1",
                        (email,),
                    )
                    latest = cursor.fetchone()
                    if latest and (now - _as_utc_naive(latest["created_at"])).total_seconds() < self.settings.resend_cooldown_sec:
                        raise AuthError("인증번호를 이미 발송했습니다. 잠시 후 다시 시도해 주세요.", 429)
                    cursor.execute(
                        "SELECT COUNT(*) AS count FROM email_verifications WHERE email = %s AND created_at >= %s",
                        (email, now - timedelta(hours=1)),
                    )
                    if int(cursor.fetchone()["count"]) >= self.settings.max_verification_sends_per_hour:
                        raise AuthError("이메일 인증 요청 횟수를 초과했습니다. 한 시간 후 다시 시도해 주세요.", 429)
                    verification_id = str(uuid4())
                    code = f"{secrets.randbelow(1_000_000):06d}"
                    cursor.execute(
                        """
                        INSERT INTO email_verifications
                            (verification_id, email, code_hash, expires_at, attempts, created_at)
                        VALUES (%s, %s, %s, %s, 0, %s)
                        """,
                        (verification_id, email, self._code_hash(verification_id, code), now + timedelta(seconds=self.settings.verification_ttl_sec), now),
                    )
        except DatabaseConfigurationError as exc:
            raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc
        try:
            self.email_service.send_verification_code(email, code, max(1, self.settings.verification_ttl_sec // 60))
        except EmailDeliveryError as exc:
            try:
                with self.database.transaction() as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("DELETE FROM email_verifications WHERE verification_id = %s", (verification_id,))
            except DatabaseConfigurationError:
                pass
            raise AuthError("인증 이메일을 발송하지 못했습니다. no-reply SMTP 설정을 확인해 주세요.", 503) from exc
        return {"expires_in_sec": self.settings.verification_ttl_sec, "resend_after_sec": self.settings.resend_cooldown_sec}

    def verify_email_code(self, email_value: Any, code_value: Any) -> None:
        email = self.normalize_email(email_value)
        code = str(code_value or "").strip()
        if not re.fullmatch(r"\d{6}", code):
            raise AuthError("인증번호는 6자리 숫자입니다.")
        now = _utcnow()
        try:
            with self.database.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT verification_id, code_hash, expires_at, attempts
                        FROM email_verifications
                        WHERE email = %s AND verified_at IS NULL AND consumed_at IS NULL
                        ORDER BY created_at DESC LIMIT 1 FOR UPDATE
                        """,
                        (email,),
                    )
                    verification = cursor.fetchone()
                    if verification is None:
                        raise AuthError("사용 가능한 이메일 인증 요청이 없습니다.")
                    if _as_utc_naive(verification["expires_at"]) < now:
                        raise AuthError("인증번호가 만료되었습니다. 다시 발송해 주세요.")
                    if int(verification["attempts"]) >= self.settings.max_verification_attempts:
                        raise AuthError("인증번호 입력 횟수를 초과했습니다. 다시 발송해 주세요.", 429)
                    if not hmac.compare_digest(verification["code_hash"], self._code_hash(verification["verification_id"], code)):
                        cursor.execute("UPDATE email_verifications SET attempts = attempts + 1 WHERE verification_id = %s", (verification["verification_id"],))
                        raise AuthError("인증번호가 일치하지 않습니다.")
                    cursor.execute("UPDATE email_verifications SET verified_at = %s WHERE verification_id = %s", (now, verification["verification_id"]))
        except DatabaseConfigurationError as exc:
            raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc

    def register(self, payload: dict[str, Any]) -> None:
        if bcrypt is None:
            raise AuthError("비밀번호 보안 모듈을 사용할 수 없습니다. requirements 설치를 확인해 주세요.", 503)
        email = self.normalize_email(payload.get("email"))
        login_id = self.normalize_login_id(payload.get("login_id"))
        password = self.validate_password(payload.get("password"))
        if password != str(payload.get("password_confirm") or ""):
            raise AuthError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")
        name = self._normalize_name(payload.get("name"))
        phone = self._normalize_phone(payload.get("phone_number"))
        employee_number = self._normalize_employee_number(payload.get("employee_number"))
        now = _utcnow()
        try:
            with self.database.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT verification_id FROM email_verifications
                        WHERE email = %s AND verified_at IS NOT NULL AND consumed_at IS NULL AND expires_at >= %s
                        ORDER BY verified_at DESC LIMIT 1 FOR UPDATE
                        """,
                        (email, now),
                    )
                    verification = cursor.fetchone()
                    if verification is None:
                        raise AuthError("서버에서 확인된 이메일 인증이 필요합니다.", 403)
                    cursor.execute(
                        """
                        SELECT email, login_id, employee_number FROM users
                        WHERE email = %s OR login_id = %s OR employee_number = %s FOR UPDATE
                        """,
                        (email, login_id, employee_number),
                    )
                    duplicate = cursor.fetchone()
                    if duplicate is not None:
                        if duplicate["email"] == email:
                            raise AuthError("이미 가입된 이메일입니다.", 409)
                        if duplicate["login_id"] == login_id:
                            raise AuthError("사용할 수 없는 ID입니다.", 409)
                        raise AuthError("이미 사용 중인 사번입니다.", 409)
                    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                    cursor.execute(
                        """
                        INSERT INTO users (email, login_id, password_hash, name, phone_number, employee_number)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (email, login_id, password_hash, name, phone, employee_number),
                    )
                    cursor.execute("UPDATE email_verifications SET consumed_at = %s WHERE verification_id = %s", (now, verification["verification_id"]))
        except DatabaseConfigurationError as exc:
            raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc

    def login(self, login_id_value: Any, password_value: Any, client_key: str) -> dict[str, Any]:
        if bcrypt is None:
            raise AuthError("비밀번호 보안 모듈을 사용할 수 없습니다. requirements 설치를 확인해 주세요.", 503)
        login_id = self.normalize_login_id(login_id_value)
        password = str(password_value or "")
        self.limiter.ensure_allowed(f"login:{client_key}:{login_id.lower()}", self.settings.login_limit, self.settings.login_window_sec)
        try:
            with self.database.transaction() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT user_id, login_id, name, password_hash, is_deleted FROM users WHERE login_id = %s LIMIT 1",
                        (login_id,),
                    )
                    user = cursor.fetchone()
                    if user is None or bool(user["is_deleted"]) or not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
                        raise AuthError("로그인 ID 또는 비밀번호가 올바르지 않습니다.", 401)
                    cursor.execute("UPDATE users SET login_at = %s WHERE user_id = %s", (_utcnow(), user["user_id"]))
                    return {"user_id": int(user["user_id"]), "login_id": user["login_id"], "name": user["name"]}
        except DatabaseConfigurationError as exc:
            raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc

    def _code_hash(self, verification_id: str, code: str) -> str:
        return hmac.new(
            self.settings.verification_secret.encode("utf-8"),
            f"{verification_id}:{code}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _as_utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo is not None else value

# JWT-backed email verification deliberately keeps all pre-registration state
# outside MySQL.  The signed token never contains the raw six-digit code.
def _jwt_b64encode(value: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt_b64decode(value: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _issue_email_jwt(self: AuthService, purpose: str, email: str, code_proof: str | None = None) -> str:
    import json

    issued_at = int(time.time())
    payload: dict[str, Any] = {
        "iss": "dabom-email-verification",
        "typ": purpose,
        "email": email,
        "iat": issued_at,
        "exp": issued_at + self.settings.verification_ttl_sec,
    }
    if code_proof is not None:
        payload["code_proof"] = code_proof
    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = ".".join(
        (
            _jwt_b64encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
            _jwt_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        )
    )
    signature = hmac.new(
        self.settings.verification_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_jwt_b64encode(signature)}"


def _read_email_jwt(self: AuthService, token: Any, expected_purpose: str) -> dict[str, Any]:
    import json

    if not isinstance(token, str) or not token:
        raise AuthError("이메일 인증 정보가 없습니다. 인증 절차를 다시 진행해 주세요.", 403)
    try:
        header_segment, payload_segment, signature_segment = token.split(".")
        signing_input = f"{header_segment}.{payload_segment}"
        expected_signature = hmac.new(
            self.settings.verification_secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
        ).digest()
        supplied_signature = _jwt_b64decode(signature_segment)
        header = json.loads(_jwt_b64decode(header_segment).decode("utf-8"))
        payload = json.loads(_jwt_b64decode(payload_segment).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("이메일 인증 정보가 유효하지 않습니다. 인증 절차를 다시 진행해 주세요.", 403) from exc
    if not hmac.compare_digest(expected_signature, supplied_signature):
        raise AuthError("이메일 인증 정보가 유효하지 않습니다. 인증 절차를 다시 진행해 주세요.", 403)
    if (
        not isinstance(header, dict)
        or header.get("alg") != "HS256"
        or header.get("typ") != "JWT"
        or not isinstance(payload, dict)
        or payload.get("iss") != "dabom-email-verification"
        or payload.get("typ") != expected_purpose
        or not isinstance(payload.get("email"), str)
    ):
        raise AuthError("이메일 인증 정보가 유효하지 않습니다. 인증 절차를 다시 진행해 주세요.", 403)
    try:
        expires_at = int(payload["exp"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthError("이메일 인증 정보가 유효하지 않습니다. 인증 절차를 다시 진행해 주세요.", 403) from exc
    if expires_at < int(time.time()):
        raise AuthError("이메일 인증이 만료되었습니다. 인증 절차를 다시 진행해 주세요.", 403)
    return payload


def _jwt_send_email_verification(self: AuthService, email_value: Any, client_key: str) -> dict[str, Any]:
    # client_key is intentionally retained for the stable API signature. This
    # JWT design has no pre-registration DB write and no resend-rate storage.
    del client_key
    email = self.normalize_email(email_value)

    try: # 이메일 중복 검사
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM users WHERE email = %s LIMIT 1",
                    (email,),
                )
                if cursor.fetchone() is not None:
                    raise AuthError("이미 가입된 이메일입니다.", 409)
    except DatabaseConfigurationError as exc:
        raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc
    
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_proof = self._code_hash(f"jwt:{email}", code)
    challenge_token = _issue_email_jwt(self, "email_code", email, code_proof)
    try:
        self.email_service.send_verification_code(email, code, max(1, self.settings.verification_ttl_sec // 60))
    except EmailDeliveryError as exc:
        raise AuthError("인증 이메일을 발송하지 못했습니다. Gmail SMTP 설정을 확인해 주세요.", 503) from exc
    return {"expires_in_sec": self.settings.verification_ttl_sec, "challenge_token": challenge_token}


def _jwt_verify_email_code(self: AuthService, email_value: Any, code_value: Any, challenge_token: Any) -> str:
    email = self.normalize_email(email_value)
    code = str(code_value or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise AuthError("인증번호는 6자리 숫자입니다.")
    challenge = _read_email_jwt(self, challenge_token, "email_code")
    if not hmac.compare_digest(challenge["email"], email):
        raise AuthError("인증 요청 이메일과 입력 이메일이 일치하지 않습니다.", 403)
    expected_proof = challenge.get("code_proof")
    actual_proof = self._code_hash(f"jwt:{email}", code)
    if not isinstance(expected_proof, str) or not hmac.compare_digest(expected_proof, actual_proof):
        raise AuthError("인증번호가 일치하지 않습니다.")
    return _issue_email_jwt(self, "email_verified", email)


def _jwt_register(self: AuthService, payload: dict[str, Any], verified_token: Any) -> None:
    verified = _read_email_jwt(self, verified_token, "email_verified")
    email = self.normalize_email(payload.get("email"))
    if not hmac.compare_digest(verified["email"], email):
        raise AuthError("인증된 이메일과 가입 이메일이 일치하지 않습니다.", 403)
    # The original registration implementation verifies email_verifications.
    # Its database-only portion is reproduced here without any verification-table access.
    if bcrypt is None:
        raise AuthError("비밀번호 보안 모듈을 사용할 수 없습니다. requirements 설치를 확인해 주세요.", 503)
    login_id = self.normalize_login_id(payload.get("login_id"))
    password = self.validate_password(payload.get("password"))
    if password != str(payload.get("password_confirm") or ""):
        raise AuthError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")
    name = self._normalize_name(payload.get("name"))
    phone = self._normalize_phone(payload.get("phone_number"))
    employee_number = self._normalize_employee_number(payload.get("employee_number"))
    try:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT email, login_id, employee_number FROM users WHERE email = %s OR login_id = %s OR employee_number = %s FOR UPDATE",
                    (email, login_id, employee_number),
                )
                duplicate = cursor.fetchone()
                if duplicate is not None:
                    if duplicate["email"] == email:
                        raise AuthError("이미 가입된 이메일입니다.", 409)

                    if duplicate["login_id"] == login_id:
                        raise AuthError("이미 사용 중인 로그인 ID입니다.", 409)

                    raise AuthError("이미 사용 중인 사번입니다.", 409)
                password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                cursor.execute(
                    "INSERT INTO users (email, login_id, password_hash, name, phone_number, employee_number) VALUES (%s, %s, %s, %s, %s, %s)",
                    (email, login_id, password_hash, name, phone, employee_number),
                )
    except DatabaseConfigurationError as exc:
        raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc


AuthService._issue_email_jwt = _issue_email_jwt
AuthService._read_email_jwt = _read_email_jwt
AuthService.send_email_verification = _jwt_send_email_verification
AuthService.verify_email_code = _jwt_verify_email_code
AuthService.register = _jwt_register

# Email is the only login identifier.  This override intentionally avoids the
# optional login_id column so the existing users table is not altered.
def _email_only_check_availability(self: AuthService, field: str, value: Any) -> dict[str, bool]:
    if field != "employee_number":
        raise AuthError("지원하지 않는 중복 확인 항목입니다.")
    employee_number = self._normalize_employee_number(value)
    try:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM users WHERE employee_number = %s LIMIT 1", (employee_number,))
                return {"available": cursor.fetchone() is None}
    except DatabaseConfigurationError as exc:
        raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc


def _email_only_register(self: AuthService, payload: dict[str, Any], verified_token: Any) -> None:
    verified = self._read_email_jwt(verified_token, "email_verified")
    email = self.normalize_email(payload.get("email"))
    if not hmac.compare_digest(verified["email"], email):
        raise AuthError("인증된 이메일과 가입 이메일이 일치하지 않습니다.", 403)
    if bcrypt is None:
        raise AuthError("비밀번호 보안 모듈을 사용할 수 없습니다. requirements 설치를 확인해 주세요.", 503)
    password = self.validate_password(payload.get("password"))
    if password != str(payload.get("password_confirm") or ""):
        raise AuthError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")
    name = self._normalize_name(payload.get("name"))
    phone = self._normalize_phone(payload.get("phone_number"))
    employee_number = self._normalize_employee_number(payload.get("employee_number"))
    try:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT email, employee_number FROM users WHERE email = %s OR employee_number = %s FOR UPDATE",
                    (email, employee_number),
                )
                duplicate = cursor.fetchone()
                if duplicate is not None:
                    if duplicate["email"] == email:
                        raise AuthError("이미 가입된 이메일입니다.", 409) # 문자 깨짐 수정
                    raise AuthError("이미 사용 중인 사번입니다.", 409)
                password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                cursor.execute(
                    "INSERT INTO users (email, password_hash, name, phone_number, employee_number) VALUES (%s, %s, %s, %s, %s)",
                    (email, password_hash, name, phone, employee_number),
                )
    except DatabaseConfigurationError as exc:
        raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc


def _email_only_login(self: AuthService, email_value: Any, password_value: Any, client_key: str) -> dict[str, Any]:
    if bcrypt is None:
        raise AuthError("비밀번호 보안 모듈을 사용할 수 없습니다. requirements 설치를 확인해 주세요.", 503)
    email = self.normalize_email(email_value)
    password = str(password_value or "")
    self.limiter.ensure_allowed(f"login:{client_key}:{email}", self.settings.login_limit, self.settings.login_window_sec)
    try:
        with self.database.transaction() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT user_id, email, name, password_hash, is_deleted FROM users WHERE email = %s LIMIT 1",
                    (email,),
                )
                user = cursor.fetchone()
                if user is None or bool(user["is_deleted"]) or not bcrypt.checkpw(password.encode("utf-8"), user["password_hash"].encode("utf-8")):
                    raise AuthError("이메일 또는 비밀번호가 올바르지 않습니다.", 401)
                cursor.execute("UPDATE users SET login_at = %s WHERE user_id = %s", (_utcnow(), user["user_id"]))
                return {"user_id": int(user["user_id"]), "login_id": user["email"], "name": user["name"]}
    except DatabaseConfigurationError as exc:
        raise AuthError("인증 데이터베이스를 사용할 수 없습니다.", 503) from exc


AuthService.check_availability = _email_only_check_availability
AuthService.register = _email_only_register
AuthService.login = _email_only_login
