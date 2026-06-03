import csv
import io
import json
import logging
import os
import statistics
from datetime import datetime, timedelta
from typing import Optional

import httpx

CONFIG_PATH = "config.json"
PERIODS_PATH = "periods.json"
BUFFER_PATH = "buffer.json"
REMINDER_DAYS = 1

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("mahouki")


def load_json(path: str):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class CycleBot:
    def __init__(self):
        self.config = load_json(CONFIG_PATH)
        self.periods: dict = load_json(PERIODS_PATH)
        self.buffer: dict = load_json(BUFFER_PATH)

        if "users" not in self.periods:
            self.periods["users"] = {}
        if "messages" not in self.buffer:
            self.buffer["messages"] = []
        if "char_count" not in self.buffer:
            self.buffer["char_count"] = 0

        self.reminder_callback = None

        self._save_periods()
        self._save_buffer()

    def set_reminder_callback(self, callback):
        self.reminder_callback = callback

    def _save_periods(self):
        save_json(PERIODS_PATH, self.periods)

    def _save_buffer(self):
        save_json(BUFFER_PATH, self.buffer)

    def _get_user(self, user_id: str) -> dict:
        uid = str(user_id)
        if uid not in self.periods["users"]:
            self.periods["users"][uid] = {"username": "", "periods": []}
        return self.periods["users"][uid]

    # ── API ──────────────────────────────────────────────────────────

    def _api_url(self) -> Optional[str]:
        url = self.config.get("api_url") or self.config.get("base_url", "")
        if not url:
            return None
        url = url.rstrip("/")
        if url.endswith("/chat/completions"):
            return url
        if "/chat/completions" not in url:
            return url + "/chat/completions"
        return url

    def _api_call(self, prompt: str) -> Optional[str]:
        url = self._api_url()
        if not url or not self.config.get("api_key"):
            return None
        headers = {
            "Authorization": f"Bearer {self.config['api_key']}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.config.get("model", "gpt-4o-mini"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
        }
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            log.error("API call failed: %s", e)
            return None

    # ── Message ingeStion ────────────────────────────────────────────

    def receive_message(self, text: str, user_id: str, username: str, timestamp: str):
        uid = str(user_id)
        user = self._get_user(uid)
        if username:
            user["username"] = username

        self.buffer["messages"].append({
            "text": text, "user_id": uid,
            "username": username, "timestamp": timestamp,
        })
        self.buffer["char_count"] += len(text)
        self._save_buffer()

        if self.buffer["char_count"] >= self.config.get("buffer_size", 10000):
            self._analyze_buffer()

    def flush_buffer(self):
        self._analyze_buffer()

    def _analyze_buffer(self):
        msgs = self.buffer["messages"]
        if not msgs:
            return
        if not self.config.get("api_key") or not self._api_url():
            log.warning("No API configured, clearing buffer without analysis")
            self._clear_buffer()
            return

        lines = []
        for m in msgs:
            t = m["timestamp"]
            u = m["username"] or m["user_id"]
            lines.append(f"[{t}] {u}: {m['text']}")
        conversation = "\n".join(lines)

        prompt = (
            "你是一个经期记录分析器。下面是一段群聊记录，请找出所有自述经期（月经/大姨妈/痛经/肚子疼暗示经期）的消息。\n"
            "只统计用户说自己的情况，不包括开玩笑或问别人的。\n"
            "对于每条匹配的消息，判断是否为自述经期：如果是，输出该用户的经期开始日期（根据聊天时间推断）。\n\n"
            "请严格按以下JSON格式输出（不要其他内容）：\n"
            '{"periods": [{"user_id": "...", "username": "...", "date": "2026-06-03", "evidence": "消息原文"}]}\n\n'
            f"群聊记录：\n{conversation}"
        )

        result = self._api_call(prompt)
        if not result:
            return

        try:
            data = json.loads(result)
        except json.JSONDecodeError:
            try:
                start = result.index("{")
                end = result.rindex("}") + 1
                data = json.loads(result[start:end])
            except (ValueError, json.JSONDecodeError):
                log.warning("Failed to parse LLM response: %s", result[:200])
                self._clear_buffer()
                return

        for entry in data.get("periods", []):
            uid = str(entry["user_id"])
            user = self._get_user(uid)
            if entry.get("username"):
                user["username"] = entry["username"]

            existing = user["periods"]
            date = entry["date"]
            if not any(p["start"] == date for p in existing):
                existing.append({
                    "start": date,
                    "end": date,
                    "evidence": entry.get("evidence", ""),
                    "confidence": "auto",
                })
                existing.sort(key=lambda x: x["start"])

        self._save_periods()
        self._clear_buffer()
        log.info("Buffer analyzed, %d periods found", len(data.get("periods", [])))

    def _clear_buffer(self):
        self.buffer["messages"] = []
        self.buffer["char_count"] = 0
        self._save_buffer()

    # ── Period CRUD ──────────────────────────────────────────────────

    def add_period(self, user_id: str, start: str, end: str = None,
                   username: str = "", evidence: str = "", confidence: str = "manual"):
        uid = str(user_id)
        user = self._get_user(uid)
        if username:
            user["username"] = username
        existing = user["periods"]
        date = start
        if not any(p["start"] == date for p in existing):
            existing.append({
                "start": start,
                "end": end or start,
                "evidence": evidence,
                "confidence": confidence,
            })
            existing.sort(key=lambda x: x["start"])
            self._save_periods()
            return True
        return False

    def del_period(self, user_id: str, start: str) -> bool:
        uid = str(user_id)
        user = self._get_user(uid)
        before = len(user["periods"])
        user["periods"] = [p for p in user["periods"] if p["start"] != start]
        if len(user["periods"]) < before:
            self._save_periods()
            return True
        return False

    def list_periods(self, user_id: str = None) -> list:
        if user_id:
            uid = str(user_id)
            user = self._get_user(uid)
            return [(uid, user["username"], p) for p in user["periods"]]
        results = []
        for uid, user in self.periods.get("users", {}).items():
            for p in user["periods"]:
                results.append((uid, user["username"], p))
        results.sort(key=lambda x: x[2]["start"])
        return results

    def export_csv(self) -> str:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["user_id", "username", "start", "end", "confidence", "evidence"])
        for uid, uname, p in self.list_periods():
            w.writerow([uid, uname, p["start"], p["end"], p.get("confidence", ""), p.get("evidence", "")])
        return buf.getvalue()

    # ── Prediction ───────────────────────────────────────────────────

    def predict(self, user_id: str) -> Optional[dict]:
        uid = str(user_id)
        user = self._get_user(uid)
        periods = user["periods"]
        if len(periods) < 2:
            return None

        intervals = []
        for i in range(1, len(periods)):
            prev = datetime.strptime(periods[i - 1]["start"], "%Y-%m-%d")
            cur = datetime.strptime(periods[i]["start"], "%Y-%m-%d")
            intervals.append((cur - prev).days)

        avg_cycle = round(statistics.mean(intervals))
        cycle_min = min(intervals)
        cycle_max = max(intervals)
        cycle_std = round(statistics.stdev(intervals)) if len(intervals) > 1 else 0

        duration = self._avg_period_duration(periods)
        last_start = datetime.strptime(periods[-1]["start"], "%Y-%m-%d")
        next_start = last_start + timedelta(days=avg_cycle)
        next_end = next_start + timedelta(days=duration - 1)
        fertile_start = next_start - timedelta(days=14)
        fertile_end = fertile_start + timedelta(days=5)

        return {
            "username": user["username"],
            "user_id": uid,
            "avg_cycle_days": avg_cycle,
            "cycle_min_days": cycle_min,
            "cycle_max_days": cycle_max,
            "cycle_std_days": cycle_std,
            "avg_duration_days": duration,
            "periods_count": len(periods),
            "last_period_start": periods[-1]["start"],
            "last_period_end": periods[-1].get("end", periods[-1]["start"]),
            "next_period_start": next_start.strftime("%Y-%m-%d"),
            "next_period_end": next_end.strftime("%Y-%m-%d"),
            "fertile_window_start": fertile_start.strftime("%Y-%m-%d"),
            "fertile_window_end": fertile_end.strftime("%Y-%m-%d"),
        }

    @staticmethod
    def _avg_period_duration(periods: list) -> int:
        durations = []
        for p in periods:
            start = datetime.strptime(p["start"], "%Y-%m-%d")
            end = datetime.strptime(p.get("end", p["start"]), "%Y-%m-%d")
            durations.append(max((end - start).days + 1, 1))
        return round(statistics.mean(durations))

    # ── Reminders ────────────────────────────────────────────────────

    def get_reminders(self, today: str = None) -> list:
        if today is None:
            today = datetime.now().strftime("%Y-%m-%d")
        target = datetime.strptime(today, "%Y-%m-%d")
        reminders = []

        for uid, user in self.periods.get("users", {}).items():
            pred = self.predict(uid)
            if pred is None:
                continue

            next_start = datetime.strptime(pred["next_period_start"], "%Y-%m-%d")
            delta = (next_start - target).days

            if delta < 0:
                continue

            if delta == 0:
                reminders.append({
                    "type": "period_start",
                    "message": f"🔴 {pred['username']} 今天可能来月经了，做好准备哦",
                    **pred,
                })
            elif delta == 1:
                reminders.append({
                    "type": "period_1day_before",
                    "message": f"🟠 {pred['username']} 明天可能来月经，提前准备好卫生用品",
                    **pred,
                })
            elif delta <= REMINDER_DAYS:
                reminders.append({
                    "type": "period_upcoming",
                    "message": f"🟡 {pred['username']} 预计 {delta} 天后（{pred['next_period_start']}）来月经",
                    **pred,
                })

        return reminders

    def fire_reminders(self, today: str = None):
        reminders = self.get_reminders(today)
        for r in reminders:
            log.info("Reminder: %s", r["message"])
            if self.reminder_callback:
                self.reminder_callback(r)
        return reminders


