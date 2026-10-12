"""Workdeck — Claude Code·Codex 작업 상태와 할 일을 띄워두는 윈도우 플로팅 창 (파이썬 파일 하나, 표준 라이브러리만)

python workdeck.py                          플로팅 창 띄우기
python workdeck.py install [옵션]           훅 등록 + 바로가기 만들기 (PC마다 한 번)
    --no-startup                            컴퓨터 켤 때 자동 실행 안 함
    --sync-dir <폴더>                       할 일을 이 폴더(외장 SSD·클라우드 폴더 등)에 저장해서 여러 PC가 같이 씀
python workdeck.py uninstall                훅·바로가기·설치본 지우기 (할 일 데이터는 남김)
python workdeck.py hook                     Claude Code 훅 (직접 실행할 일 없음)
"""
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

APP_NAME = "Workdeck"
HOME = Path.home()
DIR = HOME / ".claude" / "session-status"          # 세션 상태·창 위치·설정 (이 PC 전용)
HOME_COPY = HOME / ".claude" / "hooks" / "workdeck.py"  # 설치본. 훅과 바로가기는 이 파일을 실행
ICON = HOME_COPY.with_name("workdeck.ico")
LEGACY_NAMES = ("claude_board.py",)                # 예전 이름으로 설치된 것도 정리
SETTINGS = HOME / ".claude" / "settings.json"
CONFIG = DIR / "_config.json"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CODEX = HOME / ".codex"
KEEP = 3 * 24 * 3600   # 3일 동안 아무 움직임 없는 대화만 목록에서 빠짐 (완료·숨김 포함)
SAFE_ID = re.compile(r"[0-9A-Za-z_-]{1,80}")

# 이벤트 → 상태. work=작업중, review=검토 필요, answer=답변 필요
EVENT_STATE = {
    "UserPromptSubmit": "work",
    "PostToolUse": "work",          # 권한 승인·질문 답변 뒤 다시 작업중으로
    "PreToolUse": "answer",         # matcher가 AskUserQuestion 이라 질문할 때만 옴
    "PermissionRequest": "answer",
    "Stop": "review",
}
HOOK_EVENTS = {"UserPromptSubmit": "", "PostToolUse": "*", "PreToolUse": "AskUserQuestion",
               "PermissionRequest": "*", "Notification": "", "Stop": "", "SessionEnd": ""}


def write_json(path, data, indent=None):
    """임시 파일에 다 쓴 다음 바꿔치기 → 중간에 꺼져도 파일이 반쯤 깨지지 않음"""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=indent), "utf-8")
    os.replace(tmp, path)


def read_json(path, default=None):
    """깨졌거나 모양이 이상한 파일은 기본값으로 (남의 PC가 쓴 파일도 믿지 않음)"""
    default = {} if default is None else default
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, type(default)) else default
    except Exception:
        return default


def num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def first_line(text, n=60):
    lines = str(text or "").strip().splitlines()
    return lines[0][:n] if lines else ""


def read_tail(path, size=2_000_000):
    """큰 대화기록도 끝부분만 읽음"""
    with open(path, "rb") as fp:
        fp.seek(0, 2)
        fp.seek(max(0, fp.tell() - size))
        return fp.read().decode("utf-8", "ignore").splitlines()


def expired(s, now):
    return now - num(s.get("updated", s.get("since")), now) > KEEP


# ---------------------------------------------------------------- 훅 (Claude Code가 실행)
def hook():
    """어떤 경우에도 Claude Code를 방해하지 않게: 에러는 삼키고, 아무것도 출력하지 않음"""
    try:
        _hook(json.loads(sys.stdin.buffer.read().decode("utf-8", "ignore") or "{}"))
    except Exception:
        pass


def _hook(d):
    if not isinstance(d, dict):
        return
    sid, ev = d.get("session_id"), d.get("hook_event_name", "")
    if not isinstance(sid, str) or not SAFE_ID.fullmatch(sid):  # 파일 이름으로 쓰니까 안전한 글자만
        return
    DIR.mkdir(parents=True, exist_ok=True)
    f = DIR / f"{sid}.json"
    if ev == "SessionEnd":
        f.unlink(missing_ok=True)
        return

    state = EVENT_STATE.get(ev)
    if ev == "Notification" and "permission" in str(d.get("message") or "").lower():
        state = "answer"
    if not state:
        return

    cur = read_json(f)
    changed = cur.get("state") != state
    if not changed and ev != "UserPromptSubmit":
        return  # 도구 쓸 때마다 파일 쓰지 않기

    # 상태가 바뀌면 완료·숨김은 풀림 (새 일이 생긴 거니까)
    new = {**cur, "state": state, "cwd": str(d.get("cwd") or cur.get("cwd", "")),
           "updated": time.time(), "seen": False, "hidden": False, "since": time.time()}
    if ev == "UserPromptSubmit":
        new["last"] = first_line(d.get("prompt"))
    write_json(f, new)


# ---------------------------------------------------------------- 설치 / 제거
def is_ours(cmd):
    return " hook" in cmd and any(n in cmd for n in (HOME_COPY.name, *LEGACY_NAMES))


