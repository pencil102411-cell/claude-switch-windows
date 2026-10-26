"""同步 Claude 桌面端各账号的会话列表。

Claude 桌面端把 Code 标签页的会话列表按账号分开存放：
    %APPDATA%\\Claude\\claude-code-sessions\\<账号编号>\\<组织编号>\\local_<会话编号>.json
对话内容本身存在 %USERPROFILE%\\.claude\\projects\\，几个账号共用。
这个脚本把各账号文件夹里的会话列表文件互相补齐，切换账号后就能看到全部会话。

三种用法：
    python sync_sessions.py              只预览，不改任何文件
    python sync_sessions.py --apply      真正复制（需要先彻底退出桌面端）
    python sync_sessions.py --one-click  一键：关闭桌面端 → 同步 → 重新打开（桌面快捷方式用的就是这个）

规则（照搬 ai-usagebar / claude-acc 在 macOS 上的做法）：
- 某个账号没有的会话：从其他账号复制过去。
- 两边都有的会话：保留「最后活动时间」更新的那份。
- 在任何一个账号里删除过的会话（有 deleted_<编号> 标记）：不复制。
- 对话内容已经不在电脑上的会话：默认不复制（打开也是空的），加 --include-empty 才复制。
- 只复制 local_*.json，不碰登录信息、定时任务和 Cowork 会话。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

LOCAL_PREFIX = "local_"
DELETED_PREFIX = "deleted_"


@dataclass
class Session:
    path: Path
    session_id: str  # 文件名里 local_ 之后、.json 之前的编号
    last_activity: int  # 毫秒时间戳
    title: str
    cli_session_id: str  # 对话内容文件（.jsonl）的编号


@dataclass
class Folder:
    account: str
    org: str
    path: Path
    sessions: dict = field(default_factory=dict)  # 编号 -> Session
    deleted: set = field(default_factory=set)  # 删除标记里的编号
    broken: set = field(default_factory=set)  # 读不懂的 local_ 文件编号


@dataclass
class Action:
    kind: str  # "new" 新增 / "update" 覆盖更新
    source: Session
    target: Folder

    @property
    def destination(self) -> Path:
        return self.target.path / self.source.path.name


def default_root() -> Path:
    return Path(os.environ["APPDATA"]) / "Claude" / "claude-code-sessions"


def default_projects() -> Path:
    return Path.home() / ".claude" / "projects"


def read_session(path: Path, session_id: str):
    """读一个会话列表文件；格式不对就返回 None，这种文件既不当来源也不覆盖。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    activity = data.get("lastActivityAt")
    if not isinstance(activity, int) or isinstance(activity, bool):
        return None
    title = " ".join(str(data.get("title") or "(无标题)").split())
    return Session(path, session_id, activity, title, str(data.get("cliSessionId") or ""))


def scan_folders(root: Path) -> list:
    folders = []
    for account_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        for org_dir in sorted(p for p in account_dir.iterdir() if p.is_dir()):
            folder = Folder(account_dir.name, org_dir.name, org_dir)
            for entry in org_dir.iterdir():
                name = entry.name
                if not entry.is_file():
                    continue
                if name.startswith(LOCAL_PREFIX) and name.endswith(".json"):
                    session_id = name[len(LOCAL_PREFIX):-len(".json")]
                    session = read_session(entry, session_id)
                    if session is None:
                        folder.broken.add(session_id)
                    else:
                        folder.sessions[session_id] = session
                elif name.startswith(DELETED_PREFIX):
                    folder.deleted.add(name[len(DELETED_PREFIX):].removesuffix(".json"))
            folders.append(folder)
    return folders


def scan_transcripts(projects: Path) -> set:
    """所有对话内容文件的编号（projects\\<项目>\\<编号>.jsonl）。"""
    found = set()
    if not projects.is_dir():
        return found
    for project in os.scandir(projects):
        if not project.is_dir():
            continue
        for entry in os.scandir(project.path):
            if entry.is_file() and entry.name.endswith(".jsonl"):
                found.add(entry.name[:-len(".jsonl")])
    return found


