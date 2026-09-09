"""测试运行时不得连接桌面程序的持久数据库。"""

import os

from sqlalchemy.engine import make_url


def test_backend_tests_use_an_isolated_database():
    database_url = os.environ.get("FOCUS_DATABASE_URL")
    assert database_url, "conftest 必须在测试模块导入前配置独立数据库"
    assert make_url(database_url).database != "focus"
