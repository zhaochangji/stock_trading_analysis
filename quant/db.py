# -*- coding: utf-8 -*-
"""quant 数据库访问(与 InStock 同库,默认连接参数一致,支持环境变量覆盖)。"""

import os

import pymysql
from sqlalchemy import create_engine

db_host = os.environ.get("db_host", "localhost")
db_user = os.environ.get("db_user", "root")
db_password = os.environ.get("db_password", "root")
db_database = os.environ.get("db_database", "instockdb")
db_port = int(os.environ.get("db_port", "3306"))
db_charset = os.environ.get("db_charset", "utf8mb4")

_CONN = {"host": db_host, "user": db_user, "password": db_password,
         "database": db_database, "charset": db_charset, "port": db_port,
         "autocommit": True}

_ENGINE = None


def get_engine():
    global _ENGINE
    if _ENGINE is None:
        url = (f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}/"
               f"{db_database}?charset={db_charset}")
        _ENGINE = create_engine(url)
    return _ENGINE


def get_connection():
    return pymysql.connect(**_CONN)