def make_plan(folders: list, transcripts: set, include_empty: bool):
    """算出要复制哪些文件。只计算，不碰磁盘。"""
    tombstones = set().union(*(f.deleted for f in folders)) if folders else set()
    actions = []
    skipped = {}  # 目标文件夹路径 -> [(原因, Session)]
    for target in folders:
        # 其他账号里每个会话最新的那份
        best = {}
        for source in folders:
            if source is target:
                continue
            for session_id, session in source.sessions.items():
                seen = best.get(session_id)
                if seen is None or session.last_activity > seen.last_activity:
                    best[session_id] = session
        notes = skipped.setdefault(target.path, [])
        for session_id, session in sorted(best.items(), key=lambda kv: -kv[1].last_activity):
            mine = target.sessions.get(session_id)
            if mine is not None and mine.last_activity >= session.last_activity:
                continue  # 目标那份已经一样新或更新
            if session_id in tombstones:
                notes.append(("在某个账号里删除过", session))
            elif session_id in target.broken:
                notes.append(("目标账号里的同名文件读不懂，为安全起见不覆盖", session))
            elif session.cli_session_id not in transcripts and not include_empty:
                notes.append(("对话内容已不在电脑上，复制过去打开也是空的", session))
            else:
                actions.append(Action("new" if mine is None else "update", session, target))
    return actions, skipped


def current_account(root: Path) -> str:
    """桌面端当前登录的账号编号；只读 config.json 里这一项。"""
    try:
        data = json.loads((root.parent / "config.json").read_text(encoding="utf-8"))
        return str(data.get("lastKnownAccountUuid") or "")
    except (OSError, ValueError, AttributeError):
        return ""


def powershell(command: str):
    """跑一条 PowerShell 命令，返回输出；跑不了时返回 None。"""
    try:
        return subprocess.run(
            ["powershell", "-NoProfile", "-Command", command],
            capture_output=True, text=True, errors="replace", timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None


def desktop_processes():
    """桌面端（不是命令行版 claude.exe）的进程号列表；查不了时返回 None。"""
    out = powershell(
        "Get-Process -Name claude -ErrorAction SilentlyContinue | "
        "ForEach-Object { \"$($_.Id)`t$($_.Path)\" }"
    )
    if out is None:
        return None
    pids = []
    for line in out.splitlines():
        pid, _, path = line.strip().partition("\t")
        path = path.lower()
        if pid.isdigit() and path.endswith("\\claude.exe") and (
            "\\windowsapps\\claude_" in path or "\\anthropicclaude\\" in path
        ):
            pids.append(int(pid))
    return pids


def desktop_running():
    pids = desktop_processes()
    return None if pids is None else bool(pids)


def close_desktop(pids: list) -> bool:
    """先请求正常退出，10 秒后还在就强制结束。返回是否已全部退出。"""
    for force in (False, True):
        command = ["taskkill", "/T"] + (["/F"] if force else [])
        for pid in pids:
            command += ["/PID", str(pid)]
        subprocess.run(command, capture_output=True)
        for _ in range(20):
            time.sleep(0.5)
            pids = desktop_processes()
            if pids == []:
                return True
        if not pids:  # 查不了进程，没法确认
            return False
    return False


def open_desktop() -> bool:
    """从开始菜单的登记信息打开 Claude 桌面端（商店版和安装包版都适用）。"""
    out = powershell(
        "(Get-StartApps | Where-Object { $_.Name -eq 'Claude' } | Select-Object -First 1).AppID"
    )
    app_id = (out or "").strip()
    if not app_id:
        return False
    subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app_id}"])
    return True


