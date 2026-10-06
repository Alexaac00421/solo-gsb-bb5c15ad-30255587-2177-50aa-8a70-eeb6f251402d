"""数据库初始化入口：建表并写入政策版本与节假日表。

用法：python init_db.py
"""
from app.db import connect
from app.seed_data import seed


def main():
    connect()
    policy_count, day_count = seed()
    print(f"初始化完成：{policy_count} 个政策版本，CN 日历 {day_count} 个特殊日期（节假日/补班日）")


if __name__ == "__main__":
    main()
