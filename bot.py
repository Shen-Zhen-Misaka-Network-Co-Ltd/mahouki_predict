import json
import os
from datetime import datetime, timedelta
from typing import Optional

import httpx

CONFIG_PATH = "config.json"
PERIODS_PATH = "periods.json"
BUFFER_PATH = "buffer.json"
REMINDER_DAYS = 1


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

    def _api_call(self, prompt: str) -> Optional[str]:
        if not self.config.get("api_key"):
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
        url = self.config.get("base_url", "").rstrip("/") + "/chat/completions"
        try:
            resp = httpx.post(url, headers=headers, json=body, timeout=60)
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            print(f"[API Error] {e}")
            return None

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

    def _analyze_buffer(self):
        msgs = self.buffer["messages"]
        if not msgs:
            return
        if not self.config.get("api_key"):
            print("[WARN] No API key configured, clearing buffer without analysis")
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
                print(f"[Parse Error] LLM returned: {result[:200]}")
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

    def _clear_buffer(self):
        self.buffer["messages"] = []
        self.buffer["char_count"] = 0
        self._save_buffer()

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

        avg_cycle = round(sum(intervals) / len(intervals))
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
        return round(sum(durations) / len(durations))

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
            print(f"[Reminder] {r['message']}")
            if self.reminder_callback:
                self.reminder_callback(r)
        return reminders


if __name__ == "__main__":
    bot = CycleBot()
    print("Mahouki Predict 经期预测Bot")
    print("=" * 40)
    print("Commands:")
    print("  add <text>|<user_id>|<username>|<timestamp>")
    print("  predict <user_id>")
    print("  stats")
    print("  reminders [date]")
    print("  fire [date]")
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

        elif cmd.startswith("predict "):
            uid = cmd[8:].strip()
            pred = bot.predict(uid)
            if pred:
                print(f"  {pred['username']} ({pred['user_id']})")
                for k, v in pred.items():
                    if k not in ("username", "user_id"):
                        print(f"    {k}: {v}")
            else:
                print("  Need >=2 recorded periods for prediction")

        elif cmd == "stats":
            users = bot.periods.get("users", {})
            if not users:
                print("  No users recorded")
            for uid, u in users.items():
                name = u.get("username", uid)
                count = len(u.get("periods", []))
                pred = bot.predict(uid)
                if pred:
                    print(f"  {name} ({uid}): {count} periods, next ~{pred['next_period_start']}")
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
                print("  No reminders for today")

        elif cmd.startswith("fire"):
            parts = cmd.split(None, 1)
            date = parts[1] if len(parts) > 1 else None
            bot.fire_reminders(date)

        else:
            print("  Unknown command")