def label(folder: Folder, folders: list, current: str) -> str:
    text = f"账号 {folder.account[:8]}"
    if sum(f.account == folder.account for f in folders) > 1:
        text += f" / 组织 {folder.org[:8]}"
    if folder.account == current:
        text += "（当前登录）"
    return text


def describe(session: Session) -> str:
    when = datetime.fromtimestamp(session.last_activity / 1000).strftime("%Y-%m-%d %H:%M")
    title = session.title if len(session.title) <= 40 else session.title[:39] + "…"
    name = session.path.name
    short = name if len(name) <= 20 else name[:18] + "…"
    return f"{when}  {title}  [{short}]"


def show_plan(root: Path, projects: Path, include_empty: bool, current: str, brief: bool):
    """扫描并打印计划，返回要做的复制；只有一个账号时返回空列表。"""
    folders = scan_folders(root)
    print(f"找到 {len(folders)} 个账号文件夹：")
    for folder in folders:
        extra = f"，{len(folder.deleted)} 条删除标记" if folder.deleted else ""
        extra += f"，{len(folder.broken)} 个读不懂的文件" if folder.broken else ""
        print(f"  · {label(folder, folders, current)}：{len(folder.sessions)} 条会话{extra}")
    if len(folders) < 2:
        print("\n只有一个账号文件夹，没有可以同步的。")
        return []

    actions, skipped = make_plan(folders, scan_transcripts(projects), include_empty)
    for folder in folders:
        mine = [a for a in actions if a.target is folder]
        notes = skipped.get(folder.path, [])
        print(f"\n【复制到 {label(folder, folders, current)}】")
        if not mine and not notes:
            print("  没有需要复制的。")
        for kind, name in (("new", "新增"), ("update", "覆盖更新（其他账号里的那份更新）")):
            group = [a for a in mine if a.kind == kind]
            if not group:
                continue
            print(f"  {name} {len(group)} 条：")
            shown = group[:5] if brief else group
            for action in shown:
                print(f"    {describe(action.source)}")
            if len(group) > len(shown):
                print(f"    ……还有 {len(group) - len(shown)} 条")
        if notes:
            reasons = "；".join(sorted({reason for reason, _ in notes}))
            print(f"  不复制 {len(notes)} 条：{reasons}" if brief else f"  不复制 {len(notes)} 条：")
            if not brief:
                for reason, session in notes:
                    print(f"    {describe(session)}  —— {reason}")

    new = sum(a.kind == "new" for a in actions)
    print(f"\n合计：复制 {len(actions)} 个文件，其中新增 {new}、覆盖更新 {len(actions) - new}。")
    return actions