def load_settings():
    """settings.json을 엄격하게 읽음. 못 읽으면 None (그럴 땐 절대 덮어쓰지 않음)"""
    if not SETTINGS.exists():
        return {}
    try:
        data = json.loads(SETTINGS.read_text("utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def without_our_hooks(data):
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return {}
    clean = {}
    for ev, entries in hooks.items():
        if not isinstance(entries, list):
            clean[ev] = entries
            continue
        kept = [m for m in entries if not (isinstance(m, dict) and any(
            isinstance(h, dict) and is_ours(str(h.get("command", ""))) for h in m.get("hooks", []) or []))]
        if kept:
            clean[ev] = kept
    return clean


def save_settings(data):
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():  # 설치할 때마다 새 이름으로 백업 (이전 백업을 덮어쓰지 않음)
        shutil.copy2(SETTINGS, SETTINGS.with_name(f"settings.json.bak-workdeck-{datetime.now():%Y%m%d%H%M%S}"))
    write_json(SETTINGS, data, indent=2)


def shortcut_paths():
    programs = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs"
    return programs / "Workdeck.lnk", programs / "Startup" / "Workdeck.lnk"


def run_ps(script, **env):
    """경로는 환경변수로 넘김 → 사용자 이름에 ' 같은 글자가 있어도 명령이 깨지거나 끼어들 수 없음"""
    import subprocess
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                   env={**os.environ, **{k: str(v) for k, v in env.items()}}, check=True)


def install(args):
    startup = "--no-startup" not in args
    sync_dir = args[args.index("--sync-dir") + 1] if "--sync-dir" in args[:-1] else None

    data = load_settings()
    if data is None:
        sys.exit(f"설치 중단: {SETTINGS} 파일을 읽을 수 없어요 (주석이나 문법 오류).\n"
                 "이 파일을 고친 뒤 다시 실행해 주세요. 아무것도 바꾸지 않았어요.")

    # 1) 설치본 복사 (훅·바로가기는 이 복사본을 실행)
    HOME_COPY.parent.mkdir(parents=True, exist_ok=True)
    src = Path(__file__).resolve()
    if src != HOME_COPY.resolve():
        shutil.copy2(src, HOME_COPY)
    if src.with_name("workdeck.ico").exists() and src.with_name("workdeck.ico") != ICON.resolve():
        shutil.copy2(src.with_name("workdeck.ico"), ICON)
    for old in LEGACY_NAMES:
        HOME_COPY.with_name(old).unlink(missing_ok=True)

    # 2) Claude Code 훅 등록 (예전에 넣은 우리 훅만 빼고 다시 넣음, 다른 훅은 그대로)
    py = Path(sys.executable)
    cmd = f'"{py.as_posix()}" "{HOME_COPY.as_posix()}" hook'
    hooks = without_our_hooks(data)
    for ev, matcher in HOOK_EVENTS.items():
        entry = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
        if matcher:
            entry["matcher"] = matcher
        hooks.setdefault(ev, []).append(entry)
    save_settings({**data, "hooks": hooks})

    # 3) 설정: 할 일 동기화 폴더 (드라이브 글자는 빼고 저장 → 외장 SSD 글자가 바뀌어도 찾음)
    DIR.mkdir(parents=True, exist_ok=True)
    config = read_json(CONFIG)
    if sync_dir:
        p = Path(sync_dir).resolve()
        config["sync_rel"] = str(p.relative_to(p.anchor))
    write_json(CONFIG, config)

    # 4) 바로가기: 시작 메뉴(+ 원하면 시작프로그램) + 바탕화면
    pyw = py.with_name("pythonw.exe")
    if not pyw.exists():
        pyw = py
    menu, auto = shortcut_paths()
    targets = [menu] + ([auto] if startup else [])
    if not startup:
        auto.unlink(missing_ok=True)
    script = ("$w = New-Object -ComObject WScript.Shell; "
              "$list = @($env:WD_LNKS -split '\\|') + ([Environment]::GetFolderPath('Desktop') + '\\Workdeck.lnk'); "
              "foreach ($p in $list) { $s = $w.CreateShortcut($p); $s.TargetPath = $env:WD_PYW; "
              "$s.Arguments = '\"' + $env:WD_APP + '\"'; $s.IconLocation = $env:WD_ICON; "
              "$s.Description = 'Workdeck'; $s.Save() }")
    run_ps(script, WD_LNKS="|".join(map(str, targets)), WD_PYW=pyw, WD_APP=HOME_COPY, WD_ICON=ICON)

    print("설치 완료")
    print(f"  - Claude Code 훅 등록 (설정 백업: {SETTINGS.parent}\\settings.json.bak-workdeck-*)")
    print("  - 바로가기: 바탕화면, 시작 메뉴" + (", 시작프로그램(컴퓨터 켜면 자동 실행)" if startup else ""))
    if sync_dir:
        print(f"  - 할 일 저장 폴더: {sync_dir}")
    print("작업표시줄에 두려면 바탕화면의 Workdeck 아이콘을 우클릭 → '작업 표시줄에 고정'")
    print("이미 열려 있던 Claude Code 대화는 새로 열어야 상태가 잡혀요.")
    import subprocess
    subprocess.Popen([str(pyw), str(HOME_COPY)], cwd=str(HOME_COPY.parent))  # 바로 띄우기


def uninstall(args):
    data = load_settings()
    if data is None:
        print(f"주의: {SETTINGS} 를 읽을 수 없어 훅은 지우지 못했어요. 직접 열어서 workdeck.py 줄을 지워 주세요.")
    elif data.get("hooks"):
        save_settings({**data, "hooks": without_our_hooks(data)})
    menu, auto = shortcut_paths()
    for p in (menu, auto):
        p.unlink(missing_ok=True)
    run_ps("Remove-Item -LiteralPath ([Environment]::GetFolderPath('Desktop') + '\\Workdeck.lnk') "
           "-ErrorAction SilentlyContinue")
    for p in (HOME_COPY, ICON, *(HOME_COPY.with_name(n) for n in LEGACY_NAMES)):
        p.unlink(missing_ok=True)
    print("제거 완료: 훅, 바로가기, 설치본을 지웠어요.")
    print(f"할 일·창 위치 데이터는 남겨뒀어요. 완전히 지우려면 이 폴더를 삭제하세요: {DIR}")
    print("작업표시줄에 고정해둔 아이콘은 우클릭 → '작업 표시줄에서 제거'로 직접 빼 주세요.")


# ---------------------------------------------------------------- 세션 읽기
_cache = {}  # 파일 → (mtime, 읽은 결과). 바뀐 파일만 다시 읽기


def cached(f, fn):
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return None
    if _cache.get(f, (None,))[0] != mtime:
        try:
            _cache[f] = (mtime, fn(f))
        except Exception:
            _cache[f] = (mtime, None)
    return _cache[f][1]


def claude_transcript(f):
    """대화기록 끝부분에서 (앱 사이드바 제목, 마지막으로 보낸 메시지 첫 줄). 내용은 읽지 않음"""
    title = last = ""
    for line in reversed(read_tail(f)):
        try:
            if not title and '"type":"custom-title"' in line:
                title = str(json.loads(line).get("customTitle", ""))
            elif not last and '"type":"last-prompt"' in line:
                last = str(json.loads(line).get("lastPrompt", ""))
        except Exception:
            continue
        if title and last:
            break
    return title, first_line(last)


def claude_sessions(now):
    out = []
    for f in DIR.glob("*.json"):
        if f.name.startswith("_") or not SAFE_ID.fullmatch(f.stem):
            continue
        s = read_json(f)
        if s.get("state") not in EVENT_STATE.values() or expired(s, now):
            continue
        tr = next(CLAUDE_PROJECTS.glob(f"*/{f.stem}.jsonl"), None)
        title, last = (cached(tr, claude_transcript) if tr else None) or ("", "")
        out.append({**s, "src": "Claude", "file": f, "since": num(s.get("since"), now),
                    "title": title or str(s.get("title", "")), "last": last or str(s.get("last", "")),
                    "seen": bool(s.get("seen")), "hidden": bool(s.get("hidden"))})
    return out


# Codex는 훅 대신 대화기록(~/.codex/sessions/**/rollout-*.jsonl)을 읽어서 상태를 판단.
# Codex의 notify 설정은 다른 기능(컴퓨터 사용 등)이 쓰고 있을 수 있어서 건드리지 않음.
def codex_state(f):
    """rollout 파일 끝부분에서 마지막 작업 이벤트 → (state, since, cwd)"""
    with open(f, "rb") as fp:
        cwd = str(json.loads(fp.readline()).get("payload", {}).get("cwd", ""))
    for line in reversed(read_tail(f, 300_000)):
        if '"type":"event_msg"' not in line:
            continue
        try:
            d = json.loads(line)
            kind = str(d.get("payload", {}).get("type", ""))
            state = ("work" if kind == "task_started" else "review" if kind in ("task_complete", "turn_aborted")
                     else "answer" if "approval" in kind or "request_user_input" in kind else None)
            if state:
                since = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                return state, since, cwd
        except Exception:
            continue
    return None


def codex_titles(f):
    titles = {}
    for line in f.read_text("utf-8").splitlines():
        try:
            d = json.loads(line)
            titles[str(d["id"])] = str(d.get("thread_name", ""))
        except Exception:
            continue
    return titles


def codex_sessions(now):
    out = []
    titles = cached(CODEX / "session_index.jsonl", codex_titles) or {}
    for f in (CODEX / "sessions").glob("*/*/*/rollout-*.jsonl"):
        try:
            mtime = f.stat().st_mtime
        except OSError:
            continue
        if now - mtime > KEEP:
            continue
        info = cached(f, codex_state)
        if not info:
            continue
        state, since, cwd = info
        sid = f.stem[-36:]
        if not SAFE_ID.fullmatch(sid):
            continue
        mark = DIR / f"_codex-{sid}.json"  # 대화기록은 못 고치니 완료·숨김은 따로 저장
        m = read_json(mark)
        if m.get("gone") == since:  # 목록에서 지운 것 (새 작업이 시작되면 다시 나타남)
            continue
        s = {"src": "GPT", "file": mark, "state": state, "since": since, "cwd": cwd,
             "title": titles.get(sid, ""), "last": "", "updated": mtime,
             "seen": m.get("seen") == since, "hidden": m.get("hidden") == since}
        if not expired(s, now):
            out.append(s)
    return out


ORDER = {"answer": 0, "review": 1, "work": 2, "seen": 3}


def load_sessions():
    now = time.time()
    out = claude_sessions(now) + codex_sessions(now) + manual_items()
    for s in out:
        s["view"] = "seen" if s.get("seen") else s["state"]  # 완료로 눌러둔 건 맨 아래로
    return sorted(out, key=lambda s: (ORDER[s["view"]], -s["since"]))


# ---------------------------------------------------------------- 저장 위치 (할 일·직접 추가)
# 기본은 이 PC. install --sync-dir 로 폴더를 정해두면 그 폴더에 저장 → 여러 PC가 같은 목록을 씀.
# 드라이브 글자는 빼고 기억해서, 외장 SSD 글자가 E:→F:로 바뀌어도 찾아감.
_data_dir = {"at": 0.0, "dir": None}


def data_dir():
    """동기화 폴더 (없으면 None). 드라이브 검색은 30초에 한 번만"""
    if time.time() - _data_dir["at"] < 30:
        return _data_dir["dir"]
    found = None
    rel = read_json(CONFIG).get("sync_rel")
    if isinstance(rel, str) and rel and not Path(rel).is_absolute() and ".." not in Path(rel).parts:
        for drive in os.listdrives():
            try:
                if (Path(drive) / rel).is_dir():
                    found = Path(drive) / rel
                    break
            except OSError:
                pass
    _data_dir.update(at=time.time(), dir=found)
    return found


def todo_file():
    d = data_dir()
    return d / "할일.json" if d else DIR / "_todo.json"


def manual_file():
    d = data_dir()
    return d / "직접추가.json" if d else DIR / "_manual.json"


# ---------------------------------------------------------------- 직접 추가한 작업
# GPT 일반 채팅처럼 자동으로 못 잡는 일
def manual_items():
    out = []
    for m in read_json(manual_file()).get("items", []):
        if isinstance(m, dict) and isinstance(m.get("id"), str) and m.get("state") in ORDER:
            out.append({**m, "src": "직접", "file": manual_file(), "cwd": "", "last": "",
                        "title": str(m.get("title", "")), "since": num(m.get("since")),
                        "seen": bool(m.get("seen")), "hidden": bool(m.get("hidden"))})
    return out


def manual_update(s, fn):
    """직접 추가 목록에서 s 항목을 fn(항목) 결과로 바꿈. 결과가 None이면 삭제"""
    f = manual_file()
    items = [m for m in read_json(f).get("items", []) if isinstance(m, dict)]
    items = [x for x in (fn(m) if m.get("id") == s["id"] else m for m in items) if x is not None]
    write_json(f, {"items": items})


def manual_add(title):
    f = manual_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    items = [m for m in read_json(f).get("items", []) if isinstance(m, dict)]
    items.append({"id": f"m{time.time_ns()}", "title": title, "state": "work", "since": time.time(),
                  "seen": False, "hidden": False})
    write_json(f, {"items": items})


def manual_next_state(s):
    """직접 추가 항목은 알약을 누를 때마다 작업중 → 검토 필요 → 완료 → 작업중"""
    if s.get("seen"):
        new = {"state": "work", "seen": False}
    elif s["state"] == "work":
        new = {"state": "review", "seen": False}
    else:
        new = {"seen": True}
    manual_update(s, lambda m: {**m, **new, "since": time.time()})


def remove_session(s):
    """숨긴 항목에서 완전히 지우기. 그 대화에서 새 작업이 시작되면 다시 나타남"""
    if s["src"] == "직접":
        manual_update(s, lambda m: None)
    elif s["src"] == "GPT":
        write_json(s["file"], {**read_json(s["file"]), "gone": s["since"]})
    else:
        s["file"].unlink(missing_ok=True)


def set_flag(s, key, on):
    """완료(seen)·숨김(hidden) 켜고 끄기"""
    if s["src"] == "직접":
        manual_update(s, lambda m: {**m, key: on})
    elif s["src"] == "GPT":
        write_json(s["file"], {**read_json(s["file"]), key: s["since"] if on else None})
    else:
        write_json(s["file"], {**read_json(s["file"]), key: on})


# ---------------------------------------------------------------- 할 일
TODO_SEED = [{"name": "예시 그룹", "open": True,
              "items": [{"text": "체크해서 완료하기", "done": False},
                        {"text": "✎로 고치고 ✕로 지우기", "done": False},
                        {"text": "⠿ 잡고 끌어서 순서 바꾸기", "done": False}]}]


def clean_groups(groups):
    """남의 PC·손으로 고친 파일이어도 그릴 수 있는 모양으로 정리"""
    out = []
    for g in groups if isinstance(groups, list) else []:
        if not isinstance(g, dict):
            continue
        items = [{"text": str(i.get("text", "")), "done": bool(i.get("done"))}
                 for i in g.get("items", []) if isinstance(i, dict)] if isinstance(g.get("items"), list) else []
        out.append({"name": str(g.get("name", "그룹")), "open": bool(g.get("open")), "items": items})
    return out


def load_todo():
    f = todo_file()
    if f.exists():
        return clean_groups(read_json(f).get("groups", []))
    return clean_groups(TODO_SEED)


def save_todo(groups):
    f = todo_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    write_json(f, {"groups": groups}, indent=1)


# ---------------------------------------------------------------- 플로팅 창
# 색: 바탕 + 먹색, 회색 단계로 구분, 포인트 파랑은 하나만. 다크는 같은 규칙을 뒤집은 것
THEMES = {
    "light": {"INK": "#141414", "MUTED": "#707070", "FAINT": "#ADADAD", "CANVAS": "#FFFFFF", "SOFT": "#F3F3F3",
              "HAIR": "#E0E0E0", "ACCENT": "#0066FF", "RING": "#C4C4C4", "GRIP": "#CFCFCF"},
    "dark": {"INK": "#F2F2F3", "MUTED": "#A1A1A6", "FAINT": "#6C6C72", "CANVAS": "#1E1E20", "SOFT": "#2B2B2E",
             "HAIR": "#3A3A3E", "ACCENT": "#3D8BFF", "RING": "#5A5A60", "GRIP": "#4A4A50"},
}
WIDTH = 360


def apply_theme(name):
    """색 이름(INK, CANVAS …)을 고른 테마 값으로 바꿈. 창을 만들기 전에 부름"""
    global INK, MUTED, FAINT, CANVAS, SOFT, HAIR, ACCENT, RING, GRIP, STATUS
    t = THEMES.get(name, THEMES["light"])
    INK, MUTED, FAINT, CANVAS, SOFT = t["INK"], t["MUTED"], t["FAINT"], t["CANVAS"], t["SOFT"]
    HAIR, ACCENT, RING, GRIP = t["HAIR"], t["ACCENT"], t["RING"], t["GRIP"]
    STATUS = {  # 상태: (글자, 알약 배경, 알약 글자색)
        "answer": ("답변 필요", INK, CANVAS),
        "review": ("검토 필요", ACCENT, "#FFFFFF"),
        "work": ("작업중", SOFT, MUTED),
        "seen": ("완료 ✓", HAIR, MUTED),
    }


apply_theme("light")


# 알림 소리: 윈도우에 기본으로 들어 있는 소리 중 부드러운 것만 (C:\Windows\Media)
SOUNDS = {
    "또롱": "Windows Notify Calendar.wav",
    "톡": "Windows Notify Messaging.wav",
    "띠링": "Windows Notify Email.wav",
    "뾰롱": "Speech On.wav",
    "똑": "Windows Information Bar.wav",
    "차임": "chimes.wav",
    "기본 띵": "Windows Background.wav",  # 윈도우 기본 알림음
}
DEFAULT_SOUND = "또롱"
DEFAULT_VOLUME = 70


def sound_settings():
    """(소리 이름, 크기 0~100, 끔 여부) — 이상한 값은 기본값으로"""
    cfg = read_json(CONFIG)
    key = cfg.get("sound") if cfg.get("sound") in SOUNDS else DEFAULT_SOUND
    vol = int(min(100, max(0, num(cfg.get("volume"), DEFAULT_VOLUME))))
    return key, vol, bool(cfg.get("mute"))


def sound_file(key, volume):
    """크기를 줄인 소리는 한 번 만들어서 저장해두고 재사용 (윈도우 기본 소리는 16비트라 숫자만 줄이면 됨)"""
    import array
    import wave
    src = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Media" / SOUNDS[key]
    if volume >= 100:
        return src
    out = DIR / f"_sound-{list(SOUNDS).index(key)}-{volume}.wav"
    if not out.exists():
        with wave.open(str(src)) as w:
            params, frames = w.getparams(), w.readframes(w.getnframes())
        if params.sampwidth != 2:
            return src
        samples = array.array("h", frames)
        scale = (volume / 100) ** 2  # 귀로 듣기에 고르게 줄어들도록 제곱
        samples = array.array("h", (int(s * scale) for s in samples))
        DIR.mkdir(parents=True, exist_ok=True)
        with wave.open(str(out), "wb") as w:
            w.setparams(params)
            w.writeframes(samples.tobytes())
    return out


def play_sound(key=None, volume=None):
    import winsound
    cur_key, cur_vol, _ = sound_settings()
    key = key if key in SOUNDS else cur_key
    volume = cur_vol if volume is None else volume
    try:
        if volume > 0:
            winsound.PlaySound(str(sound_file(key, volume)), winsound.SND_FILENAME | winsound.SND_ASYNC)
    except Exception:  # 소리 파일이 없는 PC면 윈도우 기본음
        winsound.MessageBeep(winsound.MB_ICONASTERISK)


def ago(sec):
    """분 단위로만 보여줌 (초 단위면 매초 다시 그려야 해서 깜빡임)"""
    m = int(sec) // 60
    if m < 1:
        return "방금"
    if m < 60:
        return f"{m}분"
    return f"{m // 60}시간 {m % 60}분"


def round_window(root):
    """윈도우 11 둥근 모서리 + 작업표시줄 아이콘 (안 되는 환경이면 그냥 각진 창)"""
    try:
        import ctypes
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        dwm = ctypes.windll.dwmapi.DwmSetWindowAttribute
        dwm(hwnd, 33, ctypes.byref(ctypes.c_int(2)), 4)          # DWMWA_WINDOW_CORNER_PREFERENCE = 둥글게
        r, g, b = (int(HAIR[i:i + 2], 16) for i in (1, 3, 5))
        dwm(hwnd, 34, ctypes.byref(ctypes.c_int(r | g << 8 | b << 16)), 4)  # DWMWA_BORDER_COLOR = HAIR
        # 제목줄 없는 창도 작업표시줄에 아이콘이 뜨게 (WS_EX_APPWINDOW)
        u = ctypes.windll.user32
        ex = u.GetWindowLongW(hwnd, -20)
        u.SetWindowLongW(hwnd, -20, (ex & ~0x80) | 0x40000)
        root.withdraw()
        root.after(10, root.deiconify)
        if ICON.exists():
            root.iconbitmap(default=str(ICON))
    except Exception:
        pass


def already_running():
    """하나만 켜지게: 이미 떠 있으면 True (작업표시줄 아이콘을 또 눌러도 창이 두 개 안 생김)"""
    import ctypes
    k = ctypes.windll.kernel32
    already_running.handle = k.CreateMutexW(None, False, "Local\\Workdeck-single-instance")
    return k.GetLastError() == 183  # ERROR_ALREADY_EXISTS


def board():
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import messagebox

    if already_running():
        return
    apply_theme(read_json(CONFIG).get("theme", "light"))  # 아래 부품들이 이 색으로 만들어짐
    DIR.mkdir(parents=True, exist_ok=True)
    pos_file = DIR / "_pos.json"
    root = tk.Tk()
    root.title(APP_NAME)
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    root.configure(bg=CANVAS)
    families = set(tkfont.families())
    FONT = next((f for f in ("Pretendard", "Pretendard Variable", "Malgun Gothic") if f in families), "Segoe UI")
    pos = read_json(pos_file)
    x = int(num(pos.get("x"), root.winfo_screenwidth() - WIDTH - 24))
    y = int(num(pos.get("y"), 80))
    ui = {"collapsed": False, "alert": set(), "show_hidden": False, "sig": None, "size": None}

    # ---------------- 공통 부품: 알약 버튼, 동그란 체크
    def pill(parent, text, bg, fg, fn=None, size=8, bold=True):
        f = tkfont.Font(family=FONT, size=size, weight="bold" if bold else "normal")
        h = f.metrics("linespace") + 6
        w = f.measure(text) + h
        c = tk.Canvas(parent, width=w, height=h, bg=parent["bg"], highlightthickness=0)
        c.create_oval(0, 0, h, h, fill=bg, outline="")
        c.create_oval(w - h, 0, w, h, fill=bg, outline="")
        c.create_rectangle(h / 2, 0, w - h / 2, h, fill=bg, outline="")
        t = c.create_text(w / 2, h / 2, text=text, fill=fg, font=f)
        if fn:
            c.config(cursor="hand2")
            c.bind("<Button-1>", lambda e: fn())
            c.bind("<Enter>", lambda e: c.itemconfig(t, fill=INK))
            c.bind("<Leave>", lambda e: c.itemconfig(t, fill=fg))
        return c

    def check(parent, on, fn=None):
        s = 18
        c = tk.Canvas(parent, width=s, height=s, bg=parent["bg"], highlightthickness=0)
        if on:
            c.create_oval(1, 1, s - 1, s - 1, fill=INK, outline=INK)
            c.create_line(5, 9, 8, 12, 13, 6, fill=CANVAS, width=2, capstyle="round", joinstyle="round")
        else:
            c.create_oval(1, 1, s - 1, s - 1, outline=HAIR if fn is None else RING, width=1.5)
        if fn:
            c.config(cursor="hand2")
            c.bind("<Button-1>", lambda e: fn())
        return c

    def text_btn(parent, text, fn, fg=FAINT, size=10, bold=False):
        b = tk.Label(parent, text=text, bg=parent["bg"], fg=fg, cursor="hand2",
                     font=(FONT, size, "bold" if bold else "normal"))
        b.bind("<Enter>", lambda e: b.config(fg=INK))
        b.bind("<Leave>", lambda e: b.config(fg=fg))
        b.bind("<Button-1>", lambda e: fn())
        return b

    # ---------------- 머리줄: 이름 + 상태 요약 (접어도 이 줄은 남음)
    head = tk.Frame(root, bg=CANVAS, padx=16, pady=12)
    head.pack(fill="x")
    name = tk.Label(head, text=APP_NAME, bg=CANVAS, fg=INK, font=(FONT, 11, "bold"))
    name.pack(side="left")
    dot = tk.Label(head, text="●", bg=CANVAS, fg=CANVAS, font=(FONT, 7))
    dot.pack(side="left", padx=(10, 3))
    summary = tk.Label(head, text="", bg=CANVAS, fg=MUTED, font=(FONT, 9))
    summary.pack(side="left")
    text_btn(head, "✕", root.destroy).pack(side="right")
    fold_btn = text_btn(head, "—", lambda: toggle())
    fold_btn.pack(side="right", padx=(0, 10))

    gear = text_btn(head, "⚙", lambda: open_settings(not ui["settings"]), size=10)
    gear.pack(side="right", padx=(0, 10))

    settings = tk.Frame(root, bg=CANVAS, padx=16)  # ⚙ 누르면 머리줄 바로 아래 열리는 설정판
    body = tk.Frame(root, bg=CANVAS)
    addbar = tk.Frame(root, bg=CANVAS, padx=16)  # 직접 추가 줄 (세션 목록과 따로: 입력 중에 안 지워지게)
    # 할 일은 스크롤 상자 안에: 길어져도 창이 화면 밖으로 안 나가고 마우스 휠로 내려봄
    todo_box = tk.Canvas(root, bg=CANVAS, highlightthickness=0, width=WIDTH, height=1, yscrollincrement=20)
    todo = tk.Frame(todo_box, bg=CANVAS)
    todo_box.create_window(0, 0, window=todo, anchor="nw", width=WIDTH)
    root.bind_all("<MouseWheel>", lambda e: todo_box.yview_scroll(int(-e.delta / 120), "units")
                  if ui.get("scroll") else None)
    parts_below_head = (body, addbar, todo_box)
    for p in parts_below_head:
        p.pack(fill="x")
    ui["settings"] = False
    cfg0 = read_json(CONFIG)
    ui["reopen"] = bool(cfg0.pop("_reopen_settings", False))  # 테마 바꾸고 다시 켜졌으면 설정판을 다시 열어줌
    if ui["reopen"]:
        write_json(CONFIG, cfg0)

    def toggle(collapsed=None):
        ui["collapsed"] = not ui["collapsed"] if collapsed is None else collapsed
        for p in (settings, *parts_below_head):
            p.pack_forget()
        if not ui["collapsed"]:
            if ui["settings"]:
                settings.pack(fill="x")
            for p in parts_below_head:
                p.pack(fill="x")
        fold_btn.config(text="＋" if ui["collapsed"] else "—")
        fit()

    # ---------------- 설정판: 알림 소리 · 소리 크기 · 소리 끄기 (이 PC 설정에 기억)
    def open_settings(on):
        ui["settings"] = on
        gear.config(fg=INK if on else FAINT)
        if on:
            draw_settings()
        toggle(False)  # 접혀 있었으면 펼치면서 보여줌

    def save_cfg(**kw):
        write_json(CONFIG, {**read_json(CONFIG), **kw})
        draw_settings()

    def slider(parent, value, on_change, on_done, width=200):
        """둥근 막대 슬라이더. 끄는 동안 on_change, 놓으면 on_done (5 단위)"""
        h, r = 22, 7
        c = tk.Canvas(parent, width=width, height=h, bg=parent["bg"], highlightthickness=0, cursor="hand2")

        def draw(v):
            c.delete("all")
            x = r + (width - 2 * r) * v / 100
            c.create_line(r, h / 2, width - r, h / 2, fill=HAIR, width=4, capstyle="round")
            c.create_line(r, h / 2, x, h / 2, fill=INK, width=4, capstyle="round")
            c.create_oval(x - r, h / 2 - r, x + r, h / 2 + r, fill=CANVAS, outline=INK, width=2)

        def value_at(e):
            return int(round(min(100, max(0, (e.x - r) / (width - 2 * r) * 100)) / 5) * 5)

        def move(e):
            ui["vol"] = value_at(e)
            draw(ui["vol"])
            on_change(ui["vol"])

        c.bind("<Button-1>", move)
        c.bind("<B1-Motion>", move)
        c.bind("<ButtonRelease-1>", lambda e: on_done(value_at(e)))
        draw(value)
        return c

    def draw_settings():
        for w in settings.winfo_children():
            w.destroy()
        key, vol, mute = sound_settings()
        box = tk.Frame(settings, bg=SOFT, padx=14, pady=12)
        box.pack(fill="x", pady=(0, 10))

        top = tk.Frame(box, bg=SOFT)
        top.pack(fill="x")
        tk.Label(top, text="설정", bg=SOFT, fg=INK, font=(FONT, 10, "bold")).pack(side="left")
        pill(top, "닫기", CANVAS, MUTED, lambda: open_settings(False), bold=False).pack(side="right")

        tk.Label(box, text="알림 소리  (누르면 미리 들려줘요)", bg=SOFT, fg=MUTED, font=(FONT, 8),
                 anchor="w").pack(fill="x", pady=(10, 4))
        grid = tk.Frame(box, bg=SOFT)
        grid.pack(fill="x")
        for i, k in enumerate(SOUNDS):
            on = k == key and not mute
            pill(grid, k, INK if on else CANVAS, CANVAS if on else MUTED,
                 lambda k=k: (save_cfg(sound=k, mute=False), play_sound(k)), bold=on
                 ).grid(row=i // 4, column=i % 4, sticky="w", padx=(0, 6), pady=3)

        tk.Label(box, text="소리 크기", bg=SOFT, fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x", pady=(10, 2))
        vr = tk.Frame(box, bg=SOFT)
        vr.pack(fill="x")
        pct = tk.Label(vr, text=f"{vol}%", bg=SOFT, fg=INK, font=(FONT, 9, "bold"), width=5, anchor="e")
        slider(vr, vol, lambda v: pct.config(text=f"{v}%"),
               lambda v: (save_cfg(volume=v, mute=False), play_sound(volume=v))).pack(side="left")
        pct.pack(side="right")

        mr = tk.Frame(box, bg=SOFT)
        mr.pack(fill="x", pady=(10, 0))
        check(mr, mute, lambda: save_cfg(mute=not mute)).pack(side="left")
        tk.Label(mr, text="소리 끄기  (파란 점만 켜져요)", bg=SOFT, fg=INK, font=(FONT, 9)).pack(side="left", padx=8)

        tk.Label(box, text="화면", bg=SOFT, fg=MUTED, font=(FONT, 8), anchor="w").pack(fill="x", pady=(12, 4))
        tr = tk.Frame(box, bg=SOFT)
        tr.pack(fill="x")
        theme = read_json(CONFIG).get("theme", "light")
        for name, label in (("light", "라이트"), ("dark", "다크")):
            on = name == theme
            pill(tr, label, INK if on else CANVAS, CANVAS if on else MUTED,
                 None if on else (lambda n=name: switch_theme(n)), bold=on).pack(side="left", padx=(0, 6))
        fit()

    def switch_theme(name):
        """색은 창을 만들 때 정해지니까, 저장하고 창을 새로 띄움 (위치·접힘 상태는 그대로)"""
        import ctypes
        import subprocess
        write_json(CONFIG, {**read_json(CONFIG), "theme": name, "_reopen_settings": True})
        save_pos()
        ctypes.windll.kernel32.CloseHandle(already_running.handle)  # 새 창이 뜰 수 있게 '하나만 켜기' 표시를 먼저 풂
        exe = Path(sys.executable)
        exe = exe.with_name("pythonw.exe") if exe.with_name("pythonw.exe").exists() else exe
        subprocess.Popen([str(exe), str(Path(__file__).resolve())], cwd=str(Path(__file__).resolve().parent))
        root.destroy()

    def fit():
        root.update_idletasks()
        sh = root.winfo_screenheight()
        if not ui["collapsed"]:  # 할 일 상자 높이 = 화면에 남는 만큼까지만, 넘치면 스크롤
            rest = head.winfo_reqheight() + body.winfo_reqheight() + addbar.winfo_reqheight() + (
                settings.winfo_reqheight() if ui["settings"] else 0)
            need = todo.winfo_reqheight()
            room = max(120, sh - 56 - 8 - rest)
            ui["scroll"] = need > room
            todo_box.config(height=min(need, room), scrollregion=(0, 0, WIDTH, need))
            if not ui["scroll"]:
                todo_box.yview_moveto(0)
            root.update_idletasks()
        size = (WIDTH, root.winfo_reqheight())
        if size != ui["size"]:  # 크기가 바뀔 때만 (매번 바꾸면 깜빡임)
            ui["size"] = size
            old_top, old_h = root.winfo_y(), root.winfo_height()
            # 화면 아래쪽에 있으면 아랫변을 고정 → 펼치면 위로 자라고, 접으면 다시 아래로 내려감
            top = old_top + old_h - size[1] if old_top + old_h / 2 > sh / 2 else old_top
            top = max(0, min(top, sh - size[1] - 56))  # 화면·작업표시줄 밖으로 안 나가게
            root.geometry(f"{size[0]}x{size[1]}+{root.winfo_x()}+{top}")
            save_pos(top, size[1])

    def save_pos(top=None, h=None):
        write_json(pos_file, {"x": root.winfo_x(), "y": root.winfo_y() if top is None else top,
                              "h": root.winfo_height() if h is None else h, "collapsed": ui["collapsed"]})

    # 머리줄 잡고 끌어서 이동, 위치 기억
    def press(e):
        ui["dx"], ui["dy"] = e.x_root - root.winfo_x(), e.y_root - root.winfo_y()

    def drag(e):
        root.geometry(f"+{e.x_root - ui['dx']}+{e.y_root - ui['dy']}")

    for w in (head, name, dot, summary):
        w.bind("<ButtonPress-1>", press)
        w.bind("<B1-Motion>", drag)
        w.bind("<ButtonRelease-1>", lambda e: save_pos())
        w.bind("<Double-Button-1>", lambda e: toggle())

    # ---------------- 세션 목록
    def act(fn):
        fn()
        ui["sig"] = None
        refresh(reschedule=False)

    def folder_of(s):
        folder = Path(str(s.get("cwd", ""))).name
        return "" if folder.startswith("scratch-") else folder  # 앱이 만든 임시 폴더 이름은 숨김

    def row(s, hidden_list=False):
        label, pbg, pfg = STATUS[s["view"]]
        el = ago(time.time() - s["since"])
        if el == "방금":
            wait = "방금 시작" if s["view"] == "work" else "방금 끝남"
        else:
            wait = {"work": f"{el}째 작업중", "seen": f"{el} 전 끝남"}.get(s["view"], f"{el}째 기다리는 중")
        done = s["view"] == "seen" or hidden_list

        r = tk.Frame(body, bg=CANVAS, padx=16, pady=8)
        r.pack(fill="x")
        r.columnconfigure(1, weight=1)
        # 체크나 상태 알약을 누르면 완료 ↔ 원래 상태. 그 대화에 새 작업이 생기면 완료는 자동으로 풀림
        fn = lambda: act(lambda: set_flag(s, "seen", not s.get("seen")))
        check(r, bool(s.get("seen")), fn).grid(row=0, column=0, rowspan=2, sticky="n", padx=(0, 10), pady=2)
        text = (s.get("title") or s.get("last") or folder_of(s) or "새 대화")[:24]
        fg = FAINT if done else MUTED if s["view"] == "work" else INK
        tk.Label(r, text=text, bg=CANVAS, fg=fg, font=(FONT, 10, "bold"), anchor="w").grid(row=0, column=1, sticky="w")
        pill_fn = (lambda: act(lambda: manual_next_state(s))) if s["src"] == "직접" else fn
        pill(r, label, pbg, pfg, pill_fn).grid(row=0, column=2, sticky="e")
        meta = " · ".join(x for x in (s["src"], folder_of(s)[:14], wait) if x)
        tk.Label(r, text=meta, bg=CANVAS, fg=FAINT if done else MUTED, font=(FONT, 8),
                 anchor="w").grid(row=1, column=1, sticky="w", pady=(2, 0))
        if hidden_list:
            btns = tk.Frame(r, bg=CANVAS)
            btns.grid(row=1, column=2, sticky="e", pady=(4, 0))
            pill(btns, "되돌리기", SOFT, ACCENT, lambda: act(lambda: set_flag(s, "hidden", False)), bold=False
                 ).pack(side="left")
            text_btn(btns, "✕", lambda: act(lambda: remove_session(s)), fg=HAIR, size=8).pack(side="left", padx=(6, 6))
        else:
            text_btn(r, "✕", lambda: act(lambda: set_flag(s, "hidden", True)), fg=HAIR, size=8
                     ).grid(row=1, column=2, sticky="e", padx=(0, 6), pady=(2, 0))  # 숨기기

    def refresh(reschedule=True):
        try:
            sessions = load_sessions()
            shown = [s for s in sessions if not s["hidden"]]
            hidden = [s for s in sessions if s["hidden"]]

            need = {f"{s['file']}{s.get('id', '')}" for s in shown if s["view"] in ("answer", "review")}
            if need - ui["alert"] and not sound_settings()[2]:
                play_sound()  # 새로 확인할 게 생기면 설정한 소리·크기로
            ui["alert"] = need

            count = {k: sum(s["view"] == k for s in shown) for k in ("answer", "review", "work")}
            short = {"answer": "답변", "review": "검토", "work": "작업"}  # 머리줄 버튼이 밀려나지 않게 짧게
            parts = [f"{short[k]} {count[k]}" for k in ("answer", "review", "work") if count[k]]
            summary.config(text=" · ".join(parts) or "쉬는 중", fg=INK if need else MUTED)
            dot.config(fg=ACCENT if need else CANVAS)

            # 화면에 보일 내용이 그대로면 다시 그리지 않음 → 깜빡임 없음
            now = time.time()
            sig = (tuple((str(s["file"]), s.get("id"), s["view"], s["hidden"], s.get("title"), s.get("cwd"),
                          ago(now - s["since"])) for s in sessions), ui["show_hidden"])
            if sig != ui["sig"]:
                ui["sig"] = sig
                draw_sessions(shown, hidden)
            reload_todo_if_changed()
        finally:
            if reschedule:
                root.after(2000, refresh)

    def draw_addbar(adding=False):
        """'+ 직접 추가' 버튼 ↔ 입력칸. Enter=추가, Esc·✕=취소"""
        for w in addbar.winfo_children():
            w.destroy()
        if not adding:
            text_btn(addbar, "+ 직접 추가  (GPT 채팅·개인 작업)", lambda: draw_addbar(True), fg=FAINT, size=8
                     ).pack(anchor="w", pady=(0, 6))
        else:
            box = tk.Frame(addbar, bg=SOFT, padx=12, pady=6)
            box.pack(fill="x", pady=(0, 8))
            text_btn(box, "✕", lambda: draw_addbar(False), fg=FAINT, size=8).pack(side="right")  # 닫기
            e = tk.Entry(box, bg=SOFT, fg=INK, relief="flat", font=(FONT, 9), insertbackground=INK)
            e.pack(side="left", fill="x", expand=True)
            # 아무것도 안 쓰고 다른 데를 누르면 저절로 닫힘
            e.bind("<FocusOut>", lambda _: root.after_idle(lambda: draw_addbar(False)) if not e.get().strip() else None)

            def done(_):
                if e.get().strip():
                    manual_add(e.get().strip())
                    ui["sig"] = None
                    refresh(reschedule=False)
                draw_addbar(False)

            e.bind("<Return>", done)
            e.bind("<Escape>", lambda _: draw_addbar(False))
            e.after(50, e.focus_force)
        fit()

    def draw_sessions(shown, hidden):
        for w in body.winfo_children():
            w.destroy()
        for s in shown:
            row(s)
        if hidden:
            arrow = "▴" if ui["show_hidden"] else "▾"
            text_btn(body, f"숨긴 항목 {len(hidden)}개 {arrow}",
                     lambda: act(lambda: ui.update(show_hidden=not ui["show_hidden"])),
                     fg=MUTED, size=8).pack(anchor="w", padx=16, pady=(4, 8))
            if ui["show_hidden"]:
                for s in hidden:
                    row(s, hidden_list=True)
        if not shown and not hidden:
            tk.Label(body, text="진행 중인 작업이 없어요", bg=CANVAS, fg=FAINT, font=(FONT, 9)).pack(pady=8)
        fit()

    # ---------------- 할 일 (세션 목록과 따로 그림: 입력하는 중에 지워지지 않게)
    td = {"groups": load_todo(), "open": True, "adding": None, "editing": None, "mtime": -1}  # -1: 처음엔 꼭 그리기

    def todo_mtime():
        try:
            return todo_file().stat().st_mtime
        except OSError:
            return None

    def reload_todo_if_changed():
        """다른 PC에서 동기화 폴더의 할 일을 고쳤으면 다시 읽기 (입력 중엔 건드리지 않음)"""
        m = todo_mtime()
        if m != td["mtime"] and td["adding"] is None and td["editing"] is None:
            td["groups"] = load_todo()
            if m is None:  # 아직 파일이 없으면 지금 내용으로 만들어 둠
                save_todo(td["groups"])
            td["mtime"] = todo_mtime()
            draw_todo()

    def todo_change(fn):
        fn(td["groups"])
        save_todo(td["groups"])
        td["mtime"] = todo_mtime()
        draw_todo()

    def entry(parent, placeholder, on_enter, value=""):
        """입력칸. value가 있으면 그 글자를 채워서 수정 모드로. Enter=저장, Esc·✕=취소"""
        box = tk.Frame(parent, bg=SOFT, padx=12, pady=6)
        cancel = lambda: (td.update(adding=None, editing=None), draw_todo())
        text_btn(box, "✕", cancel, fg=FAINT, size=8).pack(side="right")  # 닫기
        e = tk.Entry(box, bg=SOFT, fg=INK, relief="flat", font=(FONT, 9), insertbackground=INK)
        e.pack(side="left", fill="x", expand=True)
        # 새로 추가하는 칸은 아무것도 안 쓰고 다른 데를 누르면 저절로 닫힘
        e.bind("<FocusOut>", lambda _: root.after_idle(cancel)
               if not value and e.get().strip() in ("", placeholder) and box.winfo_exists() else None)
        e.insert(0, value or placeholder)
        e.config(fg=INK if value else FAINT)

        def clear(_):
            if not value and e.get() == placeholder:
                e.delete(0, "end")
                e.config(fg=INK)

        def done(_):
            t = e.get().strip()
            if t and (value or t != placeholder):
                td.update(adding=None, editing=None)
                on_enter(t)

        e.bind("<FocusIn>", clear)
        e.bind("<Return>", done)
        e.bind("<Escape>", lambda _: cancel())
        e.after(50, lambda: (e.focus_force(), clear(None), e.select_range(0, "end") if value else None))
        return box

    def start_edit(key):
        td.update(editing=key, adding=None)
        draw_todo()

    def draw_todo():
        for w in todo.winfo_children():
            w.destroy()
        groups = td["groups"]
        td["targets"] = []  # 끌어서 놓을 수 있는 줄: (프레임, ("group", gi) 또는 ("item", gi, ii))
        left = sum(not i["done"] for g in groups for i in g["items"])

        tk.Frame(todo, bg=SOFT, height=1).pack(fill="x", padx=16, pady=(4, 0))
        h = tk.Frame(todo, bg=CANVAS, padx=16, pady=10)
        h.pack(fill="x")
        text_btn(h, f"{'▾' if td['open'] else '▸'}  할 일", lambda: (td.update(open=not td["open"]), draw_todo()),
                 fg=INK, size=10, bold=True).pack(side="left")
        pill(h, str(left), SOFT, MUTED).pack(side="left", padx=6)
        if td["open"]:
            pill(h, "+ 그룹", SOFT, MUTED, lambda: (td.update(adding="group", editing=None), draw_todo()), bold=False
                 ).pack(side="right")
            for gi, g in enumerate(groups):
                draw_group(gi, g)
            if td["adding"] == "group":
                ar = tk.Frame(todo, bg=CANVAS, padx=16, pady=4)
                ar.pack(fill="x")
                entry(ar, "새 그룹 이름 입력 후 Enter", lambda t: (
                    td.update(adding=None),
                    todo_change(lambda gs: gs.append({"name": t, "open": True, "items": []})))).pack(fill="x")
        tk.Frame(todo, bg=CANVAS, height=10).pack()
        td["line"] = tk.Frame(todo, bg=ACCENT, height=2)  # 끌 때 놓일 자리 표시
        fit()

    # ---------------- 끌어서 순서 바꾸기 (⠿ 손잡이를 잡고 위아래로)
    def drop_spot(kind, y_root):
        """커서 위치 → (놓일 위치 키, 파란 줄 y). 키 None = 맨 끝"""
        cands = [t for t in td["targets"] if kind == "item" or t[1][0] == "group"]
        for frame, key in cands:
            top = frame.winfo_rooty()
            if y_root < top + frame.winfo_height() / 2:
                return key, top - todo.winfo_rooty()
        last = cands[-1][0] if cands else todo
        return None, last.winfo_rooty() + last.winfo_height() - todo.winfo_rooty()

    def drag_move(kind, e):
        _, y = drop_spot(kind, e.y_root)
        td["line"].place(x=16, y=max(0, y - 1), width=WIDTH - 32)

    def drag_drop(src, e):
        td["line"].place_forget()
        key, _ = drop_spot(src[0], e.y_root)

        def move(gs):
            if src[0] == "group":
                gj = len(gs) if key is None else key[1]
                g = gs.pop(src[1])
                gs.insert(gj - (src[1] < gj), g)
                return
            # 항목: 다른 항목 앞으로, 그룹 머리 바로 위면 그 앞 그룹 맨 끝으로, 맨 아래면 마지막 그룹 끝으로
            if key is None:
                gj, ij = len(gs) - 1, len(gs[-1]["items"])
            elif key[0] == "item":
                gj, ij = key[1], key[2]
            elif key[1] == 0:
                gj, ij = 0, 0
            else:
                gj, ij = key[1] - 1, len(gs[key[1] - 1]["items"])
            it = gs[src[1]]["items"].pop(src[2])
            if gj == src[1] and src[2] < ij:
                ij -= 1
            gs[gj]["items"].insert(ij, it)
            gs[gj]["open"] = True

        todo_change(move)

    def handle(parent, src):
        b = tk.Label(parent, text="⠿", bg=parent["bg"], fg=GRIP, font=(FONT, 9), cursor="fleur")
        b.bind("<B1-Motion>", lambda e: drag_move(src[0], e))
        b.bind("<ButtonRelease-1>", lambda e: drag_drop(src, e))
        b.bind("<Enter>", lambda e: b.config(fg=INK))
        b.bind("<Leave>", lambda e: b.config(fg=GRIP))
        return b

    def draw_group(gi, g):
        done = sum(i["done"] for i in g["items"])
        gr = tk.Frame(todo, bg=CANVAS, padx=10, pady=3)
        gr.pack(fill="x")
        td["targets"].append((gr, ("group", gi)))
        handle(gr, ("group", gi)).pack(side="left", padx=(0, 4))
        if td["editing"] == ("group", gi):  # 그룹 이름 수정 중
            entry(gr, "그룹 이름", lambda t: todo_change(lambda gs: gs[gi].update(name=t)), value=g["name"]
                  ).pack(side="left", fill="x", expand=True)
            return
        text_btn(gr, f"{'▾' if g.get('open') else '▸'}  {g['name']}",
                 lambda: todo_change(lambda gs: g.update(open=not g.get("open"))), fg=INK, size=9).pack(side="left")
        tk.Label(gr, text=f"{done}/{len(g['items'])}", bg=CANVAS, fg=FAINT, font=(FONT, 8)).pack(side="left", padx=6)

        def del_group():
            if messagebox.askyesno("그룹 삭제", f"'{g['name']}' 그룹을 통째로 지울까요?", parent=root):
                todo_change(lambda gs: gs.pop(gi))

        text_btn(gr, "✕", del_group, fg=HAIR, size=8).pack(side="right")
        text_btn(gr, "✎", lambda: start_edit(("group", gi)), fg=HAIR, size=8).pack(side="right", padx=4)
        if not g.get("open"):
            return
        pill(gr, "+ 추가", SOFT, MUTED, lambda: (td.update(adding=gi, editing=None), draw_todo()), bold=False
             ).pack(side="right", padx=(0, 6))
        for ii, it in enumerate(g["items"]):
            ir = tk.Frame(todo, bg=CANVAS, padx=28, pady=2)
            ir.pack(fill="x")
            td["targets"].append((ir, ("item", gi, ii)))
            handle(ir, ("item", gi, ii)).pack(side="left", padx=(0, 4))
            check(ir, it["done"], lambda it=it: todo_change(lambda gs: it.update(done=not it["done"]))
                  ).pack(side="left")
            if td["editing"] == ("item", gi, ii):  # 항목 내용 수정 중
                entry(ir, "할 일", lambda t, it=it: todo_change(lambda gs: it.update(text=t)), value=it["text"]
                      ).pack(side="left", fill="x", expand=True, padx=(8, 0))
                continue
            f = tkfont.Font(family=FONT, size=9, overstrike=it["done"])
            lb = tk.Label(ir, text=it["text"], bg=CANVAS, fg=FAINT if it["done"] else INK, font=f, anchor="w")
            lb.pack(side="left", padx=8)
            lb.bind("<Double-Button-1>", lambda e, ii=ii: start_edit(("item", gi, ii)))  # 글자 더블클릭 = 수정
            text_btn(ir, "✕", lambda ii=ii: todo_change(lambda gs: gs[gi]["items"].pop(ii)),
                     fg=HAIR, size=8).pack(side="right")
            text_btn(ir, "✎", lambda ii=ii: start_edit(("item", gi, ii)), fg=HAIR, size=8).pack(side="right", padx=4)
        if td["adding"] == gi:
            ar = tk.Frame(todo, bg=CANVAS, padx=34, pady=4)
            ar.pack(fill="x")
            entry(ar, "할 일 입력 후 Enter", lambda t: (
                td.update(adding=None),
                todo_change(lambda gs: gs[gi]["items"].append({"text": t, "done": False})))).pack(fill="x")

    root.geometry(f"{WIDTH}x{int(num(pos.get('h'), 100))}+{x}+{y}")  # 지난번 크기로 시작해야 아랫변 고정이 안 어긋남
    root.update()  # 창이 실제로 떠야 위치값을 읽을 수 있음
    round_window(root)
    if pos.get("collapsed"):  # 접어둔 채로 껐으면 접힌 채로 시작
        toggle(True)
    draw_addbar()
    if ui["reopen"]:
        open_settings(True)
    refresh()
    root.mainloop()


if __name__ == "__main__":
    cmd, rest = (sys.argv[1] if len(sys.argv) > 1 else ""), sys.argv[2:]
    if cmd == "hook":
        hook()
    elif cmd == "install":
        install(rest)
    elif cmd == "uninstall":
        uninstall(rest)
    else:
        board()