# ── CLI ──────────────────────────────────────────────────────────────

def _print_pred(pred: dict):
    print(f"  {pred['username']} ({pred['user_id']})")
    print(f"  {'═' * 36}")
    print(f"    周期记录: {pred['periods_count']} 次")
    print(f"    平均周期: {pred['avg_cycle_days']} 天 (范围 {pred['cycle_min_days']}–{pred['cycle_max_days']} 天, σ={pred['cycle_std_days']})")
    print(f"    平均持续: {pred['avg_duration_days']} 天")
    print(f"    ─────────────────────────")
    print(f"    末次经期: {pred['last_period_start']} → {pred['last_period_end']}")
    print(f"    下次预测: {pred['next_period_start']} → {pred['next_period_end']}")
    print(f"    排卵窗口: {pred['fertile_window_start']} → {pred['fertile_window_end']}")


def cli():
    bot = CycleBot()
    print("Mahouki Predict 魔法期预测Bot")
    print("=" * 40)
    print("Commands:")
    print("  add <text>|<user_id>|<username>|<timestamp>   投喂消息")
    print("  flush                                        强制分析缓冲区")
    print("  predict <user_id>                            预测")
    print("  period add <user_id> <start> [end] [username] 手动添加经期")
    print("  period del <user_id> <start>                 删除经期")
    print("  period list [user_id]                        列出经期")
    print("  export                                       导出CSV")
    print("  stats                                        概览")
    print("  reminders [date]                             查看提醒")
    print("  fire [date]                                  触发提醒")
    print("  exit")
    print()

    while True:
        try:
            cmd = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if cmd == "exit":
            break

        elif cmd.startswith("add "):
            parts = cmd[4:].split("|")
            if len(parts) >= 4:
                bot.receive_message(parts[0], parts[1], parts[2], parts[3])
                print(f"  OK (buffer: {bot.buffer['char_count']} chars)")
            else:
                print("  Usage: add text|user_id|username|timestamp")

        elif cmd == "flush":
            before = bot.buffer["char_count"]
            bot.flush_buffer()
            print(f"  Buffer flushed ({before} chars processed)")

        elif cmd.startswith("predict "):
            uid = cmd[8:].strip()
            pred = bot.predict(uid)
            if pred:
                _print_pred(pred)
            else:
                print("  Need >=2 recorded periods for prediction")

        elif cmd.startswith("period add"):
            parts = cmd.split(None, 4)
            if len(parts) >= 4:
                uid = parts[2]
                start = parts[3]
                end = parts[4] if len(parts) > 4 else None
                username = parts[5] if len(parts) > 5 else ""
                ok = bot.add_period(uid, start, end, username)
                print(f"  {'Added' if ok else 'Duplicate, skipped'}")
            else:
                print("  Usage: period add <user_id> <start> [end] [username]")

        elif cmd.startswith("period del"):
            parts = cmd.split(None, 3)
            if len(parts) >= 4:
                ok = bot.del_period(parts[2], parts[3])
                print(f"  {'Deleted' if ok else 'Not found'}")
            else:
                print("  Usage: period del <user_id> <start>")

        elif cmd.startswith("period list"):
            parts = cmd.split(None, 2)
            uid = parts[2] if len(parts) > 2 else None
            rows = bot.list_periods(uid)
            if rows:
                for uid, uname, p in rows:
                    print(f"  {uid} {uname:<12s} {p['start']} → {p['end']}  [{p.get('confidence','')}]")
            else:
                print("  No periods recorded")

        elif cmd == "export":
            csv_str = bot.export_csv()
            path = "period_export.csv"
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(csv_str)
            print(f"  Exported to {path} ({len(csv_str)} bytes)")

        elif cmd == "stats":
            users = bot.periods.get("users", {})
            if not users:
                print("  No users recorded")
            for uid, u in users.items():
                name = u.get("username", uid)
                count = len(u.get("periods", []))
                pred = bot.predict(uid)
                if pred:
                    print(f"  {name} ({uid}): {count} periods, "
                          f"cycle ~{pred['avg_cycle_days']}d, "
                          f"next ~{pred['next_period_start']}")
                else:
                    print(f"  {name} ({uid}): {count} periods (need >=2 for prediction)")

        elif cmd.startswith("reminders"):
            parts = cmd.split(None, 1)
            date = parts[1] if len(parts) > 1 else None
            reminders = bot.get_reminders(date)
            if reminders:
                for r in reminders:
                    print(f"  {r['message']}")
            else:
                print("  No reminders")

        elif cmd.startswith("fire"):
            parts = cmd.split(None, 1)
            date = parts[1] if len(parts) > 1 else None
            bot.fire_reminders(date)

        else:
            print("  Unknown command")


if __name__ == "__main__":
    cli()
