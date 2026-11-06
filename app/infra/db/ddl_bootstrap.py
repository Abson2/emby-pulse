"""2.0 批次 B3-B：``ensure_*`` 纯建表样板的公共实现（无状态）。

全库有 20 个 ``ensure_xxx_tables()`` 逐字复制了同一段 5~6 行骨架：

    with system_store.connect() as conn:
        cursor = conn.cursor()
        ensure_registered_table(cursor, TABLE)   # 或 for table_name in TABLES
        conn.commit()

本模块把它收敛为一个**纯函数**：``store`` 与 ``ensure_fn`` 由调用方显式
传入，模块本身不持有任何 ``system_store`` / 注册表状态，因此：

* 连接次数、cursor 创建、DDL 调用顺序、commit 次数、异常传播逐字等价；
* 各 DAO 模块仍使用自己已导入的 ``system_store`` / ``ensure_registered_table``，
  测试桩替换这两个名字的语义不变；
* 不引入缓存：带 PRAGMA / ALTER / 业务 SQL 的 ``ensure_*`` 一律不接入。

用法（调用点只保留一行）：
    ensure_registered_tables(system_store, ensure_registered_table, ("表名",))
"""
from __future__ import annotations


def ensure_registered_tables(store, ensure_fn, table_names) -> None:
    """逐表执行注册式 DDL，单次连接、单次提交；与复制的骨架完全等价。"""
    with store.connect() as conn:
        cursor = conn.cursor()
        for table_name in table_names:
            ensure_fn(cursor, table_name)
        conn.commit()
