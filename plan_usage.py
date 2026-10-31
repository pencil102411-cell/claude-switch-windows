"""从桌面端自己的用量记录里，读出每个账号的 5 小时额度和周额度。

桌面端运行时会隔一段时间（大约 15 分钟）把当前组织的用量百分比记进数据文件夹里的
plan-usage-history.json：
    {"version": 2, "samples": [{"t": 毫秒时间戳, "org": 组织编号, "u": {"fh": 5小时用量%, "sd": 7天用量%}}]}
这个文件不随账号切换，所有账号的记录都在里面。账号和组织的对应关系来自
claude-code-sessions\\<账号编号>\\<组织编号> 的文件夹结构。不联网，也不读登录令牌。

没在登录的账号只能看到它最后一次登录时记下的数据。记录里没有恢复时间，只能推算：
- 最后一条记录离现在已超过窗口长度（5 小时 / 7 天）的，额度一定已经恢复；
- 恢复时间只估一个「最晚」时刻。
"""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

HISTORY_FILE = "plan-usage-history.json"
FIVE_HOURS = 5 * 3600 * 1000
WEEK = 7 * 24 * 3600 * 1000
RESET = "已恢复"
IDLE = "未开始"


@dataclass
class Window:
    remaining: int  # 剩余 %
    reset_by: Optional[int]  # 最晚在这个时刻（毫秒）恢复；估不出时为 None
    note: str  # "" / RESET / IDLE


@dataclass
class Usage:
    sampled_at: int  # 最后一条记录的时间（毫秒）
    five_hour: Window
    weekly: Window


def left(used: float) -> int:
    return max(0, min(100, round(100 - used)))


def read_samples(path: Path) -> dict:
    """组织编号 -> [(时间, 5小时用量, 7天用量)]，按时间排好；读不了就返回空。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    samples = data.get("samples") if isinstance(data, dict) else None
    by_org = {}
    for item in samples if isinstance(samples, list) else []:
        try:
            t, org, fh, sd = item["t"], item["org"], item["u"]["fh"], item["u"]["sd"]
        except (KeyError, TypeError):
            continue
        numbers = (t, fh, sd)
        if isinstance(org, str) and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in numbers):
            by_org.setdefault(org, []).append((int(t), fh, sd))
    for rows in by_org.values():
        rows.sort()
    return by_org


def account_orgs(sessions_root: Path) -> dict:
    """账号编号 -> [组织编号]。"""
    found = {}
    if sessions_root.is_dir():
        for account in sessions_root.iterdir():
            if account.is_dir():
                found[account.name] = [org.name for org in account.iterdir() if org.is_dir()]
    return found


def five_hour(rows: list, now: int) -> Window:
    last_t, used, _ = rows[-1]
    if now - last_t >= FIVE_HOURS:
        return Window(100, None, RESET)
    if used <= 0:
        return Window(100, None, IDLE)
    # 往回找本窗口最早的一条记录；窗口在它之前就开始了，所以最晚在它之后 5 小时恢复
    start, later = last_t, used
    for t, value, _ in reversed(rows[:-1]):
        if value <= 0 or value > later or last_t - t >= FIVE_HOURS:
            break
        start, later = t, value
    if start + FIVE_HOURS <= now:
        return Window(100, None, RESET)
    return Window(left(used), start + FIVE_HOURS, "")


def weekly(rows: list, now: int) -> Window:
    last_t, _, used = rows[-1]
    if now - last_t >= WEEK:
        return Window(100, None, RESET)
    # 周额度按固定周期恢复：找最近一次用量下降（上一次恢复），之后每 7 天恢复一次
    reset_by = None
    for (_, _, before), (t, _, after) in zip(reversed(rows[:-1]), reversed(rows[1:])):
        if after < before:
            reset_by = t + WEEK
            break
    if reset_by is not None:
        while reset_by <= last_t:  # 中间有几周没有记录
            reset_by += WEEK
        if reset_by <= now:
            return Window(100, None, RESET)
    return Window(left(used), reset_by, "")


class Snapshot:
    """某一时刻读到的用量记录；读一次，之后按需推算各账号的额度。"""

    def __init__(self, data_dir: Path):
        self.samples = read_samples(data_dir / HISTORY_FILE)
        self.orgs = account_orgs(data_dir / "claude-code-sessions")

    def usage(self, account: str, now: int) -> Optional[Usage]:
        candidates = [self.samples[org] for org in self.orgs.get(account, []) if org in self.samples]
        if not candidates:
            return None
        rows = max(candidates, key=lambda r: r[-1][0])  # 一个账号有几个组织时，取最近在用的
        return Usage(rows[-1][0], five_hour(rows, now), weekly(rows, now))
