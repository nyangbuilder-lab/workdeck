"""python test_workdeck.py — 가짜 홈 폴더에서 핵심 동작만 확인 (실제 설정은 건드리지 않음)"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = os.environ["HOME"] = str(home)  # Path.home()을 가짜 폴더로
sys.path.insert(0, str(Path(__file__).parent))
import workdeck as w  # noqa: E402

assert w.HOME == home


def run_hook(payload):
    subprocess.run([sys.executable, str(Path(w.__file__)), "hook"], input=json.dumps(payload).encode(),
                   env=os.environ, check=True, capture_output=True)


# 1) 훅: 상태 기록 → 바뀜 → 세션 끝나도(앱 재시작) 남아 있음
run_hook({"session_id": "abc-123", "hook_event_name": "UserPromptSubmit", "prompt": "첫 줄\n둘째 줄"})
f = w.DIR / "abc-123.json"
assert json.loads(f.read_text("utf-8"))["state"] == "work"
assert json.loads(f.read_text("utf-8"))["last"] == "첫 줄"
run_hook({"session_id": "abc-123", "hook_event_name": "Stop"})
assert json.loads(f.read_text("utf-8"))["state"] == "review"
run_hook({"session_id": "abc-123", "hook_event_name": "SessionEnd"})
assert json.loads(f.read_text("utf-8"))["state"] == "review"
f.unlink()

# 2) 훅: 이상한 session_id로 폴더 밖 파일을 지우거나 쓸 수 없음
victim = home / ".claude" / "victim.json"
victim.write_text("{}", "utf-8")
for bad in ("../victim", "..\\victim", "C:\\victim", "a/b", "", 123):
    run_hook({"session_id": bad, "hook_event_name": "SessionEnd"})
    run_hook({"session_id": bad, "hook_event_name": "Stop"})
assert victim.exists()
assert not list(home.rglob("*.json.tmp")) and not (home / "victim.json").exists()

# 3) 훅: 깨진 입력에도 조용히 끝남 (출력 없음 = Claude Code에 끼어들지 않음)
for raw in (b"not json", b"[1,2]", b"", b"\xff\xfe"):
    r = subprocess.run([sys.executable, str(Path(w.__file__)), "hook"], input=raw, env=os.environ, capture_output=True)
    assert r.returncode == 0 and r.stdout == b"" and r.stderr == b"", (raw, r)

# 4) 설정: 우리 훅만 빼고 남의 훅은 그대로
data = {"hooks": {"Stop": [
    {"hooks": [{"type": "command", "command": '"py" "C:/x/.claude/hooks/workdeck.py" hook'}]},
    {"hooks": [{"type": "command", "command": '"py" "C:/x/.claude/hooks/claude_board.py" hook'}]},
    {"hooks": [{"type": "command", "command": "node other.js"}]},
    "이상한 값",
]}, "permissions": {"allow": ["x"]}}
clean = w.without_our_hooks(data)
assert clean["Stop"] == [{"hooks": [{"type": "command", "command": "node other.js"}]}, "이상한 값"], clean

# 5) 설정: 주석 있는 settings.json은 못 읽으면 None → 설치가 덮어쓰지 않음
w.SETTINGS.write_text('{ // 주석\n "a": 1 }', "utf-8")
assert w.load_settings() is None
w.SETTINGS.write_text('{"a": 1}', "utf-8")
assert w.load_settings() == {"a": 1}

# 6) 깨진 할 일·직접 추가 파일도 그릴 수 있는 모양으로
assert w.clean_groups([{"name": 1, "items": [{"text": "a"}, "x"]}, "y", {"items": "z"}]) == [
    {"name": "1", "open": False, "items": [{"text": "a", "done": False}]},
    {"name": "그룹", "open": False, "items": []}]
w.DIR.mkdir(parents=True, exist_ok=True)
w.manual_file().write_text(json.dumps({"items": [{"id": "m1", "state": "nope"}, 5, {"id": "m2", "state": "work"}]}), "utf-8")
assert [m["id"] for m in w.manual_items()] == ["m2"]
w.manual_file().write_text("[]", "utf-8")
assert w.manual_items() == []

# 7) 직접 추가: 작업중 → 검토 필요 → 완료 → 작업중
w.manual_file().unlink()
w.manual_add("GPT 채팅")
views = []
for _ in range(4):
    s = w.load_sessions()[0]
    views.append(s["view"])
    w.manual_next_state(s)
assert views == ["work", "review", "seen", "work"], views

# 8) 제목 클릭 → Claude 앱 대화 번호(local_…)를 찾아 claude:// 주소로 열기
app = home / "AppData" / "Roaming"
os.environ["APPDATA"] = str(app)
sess = app / "Claude" / "claude-code-sessions" / "acct" / "org"
sess.mkdir(parents=True)
(sess / "local_aaa-111.json").write_text(json.dumps({"sessionId": "local_aaa-111", "cliSessionId": "cli-1"}), "utf-8")
(sess / "local_evil.json").write_text(json.dumps({"sessionId": "local_x&rm", "cliSessionId": "cli-2"}), "utf-8")
opened = []
w.os.startfile = opened.append
assert w.open_claude_session("cli-1") and opened == ["claude://code/continue?session=local_aaa-111&source=desktop_action"]
assert not w.open_claude_session("cli-2")          # 이상한 앱 번호는 열지 않음
assert not w.open_claude_session("../x") and not w.open_claude_session("none")
assert len(opened) == 1

# 9) 이름 바꾸기·우선순위: 상태가 바뀌어도 남고, 우선순위 순서로 정렬 (완료는 맨 아래)
w.manual_file().write_text(json.dumps({"items": []}), "utf-8")
for t_ in ("가", "나", "다"):
    w.manual_add(t_)
ss = {s["title"]: s for s in w.load_sessions() if s["src"] == "직접"}
w.set_meta(ss["다"], prio=1)
w.set_meta(ss["가"], prio=5)
w.set_meta(ss["나"], prio=2, my_title="나 새이름")
w.set_flag(ss["가"], "seen", True)
order = [s["title"] for s in w.load_sessions() if s["src"] == "직접"]
assert order == ["다", "나 새이름", "가"], order
run_hook({"session_id": "pr-1", "hook_event_name": "Stop"})
c = [s for s in w.load_sessions() if s["src"] == "Claude"][0]
w.set_meta(c, prio=3, my_title="내 이름")
run_hook({"session_id": "pr-1", "hook_event_name": "UserPromptSubmit", "prompt": "x"})
c = [s for s in w.load_sessions() if s["src"] == "Claude"][0]
assert c["title"] == "내 이름" and c["prio"] == 3 and c["state"] == "work", c
w.set_meta(c, my_title=None, prio=None)
c = [s for s in w.load_sessions() if s["src"] == "Claude"][0]
assert c["title"] != "내 이름" and c["prio"] == 0

print("ok")
