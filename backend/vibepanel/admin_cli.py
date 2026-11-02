"""Đặt lại mật khẩu admin từ server khi quên (chạy trong container):

    docker exec -it opsvibes opsvibes-reset-password [--email EMAIL] [--password]

Mặc định sinh mật khẩu ngẫu nhiên và in ra. Mọi phiên đăng nhập của tài khoản bị đăng xuất.
"""

import argparse
import getpass
import secrets
import sys

from sqlalchemy import delete, select

from .config import get_settings
from .db import init_engine, session_factory
from .models import User, UserSession
from .security import hash_password

MIN_PASSWORD = 10


def _ask_password() -> str:
    while True:
        pw = getpass.getpass("Mật khẩu mới / New password: ")
        if len(pw) < MIN_PASSWORD:
            print(f"Cần ít nhất {MIN_PASSWORD} ký tự / At least {MIN_PASSWORD} characters", file=sys.stderr)
            continue
        if getpass.getpass("Nhập lại / Confirm: ") == pw:
            return pw
        print("Hai lần nhập không khớp / Passwords do not match", file=sys.stderr)


def reset_password_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="opsvibes-reset-password", description="Đặt lại mật khẩu admin / Reset an admin password")
    parser.add_argument("--email", help="tài khoản cần đặt lại (bỏ trống nếu chỉ có một admin)")
    parser.add_argument("--password", action="store_true", help="tự nhập mật khẩu thay vì sinh ngẫu nhiên")
    args = parser.parse_args(argv)

    settings = get_settings()
    if not settings.db_path.exists():
        print(f"Không thấy DB tại {settings.db_path} / Database not found", file=sys.stderr)
        return 1
    init_engine(f"sqlite:///{settings.db_path}")

    with session_factory()() as db:
        if args.email:
            user = db.scalar(select(User).where(User.email == args.email.strip().lower()))
            if user is None:
                print(f"Không có tài khoản {args.email} / No such account", file=sys.stderr)
                return 1
        else:
            admins = db.scalars(select(User).where(User.is_admin.is_(True)).order_by(User.id)).all()
            if not admins:
                print("Chưa có admin, dùng setup token để tạo / No admin yet, use the setup token", file=sys.stderr)
                return 1
            if len(admins) > 1:
                print("Có nhiều admin, chọn bằng --email / Several admins, pick one with --email:", file=sys.stderr)
                for a in admins:
                    print(f"  {a.email}", file=sys.stderr)
                return 1
            user = admins[0]

        password = _ask_password() if args.password else secrets.token_urlsafe(12)
        user.password_hash = hash_password(password)
        db.execute(delete(UserSession).where(UserSession.user_id == user.id))
        db.commit()
        email = user.email

    print(f"Đã đặt lại mật khẩu / Password reset: {email}")
    if not args.password:
        print(f"Mật khẩu mới / New password: {password}")
    print("Mọi phiên đăng nhập đã bị đăng xuất / All sessions were signed out")
    return 0