def apply_plan(actions: list, root: Path, backup_dir: Path) -> Path:
    """先整体备份会话列表文件夹，再逐个复制（先写临时文件再改名，不会留下半个文件）。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"claude-code-sessions-{stamp}"
    counter = 1
    while backup.exists():  # 同一秒内跑了两次
        counter += 1
        backup = backup_dir / f"claude-code-sessions-{stamp}-{counter}"
    shutil.copytree(root, backup)
    for action in actions:
        temp = action.target.path / f".sync-tmp-{action.source.session_id}"
        shutil.copy2(action.source.path, temp)
        os.replace(temp, action.destination)
    return backup


def ask(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def one_click(root: Path, projects: Path, args, real: bool) -> int:
    """关闭桌面端 → 同步 → 重新打开。real=False 时（测试用的假文件夹）不碰桌面端。"""
    print("Claude 桌面端会话列表同步 —— 一键模式\n")
    if not root.is_dir():
        print("找不到会话列表文件夹。这台电脑上可能没用过桌面端的 Code 标签页。")
        return 1
    if real:
        pids = desktop_processes()
        if pids is None:
            print("无法确认 Claude 桌面端是否在运行，没有做任何改动。")
            return 1
        if pids:
            print("Claude 桌面端正在运行。同步前要先关闭它，里面正在进行的对话会被中断。")
            answer = ask("输入 Y 再按回车：关闭桌面端并开始同步；直接按回车：取消\n> ")
            if answer.strip().lower() != "y":
                print("已取消，没有做任何改动。")
                return 0
            print("正在关闭 Claude 桌面端……")
            if not close_desktop(pids):
                print("没能关闭桌面端，没有做任何改动。请在右下角托盘里手动退出它，再点一次。")
                return 1
            time.sleep(1)  # 等它把最后的状态写完
            print("已关闭。\n")

    current = current_account(root) if real else ""
    actions = show_plan(root, projects, args.include_empty, current, brief=True)
    if actions:
        backup = apply_plan(actions, root, args.backup_dir)
        print(f"同步完成。改动前的会话列表备份在：{backup}")
    else:
        print("两边已经一致，没有改动任何文件。")

    if real:
        print("\n正在重新打开 Claude 桌面端……")
        if not open_desktop():
            print("没找到桌面端的启动入口，请从开始菜单手动打开 Claude。")
        print("想换账号：在桌面端左下角退出登录，再用另一个账号登录。")
        print("以后在任意账号里新开了会话，再点一次这个图标就会同步给其他账号。")
    return 0


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="同步 Claude 桌面端各账号的会话列表（默认只预览）")
    parser.add_argument("--apply", action="store_true", help="真正复制（默认只预览）")
    parser.add_argument("--one-click", action="store_true",
                        help="一键：关闭桌面端 → 同步 → 重新打开")
    parser.add_argument("--include-empty", action="store_true",
                        help="对话内容已不在电脑上的会话也复制")
    parser.add_argument("--root", type=Path, default=None, help="会话列表文件夹（一般不用填）")
    parser.add_argument("--projects", type=Path, default=None, help="对话内容文件夹（一般不用填）")
    parser.add_argument("--backup-dir", type=Path, default=Path(__file__).resolve().parent / "backups",
                        help="同步时备份放哪（默认在脚本旁边的 backups 文件夹）")
    args = parser.parse_args()

    root = args.root or default_root()
    projects = args.projects or default_projects()
    real = args.root is None  # 动的是真实的会话列表，才需要管桌面端

    if args.one_click:
        try:
            return one_click(root, projects, args, real)
        except Exception as error:  # 窗口马上要等回车，至少让人看到出了什么错
            print(f"\n出错了，同步没有完成：{error!r}")
            return 1
        finally:
            ask("\n按回车关闭这个窗口……")

    mode = "同步模式" if args.apply else "预览模式（不会改动任何文件）"
    print(f"Claude 桌面端会话列表同步 —— {mode}\n")
    print(f"会话列表位置：{root}")
    print(f"对话内容位置：{projects}\n")
    if not root.is_dir():
        print("找不到会话列表文件夹。这台电脑上可能没用过桌面端的 Code 标签页。")
        return 1

    running = desktop_running() if real else False
    if args.apply and running is not False:
        if running is None:
            print("无法确认 Claude 桌面端是否已退出，没有做任何改动。")
        else:
            print("Claude 桌面端还在运行，没有做任何改动。请彻底退出它（包括右下角托盘图标）后再试。")
        return 1

    current = current_account(root) if real else ""
    actions = show_plan(root, projects, args.include_empty, current, brief=False)
    if not args.apply:
        if running:
            print("提示：Claude 桌面端正在运行。真正同步前要先彻底退出它（包括右下角托盘图标）。")
        print("这次是预览，没有改动任何文件。确认无误后，退出桌面端，再运行：")
        print("  python sync_sessions.py --apply")
        return 0
    if not actions:
        print("没有需要复制的，没有做任何改动。")
        return 0
    backup = apply_plan(actions, root, args.backup_dir)
    print(f"已完成。改动前的会话列表整体备份在：{backup}")
    print(f"如需还原：退出桌面端，把这个备份文件夹里的内容拷回 {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
