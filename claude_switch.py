"""Claude 桌面端一键切换账号（Windows）。

桌面端同一时间只能登录一个账号。这个工具把每个账号的登录状态各存一份，
切换时把当前账号的最新状态存回去、换上目标账号的，再同步会话列表并重新打开桌面端。
做法参照 ai-usagebar / claude-acc 在 macOS 上的实现，按 Windows 上的文件位置做了调整。

一个账号的「登录状态」包括：
- %APPDATA%\\Claude\\config.json 里的 oauth:* 和 lastKnownAccountUuid 这几项（加密的登录令牌）
- Network\\Cookies、Local Storage、Session Storage、IndexedDB（桌面端内置浏览器记住的身份）
登录令牌是用这台电脑、这个 Windows 用户专属的密钥加密的，拷到别的电脑或别的用户下用不了。

保存的账号放在 %LOCALAPPDATA%\\claude-switch\\profiles，
每次切换前的原始状态备份在 %LOCALAPPDATA%\\claude-switch\\backups（保留最近 10 份）。

用法：
    python claude_switch.py              打开菜单（桌面快捷方式用的就是这个）
    python claude_switch.py list         列出已保存的账号
    python claude_switch.py save 名字    把当前登录的账号存下来
    python claude_switch.py switch 名字  切换到已保存的账号
    python claude_switch.py add 名字     添加新账号（会打开登录界面）
    python claude_switch.py undo         撤销上一次切换或添加
"""

import argparse
import json
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sync_sessions as sync

TOKEN_PREFIX = "oauth:"
ACCOUNT_KEY = "lastKnownAccountUuid"
# 新版桌面端的 Cookie 在 Network 子文件夹里；老版本在根目录，两处都管
COOKIE_FILES = ["Network/Cookies", "Network/Cookies-journal", "Cookies", "Cookies-journal"]
STATE_DIRS = ["Local Storage", "Session Storage", "IndexedDB"]
# 远程控制用的临时会话编号，换账号后就失效了，每次切换都删掉让桌面端重建
BRIDGE_FILE = "bridge-state.json"
KEEP_BACKUPS = 10
BAD_LABEL_CHARS = set('\\/:*?"<>|')


class Stop(Exception):
    """要中止操作，并把原因告诉用户。"""


@dataclass
class Env:
    data_dir: Path  # 桌面端数据文件夹
    store: Path  # 本工具的保存位置
    projects: Path  # 对话内容文件夹（同步会话列表时判断用）
    sessions_backup: Path  # 同步会话列表时的备份位置
    use_app: bool  # False 时不碰真实的桌面端（测试用）
    settle: int  # 新账号登录后等多久再关桌面端（秒）
    login_timeout: int  # 等新账号登录最多多久（秒）
    yes: bool  # 不再逐项确认

    @property
    def config(self) -> Path:
        return self.data_dir / "config.json"

    @property
    def profiles(self) -> Path:
        return self.store / "profiles"

    @property
    def backups(self) -> Path:
        return self.store / "backups"


@dataclass
class Profile:
    label: str
    account: str
    saved_at: str
    path: Path


# ---------- config.json ----------

def read_config(env: Env) -> dict:
    try:
        data = json.loads(env.config.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except ValueError as error:
        raise Stop(f"桌面端的 config.json 读不懂，为安全起见不做任何改动：{error}")
    if not isinstance(data, dict):
        raise Stop("桌面端的 config.json 格式不对，为安全起见不做任何改动。")
    return data


def write_config(env: Env, data: dict) -> None:
    pretty = env.config.exists() and "\n" in env.config.read_text(encoding="utf-8").strip()
    text = json.dumps(data, ensure_ascii=False, indent=2 if pretty else None)
    atomic_write(env.config, text.encode("utf-8"))


def login_of(config: dict):
    """(账号编号, 登录令牌) ；没登录时账号编号为 None。"""
    tokens = {k: v for k, v in config.items() if k.startswith(TOKEN_PREFIX) and isinstance(v, str) and v}
    account = config.get(ACCOUNT_KEY)
    if not (isinstance(account, str) and account and tokens):
        return None, {}
    return account, tokens


# ---------- 文件小工具 ----------

def atomic_write(path: Path, data: bytes) -> None:
    """先写临时文件再改名，中途出错不会留下写了一半的文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp-switch")
    temp.write_bytes(data)
    os.replace(temp, path)


def remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def copy_item(source: Path, destination: Path) -> None:
    """把文件或文件夹拷到目标位置，目标原有的先删掉；来源不存在就只删目标。"""
    remove(destination)
    if source.is_dir():
        shutil.copytree(source, destination)
    elif source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def identity_items() -> list:
    return COOKIE_FILES + STATE_DIRS


# ---------- 已保存的账号 ----------

def load_profiles(env: Env) -> list:
    profiles = []
    if not env.profiles.is_dir():
        return profiles
    for folder in sorted(p for p in env.profiles.iterdir() if p.is_dir() and not p.name.startswith(".")):
        try:
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            tokens = json.loads((folder / "tokens.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        account = meta.get("accountUuid")
        if isinstance(account, str) and account and login_of(tokens)[0] == account:
            profiles.append(Profile(folder.name, account, str(meta.get("savedAt", "")), folder))
    return profiles


def find(profiles: list, label: str = None, account: str = None):
    for profile in profiles:
        if (label is not None and profile.label == label) or (account is not None and profile.account == account):
            return profile
    return None


def check_label(label: str) -> str:
    label = label.strip()
    if not label or label in (".", "..") or label.startswith(".") or len(label) > 30:
        raise Stop("名字不能为空、不能以点开头，最多 30 个字。")
    if any(ch in BAD_LABEL_CHARS for ch in label):
        raise Stop('名字里不能有这些符号：\\ / : * ? " < > |')
    return label


def snapshot(env: Env, label: str) -> None:
    """把桌面端当前的登录状态存成账号 label（桌面端必须已关闭）。"""
    account, tokens = login_of(read_config(env))
    if account is None:
        raise Stop("桌面端当前没有登录任何账号，没有可保存的。")
    env.profiles.mkdir(parents=True, exist_ok=True)
    staging = env.profiles / f".pending-{label}"
    remove(staging)
    staging.mkdir()
    (staging / "tokens.json").write_text(json.dumps({**tokens, ACCOUNT_KEY: account}), encoding="utf-8")
    (staging / "meta.json").write_text(json.dumps({
        "label": label, "accountUuid": account,
        "savedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }, ensure_ascii=False), encoding="utf-8")
    for item in identity_items():
        source = env.data_dir / item
        if source.exists():
            copy_item(source, staging / "desktop-state" / item)
    # 新的先就位再删旧的；中途出错时旧的还在
    final = env.profiles / label
    previous = env.profiles / f".previous-{label}"
    remove(previous)
    if final.exists():
        os.replace(final, previous)
    try:
        os.replace(staging, final)
    except OSError:
        if previous.exists():
            os.replace(previous, final)
        raise
    remove(previous)


def install(env: Env, profile: Profile) -> None:
    """把已保存账号的登录状态换到桌面端上（桌面端必须已关闭）。"""
    tokens = json.loads((profile.path / "tokens.json").read_text(encoding="utf-8"))
    config = {k: v for k, v in read_config(env).items() if not k.startswith(TOKEN_PREFIX)}
    config.update(tokens)
    write_config(env, config)
    for item in identity_items():
        copy_item(profile.path / "desktop-state" / item, env.data_dir / item)
    remove(env.data_dir / BRIDGE_FILE)


def clear_login(env: Env) -> None:
    """让桌面端下次打开时停在登录界面（不点「退出登录」，免得把已保存的登录作废）。"""
    config = {k: v for k, v in read_config(env).items()
              if not k.startswith(TOKEN_PREFIX) and k != ACCOUNT_KEY}
    write_config(env, config)
    for item in identity_items() + [BRIDGE_FILE]:
        remove(env.data_dir / item)


# ---------- 备份 ----------

def backup(env: Env) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = env.backups / stamp
    counter = 1
    while target.exists():
        counter += 1
        target = env.backups / f"{stamp}-{counter}"
    target.mkdir(parents=True)
    for item in identity_items() + ["config.json", BRIDGE_FILE]:
        source = env.data_dir / item
        if source.exists():
            copy_item(source, target / item)
    (target / ".complete").write_text("ok", encoding="utf-8")
    for old in sorted(p for p in env.backups.iterdir() if p.is_dir())[:-KEEP_BACKUPS]:
        remove(old)
    return target


def latest_backup(env: Env):
    if not env.backups.is_dir():
        return None
    done = sorted(p for p in env.backups.iterdir() if (p / ".complete").exists())
    return done[-1] if done else None


def restore_backup(env: Env, folder: Path) -> None:
    for item in identity_items() + ["config.json", BRIDGE_FILE]:
        copy_item(folder / item, env.data_dir / item)


# ---------- 桌面端 ----------

def stop_app(env: Env) -> bool:
    """关掉桌面端；返回它原来是否开着。"""
    if not env.use_app:
        return False
    pids = sync.desktop_processes()
    if pids is None:
        raise Stop("无法确认 Claude 桌面端是否在运行，没有做任何改动。")
    if not pids:
        return False
    print("正在关闭 Claude 桌面端……")
    if not sync.close_desktop(pids):
        raise Stop("没能关闭桌面端，没有做任何改动。请在右下角托盘里手动退出它，再试一次。")
    time.sleep(1)  # 等它把退出前的状态写完
    return True


def start_app(env: Env) -> None:
    if env.use_app:
        print("正在打开 Claude 桌面端……")
        if not sync.open_desktop():
            print("没找到桌面端的启动入口，请从开始菜单手动打开 Claude。")


def sync_sessions(env: Env) -> None:
    root = env.data_dir / "claude-code-sessions"
    if not root.is_dir():
        return
    folders = sync.scan_folders(root)
    actions, _ = sync.make_plan(folders, sync.scan_transcripts(env.projects), False)
    if actions:
        sync.apply_plan(actions, root, env.sessions_backup)
        print(f"已同步会话列表：复制了 {len(actions)} 条会话。")


def confirm(env: Env, message: str) -> bool:
    if env.yes:
        return True
    return sync.ask(f"{message}\n输入 Y 再按回车继续；直接按回车取消\n> ").strip().lower() == "y"


# ---------- 各项操作 ----------

def describe_current(env: Env, profiles: list) -> str:
    account, _ = login_of(read_config(env))
    if account is None:
        return "当前桌面端没有登录。"
    profile = find(profiles, account=account)
    return f"当前登录：{profile.label}" if profile else f"当前登录的账号（编号 {account[:8]}）还没保存。"


def save_outgoing(env: Env, save_current_as) -> None:
    """换下当前账号前，先把它最新的登录状态存回去（登录令牌会定期更新，旧的可能已作废）。"""
    account, _ = login_of(read_config(env))
    if account is None:
        return
    profile = find(load_profiles(env), account=account)
    if profile is not None:
        snapshot(env, profile.label)
    elif save_current_as:
        snapshot(env, save_current_as)
        print(f"已把当前账号保存为「{save_current_as}」。")
    else:
        raise Stop("当前登录的账号还没保存，换下来就丢了。请先保存它（菜单里会提示起名字）。")


def check_new_label(env: Env, label: str, profiles: list) -> str:
    label = check_label(label)
    if find(profiles, label=label):
        raise Stop(f"已经有叫「{label}」的账号了，换个名字。")
    return label


def cmd_list(env: Env) -> None:
    profiles = load_profiles(env)
    print(describe_current(env, profiles))
    if not profiles:
        print("还没有保存任何账号。")
    for profile in profiles:
        print(f"  · {profile.label}（编号 {profile.account[:8]}，{profile.saved_at} 保存）")


def cmd_save(env: Env, label: str) -> None:
    profiles = load_profiles(env)
    label = check_label(label)
    account, _ = login_of(read_config(env))
    if account is None:
        raise Stop("桌面端当前没有登录任何账号，没有可保存的。")
    same = find(profiles, account=account)
    if same and same.label != label:
        raise Stop(f"当前账号已经保存过了，叫「{same.label}」。")
    other = find(profiles, label=label)
    if other and other.account != account:
        raise Stop(f"「{label}」这个名字已经给另一个账号用了，换个名字。")
    if not confirm(env, "保存账号需要先关闭桌面端（正在进行的对话会中断），完成后自动重新打开。"):
        print("已取消，没有做任何改动。")
        return
    was_running = stop_app(env)
    try:
        snapshot(env, label)
        print(f"已把当前账号保存为「{label}」。")
    finally:
        if was_running:
            start_app(env)


def cmd_switch(env: Env, label: str, save_current_as=None) -> None:
    profiles = load_profiles(env)
    target = find(profiles, label=label)
    if target is None:
        raise Stop(f"没有叫「{label}」的已保存账号。")
    account, _ = login_of(read_config(env))
    if account == target.account:
        print(f"桌面端现在就是「{label}」，不用切换。")
        return
    if account and not find(profiles, account=account) and not save_current_as:
        raise Stop("当前登录的账号还没保存，换下来就丢了。请先保存它。")
    if not confirm(env, f"切换到「{label}」：会关闭桌面端（正在进行的对话会中断），换好后自动重新打开。"):
        print("已取消，没有做任何改动。")
        return
    stop_app(env)
    try:
        saved = backup(env)
        save_outgoing(env, save_current_as)
        sync_sessions(env)
        install(env, target)
        print(f"已切换到「{label}」。切换前的状态备份在：{saved}")
    finally:
        start_app(env)


def wait_for_login(env: Env):
    """等用户在桌面端登录；返回新账号编号，超时返回 None。按 Ctrl+C 取消。"""
    deadline = time.time() + env.login_timeout
    while time.time() < deadline:
        try:
            account, _ = login_of(read_config(env))
        except Stop:
            account = None  # 桌面端正写到一半，下一轮再读
        if account:
            return account
        time.sleep(1)
    return None


def cmd_add(env: Env, label: str, save_current_as=None) -> None:
    profiles = load_profiles(env)
    label = check_new_label(env, label, profiles)
    account, _ = login_of(read_config(env))
    if account and not find(profiles, account=account) and not save_current_as:
        raise Stop("当前登录的账号还没保存，换下来就丢了。请先保存它。")
    if not confirm(env, "添加账号：会关闭桌面端（正在进行的对话会中断），然后重新打开到登录界面，"
                        "你在里面登录新账号即可。"):
        print("已取消，没有做任何改动。")
        return
    stop_app(env)
    saved = backup(env)
    try:
        save_outgoing(env, save_current_as)
        clear_login(env)
    except BaseException:
        restore_backup(env, saved)
        start_app(env)
        raise
    start_app(env)
    print(f"\n请在打开的 Claude 窗口里登录新账号（最多等 {env.login_timeout // 60} 分钟，按 Ctrl+C 取消）。")
    print("登录完成后这里会自动继续。")
    try:
        new_account = wait_for_login(env)
    except KeyboardInterrupt:
        new_account = None
    if new_account is None:
        print("\n没等到登录，正在恢复原来的账号……")
        stop_app(env)
        restore_backup(env, saved)
        start_app(env)
        print("已恢复，没有添加账号。")
        return

    existing = find(load_profiles(env), account=new_account)
    name = existing.label if existing else label
    print(f"\n检测到登录成功。先别开始新任务：等桌面端把登录信息存好（约 {env.settle} 秒），"
          "之后会自动关闭、保存、再打开。")
    for left in range(env.settle, 0, -10):
        print(f"  还剩 {left} 秒……")
        time.sleep(min(10, left))
    stop_app(env)
    try:
        snapshot(env, name)
        sync_sessions(env)
    finally:
        start_app(env)
    if existing:
        print(f"这个账号之前保存过，叫「{name}」，已更新它的登录信息。")
    else:
        print(f"已添加账号「{name}」。")


def cmd_undo(env: Env) -> None:
    folder = latest_backup(env)
    if folder is None:
        raise Stop("没有可撤销的备份。")
    if not confirm(env, f"撤销上一次切换或添加，回到 {folder.name} 那时的登录状态（会关闭再重新打开桌面端）。"):
        print("已取消，没有做任何改动。")
        return
    stop_app(env)
    try:
        save_outgoing(env, None)  # 当前账号的最新登录先存回去，免得它的令牌作废
    except Stop:
        pass  # 当前账号没保存过，就只能放弃它
    try:
        restore_backup(env, folder)
        print("已撤销。")
    finally:
        start_app(env)


# ---------- 菜单 ----------

def ask_label(prompt: str) -> str:
    return sync.ask(prompt).strip()


def menu(env: Env) -> None:
    print("Claude 桌面端 一键切换账号\n")
    profiles = load_profiles(env)
    account, _ = login_of(read_config(env))
    current = find(profiles, account=account) if account else None
    print(describe_current(env, profiles))
    others = [p for p in profiles if current is None or p.account != current.account]
    print()
    for number, profile in enumerate(others, 1):
        print(f"  {number}. 切换到「{profile.label}」")
    print("  A. 添加一个新账号")
    if account and current is None:
        print("  S. 保存当前登录的账号")
    print("  U. 撤销上一次切换或添加")
    choice = sync.ask("\n输入编号或字母再按回车；直接按回车退出\n> ").strip().lower()
    if not choice:
        return

    # 当前账号没保存时，换下来之前先让用户起个名字存下来
    save_current_as = None
    if account and current is None and choice in [str(i) for i in range(1, len(others) + 1)] + ["a"]:
        save_current_as = check_new_label(env, ask_label(
            "当前登录的账号还没保存。先给它起个名字（比如「工作号」）：\n> "), profiles)

    if choice.isdigit() and 1 <= int(choice) <= len(others):
        cmd_switch(env, others[int(choice) - 1].label, save_current_as)
    elif choice == "a":
        label = ask_label("给新账号起个名字（比如「个人号」）：\n> ")
        if save_current_as and label.strip() == save_current_as:
            raise Stop("两个账号不能用同一个名字。")
        cmd_add(env, label, save_current_as)
    elif choice == "s" and account and current is None:
        cmd_save(env, ask_label("给当前账号起个名字（比如「工作号」）：\n> "))
    elif choice == "u":
        cmd_undo(env)
    else:
        print("没有这个选项。")


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Claude 桌面端一键切换账号")
    parser.add_argument("command", nargs="?", default="menu",
                        choices=["menu", "list", "save", "switch", "add", "undo"])
    parser.add_argument("label", nargs="?", default=None, help="账号名字")
    parser.add_argument("--yes", action="store_true", help="不再逐项确认")
    # 下面几项只在测试时用
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--store", type=Path, default=None)
    parser.add_argument("--projects", type=Path, default=None)
    parser.add_argument("--sessions-backup", type=Path, default=here / "backups")
    parser.add_argument("--no-app", action="store_true")
    parser.add_argument("--settle", type=int, default=60)
    parser.add_argument("--login-timeout", type=int, default=600)
    args = parser.parse_args()

    env = Env(
        data_dir=args.data_dir or Path(os.environ["APPDATA"]) / "Claude",
        store=args.store or Path(os.environ["LOCALAPPDATA"]) / "claude-switch",
        projects=args.projects or sync.default_projects(),
        sessions_backup=args.sessions_backup,
        use_app=not args.no_app,
        settle=args.settle,
        login_timeout=args.login_timeout,
        yes=args.yes,
    )
    interactive = args.command == "menu"
    try:
        if not env.data_dir.is_dir():
            raise Stop("没找到 Claude 桌面端的数据文件夹，这台电脑上可能没装桌面端。")
        if args.command in ("save", "switch", "add") and not args.label:
            raise Stop(f"请在 {args.command} 后面写上账号名字。")
        {
            "menu": lambda: menu(env),
            "list": lambda: cmd_list(env),
            "save": lambda: cmd_save(env, args.label),
            "switch": lambda: cmd_switch(env, args.label),
            "add": lambda: cmd_add(env, args.label),
            "undo": lambda: cmd_undo(env),
        }[args.command]()
        code = 0
    except Stop as reason:
        print(f"\n{reason}")
        code = 1
    except KeyboardInterrupt:
        print("\n已取消。")
        code = 1
    except Exception as error:  # 至少让人看到出了什么错
        print(f"\n出错了，操作没有完成：{error!r}")
        print("如果桌面端登录状态不对，可以再打开这个工具选 U 撤销。")
        code = 1
    if interactive:
        sync.ask("\n按回车关闭这个窗口……")
    return code


if __name__ == "__main__":
    sys.exit(main())
